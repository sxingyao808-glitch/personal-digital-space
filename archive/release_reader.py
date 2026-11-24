"""Public read-only resolver for the active Release Bundle (P2).

Every public route resolves the registry pointer and reads *only* the active
bundle. This module deliberately never opens the private database
(``archive.db``), never reads ``objects/`` and never falls back to the live
store when the active bundle is missing or corrupt.

Failure semantics:

* no active release           -> :class:`ActiveReleaseNotFound` (404)
* content/asset not in bundle -> :class:`ActiveReleaseNotFound` (404)
* registry/bundle corrupt     -> :class:`ActiveReleaseUnavailable` (503)

The registry is re-read whenever ``index.json`` changes (mtime/size). Bundle
file hashes are verified via a stat-signature cache: a file is re-hashed only
when its size or mtime changed. This keeps per-request cost bounded while still
detecting tampering.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .release import (
    RELEASE_FORMAT,
    RELEASE_FORMAT_VERSION,
    ReleaseError,
    ReleaseRegistry,
    is_public_bundle_path,
)
from .store import ArchiveError


class ActiveReleaseUnavailable(ArchiveError):
    status = 503
    code = "active_release_unavailable"


class ActiveReleaseNotFound(ArchiveError):
    status = 404
    code = "public_not_found"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ActiveSnapshot:
    """Verified, immutable view of the currently active bundle."""

    def __init__(self, release_id: str, root: Path, manifest: dict[str, Any]) -> None:
        self.release_id = release_id
        self.root = root
        self.manifest = manifest
        self.declared: dict[str, dict[str, Any]] = {
            entry["path"]: entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        self.items: dict[str, dict[str, Any]] = {
            item["content_id"]: item
            for item in manifest.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("content_id"), str)
        }
        self.by_public_id: dict[str, dict[str, Any]] = {
            item["public_id"]: item
            for item in self.items.values()
            if isinstance(item.get("public_id"), str)
        }
        self.attachments: dict[str, dict[str, Any]] = {}
        self.attachment_paths: dict[str, dict[str, Any]] = {}
        self.attachments_by_public_id: dict[str, dict[str, Any]] = {}
        for item in self.items.values():
            for attachment in item.get("attachments", []):
                self.attachments[attachment["attachment_id"]] = attachment
                self.attachment_paths[attachment["path"]] = attachment
                if attachment.get("public_id"):
                    self.attachments_by_public_id[attachment["public_id"]] = attachment


class ActiveReleaseReader:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.registry = ReleaseRegistry(self.data_dir)
        self._cache_key: tuple[int, int] | None | object = object()
        self._snapshot: ActiveSnapshot | None = None
        self._file_signatures: dict[tuple[str, str], tuple[int, int]] = {}

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def _index_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.registry.index_path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def snapshot(self) -> ActiveSnapshot | None:
        if self.registry.withdrawal_pending():
            raise ActiveReleaseUnavailable(
                "撤回尚未完成，公开入口已失败关闭。"
            )
        key = self._index_signature()
        if key == self._cache_key:
            return self._snapshot
        snapshot = self._load_snapshot()
        self._cache_key = key
        self._snapshot = snapshot
        self._file_signatures = {}
        return snapshot

    def _load_snapshot(self) -> ActiveSnapshot | None:
        if self.registry.withdrawal_pending():
            raise ActiveReleaseUnavailable(
                "撤回尚未完成，公开入口已失败关闭。"
            )
        if not self.registry.index_path.is_file():
            return None
        try:
            index = self.registry._load_index()
        except ReleaseError as exc:
            raise ActiveReleaseUnavailable(
                "active Release 注册表不可用。", reason=exc.message
            ) from exc
        active_id = index["active_release_id"]
        if not active_id:
            return None
        record = next(
            (item for item in index["releases"] if item["release_id"] == active_id),
            None,
        )
        if record is None or record.get("status") != "active":
            raise ActiveReleaseUnavailable(
                "active Release 指针与注册表记录不一致。", release_id=active_id
            )
        root = self.registry.releases_dir / active_id
        manifest_path = root / "release.json"
        if not manifest_path.is_file():
            raise ActiveReleaseUnavailable(
                "active Release Bundle 缺失。", release_id=active_id
            )
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActiveReleaseUnavailable(
                "active Release 清单不可解析。", release_id=active_id, reason=str(exc)
            ) from exc
        if manifest.get("format") != RELEASE_FORMAT:
            raise ActiveReleaseUnavailable(
                "active Release 清单格式不匹配。", release_id=active_id
            )
        if manifest.get("format_version") != RELEASE_FORMAT_VERSION:
            raise ActiveReleaseUnavailable(
                "active Release 不是受支持的 Bundle 版本。", release_id=active_id
            )
        if manifest.get("release_id") != active_id:
            raise ActiveReleaseUnavailable(
                "active Release 清单 ID 不一致。", release_id=active_id
            )
        if record.get("manifest_sha256") and (
            _sha256_bytes(manifest_bytes) != record["manifest_sha256"]
        ):
            raise ActiveReleaseUnavailable(
                "active Release 清单哈希与注册表不一致。", release_id=active_id
            )
        recomputed = _sha256_bytes(
            json.dumps(
                {"files": manifest.get("files", [])},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if recomputed != manifest.get("bundle_sha256"):
            raise ActiveReleaseUnavailable(
                "active Release Bundle 哈希不一致。", release_id=active_id
            )

        snapshot = ActiveSnapshot(active_id, root, manifest)
        self._check_bundle_shape(snapshot)
        return snapshot

    def _check_bundle_shape(self, snapshot: ActiveSnapshot) -> None:
        for path in snapshot.declared:
            if not is_public_bundle_path(path):
                raise ActiveReleaseUnavailable(
                    "active Bundle 包含不允许的公开路径。",
                    path=path,
                    release_id=snapshot.release_id,
                )
        for content_id, item in snapshot.items.items():
            if item.get("payload_path") not in snapshot.declared:
                raise ActiveReleaseUnavailable(
                    "active Bundle 缺少 Content payload。",
                    content_id=content_id,
                    release_id=snapshot.release_id,
                )
            if item.get("page_path") not in snapshot.declared:
                raise ActiveReleaseUnavailable(
                    "active Bundle 缺少 Content 页面。",
                    content_id=content_id,
                    release_id=snapshot.release_id,
                )
            if item.get("space_path") not in snapshot.declared:
                raise ActiveReleaseUnavailable(
                    "active Bundle 缺少已公开空间页面。",
                    content_id=content_id,
                    release_id=snapshot.release_id,
                )
        if "assets/style.css" not in snapshot.declared:
            raise ActiveReleaseUnavailable(
                "active Bundle 缺少公开样式。", release_id=snapshot.release_id
            )
        if "index.html" not in snapshot.declared:
            raise ActiveReleaseUnavailable(
                "active Bundle 缺少公开首页。", release_id=snapshot.release_id
            )
        actual_files = {
            path.relative_to(snapshot.root).as_posix()
            for path in snapshot.root.rglob("*")
            if path.is_file() and path.name != "release.json"
        }
        declared = set(snapshot.declared)
        if actual_files - declared:
            raise ActiveReleaseUnavailable(
                "active Bundle 包含未声明的文件。",
                release_id=snapshot.release_id,
                paths=sorted(actual_files - declared)[:5],
            )
        if declared - actual_files:
            raise ActiveReleaseUnavailable(
                "active Bundle 缺少清单声明的文件。",
                release_id=snapshot.release_id,
                paths=sorted(declared - actual_files)[:5],
            )

    # ------------------------------------------------------------------
    # Verified file access
    # ------------------------------------------------------------------
    def _resolve_path(self, snapshot: ActiveSnapshot, relative: str) -> Path:
        if not is_public_bundle_path(relative):
            raise ActiveReleaseUnavailable("路径不在公开范围内。", path=relative)
        parts = Path(relative).parts
        if ".." in parts or relative.startswith("/"):
            raise ActiveReleaseUnavailable("路径包含目录穿越。", path=relative)
        current = snapshot.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ActiveReleaseUnavailable("Bundle 中不允许符号链接。", path=relative)
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise ActiveReleaseUnavailable(
                "active Bundle 文件缺失。", path=relative, reason=str(exc)
            ) from exc
        if not resolved.is_relative_to(snapshot.root):
            raise ActiveReleaseUnavailable("路径逃逸出 Bundle。", path=relative)
        return resolved

    def _verified_file(self, snapshot: ActiveSnapshot, relative: str) -> Path:
        entry = snapshot.declared.get(relative)
        if entry is None:
            raise ActiveReleaseNotFound("公开资源不在 active Release 中。", path=relative)
        path = self._resolve_path(snapshot, relative)
        stat = path.stat()
        signature = (snapshot.release_id, relative)
        if self._file_signatures.get(signature) == (stat.st_size, stat.st_mtime_ns):
            return path
        if stat.st_size != int(entry.get("bytes", -1)):
            raise ActiveReleaseUnavailable(
                "active Bundle 文件大小与清单不一致。", path=relative
            )
        if _sha256_file(path) != entry.get("sha256"):
            raise ActiveReleaseUnavailable(
                "active Bundle 文件哈希与清单不一致。", path=relative
            )
        self._file_signatures[signature] = (stat.st_size, stat.st_mtime_ns)
        return path

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------
    def bundle_file(self, relative: str) -> Path:
        """Any declared public bundle file (home, content page, space page)."""
        if not is_public_bundle_path(relative):
            raise ActiveReleaseNotFound("请求的公开路径不在允许范围内。", path=relative)
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        return self._verified_file(snapshot, relative)

    def public_index(self) -> dict[str, Any]:
        """Listing of the active bundle, used by the anonymous entry page.

        Only manifest metadata is used; the private database is never opened.
        """
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        items = [
            {
                "id": item.get("public_id"),
                "title": item.get("title") or "未命名",
                "summary": item.get("summary") or "",
                "type": item.get("content_type") or "",
                "primary_space": item.get("primary_space") or "",
                "updated_at": item.get("updated_at") or "",
            }
            for item in snapshot.items.values()
            if item.get("public_id")
        ]
        items.sort(key=lambda entry: (entry["updated_at"], entry["id"]), reverse=True)
        return {"release_id": snapshot.release_id, "entries": items}

    @staticmethod
    def _resolve_item(snapshot: ActiveSnapshot, identifier: str) -> dict[str, Any] | None:
        item = snapshot.by_public_id.get(identifier)
        if item is not None:
            return item
        # Compatibility for links from pre-v2 bundles/tests; never echoed in
        # public output.
        return snapshot.items.get(identifier)

    def content_page_path(self, content_id: str) -> Path:
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        item = self._resolve_item(snapshot, content_id)
        if item is None:
            raise ActiveReleaseNotFound("该内容不在当前公开版本中。", content_id=content_id)
        return self._verified_file(snapshot, item["page_path"])

    def content_payload(self, content_id: str) -> dict[str, Any]:
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        item = self._resolve_item(snapshot, content_id)
        if item is None:
            raise ActiveReleaseNotFound("该内容不在当前公开版本中。", content_id=content_id)
        path = self._verified_file(snapshot, item["payload_path"])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActiveReleaseUnavailable(
                "公开 payload 不可解析。", content_id=content_id, reason=str(exc)
            ) from exc
        expected_id = item.get("public_id") or item["content_id"]
        if not isinstance(payload, dict) or payload.get("id") != expected_id:
            raise ActiveReleaseUnavailable(
                "公开 payload 内容不一致。", content_id=content_id
            )
        return payload

    def asset_path(self, relative: str) -> Path:
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        if not relative.startswith("assets/"):
            raise ActiveReleaseNotFound("资源不在公开范围内。", path=relative)
        return self._verified_file(snapshot, relative)

    def attachment_path(
        self, attachment_id: str, filename: str | None = None
    ) -> tuple[Path, dict[str, Any]]:
        snapshot = self.snapshot()
        if snapshot is None:
            raise ActiveReleaseNotFound("当前没有公开版本。")
        attachment = snapshot.attachments_by_public_id.get(
            attachment_id
        ) or snapshot.attachments.get(attachment_id)
        if attachment is None:
            raise ActiveReleaseNotFound(
                "附件不在当前公开版本中。", attachment_id=attachment_id
            )
        if filename is not None and attachment["filename"] != filename:
            raise ActiveReleaseNotFound(
                "附件路径与当前公开版本不一致。", attachment_id=attachment_id
            )
        path = self._verified_file(snapshot, attachment["path"])
        return path, attachment
