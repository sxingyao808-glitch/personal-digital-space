"""Versioned archive export package.

The export is a read-only projection of the local archive: a ZIP containing
self-describing JSON datasets plus the verified attachment bytes. It never
changes visibility, never publishes content, and never claims success when
data could not be represented. Problems are reported per item.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .db import SCHEMA_VERSION, connect, utcnow
from .package_io import publish_new_file, verify_written_zip
from .store import ArchiveError, NotFoundError, ValidationError

EXPORT_FORMAT = "personal-archive-export"
EXPORT_FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.zip$")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


class ExportError(ArchiveError):
    status = 500
    code = "export_failed"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _issue(code: str, severity: str, target: str, detail: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "target": target, "detail": detail}


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")


class ArchiveExporter:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "archive.db"
        self.objects = self.data_dir / "objects"
        self.staging = self.data_dir / "staging"
        self.quarantine = self.data_dir / "quarantine"
        self.exports_dir = self.data_dir / "exports"

    # ------------------------------------------------------------------
    # Snapshot (single consistent read; never writes)
    # ------------------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN")
            content = [dict(r) for r in conn.execute("SELECT * FROM content ORDER BY created_at, id")]
            relations = [
                dict(r) for r in conn.execute("SELECT * FROM relations ORDER BY created_at, id")
            ]
            attributes = [
                dict(r) for r in conn.execute("SELECT * FROM attributes ORDER BY created_at, id")
            ]
            attachments = [
                dict(r) for r in conn.execute("SELECT * FROM attachments ORDER BY created_at, id")
            ]
            integrity = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM integrity_issues WHERE state = 'open' ORDER BY first_seen, fingerprint"
                )
            ]
            content_spaces = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM content_spaces ORDER BY content_id, space"
                )
            ]
            conn.rollback()
        finally:
            conn.close()
        return {
            "content": content,
            "relations": relations,
            "attributes": attributes,
            "attachments": attachments,
            "integrity_issues": integrity,
            "content_spaces": content_spaces,
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export(self, output_path: str | Path | None = None) -> dict[str, Any]:
        generated_at = utcnow()
        snapshot = self._snapshot()
        issues: list[dict[str, Any]] = []

        content = self._content_records(snapshot["content"], snapshot["content_spaces"])
        content_state = {row["id"]: row["state"] for row in content}
        relations = self._relation_records(snapshot["relations"], content_state, issues)
        attributes = [dict(row, confirmed=bool(row["confirmed"])) for row in snapshot["attributes"]]
        attachments, attachment_files = self._attachment_records(
            snapshot["attachments"], issues
        )

        unreferenced, quarantine_files, staging_files = self._inspect_storage(
            {row["id"] for row in snapshot["attachments"]}, issues
        )

        counts = {
            "content_total": len(content),
            "content_active": sum(1 for row in content if row["state"] == "active"),
            "content_deleted": sum(1 for row in content if row["state"] == "deleted"),
            "content_public": sum(1 for row in content if row["public"]),
            "content_private": sum(1 for row in content if not row["public"]),
            "relations_total": len(relations),
            "relations_intact": sum(1 for row in relations if row["intact"]),
            "relations_with_missing_endpoint": sum(1 for row in relations if not row["intact"]),
            "attributes_total": len(attributes),
            "attachments_total": len(attachments),
            "attachments_included": sum(1 for row in attachments if row["included"]),
            "attachment_bytes_exported": sum(
                row["exported_bytes"] or 0 for row in attachments if row["included"]
            ),
            "open_integrity_issues": len(snapshot["integrity_issues"]),
            "unreferenced_object_files": len(unreferenced),
            "staging_files": len(staging_files),
            "quarantine_files": len(quarantine_files),
            "issues_total": len(issues),
            "issues_error": sum(1 for row in issues if row["severity"] == SEVERITY_ERROR),
            "issues_warning": sum(1 for row in issues if row["severity"] == SEVERITY_WARNING),
        }
        complete = counts["issues_error"] == 0

        datasets = {
            "content.json": self._dataset(
                "content", generated_at, content
            ),
            "relations.json": self._dataset(
                "relations", generated_at, relations
            ),
            "attributes.json": self._dataset(
                "attributes", generated_at, attributes
            ),
            "attachments.json": self._dataset(
                "attachments", generated_at, attachments
            ),
            "integrity_issues.json": self._dataset(
                "integrity_issues", generated_at, snapshot["integrity_issues"]
            ),
        }
        dataset_bytes = {name: _json_bytes(payload) for name, payload in datasets.items()}

        files: list[dict[str, Any]] = []
        for name, data in dataset_bytes.items():
            files.append(
                {
                    "path": name,
                    "kind": "dataset",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        for entry in attachment_files:
            files.append(
                {
                    "path": entry["path"],
                    "kind": "attachment",
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                }
            )

        manifest = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "app_version": __version__,
            "generated_at": generated_at,
            "counts": counts,
            "completeness": {
                "complete": complete,
                "status": "complete" if complete else "partial",
                "note": (
                    "complete=true 表示所有已记录元素均已完整导出。"
                    "complete=false 表示导出包已生成，但有元素无法完整表达，详见 issues。"
                ),
            },
            "boundaries": {
                "scope": "owner-local-archive",
                "includes_private_content": True,
                "publishes_content": False,
                "changes_visibility": False,
                "includes_in_flight_staging_bytes": False,
            },
            "issues": issues,
            "files": files,
            "attachments": [
                {
                    "id": row["id"],
                    "content_id": row["content_id"],
                    "state": row["state"],
                    "path": row["export_path"],
                    "included": row["included"],
                    "verification": row["verification"],
                }
                for row in attachments
            ],
            "unreferenced_objects": unreferenced,
            "staging_inventory": staging_files,
            "quarantine_inventory": quarantine_files,
            "integrity_issues_open": snapshot["integrity_issues"],
        }

        target = self._output_path(output_path)
        package_bytes, package_sha = self._write_package(
            target, dataset_bytes, manifest, attachment_files
        )

        return {
            "filename": target.name,
            "path": str(target),
            "bytes": package_bytes,
            "sha256": package_sha,
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "complete": complete,
            "status": "complete" if complete else "partial",
            "counts": counts,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Record shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _dataset(name: str, generated_at: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "dataset": name,
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "count": len(items),
            "items": items,
        }

    @staticmethod
    def _content_records(
        rows: list[dict[str, Any]], space_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        spaces: dict[str, list[str]] = {}
        for row in space_rows:
            spaces.setdefault(row["content_id"], []).append(row["space"])
        records = []
        for row in rows:
            selected = bool(row["visibility_selected"])
            records.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "body": row["body"],
                    "summary": row["summary"],
                    "type": row["type"],
                    "primary_space": row["primary_space"],
                    "lifecycle": row["lifecycle"],
                    "related_spaces": spaces.get(row["id"], []),
                    "visibility": row["visibility"],
                    "visibility_selected": selected,
                    "public": row["state"] == "active"
                    and selected
                    and row["visibility"] == "public",
                    "revision": row["revision"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "deleted_at": row["deleted_at"],
                }
            )
        return records

    @staticmethod
    def _relation_records(
        rows: list[dict[str, Any]],
        content_state: dict[str, str],
        issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = []
        for row in rows:
            from_exists = row["from_id"] in content_state
            to_exists = row["to_id"] in content_state
            intact = from_exists and to_exists
            if not intact:
                issues.append(
                    _issue(
                        "relation_endpoint_missing",
                        SEVERITY_ERROR,
                        row["id"],
                        "关系端点不存在，关系无法完整表达。",
                    )
                )
            records.append(
                {
                    "id": row["id"],
                    "from_id": row["from_id"],
                    "to_id": row["to_id"],
                    "type": row["type"],
                    "direction": "from->to",
                    "from_exists": from_exists,
                    "to_exists": to_exists,
                    "from_state": content_state.get(row["from_id"]),
                    "to_state": content_state.get(row["to_id"]),
                    "intact": intact,
                    "created_at": row["created_at"],
                }
            )
        return records

    def _attachment_records(
        self, rows: list[dict[str, Any]], issues: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for row in rows:
            record = {
                "id": row["id"],
                "content_id": row["content_id"],
                "filename": row["filename"],
                "mime": row["mime"],
                "visibility": row["visibility"],
                "state": row["state"],
                "byte_size": row["byte_size"],
                "sha256": row["sha256"],
                "operation_id": row["operation_id"],
                "note": row["note"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "export_path": None,
                "exported_bytes": None,
                "exported_sha256": None,
                "included": False,
                "verification": "not_exported",
            }
            src = self.objects / row["id"]
            if row["state"] != "READY":
                issues.append(
                    _issue(
                        "attachment_not_ready",
                        SEVERITY_ERROR,
                        row["id"],
                        f"附件状态为 {row['state']}，没有可导出的已完成文件。",
                    )
                )
            elif not src.is_file():
                issues.append(
                    _issue(
                        "attachment_file_missing",
                        SEVERITY_ERROR,
                        row["id"],
                        "附件元数据存在，但对象文件缺失，无法导出内容。",
                    )
                )
            else:
                digest, size = _hash_file(src)
                record.update(
                    {
                        "export_path": f"files/attachments/{row['id']}",
                        "exported_bytes": size,
                        "exported_sha256": digest,
                        "included": True,
                    }
                )
                files.append(
                    {
                        "path": record["export_path"],
                        "source": src,
                        "bytes": size,
                        "sha256": digest,
                    }
                )
                if row["sha256"] is None:
                    record["verification"] = "no_recorded_hash"
                    issues.append(
                        _issue(
                            "attachment_hash_unrecorded",
                            SEVERITY_WARNING,
                            row["id"],
                            "附件元数据没有记录 SHA-256，导出时按实际文件计算。",
                        )
                    )
                elif row["sha256"].lower() != digest:
                    record["verification"] = "sha256_mismatch"
                    issues.append(
                        _issue(
                            "attachment_hash_mismatch",
                            SEVERITY_ERROR,
                            row["id"],
                            "对象文件 SHA-256 与元数据不一致，导出内容可能已损坏。",
                        )
                    )
                else:
                    record["verification"] = "match"
                if row["byte_size"] is not None and row["byte_size"] != size:
                    issues.append(
                        _issue(
                            "attachment_size_mismatch",
                            SEVERITY_ERROR,
                            row["id"],
                            f"对象文件大小 {size} 与记录的 {row['byte_size']} 不一致。",
                        )
                    )
            records.append(record)
        return records, files

    def _inspect_storage(
        self, known_ids: set[str], issues: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        unreferenced: list[dict[str, Any]] = []
        if self.objects.is_dir():
            for path in sorted(self.objects.iterdir()):
                if not path.is_file() or path.name in known_ids:
                    continue
                digest, size = _hash_file(path)
                unreferenced.append({"name": path.name, "bytes": size, "sha256": digest})
                issues.append(
                    _issue(
                        "unreferenced_object_file",
                        SEVERITY_WARNING,
                        path.name,
                        "对象目录中存在没有附件元数据的文件，未纳入导出包。",
                    )
                )

        staging_files = []
        if self.staging.is_dir():
            staging_files = [
                {"name": path.name, "bytes": path.stat().st_size}
                for path in sorted(self.staging.iterdir())
                if path.is_file()
            ]
            if staging_files:
                issues.append(
                    _issue(
                        "staging_files_present",
                        SEVERITY_WARNING,
                        "staging",
                        "存在未完成的暂存文件，它们不属于已提交归档，未纳入导出包。",
                    )
                )

        quarantine_files = []
        if self.quarantine.is_dir():
            for path in sorted(self.quarantine.iterdir()):
                if not path.is_file():
                    continue
                digest, size = _hash_file(path)
                quarantine_files.append(
                    {"name": path.name, "bytes": size, "sha256": digest}
                )
            if quarantine_files:
                issues.append(
                    _issue(
                        "quarantine_files_present",
                        SEVERITY_WARNING,
                        "quarantine",
                        "隔离区存在文件，它们未通过核对，未纳入导出包。",
                    )
                )
        return unreferenced, quarantine_files, staging_files

    # ------------------------------------------------------------------
    # Package writing
    # ------------------------------------------------------------------
    def _output_path(self, output_path: str | Path | None) -> Path:
        if output_path is None:
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return self.exports_dir / f"archive-export-{stamp}-{uuid.uuid4().hex[:8]}.zip"

        target = Path(output_path).expanduser()
        if not str(target).strip():
            raise ValidationError("输出路径不能为空。", field="output_path")
        if target.exists():
            raise ValidationError("输出路径已存在，未覆盖已有文件。", field="output_path")
        parent = target.parent
        if not parent.is_dir():
            raise ValidationError("输出路径的上级目录不存在。", field="output_path")
        if target.suffix.lower() != ".zip":
            raise ValidationError("输出文件必须使用 .zip 扩展名。", field="output_path")
        return target

    def _write_package(
        self,
        target: Path,
        datasets: dict[str, bytes],
        manifest: dict[str, Any],
        attachment_files: list[dict[str, Any]],
    ) -> tuple[int, str]:
        tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(
                tmp_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for name, data in datasets.items():
                    archive.writestr(name, data)
                archive.writestr("manifest.json", _json_bytes(manifest))
                for entry in attachment_files:
                    archive.write(entry["source"], entry["path"])
            verify_written_zip(tmp_path, _json_bytes(manifest), manifest["files"])
            digest, size = _hash_file(tmp_path)
            publish_new_file(tmp_path, target)
        except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
            raise ExportError(
                "导出包写入失败。",
                output_path=str(target),
                reason=str(exc),
            ) from exc
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        return size, digest

    # ------------------------------------------------------------------
    # Download resolution (owner-only route helper)
    # ------------------------------------------------------------------
    def resolve_download(self, filename: str) -> Path:
        if not DOWNLOAD_PATTERN.match(filename or ""):
            raise NotFoundError("导出文件不存在。")
        path = (self.exports_dir / filename).resolve()
        if path.parent != self.exports_dir.resolve() or not path.is_file():
            raise NotFoundError("导出文件不存在。")
        return path
