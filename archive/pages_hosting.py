"""Owner-side deployment history, production verification and privacy removal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .cloudflare_pages import (
    ACCOUNT, CF_RAY, DEPLOYMENT, MAX_ASSET_BYTES, MAX_ASSETS, PROJECT, TOKEN,
    PagesClient, PagesError, WindowsCredentials,
)
from .db import utcnow
from .pages_verification import (
    CACHE_OBSERVATIONS, PROBE_BATCH, REQUIRED_CLEAN_ROUNDS, independent_host, public_observation, retirement_pair,
    track_blockers,
)
from .release import _atomic_write, _canonical_bytes
from .store import ArchiveError

EMPTY_SITE = b'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Archive</title></html>'
NOT_FOUND = b'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>404</title><h1>404</h1></html>'
IN_FLIGHT = {"creating", "verifying"}
POSSIBLY_PUBLIC = IN_FLIGHT | {"succeeded", "cleanup_pending"}
HTML_PATH = re.compile(r"(?:content/p_[0-9a-f]{20}\.html|spaces/[A-Z][A-Z0-9_-]{0,23}/index\.html)")
ATTACHMENT_PATH = re.compile(r"attachments/a_[0-9a-f]{20}/[\w .-]{1,120}")


@dataclass(frozen=True)
class Asset:
    mime: str
    size: int
    sha256: str
    value: Path | bytes
    internal_marker: bytes = b""

    def read(self) -> bytes:
        result = self.value
        if isinstance(result, Path):
            try:
                for parent in result.parents:
                    if getattr(parent.lstat(), "st_file_attributes", 0) & 0x400 or parent.is_symlink():
                        raise PagesError("公开文件路径无法通过安全校验。")
                info = result.lstat()
                if (not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400
                        or info.st_size != self.size):
                    raise PagesError("公开文件无法通过安全校验。")
                with result.open("rb") as stream:
                    result = stream.read(self.size + 1)
            except OSError:
                raise PagesError("无法读取本机公开文件。") from None
        if len(result) != self.size or hashlib.sha256(result).hexdigest() != self.sha256:
            raise PagesError("公开文件内容与校验记录不一致。")
        if self.internal_marker:
            return result.replace(self.internal_marker, b"", 1)
        return result

    def facts(self) -> dict:
        if self.internal_marker:
            published = self.read()
            return {"size": len(published), "sha256": hashlib.sha256(published).hexdigest(), "mime": self.mime}
        return {"size": self.size, "sha256": self.sha256, "mime": self.mime}


def static_asset(value: bytes) -> Asset:
    return Asset("text/html", len(value), hashlib.sha256(value).hexdigest(), value)


def public_path(path: str) -> str:
    if path == "index.html":
        return ""
    if path.endswith("/index.html"):
        return path[:-10]
    if path.endswith(".html"):
        return path[:-5]
    return path


def removed_paths(paths, current_paths) -> set[str]:
    return {path for path in set(paths) - set(current_paths)
            if path.startswith(("content/", "attachments/", "spaces/"))}


def _verification_error(kind, message, method, status=None, headers=None, body=b""):
    details = {"kind": kind, "method": method}
    if status is not None:
        details["http_status"] = status
    if headers is not None:
        ray_id = headers.get("CF-Ray", "")
        if CF_RAY.fullmatch(ray_id):
            details["ray_id"] = ray_id
        edge_code = headers.get("CF-Error-Type", "")
        if not re.fullmatch(r"[0-9]{4}", edge_code):
            match = re.fullmatch(rb"\s*error code:\s*([0-9]{4})\s*", body, re.IGNORECASE)
            edge_code = match[1].decode("ascii") if match else ""
        if edge_code:
            details["cloudflare_code"] = int(edge_code)
    return PagesError(message, http_status=status, verification=details)


def _public_http_error(method, status, headers, body):
    if status == 403:
        return _verification_error("access_denied", "Cloudflare 拒绝访问生产网址。", method, status, headers, body)
    return _verification_error("http_error", "线上核验请求失败。", method, status, headers, body)


def _record_error(record, error):
    record["message"] = error.message
    verification = error.details.get("verification")
    if verification is None:
        record.pop("verification_error", None)
    else:
        record["verification_error"] = verification


class PagesHosting:
    def __init__(self, data_dir: Path, releases, csp: str):
        self.path = data_dir / "hosting" / "pages.json"
        self.releases = releases
        self.csp = csp
        self.credentials = WindowsCredentials()
        self.client_factory = PagesClient
        self.poll_delay = 0.8
        self.probe_proxy = os.environ.get("PDS_PAGES_VERIFICATION_PROXY")
        self.cache_observer = None  # Optional read-only evidence source. Pages itself exposes no verified purge.
        self.headers = (
            f"/*\n  Content-Security-Policy: {csp}\n"
            "  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
            "  X-Frame-Options: DENY\n  Cache-Control: no-store, no-transform\n"
            "/attachments/*\n  ! Content-Security-Policy\n"
            "  Content-Security-Policy: sandbox; default-src 'none'\n"
            "  Content-Disposition: attachment\n  Content-Type: application/octet-stream\n"
        ).encode("ascii")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["version"] == 1 and isinstance(data["records"], list):
                return data
        except FileNotFoundError:
            return {"version": 1, "target": None, "records": []}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        raise PagesError("本机托管记录无法读取。请保留记录并检查数据目录。")

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, _canonical_bytes(data))
        except OSError:
            raise PagesError("本机无法保存托管记录。请检查数据目录后再核对线上结果。") from None

    def projects(self, account: str, token: str) -> list[str]:
        target = self._load()["target"]
        if not token and target and target["account"] == account:
            token = self.credentials.read(target["credential"])
        return self.client_factory(account, "project-list", token).projects(account)

    def configure(self, account: str, name: str, token: str) -> dict:
        data = self._load()
        if not ACCOUNT.fullmatch(account) or not PROJECT.fullmatch(name):
            raise PagesError("请输入 Cloudflare 账户 ID 和直接上传项目名称。")
        old = data["target"]
        if old and (old["account"], old["project"]) != (account, name):
            if any(row["status"] in POSSIBLY_PUBLIC for row in data["records"]):
                raise PagesError("当前项目已有上线记录。请先处理旧项目的公开副本。")
        if not token and old and (old["account"], old["project"]) == (account, name):
            token = self.credentials.read(old["credential"])
        if not TOKEN.fullmatch(token):
            raise PagesError("请输入具有 Pages 编辑权限的 API Token。")
        client = self.client_factory(account, name, token)
        project = client.project()
        client.upload_token()
        current = (project.get("canonical_deployment") or {}).get("id")
        same_target = old and (old["account"], old["project"]) == (account, name)
        if current and not same_target:
            raise PagesError("目标项目已有其他来源的生产部署。请选择专用于此资料库的空项目。")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not DEPLOYMENT.fullmatch(project_id):
            raise PagesError("Cloudflare 未返回有效的项目标识。")
        if old and data["records"] and project_id != old["project_id"]:
            raise PagesError("目标项目已变更，请在 Cloudflare 控制台检查原项目。")
        key = "PersonalDigitalSpace/Cloudflare/" + uuid4().hex
        target = {"account": account, "project": name, "credential": key,
                  "project_id": project_id, "domain": project["subdomain"], "checked_at": utcnow()}
        self.credentials.save(key, token)
        data["target"] = target
        data["connection_error"] = ""
        if old and same_target:
            data["production_unknown"] = current != next(
                (row.get("deployment_id") for row in reversed(data["records"]) if row.get("production_verified")),
                None,
            )
        try:
            self._save(data)
        except PagesError:
            self.credentials.delete(key)
            raise
        if old:
            self.credentials.delete(old["credential"])
        return self.status()

    def _client(self, target):
        token = self.credentials.read(target["credential"])
        return self.client_factory(target["account"], target["project"], token)

    @staticmethod
    def _project(client, target):
        project = client.project()
        if project.get("id") != target["project_id"] or project.get("subdomain") != target["domain"]:
            raise PagesError("Cloudflare 项目身份或公开网址已变化，请重新核对。")
        return project

    def status(self, active_id=None, eligible_ids=None, *, withdrawal_required=False) -> dict:
        data = self._load()
        target = data["target"]
        if not target:
            return {"status": "not_configured"}
        records = [row for row in data["records"] if row["project_id"] == target["project_id"]]
        latest = self._current_attempt(records)
        current = next((row for row in reversed(records) if row.get("production_verified")), {})
        possible = set()
        for row in records:
            if row["status"] in POSSIBLY_PUBLIC and not row.get("deleted"):
                possible.update(row["content_ids"])
        pending = sorted(possible - set(eligible_ids)) if eligible_ids is not None else []
        state = latest.get("status", "waiting")
        if state in IN_FLIGHT:
            state = "unconfirmed"
        if state == "succeeded" and data.get("production_unknown"):
            state = "unconfirmed"
        if (state == "succeeded" and current.get("release_id") != active_id
                and (current.get("release_id") is not None or withdrawal_required)):
            state = "waiting"
        return {
            "status": state,
            "project": target["project"],
            "account": target["account"],
            "target_id": target["project_id"],
            "target_url": "https://" + target["domain"],
            "public_url": "https://" + target["domain"]
            if (current or (latest.get("verification_job") or {}).get("positive", {}).get("done"))
            and not data.get("production_unknown") else None,
            "checked_at": target["checked_at"],
            "connection_error": data.get("connection_error", ""),
            "production_unknown": bool(data.get("production_unknown")),
            "current_release_id": current.get("release_id"),
            "last_attempt": {key: latest.get(key) for key in (
                "release_id", "deployment_id", "started_at", "message", "verification_error") } if latest else None,
            "verification": self._verification_status(latest),
            "can_publish_withdrawal_successor": self._can_publish_withdrawal_successor(data, latest),
            "pending_removal_ids": pending,
            "online_content_ids": sorted(possible),
            "history": [{key: row.get(key) for key in (
                "release_id", "deployment_id", "started_at", "status", "deleted", "message", "previous_release_ids")}
                for row in reversed(records[-50:])],
        }

    @staticmethod
    def preflight_plan(plan: dict) -> None:
        spaces = {row["primary_space"] for row in plan["contents"]}
        total = len(plan["content_ids"]) + len(plan["attachments"]) + len(spaces) + 3
        if total > MAX_ASSETS:
            raise PagesError("待发布文件超过 Cloudflare Pages 文件数量上限。")
        if any(row["bytes"] > MAX_ASSET_BYTES for row in plan["attachments"]):
            raise PagesError("有公开附件超过 Cloudflare Pages 的 25 MiB 单文件上限。")

    def _assets(self, release_id: str | None):
        assets = {"404.html": static_asset(NOT_FOUND)}
        if self.releases.withdrawal_pending():
            raise PagesError("本机有待处理的撤回，请先核对当前公开状态。")
        if release_id is None:
            assets["index.html"] = static_asset(EMPTY_SITE)
            return assets, []
        try:
            release = self.releases.get(release_id)
            if release["record"]["status"] != "active" or not self.releases.verify(release_id)["ok"]:
                raise PagesError("本机公开版本未通过完整性校验。")
            manifest = release["manifest"]
            self.releases._validate_release_against_db(manifest)
        except PagesError:
            raise
        except ArchiveError:
            raise PagesError("本机版本与当前公开选择不一致，请更新本机网站后再上线。") from None
        return self._manifest_assets(release_id, manifest, assets), manifest["content_ids"]

    def _manifest_assets(self, release_id, manifest, assets):
        root = self.releases.releases_dir / release_id
        for entry in manifest["files"]:
            path = entry["path"]
            if path == "index.html" or HTML_PATH.fullmatch(path):
                mime = "text/html"
            elif path == "assets/style.css":
                mime = "text/css"
            elif ATTACHMENT_PATH.fullmatch(path) and not path.endswith(("/.", "/..")):
                mime = "application/octet-stream"
            elif re.fullmatch(r"content/p_[0-9a-f]{20}\.json", path):
                continue  # Local public API projection is unnecessary on the website.
            else:
                raise PagesError("本机版本包含网站不支持的文件路径。")
            if entry["bytes"] > MAX_ASSET_BYTES:
                raise PagesError("有公开文件超过 Cloudflare Pages 的 25 MiB 单文件上限。")
            marker = b""
            if path.startswith("content/"):
                marker = f'  <meta name="release-id" content="{release_id}">\n'.encode("ascii")
            assets[path] = Asset(mime, entry["bytes"], entry["sha256"], root / path, marker)
        if "index.html" not in assets or len(assets) > MAX_ASSETS:
            raise PagesError("本机版本缺少网站首页，或超过 Cloudflare Pages 文件数量上限。")
        return assets

    def _recover_legacy_facts(self, record):
        release_id = record.get("release_id")
        assets = {"404.html": static_asset(NOT_FOUND)}
        if release_id is None:
            assets["index.html"] = static_asset(EMPTY_SITE)
        else:
            try:
                if not self.releases.verify(release_id)["ok"]:
                    raise PagesError("旧部署缺少可核验的本机版本。")
                manifest = self.releases.get(release_id)["manifest"]
                assets = self._manifest_assets(release_id, manifest, assets)
            except ArchiveError:
                raise PagesError("旧部署缺少可核验的本机版本。") from None
        facts = {path: asset.facts() for path, asset in assets.items()}
        fingerprint = hashlib.sha256(_canonical_bytes(facts) + self.headers).hexdigest()
        if fingerprint != record.get("fingerprint") or set(facts) != set(record["paths"]):
            raise PagesError("旧部署指纹与本机版本不同，无法安全补齐核验。")
        record.update(asset_facts=facts, samples=self._samples(facts))

    def _probes(self, client):
        if self.probe_proxy:
            return {"local": client.direct_public_probe(), "independent": client.public_probe(self.probe_proxy)}
        return {"local": client.public}

    @staticmethod
    def _can_publish_withdrawal_successor(data, record):
        job = record.get("verification_job", {})
        return bool(
            record.get("status") in {"verifying", "cleanup_pending"}
            and not data.get("production_unknown") and record.get("remote_identity")
            and job.get("canonical_id") == record.get("deployment_id") and job.get("positive", {}).get("done")
            and job.get("removed")
        )

    @staticmethod
    def _current_attempt(records):
        if not records:
            return {}
        latest = records[-1]
        # An attempt that never obtained a deployment cannot supersede an open task.
        for row in reversed(records):
            if row.get("status") != "failed" or row.get("deployment_id"):
                return row if row.get("status") in IN_FLIGHT | {"cleanup_pending"} else latest
        return latest

    @staticmethod
    def _verification_status(record):
        job = record.get("verification_job")
        if not job:
            return None
        outcomes = job.get("attempts", [])
        return {
            "canonical_id": job.get("canonical_id"),
            "canonical_confirmed": bool(record.get("deployment_id") and job.get("canonical_id") == record["deployment_id"]),
            "positive_verified": job["positive"]["done"],
            "production_retired": job["production"]["clean_rounds"] >= REQUIRED_CLEAN_ROUNDS,
            "retirement_required": bool(job["removed"]),
            "clean_rounds": job["production"]["clean_rounds"],
            "required_rounds": REQUIRED_CLEAN_ROUNDS,
            "cache_status": job["cache_status"],
            "blocking_pops": sorted({pop for pops in job["production"]["blocking"].values() for pop in pops}),
            "aliases": list(job["aliases"].values()),
            "recent_observations": [row for attempt in outcomes[-3:] for row in attempt["http"]][-40:],
            "complete": job.get("complete", False),
        }

    @staticmethod
    def _samples(assets, previous=None):
        previous = previous or {}
        paths = {"index.html"}
        paths.update(path for path, facts in assets.items() if path.startswith(("content/", "attachments/", "spaces/", "assets/"))
                     and previous.get(path) != facts)
        return {path: assets[path] for path in sorted(paths)}

    @staticmethod
    def _canonical_id(project):
        return (project.get("canonical_deployment") or {}).get("id")

    def _verify_public(self, client, target, record, attempt):
        domain = target["domain"]
        job = record.get("verification_job")
        offset = job["positive"]["offset"] if job else 0
        samples = list(record["samples"].items())
        for path, expected in samples[offset:offset + PROBE_BATCH]:
            evidence, headers, body = public_observation(
                client.public, "local", domain, public_path(path), "GET", limit=max(expected["size"], 1),
            )
            if attempt is not None:
                attempt["http"].append(evidence)
            status = evidence["http_status"]
            if status is None:
                raise _verification_error("http_error", "无法完成线上核验请求，请检查网络连接。", "GET")
            if status in (404, 410):
                raise _verification_error("file_mismatch", "生产网址文件核验失败：应公开的文件不存在。",
                                          "GET", status, headers)
            if status != 200:
                raise _public_http_error("GET", status, headers, body)
            if len(body) != expected["size"] or evidence.get("sha256") != expected["sha256"]:
                raise _verification_error("file_mismatch", "生产网址文件核验失败：文件内容与发布版本不一致。",
                                          "GET", status, headers)
            mime = headers.get("Content-Type", "").split(";")[0].strip().lower()
            disposition = headers.get("Content-Disposition", "").split(";")[0].strip().lower()
            policy = "sandbox; default-src 'none'" if path.startswith("attachments/") else self.csp
            csp = headers.get("Content-Security-Policy", "")
            cache = {part.strip().lower() for part in headers.get("Cache-Control", "").split(",")}
            if (mime != expected["mime"] or csp.strip() != policy
                    or headers.get("X-Content-Type-Options", "").strip().lower() != "nosniff"
                    or headers.get("X-Frame-Options", "").strip().lower() != "deny"
                    or headers.get("Referrer-Policy", "").strip().lower() != "no-referrer"
                    or not {"no-store", "no-transform"} <= cache
                    or (path.startswith("attachments/") and disposition != "attachment")):
                raise _verification_error("header_mismatch", "生产网址响应头核验失败：内容类型或安全响应头不符合要求。",
                                          "GET", status, headers)
            offset += 1
        if job:
            job["positive"]["offset"] = offset
        if offset < len(samples):
            return False
        for path in ("api/content", "releases"):
            evidence, headers, body = public_observation(client.public, "local", domain, path, "HEAD", limit=0)
            if attempt is not None:
                attempt["http"].append(evidence)
            status = evidence["http_status"]
            # These two admin endpoints already used 404 or 410 as their hiding contract.
            if status not in (404, 410):
                if status == 200:
                    raise _verification_error("file_mismatch", "生产网址文件核验失败：应隐藏的路径仍可访问。",
                                              "HEAD", status, headers)
                if status is None:
                    raise _verification_error("http_error", "无法完成线上核验请求，请检查网络连接。", "HEAD")
                raise _public_http_error("HEAD", status, headers, body)
        return True

    def _new_job(self, record, previous):
        removed = {}
        for old in previous:
            facts_by_path = old.get("asset_facts")
            if not facts_by_path:
                facts_by_path = dict(old.get("samples", {}))
                try:
                    manifest = self.releases.get(old["release_id"])["manifest"] if old.get("release_id") else None
                except ArchiveError:
                    manifest = None
                if manifest:
                    facts_by_path.update({entry["path"]: {"size": entry["bytes"], "sha256": entry["sha256"]}
                                          for entry in manifest["files"] if entry["kind"] == "attachment"})
            for path in removed_paths(old["paths"], record["paths"]):
                facts = removed.setdefault(path, [])
                fact = facts_by_path.get(path)
                if fact and fact not in facts:
                    facts.append(fact)
        blocking = {}
        aliases = {}
        for old in previous:
            job = old.get("verification_job", {})
            for path, facts in job.get("removed", {}).items():
                if path in record["paths"]:
                    continue
                inherited = removed.setdefault(path, [])
                inherited.extend(fact for fact in facts if fact not in inherited)
            for path, pops in job.get("production", {}).get("blocking", {}).items():
                if path in removed:
                    blocking[path] = sorted(set(blocking.get(path, [])) | set(pops))
            for deployment_id, alias in job.get("aliases", {}).items():
                if alias.get("blocking"):
                    saved_alias = aliases.setdefault(deployment_id, {"id": alias["id"], "blocking": {}})
                    for path, pops in alias["blocking"].items():
                        previous_pops = saved_alias["blocking"].get(path, [])
                        saved_alias["blocking"][path] = sorted(set(previous_pops) | set(pops))
        legacy_error = record.get("verification_error", {})
        ray = legacy_error.get("ray_id", "")
        if legacy_error.get("http_status") == 200 and legacy_error.get("method") == "HEAD" and CF_RAY.fullmatch(ray) and "-" in ray:
            for path in removed:
                blocking[path] = sorted(set(blocking.get(path, [])) | {ray[-3:]})
        return {
            "created_at": utcnow(), "canonical_id": None, "positive": {"offset": 0, "done": False},
            "removed": removed, "production": {"offset": 0, "round_ok": True, "clean_rounds": 0, "blocking": blocking},
            "aliases": aliases, "cache_status": "UNKNOWN" if removed else "NOT_NEEDED", "attempts": [],
            "complete": False,
        }

    def _cache_status(self, target, record, paths):
        if self.cache_observer is None:
            return "UNSUPPORTED"
        try:
            state = self.cache_observer(target["domain"], record["deployment_id"], tuple(paths))
        except TimeoutError:
            return "TIMEOUT"
        except (OSError, PagesError):
            return "FAILED"
        return state if isinstance(state, str) and state in CACHE_OBSERVATIONS else "UNKNOWN"

    def _verify_retirement(self, job, probes, host, attempt):
        state = job["production"]
        paths = sorted(job["removed"])
        if len(probes) < 2:
            attempt["reason"] = "independent_exit_required"
        start = state["offset"]
        for path in paths[start:start + PROBE_BATCH]:
            observations, clean, bad, missing = retirement_pair(probes, host, public_path(path), job["removed"][path])
            attempt["http"].extend(observations)
            cleared = track_blockers(state["blocking"], path, clean, bad)
            sufficient = missing and len(clean) >= 2 and cleared
            state["round_ok"] &= sufficient
            if not sufficient and len(probes) >= 2:
                attempt["reason"] = "production_retirement_unconfirmed"
        state["offset"] = min(start + PROBE_BATCH, len(paths))
        if state["offset"] != len(paths):
            return False
        state["clean_rounds"] = (min(REQUIRED_CLEAN_ROUNDS, state["clean_rounds"] + 1)
                                 if state["round_ok"] else 0)
        state["offset"] = 0
        state["round_ok"] = True
        return state["clean_rounds"] >= REQUIRED_CLEAN_ROUNDS

    @staticmethod
    def _identity(deployment, row, target):
        marker = ((deployment.get("deployment_trigger") or {}).get("metadata") or {}).get("commit_message")
        host = independent_host(deployment.get("url"), target["domain"])
        created_at = deployment.get("created_on")
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return None
        if (deployment.get("id") != row.get("deployment_id") or deployment.get("environment") != "production"
                or deployment.get("project_id", target["project_id"]) != target["project_id"]
                or row.get("project_id") != target["project_id"] or not host or not created.tzinfo
                or not isinstance(marker, str) or not re.fullmatch(r"PersonalDigitalSpace [0-9a-f]{32}", marker)
                or marker != row.get("marker")):
            return None
        return {"host": host, "created_at": created.isoformat(), "origin": "application_marker"}

    @staticmethod
    def _approval(target, record, candidate):
        fields = [target["project_id"], record["deployment_id"], candidate["id"],
                  candidate["host"], candidate["created_at"], candidate["paths"]]
        return hashlib.sha256(_canonical_bytes(fields)).hexdigest()

    def _verify_aliases(self, client, target, record, previous, probes, attempt):
        job = record["verification_job"]
        all_retired = True
        for old in previous:
            paths = sorted(removed_paths(old["paths"], record["paths"]))
            if not paths:
                continue
            deployment_id = old.get("deployment_id")
            alias = job["aliases"].setdefault(deployment_id or "unknown", {"id": deployment_id})
            alias.update(paths=paths, status="unknown", approval=None)
            all_retired &= self._verify_alias(client, target, record, old, alias, probes, attempt)
        return all_retired

    def _verify_alias(self, client, target, record, old, alias, probes, attempt):
        deployment_id = alias["id"]
        facts_by_path = record["verification_job"]["removed"]
        if not deployment_id or deployment_id == record["deployment_id"]:
            return False
        absent = False
        try:
            remote = client.deployment(deployment_id)
        except PagesError as error:
            if error.http_status != 404:
                return False
            absent = True
            identity = old.get("remote_identity")
        else:
            identity = self._identity(remote, old, target)
        if not identity:
            return False
        old["remote_identity"] = identity
        alias.update(identity)
        alias["api_absent"] = absent
        alias["protected"] = not old.get("retirement_managed")
        alias["delete_requested"] = bool(old.get("delete_request"))
        blocking = alias.setdefault("blocking", {})
        if not absent:
            # A small public sample distinguishes a live alias from a control-plane-only record.
            path = public_path(alias["paths"][0])
            rows, clean, bad, missing = retirement_pair(probes, alias["host"], path, facts_by_path[alias["paths"][0]])
            attempt["http"].extend(rows)
            track_blockers(blocking, path, clean, bad)
            alias["status"] = "present" if missing else "accessible" if any(
                row["http_status"] in (200, 206) for row in rows) else "unconfirmed"
            if not alias["protected"] and not alias["delete_requested"]:
                alias["approval"] = self._approval(target, record, alias)
            return False
        if alias.get("verified_at"):
            alias["status"] = "retired"
            return True
        paths = ["index.html", *alias["paths"]]
        start = alias.get("offset", 0)
        retired = alias.get("round_ok", True)
        for asset in paths[start:start + PROBE_BATCH]:
            path = public_path(asset)
            rows, clean, bad, missing = retirement_pair(probes, alias["host"], path, facts_by_path.get(asset, []))
            attempt["http"].extend(rows)
            cleared = track_blockers(blocking, path, clean, bad)
            retired &= missing and len(clean) >= 2 and cleared
        end = start + PROBE_BATCH
        if end < len(paths):
            alias.update(offset=end, round_ok=retired, status="verifying")
            return False
        alias.update(offset=0, round_ok=True, status="retired" if retired else "unconfirmed")
        if retired:
            alias["verified_at"] = utcnow()
            old["deleted"] = True
            if old.get("delete_request"):
                old["delete_request"]["state"] = "VERIFIED_ABSENT"
        return retired

    def cleanup(self, active_id, target_id, deployment_id, approval):
        data = self._load()
        target = data["target"]
        if (not target or target["project_id"] != target_id or not isinstance(deployment_id, str)
                or not DEPLOYMENT.fullmatch(deployment_id) or not isinstance(approval, str)):
            raise PagesError("清理目标无效，请刷新后重新确认。")
        records = [row for row in data["records"] if row["project_id"] == target_id]
        record = self._current_attempt(records)
        job = record.get("verification_job", {})
        candidate = job.get("aliases", {}).get(deployment_id, {})
        old = next((row for row in records if row.get("deployment_id") == deployment_id), {})
        if (record.get("status") != "cleanup_pending"
                or not candidate.get("approval") or candidate["approval"] != approval
                or not old.get("retirement_managed") or old.get("delete_request")
                or deployment_id == record.get("deployment_id")):
            raise PagesError("旧部署尚未满足清理条件，或批准的目标已变化。")
        client = self._client(target)
        # Recheck both the original public addresses and current control plane before deleting.
        self._run_observation(data, client, record)
        candidate = job["aliases"].get(deployment_id, {})
        if (record["status"] != "cleanup_pending" or not record.get("production_verified")
                or job["production"]["clean_rounds"] < REQUIRED_CLEAN_ROUNDS
                or job["attempts"][-1]["outcome"] != "cleanup_pending"
                or candidate.get("approval") != approval):
            raise PagesError("生产部署、核验结果或清理目标已变化，请重新核对。")
        self._check_cleanup_paths(data, client, record, candidate)
        canonical = self._canonical_id(self._project(client, target))
        if canonical != record["deployment_id"]:
            self._mark_drift(data, record, canonical)
            self._save(data)
            raise PagesError("当前生产部署已变化，请刷新后重新核对。")
        old["delete_request"] = {"at": utcnow(), "approval": approval, "state": "REQUESTED"}
        candidate.update(delete_requested=True, approval=None)
        job["production"].update(clean_rounds=0, offset=0, round_ok=True)
        job["complete"] = False
        self._save(data)  # An ambiguous response or crash must not repeat this destructive request.
        try:
            client.delete_deployment(deployment_id)
        except PagesError as error:
            old["delete_request"].update(state="UNCONFIRMED", http_status=error.http_status)
            record["message"] = "旧部署移除结果待确认，请核对部署记录与独立地址。"
        else:
            old["delete_request"]["state"] = "API_ABSENT"
            record["message"] = "已收到旧部署移除回执，正在等待复验原网址与独立地址。"
        self._save(data)
        self._run_observation(data, client, record)
        return self.status(active_id)

    def _check_cleanup_paths(self, data, client, record, candidate):
        job = record["verification_job"]
        state = job["production"]
        host = data["target"]["domain"]
        probes = self._probes(client)
        attempt = {"at": utcnow(), "outcome": "incomplete", "reason": "cleanup_preflight", "http": []}
        job["attempts"].append(attempt)
        ready = True
        for path in candidate["paths"][:PROBE_BATCH]:
            rows, clean, bad, missing = retirement_pair(probes, host, public_path(path), job["removed"][path])
            attempt["http"].extend(rows)
            cleared = track_blockers(state["blocking"], path, clean, bad)
            ready &= missing and len(clean) >= 2 and cleared
        attempt["outcome"] = "verified" if ready else "blocked"
        if not ready:
            state.update(clean_rounds=0, offset=0, round_ok=True)
            candidate["approval"] = None
            record.update(status="verifying", production_verified=False,
                          message="批准前复验发现旧生产网址尚未下线。")
        self._save(data)
        if not ready:
            raise PagesError("旧生产网址尚未通过复验，旧部署未移除。")

    def _observe(self, data, client, record, attempt, *, polls=1, recheck_retirement=False):
        target = data["target"]
        deployment_id = record.get("deployment_id")
        if not deployment_id:
            deployment_id = client.find_deployment(record["marker"])
            if not deployment_id:
                raise PagesError("暂未找到提交记录。请核对 Cloudflare 部署历史后再次查询。")
        if record["status"] == "creating" or not record.get("deployment_id"):
            record.update(deployment_id=deployment_id, status="verifying")
            self._save(data)
        for retry in range(polls):
            deployment = client.deployment(deployment_id)
            if deployment.get("id") != deployment_id or deployment.get("environment") != "production":
                raise PagesError("暂时无法确认该部署属于生产网站。")
            stage = deployment.get("latest_stage") or {}
            if stage.get("status") in ("failure", "canceled") or deployment.get("is_skipped"):
                record.update(status="failed", message="Cloudflare 未完成本次部署。")
                return
            if stage.get("name") == "deploy" and stage.get("status") == "success":
                break
            if retry + 1 < polls:
                time.sleep(self.poll_delay)
        else:
            raise PagesError("Cloudflare 正在部署。稍后可核对线上结果。")
        identity = self._identity(deployment, record, target)
        if identity:
            record["remote_identity"] = identity
        job = record["verification_job"]
        canonical = self._canonical_id(self._project(client, target))
        if canonical != deployment_id:
            self._mark_drift(data, record, canonical)
            attempt["reason"] = "canonical_changed"
            return
        job["canonical_id"] = canonical
        data["production_unknown"] = False
        if not job["positive"]["done"]:
            job["positive"]["done"] = self._verify_public(client, target, record, attempt)
        if not job["positive"]["done"]:
            record["message"] = "生产部署已切换，正在分批核对新版资源。"
            return
        previous = self._previous(data, record)
        if job["removed"]:
            cache_status = self._cache_status(target, record, sorted(job["removed"]))
            if job["cache_status"] != cache_status:
                job["production"].update(clean_rounds=0, offset=0, round_ok=True)
            job["cache_status"] = cache_status
            probes = self._probes(client)
            production_clear = ((not recheck_retirement and job["production"]["clean_rounds"] >= REQUIRED_CLEAN_ROUNDS)
                                or self._verify_retirement(job, probes, target["domain"], attempt))
            if not production_clear or job["cache_status"] not in ("UNSUPPORTED", "COMPLETED"):
                job["complete"] = False
                record.update(status="verifying", production_verified=False)
                record["message"] = "新版已核验，旧生产网址或缓存状态仍待核对。"
                return
            aliases_clear = self._verify_aliases(client, target, record, previous, probes, attempt)
        else:
            aliases_clear = True
        canonical = self._canonical_id(self._project(client, target))
        if canonical != deployment_id:
            self._mark_drift(data, record, canonical)
            attempt["reason"] = "canonical_changed"
            return
        for old in previous:
            old["production_verified"] = False
        data["production_unknown"] = False
        job["complete"] = aliases_clear
        record.update(production_verified=True, status="succeeded" if aliases_clear else "cleanup_pending",
                      message="已核实 Cloudflare 生产网址及适用的旧地址退场。"
                      if aliases_clear else "生产网址已核验，旧部署独立地址仍待处理。")

    @staticmethod
    def _previous(data, record):
        return [old for old in data["records"] if old is not record and old["project_id"] == record["project_id"]
                and old["status"] in POSSIBLY_PUBLIC]

    @staticmethod
    def _mark_drift(data, record, canonical):
        job = record["verification_job"]
        job.update(canonical_id=canonical, complete=False)
        job["positive"].update(offset=0, done=False)
        job["production"].update(offset=0, round_ok=True, clean_rounds=0)
        for alias in job["aliases"].values():
            alias.update(approval=None, offset=0, round_ok=True)
            alias.pop("verified_at", None)
        data["production_unknown"] = True
        record.update(status="verifying", production_verified=False,
                      message="当前 Canonical 已变化，需重新核对；旧网址下线任务仍保留。")

    def _run_observation(self, data, client, record, *, polls=1, recheck_retirement=False):
        if "verification_job" not in record:
            record["verification_job"] = self._new_job(record, self._previous(data, record))
        job = record["verification_job"]
        if job["attempts"] and job["attempts"][-1]["outcome"] == "incomplete":
            job["production"].update(clean_rounds=0, offset=0, round_ok=True)
            job["complete"] = False
            for alias in job["aliases"].values():
                alias.update(approval=None, offset=0, round_ok=True)
                alias.pop("verified_at", None)
        attempt = {"at": utcnow(), "http": [], "outcome": "incomplete"}
        job["attempts"].append(attempt)
        self._save(data)
        record.pop("verification_error", None)
        try:
            if "asset_facts" not in record:
                self._recover_legacy_facts(record)
            self._observe(data, client, record, attempt, polls=polls, recheck_retirement=recheck_retirement)
        except PagesError as error:
            _record_error(record, error)
            attempt["error"] = dict(error.details.get("verification", {}))
            attempt["error"].pop("ray_id", None)  # Individual observations already hold masked Ray IDs.
            if not attempt["error"]:
                attempt["error"] = {"kind": "operation_error", "http_status": error.http_status}
            attempt["outcome"] = "error"
        else:
            attempt["outcome"] = record["status"]
        finally:
            attempt.update(canonical_id=job["canonical_id"], cache_status=job["cache_status"])
            self._save(data)

    def check(self) -> None:
        data = self._load()
        target = data["target"]
        if not target:
            raise PagesError("请先连接 Cloudflare Pages。")
        try:
            client = self._client(target)
            project = self._project(client, target)
        except PagesError as error:
            data["connection_error"] = error.message
            self._save(data)
            return
        target["checked_at"] = utcnow()
        data["connection_error"] = ""
        records = [row for row in data["records"] if row["project_id"] == target["project_id"]]
        last = self._current_attempt(records)
        canonical = self._canonical_id(project)
        verified = next((row.get("deployment_id") for row in reversed(records) if row.get("production_verified")), None)
        data["production_unknown"] = canonical != verified
        if (last.get("status") in IN_FLIGHT | {"cleanup_pending"}
                or (last.get("status") == "succeeded" and
                    (canonical != last.get("deployment_id") or last.get("verification_job", {}).get("removed")))):
            self._run_observation(data, client, last, recheck_retirement=True)
        else:
            self._save(data)

    def deploy(self, release_id, target_id, *, force=False, withdrawal_successor=False) -> dict:
        data = self._load()
        target = data["target"]
        if not target or target["project_id"] != target_id:
            raise PagesError("托管项目已变更，请刷新后重新确认。")
        records = [row for row in data["records"] if row["project_id"] == target_id]
        last = self._current_attempt(records)
        successor = withdrawal_successor and not force and self._can_publish_withdrawal_successor(data, last)
        if last.get("status") in IN_FLIGHT and not successor:
            raise PagesError("上次部署结果尚未确认，请先核对线上结果。")
        if last.get("status") == "cleanup_pending" and not successor:
            self._run_observation(data, self._client(target), last)
            return self.status(release_id)
        if last.get("status") == "succeeded" and last["release_id"] == release_id and not force:
            self.check()
            return self.status(release_id)
        assets = None
        if successor:
            assets, ids = self._assets(release_id)
            if (not set(assets) <= set(last["paths"]) or not set(ids) <= set(last["content_ids"])
                    or not removed_paths(last["paths"], assets)):
                raise PagesError("仍有退场任务时，后继发布只能移除当前线上内容或附件。")
            if any(path.startswith("attachments/") and asset.facts() != last["asset_facts"].get(path)
                   for path, asset in assets.items()):
                raise PagesError("仍有退场任务时，后继发布不能更换剩余附件。")
        record = {
            "project_id": target_id, "release_id": release_id, "started_at": utcnow(),
            "status": "failed", "marker": "PersonalDigitalSpace " + uuid4().hex,
            "content_ids": [], "paths": [], "deployment_id": None, "retirement_managed": True,
        }
        data["records"].append(record)
        try:
            if assets is None:
                assets, ids = self._assets(release_id)
            facts = {path: asset.facts() for path, asset in assets.items()}
            fingerprint = hashlib.sha256(_canonical_bytes(facts) + self.headers).hexdigest()
            client = self._client(target)
            project = self._project(client, target)
            canonical = self._canonical_id(project)
            verified = last if successor else next((old for old in reversed(records) if old.get("production_verified")), {})
            if canonical != verified.get("deployment_id"):
                if successor:
                    self._mark_drift(data, last, canonical)
                    data["records"].pop()
                    self._save(data)
                    return self.status(release_id)
                data["production_unknown"] = True
                raise PagesError("检测到其他来源的生产部署。请先核对 Cloudflare 项目。")
            if not force and last.get("status") == "succeeded" and last.get("fingerprint") == fingerprint:
                last.setdefault("previous_release_ids", []).append(last["release_id"])
                last["release_id"] = release_id
                data["records"].pop()
                self._save(data)
                return self.status(release_id)
            record.update(content_ids=ids, paths=sorted(assets), asset_facts=facts,
                          samples=self._samples(facts, verified.get("asset_facts")), fingerprint=fingerprint)
            manifest = client.upload(assets, client.upload_token())
            if successor:
                current = self._canonical_id(self._project(client, target))
                if current != canonical:
                    self._mark_drift(data, last, current)
                    data["records"].pop()
                    self._save(data)
                    return self.status(release_id)
            record.update(status="creating", message="部署已提交或等待确认。")
            self._save(data)  # Retain the marker before an irreversible request.
            try:
                result = client.create_deployment(manifest, project["production_branch"], record["marker"], self.headers)
            except PagesError as error:
                if error.http_status is not None and 400 <= error.http_status < 500:
                    record["status"] = "failed"
                raise
            deployment_id = result.get("id", "") if isinstance(result, dict) else ""
            if not isinstance(deployment_id, str) or not DEPLOYMENT.fullmatch(deployment_id):
                raise PagesError("未取得可靠的部署编号，请核对线上结果。")
            record.update(deployment_id=deployment_id, status="verifying")
            self._save(data)
            self._run_observation(data, client, record, polls=3)
        except PagesError as error:
            if successor and record["status"] == "failed":
                # No deployment was accepted; keep the prior Canonical's job active.
                data["records"].pop()
                last["message"] = "撤回后继版本尚未提交，可重试本机版本的线上撤回。"
                last["verification_job"]["attempts"].append({
                    "at": utcnow(), "outcome": "withdrawal_submit_failed", "http": [],
                    "error": {"http_status": error.http_status},
                })
            else:
                _record_error(record, error)
            self._save(data)
        return self.status(release_id)
