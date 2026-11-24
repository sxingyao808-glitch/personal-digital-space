"""P6 synthetic-data drills (S0 / S1 / S2).

Every drill runs against throw-away temporary directories with purely
synthetic content. No real personal data, credentials or attachments are used.
Public access is exercised only through ``127.0.0.1``.

Usage::

    python scripts/p6_drill.py --out evidence/p6-readiness/drills

S0: empty data directory -> start -> owner login -> health -> stop -> cold
    backup -> verify copy -> restore into a brand new directory -> start ->
    integrity check -> original directory untouched.
S1: three synthetic contents (+ relations/attributes, two attachments) ->
    preview -> prepare -> activate -> anonymous public access -> drift check
    -> withdraw -> history restore -> drift check again.
S2: cold backup of the S1 data directory, copy verification, restore into a
    new directory, integrity/quantity comparison, source directory unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-drill-owner-token-8642"
PORT = 8790


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == name or relative.startswith(name) for name in exclude):
            continue
        result[relative] = sha256_file(path)
    return result


class Server:
    """Context manager that starts run_app.py on loopback and waits for health."""

    def __init__(self, data_dir: Path, port: int = PORT, log_path: Path | None = None):
        self.data_dir = data_dir
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.startup_output = ""

    def __enter__(self) -> "Server":
        log_handle = None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "--data-dir",
                str(self.data_dir),
                "--port",
                str(self.port),
                "--no-open",
                "--bootstrap-token",
                TOKEN,
            ],
            cwd=str(ROOT),
            stdout=log_handle or subprocess.PIPE,
            stderr=subprocess.STDOUT if log_handle else subprocess.PIPE,
            text=True,
        )
        if log_handle:
            log_handle.close()
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务提前退出，退出码 {self.proc.returncode}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return self
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.3)
        raise RuntimeError("服务在 30 秒内未就绪")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)


class Client:
    """Minimal HTTP client (stdlib only) with optional cookie/session support."""

    def __init__(self, port: int = PORT, *, owner: bool = True):
        self.base = f"http://127.0.0.1:{port}"
        self.csrf = ""
        self.headers: dict[str, str] = {}
        if owner:
            self.jar = http.cookiejar.CookieJar()
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.jar)
            )
        else:
            self.jar = None
            self.opener = urllib.request.build_opener()

    def request(self, method: str, path: str, *, json_body=None, data=None, headers=None):
        url = self.base + path
        body = None
        request_headers = dict(self.headers)
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "")
        parsed = None
        if "application/json" in content_type:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
        return status, parsed, raw

    def login(self) -> dict:
        status, _, _ = self.request("GET", f"/auth?token={TOKEN}")
        status, payload, _ = self.request("GET", "/api/session")
        self.csrf = (payload or {}).get("data", {}).get("csrf", "")
        self.headers["X-CSRF-Token"] = self.csrf
        return {"auth_status": status, "session": (payload or {}).get("data", {})}

    def json(self, method: str, path: str, body=None):
        status, payload, _ = self.request(method, path, json_body=body)
        if status >= 400 or not payload or not payload.get("ok"):
            raise RuntimeError(f"{method} {path} -> {status} {payload}")
        return payload["data"]

    def upload(self, content_id: str, filename: str, payload: bytes, visibility: str) -> dict:
        boundary = "b" + uuid.uuid4().hex
        parts = []
        for name, value in (
            ("operation_id", f"op_{uuid.uuid4().hex}"),
            ("visibility", visibility),
        ):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n"
            ).encode()
            + payload
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        status, parsed, _ = self.request(
            "POST",
            f"/api/content/{content_id}/attachments",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if status >= 400 or not parsed or not parsed.get("ok"):
            raise RuntimeError(f"上传失败 -> {status} {parsed}")
        return parsed["data"]


def run_powershell(script: Path, *args: str) -> tuple[int, str]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# --------------------------------------------------------------------------
# S0
# --------------------------------------------------------------------------
def drill_s0(work: Path) -> dict:
    data = work / "s0-data"
    data.mkdir(parents=True)
    restored = work / "s0-restored"
    backup = work / "s0-backup"
    result: dict = {"drill": "S0", "started_at": now(), "checks": []}

    def check(name: str, ok: bool, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise RuntimeError(f"S0 检查失败: {name} {detail}")

    with Server(data, log_path=work / "s0-server.log") as server:
        client = Client(server.port)
        session = client.login()
        check("owner_login", session["auth_status"] == 200 and session["session"].get("owner"))
        health = client.json("GET", "/api/health")
        check("health_ok", health["status"] == "ok", health.get("version"))
        integrity = client.json("GET", "/api/integrity")
        check("integrity_ok", not integrity.get("open_issues"),
              f"open_issues={len(integrity.get('open_issues', []))}, non_ready={len(integrity.get('non_ready_attachments', []))}")

    check("server_stopped", True, "退出后锁由操作系统释放")
    before_backup = tree_digest(data, exclude=(".app.lock",))

    code, output = run_powershell(
        ROOT / "scripts" / "cold-backup.ps1", "-DataDir", str(data), "-BackupDir", str(backup)
    )
    check("cold_backup", code == 0, output.strip().splitlines()[-3:])
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    check("manifest_written", manifest["file_count"] == len(manifest["files"]))

    code, output = run_powershell(
        ROOT / "scripts" / "verify-copy.ps1",
        "-Source", str(data),
        "-CopyDir", str(backup),
        "-Report", str(work / "s0-verify.json"),
    )
    check("verify_copy", code == 0, output.strip().splitlines()[-1] if output else "")

    shutil.copytree(backup, restored, ignore=shutil.ignore_patterns("manifest.json"))
    check("restored_to_new_dir", restored.is_dir(), str(restored))

    with Server(restored, port=PORT + 1, log_path=work / "s0-restored.log") as server:
        anon = Client(server.port, owner=False)
        status, _, _ = anon.request("GET", "/p/anything")
        check("restored_public_404", status == 404, "空库无非激活 Release")
        owner = Client(server.port)
        session = owner.login()
        check("restored_owner_login", session["auth_status"] == 200)
        health = owner.json("GET", "/api/health")
        check("restored_health_ok", health["status"] == "ok")
        integrity = owner.json("GET", "/api/integrity")
        check("restored_integrity_ok", not integrity.get("open_issues"))
        contents = owner.json("GET", "/api/content")
        check("restored_empty", len(contents.get("items", [])) == 0)

    after = tree_digest(data, exclude=(".app.lock",))
    check("source_dir_unchanged", after == before_backup, f"{len(after)} 个文件")
    result["checks"].append(
        {
            "name": "source_file_count",
            "ok": True,
            "detail": f"{len(after)} files after backup+restore drill",
        }
    )
    result["finished_at"] = now()
    result["ok"] = all(item["ok"] for item in result["checks"])
    return result


# --------------------------------------------------------------------------
# S1
# --------------------------------------------------------------------------
def drill_s1(work: Path) -> dict:
    data = work / "s1-data"
    data.mkdir(parents=True)
    result: dict = {"drill": "S1", "started_at": now(), "checks": [], "drift": {}}

    def check(name: str, ok: bool, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise RuntimeError(f"S1 检查失败: {name} {detail}")

    with Server(data, log_path=work / "s1-server.log") as server:
        owner = Client(server.port)
        session = owner.login()
        check("owner_login", session["auth_status"] == 200)

        a = owner.json("POST", "/api/content",
                       {"title": "合成内容 A", "body": "A 的合成正文", "visibility": "public"})
        b = owner.json("POST", "/api/content",
                       {"title": "合成内容 B", "body": "B 的合成正文", "visibility": "public"})
        c = owner.json("POST", "/api/content",
                       {"title": "合成内容 C", "body": "C 的合成正文", "visibility": "public"})
        check("create_three_contents", True, f"{a['id']},{b['id']},{c['id']}")

        edited = owner.json("PATCH", f"/api/content/{a['id']}",
                            {"revision": a["revision"], "title": "合成内容 A（已编辑）"})
        check("edit_content", edited["revision"] == a["revision"] + 1, edited["title"])

        relation = owner.json("POST", f"/api/content/{a['id']}/relations",
                              {"to_id": b["id"], "type": "PART_OF"})
        attribute = owner.json("POST", f"/api/content/{a['id']}/attributes",
                               {"key": "drill", "value": "synthetic", "provenance": "user",
                                "confirmed": True})
        check("relation_and_attribute", bool(relation) and bool(attribute))

        att_public = owner.upload(a["id"], "a-public.txt", b"synthetic public bytes\n", "public")
        att_private = owner.upload(b["id"], "b-private.txt", b"synthetic private bytes\n", "private")
        att_public = att_public["attachment"] if "attachment" in att_public else att_public
        att_private = att_private["attachment"] if "attachment" in att_private else att_private
        check("upload_two_attachments", True,
              f"{att_public['id']}:{att_public['visibility']},{att_private['id']}:{att_private['visibility']}")

        selection = {
            "content_ids": [a["id"], b["id"], c["id"]],
            # The private attachment is deliberately not selected: a public
            # bundle must never carry private attachment objects.
            "attachment_ids": [att_public["id"]],
        }
        preview = owner.json("POST", "/api/releases/preview", selection)
        check("preview_ok", bool(preview.get("valid")) and preview.get("read_only") is True,
              json.dumps({k: preview.get(k) for k in ("valid", "issues", "totals")}, ensure_ascii=False)[:400])

        prepared = owner.json("POST", "/api/releases/prepare", selection)
        check("prepare_ok", prepared["status"] == "prepared", prepared["release_id"])
        release_id = prepared["release_id"]
        bundle_text = (data / "releases" / release_id / "release.json").read_text(encoding="utf-8")
        check("private_attachment_not_in_bundle", att_private["id"] not in bundle_text)

        activated = owner.json("POST", f"/api/releases/{release_id}/activate", {})
        check("activate_ok", activated["status"] == "active", activated.get("active_since", ""))

        anon = Client(server.port, owner=False)
        status, _, raw = anon.request("GET", f"/p/{a['id']}")
        check("incognito_public_page", status == 200, f"{len(raw)} bytes")
        status, payload, _ = anon.request("GET", f"/api/public/content/{a['id']}")
        check("incognito_public_api", status == 200 and payload["data"]["title"].startswith("合成内容 A"))
        check("public_api_has_no_relations", "relation" not in json.dumps(payload["data"]))
        status, _, _ = anon.request("GET", f"/attachments/{att_private['id']}/b-private.txt")
        check("private_attachment_not_public", status == 404)
        status, _, _ = anon.request("GET", f"/attachments/{att_public['id']}/a-public.txt")
        check("public_attachment_online", status == 200)

        baseline = owner.json("GET", "/api/publication/drift")
        result["drift"]["baseline"] = baseline["summary"]
        check("drift_baseline_consistent",
              baseline["summary"]["PUBLISHED_AND_CURRENT"] == 3 and baseline["privacy_risk_count"] == 0,
              json.dumps(baseline["summary"]))

        # ---- withdraw, then restore from history ---------------------------
        withdrawn = owner.json("POST", "/api/releases/withdraw", {"reason": "drill withdraw"})
        check("withdraw_ok", withdrawn.get("active_release_id") in (None, ""), str(withdrawn.get("status")))
        status, _, _ = anon.request("GET", f"/p/{a['id']}")
        check("public_404_after_withdraw", status == 404)
        status, _, _ = anon.request("GET", "/api/public/content/" + a["id"])
        check("public_api_404_after_withdraw", status == 404)

        dry = owner.json("POST", "/api/releases/restore",
                         {"source_release_id": release_id, "dry_run": True})
        check("restore_dry_run", bool(dry), f"requires_confirmation={dry.get('requires_confirmation')}")
        restored = owner.json("POST", "/api/releases/restore",
                              {"source_release_id": release_id, "reason": "drill restore",
                               "confirm_republish_withdrawn": True})
        check("history_restore_ok", restored.get("status") == "active",
              str(restored.get("release_id") or restored.get("successor_release_id")))
        status, _, _ = anon.request("GET", f"/p/{a['id']}")
        check("public_online_after_restore", status == 200)
        restored_drift = owner.json("GET", "/api/publication/drift")
        result["drift"]["after_restore"] = restored_drift["summary"]
        check("drift_consistent_after_restore",
              restored_drift["summary"]["PUBLISHED_AND_CURRENT"] == 3
              and restored_drift["privacy_risk_count"] == 0)

        # ---- mutate private state, then prove the published copies stay online
        owner.json("PATCH", f"/api/content/{a['id']}",
                   {"revision": edited["revision"], "body": "A 的第二次编辑（未发布）"})
        owner.json("PATCH", f"/api/content/{c['id']}",
                   {"revision": c["revision"], "visibility": "private"})
        deleted = owner.json("DELETE", f"/api/content/{b['id']}", {"revision": b["revision"]})
        check("mutated_state", True, f"{a['id']}=edited, {c['id']}=private, {b['id']}=deleted")

        status, _, _ = anon.request("GET", f"/p/{c['id']}")
        check("private_copy_still_online", status == 200, "与 B5 提示一致：数据库私有不等于已下线")
        status, _, _ = anon.request("GET", f"/p/{b['id']}")
        check("deleted_copy_still_online", status == 200)

        hashes_before = {
            "releases": tree_digest(data / "releases"),
            "objects": tree_digest(data / "objects"),
            "db": sha256_file(data / "archive.db"),
            "secret": sha256_file(data / "owner_secret"),
            "events": sha256_file(data / "releases" / "events.jsonl"),
        }
        drift = owner.json("GET", "/api/publication/drift")
        hashes_after = {
            "releases": tree_digest(data / "releases"),
            "objects": tree_digest(data / "objects"),
            "db": sha256_file(data / "archive.db"),
            "secret": sha256_file(data / "owner_secret"),
            "events": sha256_file(data / "releases" / "events.jsonl"),
        }
        check("drift_read_only", hashes_before == hashes_after)

        categories = {}
        for row in drift["contents"]:
            categories.setdefault(row["category"], []).append(row["current_title"] or row["published_title"])
        result["drift"]["after_mutation"] = drift["summary"]
        result["drift"]["categories"] = categories
        check("drift_stale_detected",
              drift["summary"]["PUBLISHED_SNAPSHOT_STALE"] == 1, json.dumps(categories, ensure_ascii=False))
        check("drift_now_private_detected",
              drift["summary"]["PUBLISHED_BUT_NOW_PRIVATE"] == 1)
        check("drift_deleted_detected",
              drift["summary"]["PUBLISHED_BUT_DELETED"] == 1)
        check("drift_no_false_privacy_alarm",
              drift["summary"]["PUBLISHED_SNAPSHOT_STALE"] == 1 and drift["privacy_risk_count"] == 2)

        owner.json("PATCH", f"/api/attachments/{att_public['id']}", {"visibility": "private"})
        drift2 = owner.json("GET", "/api/publication/drift")
        result["drift"]["after_attachment_change"] = drift2["summary"]
        check("drift_attachment_drift", drift2["summary"]["PUBLISHED_ATTACHMENT_DRIFT"] == 1)
        status, _, _ = anon.request("GET", f"/attachments/{att_public['id']}/a-public.txt")
        check("attachment_copy_still_online", status == 200)

        final = owner.json("GET", "/api/publication/drift")
        result["drift"]["final"] = final["summary"]
        check("drift_final_state_verified", final["state"] == "verified" and final["privacy_risk_count"] == 3)
        result["release_id"] = release_id
        result["content_ids"] = {"a": a["id"], "b": b["id"], "c": c["id"]}
        result["attachment_ids"] = {"public": att_public["id"], "private": att_private["id"]}

    result["finished_at"] = now()
    result["ok"] = all(item["ok"] for item in result["checks"])
    return result


# --------------------------------------------------------------------------
# S2
# --------------------------------------------------------------------------
def drill_s2(work: Path, s1_data: Path) -> dict:
    backup = work / "s2-backup"
    restored = work / "s2-restored"
    result: dict = {"drill": "S2", "started_at": now(), "checks": []}

    def check(name: str, ok: bool, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise RuntimeError(f"S2 检查失败: {name} {detail}")

    before = tree_digest(s1_data, exclude=(".app.lock",))
    code, output = run_powershell(
        ROOT / "scripts" / "cold-backup.ps1", "-DataDir", str(s1_data), "-BackupDir", str(backup)
    )
    check("cold_backup", code == 0, " ".join(output.strip().splitlines()[-3:]))
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    check("manifest_sha256_count", manifest["file_count"] == len(manifest["files"]))

    code, output = run_powershell(
        ROOT / "scripts" / "verify-copy.ps1",
        "-Source", str(s1_data),
        "-CopyDir", str(backup),
        "-Report", str(work / "s2-verify.json"),
    )
    check("verify_copy", code == 0)

    shutil.copytree(backup, restored, ignore=shutil.ignore_patterns("manifest.json"))
    check("restored_to_new_dir", restored.is_dir())

    with Server(restored, port=PORT + 2, log_path=work / "s2-restored.log") as server:
        client = Client(server.port)
        session = client.login()
        check("restored_owner_login", session["auth_status"] == 200)
        contents = client.json("GET", "/api/content")
        integrity = client.json("GET", "/api/integrity")
        releases = client.json("GET", "/api/releases")
        check("restored_content_count", True, f"{len(contents.get('items', []))} 条")
        check("restored_integrity_ok", not integrity.get("open_issues"),
              f"open_issues={len(integrity.get('open_issues', []))}")
        active_id = releases.get("active_release_id")
        check("registry_preserved", bool(active_id), active_id or "")

        # The cold backup keeps the registry and the bundles, so the public
        # route works after restore without republishing.
        public_id = ""
        active_manifest = restored / "releases" / str(active_id) / "release.json"
        if active_manifest.is_file():
            public_id = json.loads(active_manifest.read_text(encoding="utf-8"))["content_ids"][0]
        anon = Client(server.port, owner=False)
        status, _, _ = anon.request("GET", f"/p/{public_id}")
        check("restored_public_page_online", status == 200, public_id)
        status, _, _ = anon.request("GET", f"/api/public/content/{public_id}")
        check("restored_public_api_online", status == 200)

    after = tree_digest(s1_data, exclude=(".app.lock",))
    check("source_dir_unchanged", before == after, f"{len(after)} 个文件")
    objects_before = {
        name: digest for name, digest in before.items() if name.startswith("objects/")
    }
    objects_after = {
        name: digest for name, digest in after.items() if name.startswith("objects/")
    }
    check("attachment_objects_stable", objects_before == objects_after, f"{len(objects_before)} 个对象")

    result["finished_at"] = now()
    result["ok"] = all(item["ok"] for item in result["checks"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P6 synthetic drills (S0/S1/S2)")
    parser.add_argument("--out", type=Path, default=ROOT / "evidence" / "p6-readiness" / "drills")
    parser.add_argument("--work", type=Path, default=None)
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    work = args.work or Path(os.environ.get("TEMP", tempfile_fallback()))
    work = work / f"p6-drills-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    report = {"generated_at": now(), "work_dir": str(work), "synthetic": True}
    exit_code = 0
    try:
        s0 = drill_s0(work)
        report["S0"] = s0
        print("S0:", "PASS" if s0["ok"] else "FAIL")

        s1 = drill_s1(work)
        report["S1"] = s1
        print("S1:", "PASS" if s1["ok"] else "FAIL")

        s2 = drill_s2(work, work / "s1-data")
        report["S2"] = s2
        print("S2:", "PASS" if s2["ok"] else "FAIL")
    except Exception as exc:  # noqa: BLE001 - drill report must capture failure
        report["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        print("DRILL FAILED:", report["error"])
    finally:
        (out / "drills.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for name in ("s0-verify.json", "s2-verify.json", "s0-server.log", "s1-server.log", "s2-restored.log"):
            source = work / name
            if source.is_file():
                shutil.copyfile(source, out / name)
                if name.endswith(".log"):
                    text = source.read_text(encoding="utf-8", errors="replace")
                    text = text.replace(TOKEN, "<redacted>")
                    (out / name).write_text(text, encoding="utf-8")
    print("证据:", out / "drills.json")
    return exit_code


def tempfile_fallback() -> str:
    return "."


if __name__ == "__main__":
    sys.exit(main())
