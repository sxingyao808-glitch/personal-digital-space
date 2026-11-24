"""Whole-archive backup and restore.

A backup is a restorable point-in-time copy of the archive: the SQLite
database captured through SQLite's own backup API plus every READY attachment
object referenced by that snapshot. It is not an export projection, and a
successful backup is not proof that restore works until a restore from the
actual artifact has been audited.

Restore always writes to a new target directory. It never modifies the source
archive, never overwrites an existing directory, and publishes the restored
directory only after every package hash, database check and identity check
passes. Failures leave the source untouched and remove the staging copy.

A restored directory may be published with non-READY attachment metadata. That
outcome is reported as ``published_partial`` and never as ``verified``. Files
excluded from the backup scope are disclosed through the manifest's existing
``excluded`` inventory; they are never described as restored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .db import SCHEMA_VERSION, connect, missing_schema_tables, utcnow
from .win_data import make_data_dir
from .package_io import publish_new_file, verify_written_zip
from .store import ArchiveError, ValidationError

BACKUP_FORMAT = "personal-archive-backup"
BACKUP_FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


class BackupError(ArchiveError):
    status = 500
    code = "backup_failed"


class RestoreError(ArchiveError):
    status = 500
    code = "restore_failed"


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


def _hash_member(archive: zipfile.ZipFile, name: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name) as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _issue(code: str, severity: str, target: str, detail: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "target": target, "detail": detail}


def _schema_version_from_meta(meta: dict[str, Any]) -> int | None:
    value = meta.get("schema_version")
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fault(name: str) -> None:
    """Test-only interruption points. Never active unless explicitly enabled."""
    if os.environ.get("ARCHIVE_TEST_MODE") != "1":
        return
    if os.environ.get("ARCHIVE_FAULT_INJECT") != name:
        return
    if name in ("backup_after_snapshot", "backup_before_replace", "restore_after_extract"):
        os._exit(74)


class ArchiveBackup:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "archive.db"
        self.objects = self.data_dir / "objects"
        self.staging = self.data_dir / "staging"
        self.quarantine = self.data_dir / "quarantine"
        self.backups_dir = self.data_dir / "backups"

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------
    def create(self, output_path: str | Path | None = None) -> dict[str, Any]:
        if not self.db_path.is_file():
            raise ValidationError("归档数据库不存在，无法备份。", field="data_dir")

        target = self._output_path(output_path)
        started_at = utcnow()
        snapshot_dir = Path(tempfile.mkdtemp(prefix="archive-backup-"))
        try:
            snapshot_db = snapshot_dir / "archive.db"
            self._snapshot_database(snapshot_db)
            _fault("backup_after_snapshot")

            snapshot = self._read_snapshot(snapshot_db)
            errors: list[dict[str, Any]] = []
            warnings: list[dict[str, Any]] = []

            database_version = _schema_version_from_meta(snapshot["meta"])
            if database_version != SCHEMA_VERSION:
                raise BackupError(
                    "备份未完成：数据库 schema 版本与当前实现不兼容，未生成备份包。",
                    issues=[
                        _issue(
                            "backup_schema_version_mismatch",
                            SEVERITY_ERROR,
                            "archive.db",
                            "数据库 schema 版本为 {}，当前支持版本为 {}。".format(
                                database_version, SCHEMA_VERSION
                            ),
                        )
                    ],
                )

            if snapshot["integrity_check"] != "ok":
                errors.append(
                    _issue(
                        "database_integrity_check_failed",
                        SEVERITY_ERROR,
                        "archive.db",
                        f"SQLite integrity_check 返回：{snapshot['integrity_check']}",
                    )
                )
            for row in snapshot["foreign_key_violations"]:
                warnings.append(
                    _issue(
                        "relation_endpoint_missing",
                        SEVERITY_WARNING,
                        row.get("rowid", "relations"),
                        "数据库存在外键不一致（关系端点缺失），备份按原样保留并在恢复审计中报告。",
                    )
                )
            for row in snapshot["integrity_issues"]:
                warnings.append(
                    _issue(
                        "open_integrity_issue",
                        SEVERITY_WARNING,
                        row["target"],
                        f"归档存在未解决的完整性问题：{row['kind']}",
                    )
                )

            object_files, attachment_errors, attachment_warnings = self._collect_objects(
                snapshot["attachments"]
            )
            errors.extend(attachment_errors)
            warnings.extend(attachment_warnings)

            excluded, storage_warnings = self._inspect_uncommitted_storage(
                {row["id"] for row in snapshot["attachments"]}
            )
            warnings.extend(storage_warnings)

            if errors:
                raise BackupError(
                    "备份未完成：归档中存在无法完整备份的数据，未生成任何备份包。",
                    issues=errors,
                )

            db_digest, db_size = _hash_file(snapshot_db)
            files = [
                {
                    "path": "archive.db",
                    "kind": "database",
                    "bytes": db_size,
                    "sha256": db_digest,
                }
            ]
            for entry in object_files:
                files.append(
                    {
                        "path": entry["path"],
                        "kind": "attachment",
                        "bytes": entry["bytes"],
                        "sha256": entry["sha256"],
                    }
                )

            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "app_version": __version__,
                "generated_at": started_at,
                "source_name": self.data_dir.name,
                "snapshot": {
                    "method": "sqlite-backup-api",
                    "started_at": started_at,
                    "finished_at": utcnow(),
                },
                "database": {
                    "path": "archive.db",
                    "bytes": db_size,
                    "sha256": db_digest,
                    "integrity_check": snapshot["integrity_check"],
                    "meta": snapshot["meta"],
                },
                "counts": snapshot["counts"],
                "identity": snapshot["identity"],
                "attachments": snapshot["attachments"],
                "objects": [
                    {
                        "id": entry["id"],
                        "path": entry["path"],
                        "content_id": entry["content_id"],
                        "state": entry["state"],
                        "bytes": entry["bytes"],
                        "sha256": entry["sha256"],
                    }
                    for entry in object_files
                ],
                "integrity_issues": snapshot["integrity_issues"],
                "warnings": warnings,
                "excluded": excluded,
                "boundaries": {
                    "scope": "owner-local-archive",
                    "includes_private_content": True,
                    "publishes_content": False,
                    "changes_visibility": False,
                    "overwrites_source": False,
                    "includes_in_flight_staging": False,
                    "includes_session_secret": False,
                },
                "files": files,
            }

            package_bytes, package_sha = self._write_package(
                target, snapshot_db, object_files, manifest
            )
        finally:
            for path in sorted(snapshot_dir.glob("*"), reverse=True):
                if path.is_file():
                    path.unlink(missing_ok=True)
            snapshot_dir.rmdir()

        return {
            "filename": target.name,
            "path": str(target),
            "bytes": package_bytes,
            "sha256": package_sha,
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": started_at,
            "complete": True,
            "status": "complete",
            "counts": manifest["counts"],
            "warnings": manifest["warnings"],
        }

    def _snapshot_database(self, destination: Path) -> None:
        source = connect(self.db_path)
        try:
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
        except (sqlite3.Error, OSError) as exc:
            raise BackupError("无法获取数据库一致性快照。", reason=str(exc)) from exc
        finally:
            source.close()

    def _read_snapshot(self, snapshot_db: Path) -> dict[str, Any]:
        conn = connect(snapshot_db)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            violations = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
            content = [dict(row) for row in conn.execute("SELECT * FROM content ORDER BY id")]
            relations = [dict(row) for row in conn.execute("SELECT * FROM relations ORDER BY id")]
            attributes = [dict(row) for row in conn.execute("SELECT * FROM attributes ORDER BY id")]
            attachments = [dict(row) for row in conn.execute("SELECT * FROM attachments ORDER BY id")]
            integrity_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM integrity_issues WHERE state = 'open' ORDER BY fingerprint"
                )
            ]
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta")}
        except sqlite3.Error as exc:
            raise BackupError("无法读取数据库快照。", reason=str(exc)) from exc
        finally:
            conn.close()

        counts = {
            "content_total": len(content),
            "content_active": sum(1 for row in content if row["state"] == "active"),
            "content_deleted": sum(1 for row in content if row["state"] == "deleted"),
            "relations_total": len(relations),
            "attributes_total": len(attributes),
            "attachments_total": len(attachments),
            "attachments_ready": sum(1 for row in attachments if row["state"] == "READY"),
            "attachments_not_ready": sum(1 for row in attachments if row["state"] != "READY"),
            "open_integrity_issues": len(integrity_rows),
        }
        identity = {
            "content_ids": [row["id"] for row in content],
            "deleted_content_ids": [row["id"] for row in content if row["state"] == "deleted"],
            "relation_ids": [row["id"] for row in relations],
            "relation_edges": [
                {"id": row["id"], "from_id": row["from_id"], "to_id": row["to_id"], "type": row["type"]}
                for row in relations
            ],
            "attribute_ids": [row["id"] for row in attributes],
            "attribute_states": [
                {
                    "id": row["id"],
                    "content_id": row["content_id"],
                    "provenance": row["provenance"],
                    "confirmed": bool(row["confirmed"]),
                }
                for row in attributes
            ],
            "attachment_ids": [row["id"] for row in attachments],
        }
        attachment_records = [
            {
                "id": row["id"],
                "content_id": row["content_id"],
                "filename": row["filename"],
                "state": row["state"],
                "byte_size": row["byte_size"],
                "sha256": row["sha256"],
                "visibility": row["visibility"],
                "included": row["state"] == "READY",
                "path": f"objects/{row['id']}" if row["state"] == "READY" else None,
            }
            for row in attachments
        ]
        return {
            "integrity_check": integrity,
            "foreign_key_violations": violations,
            "content": content,
            "relations": relations,
            "attributes": attributes,
            "attachments": attachment_records,
            "integrity_issues": integrity_rows,
            "meta": meta,
            "counts": counts,
            "identity": identity,
        }

    def _collect_objects(
        self, attachments: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        files: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        for row in attachments:
            if row["state"] != "READY":
                warnings.append(
                    _issue(
                        "attachment_not_ready",
                        SEVERITY_WARNING,
                        row["id"],
                        f"附件状态为 {row['state']}，只备份元数据，没有可提交的对象字节。",
                    )
                )
                continue
            source = self.objects / row["id"]
            if not source.is_file():
                errors.append(
                    _issue(
                        "attachment_file_missing",
                        SEVERITY_ERROR,
                        row["id"],
                        "附件状态为 READY，但对象文件缺失；备份会丢失数据。",
                    )
                )
                continue
            digest, size = _hash_file(source)
            if row["sha256"] is None:
                warnings.append(
                    _issue(
                        "attachment_hash_unrecorded",
                        SEVERITY_WARNING,
                        row["id"],
                        "附件元数据未记录 SHA-256，备份按实际文件计算。",
                    )
                )
            elif row["sha256"].lower() != digest:
                errors.append(
                    _issue(
                        "attachment_hash_mismatch",
                        SEVERITY_ERROR,
                        row["id"],
                        "对象文件 SHA-256 与元数据不一致，备份将包含损坏数据。",
                    )
                )
                continue
            if row["byte_size"] is not None and row["byte_size"] != size:
                errors.append(
                    _issue(
                        "attachment_size_mismatch",
                        SEVERITY_ERROR,
                        row["id"],
                        f"对象文件大小 {size} 与记录的 {row['byte_size']} 不一致。",
                    )
                )
                continue
            files.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "content_id": row["content_id"],
                    "state": row["state"],
                    "source": source,
                    "bytes": size,
                    "sha256": digest,
                }
            )
        return files, errors, warnings

    def _inspect_uncommitted_storage(
        self, known_ids: set[str]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []

        staging_files = []
        if self.staging.is_dir():
            staging_files = [
                {"name": path.name, "bytes": path.stat().st_size}
                for path in sorted(self.staging.iterdir())
                if path.is_file()
            ]
            if staging_files:
                warnings.append(
                    _issue(
                        "staging_files_excluded",
                        SEVERITY_WARNING,
                        "staging",
                        "存在未提交的暂存文件，它们不属于归档快照，未纳入备份。",
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
                warnings.append(
                    _issue(
                        "quarantine_files_excluded",
                        SEVERITY_WARNING,
                        "quarantine",
                        "隔离区存在文件，它们未通过核对，未纳入备份。",
                    )
                )

        unreferenced = []
        if self.objects.is_dir():
            for path in sorted(self.objects.iterdir()):
                if not path.is_file() or path.name in known_ids:
                    continue
                digest, size = _hash_file(path)
                unreferenced.append({"name": path.name, "bytes": size, "sha256": digest})
                warnings.append(
                    _issue(
                        "unreferenced_object_file",
                        SEVERITY_WARNING,
                        path.name,
                        "对象目录存在无元数据引用的文件，未纳入备份。",
                    )
                )

        return {
            "staging": staging_files,
            "quarantine": quarantine_files,
            "unreferenced_objects": unreferenced,
            "owner_secret": True,
            "exports": True,
        }, warnings

    def _output_path(self, output_path: str | Path | None) -> Path:
        if output_path is None:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return self.backups_dir / f"archive-backup-{stamp}-{uuid.uuid4().hex[:8]}.zip"

        target = Path(output_path).expanduser().resolve()
        if not str(target).strip():
            raise ValidationError("备份路径不能为空。", field="output_path")
        if target.exists():
            raise ValidationError("备份路径已存在，未覆盖已有文件。", field="output_path")
        if not target.parent.is_dir():
            raise ValidationError("备份路径的上级目录不存在。", field="output_path")
        if target.suffix.lower() != ".zip":
            raise ValidationError("备份文件必须使用 .zip 扩展名。", field="output_path")
        return target

    def _write_package(
        self,
        target: Path,
        snapshot_db: Path,
        object_files: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> tuple[int, str]:
        tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with zipfile.ZipFile(
                tmp_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                archive.write(snapshot_db, "archive.db")
                archive.writestr("manifest.json", _json_bytes(manifest))
                for entry in object_files:
                    archive.write(entry["source"], entry["path"])
            verify_written_zip(tmp_path, _json_bytes(manifest), manifest["files"])
            _fault("backup_before_replace")
            digest, size = _hash_file(tmp_path)
            publish_new_file(tmp_path, target)
        except (ValueError, zipfile.BadZipFile, KeyError) as exc:
            raise BackupError("已写入的备份包校验失败。", output_path=str(target)) from exc
        except OSError as exc:
            raise BackupError(
                "备份包写入失败。", output_path=str(target), reason=str(exc)
            ) from exc
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        return size, digest


class ArchiveRestorer:
    def __init__(self, backup_path: str | Path) -> None:
        self.backup_path = Path(backup_path)

    def restore(self, target_dir: str | Path) -> dict[str, Any]:
        if not self.backup_path.is_file():
            raise ValidationError("备份文件不存在。", field="backup")
        if self.backup_path.suffix.lower() != ".zip":
            raise ValidationError("备份文件必须是 .zip。", field="backup")

        target = Path(target_dir).expanduser().resolve()
        if target.exists():
            raise ValidationError("目标目录已存在，恢复不会覆盖现有数据。", field="target")
        if not target.parent.is_dir():
            raise ValidationError("目标目录的上级目录不存在。", field="target")
        if target == self.backup_path.parent.resolve():
            raise ValidationError("目标目录不能与备份文件所在目录相同。", field="target")

        staging_dir = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
        make_data_dir(staging_dir)
        try:
            with zipfile.ZipFile(self.backup_path) as archive:
                manifest = self._read_manifest(archive)
                self._verify_members(archive, manifest)
                self._extract(archive, staging_dir, manifest)

            audit = audit_data_dir(staging_dir, manifest)
            if not audit["ok"]:
                raise RestoreError(
                    "恢复审计未通过，未发布目标目录；原始数据未被修改。",
                    issues=audit["errors"],
                )
            _fault("restore_after_extract")
            os.replace(staging_dir, target)
        except (zipfile.BadZipFile, OSError) as exc:
            self._cleanup(staging_dir)
            raise RestoreError(
                "恢复失败，原始数据未被修改。", backup=str(self.backup_path), reason=str(exc)
            ) from exc
        except BaseException:
            self._cleanup(staging_dir)
            raise

        package_digest, package_size = _hash_file(self.backup_path)
        final_audit = audit_data_dir(target, manifest)
        excluded = manifest.get("excluded")
        warnings = list(final_audit["warnings"])
        if isinstance(excluded, dict):
            excluded_counts = {
                key: len(value)
                for key, value in excluded.items()
                if isinstance(value, list) and value
            }
        else:
            excluded_counts = {}
        if excluded_counts:
            warnings.append(
                _issue(
                    "backup_excluded_storage_not_restored",
                    SEVERITY_WARNING,
                    "excluded",
                    "备份按既定边界未包含以下内容，恢复目录不包含这些字节：{}。清单见 excluded。".format(
                        "、".join(
                            f"{key} {count} 个" for key, count in excluded_counts.items()
                        )
                    ),
                )
            )

        attachments_not_ready = int(final_audit["checks"].get("attachments_not_ready", 0) or 0)
        audit_ok = bool(final_audit["ok"])
        if not audit_ok:
            status = "published_with_audit_errors"
        elif final_audit["checks"].get("foreign_key_violations"):
            status = "published_with_foreign_key_warnings"
        elif attachments_not_ready:
            status = "published_partial"
        else:
            status = "verified"
        verified = status == "verified"
        return {
            "restored": True,
            "target": str(target),
            "backup": str(self.backup_path),
            "backup_bytes": package_size,
            "backup_sha256": package_digest,
            "format": manifest["format"],
            "format_version": manifest["format_version"],
            "schema_version": manifest["schema_version"],
            "backup_generated_at": manifest["generated_at"],
            "verified": verified,
            "restore_fidelity": audit_ok,
            "attachments_complete": audit_ok and attachments_not_ready == 0,
            "foreign_keys_healthy": final_audit["checks"].get("foreign_key_violations") == 0,
            "status": status,
            "counts": final_audit["counts"],
            "warnings": warnings,
            "errors": final_audit["errors"],
            "checks": final_audit["checks"],
            "excluded": excluded,
        }

    def _read_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            info = archive.getinfo("manifest.json")
        except KeyError as exc:
            raise RestoreError("备份包缺少 manifest.json。") from exc
        if info.file_size > MAX_MANIFEST_BYTES:
            raise RestoreError("备份清单异常过大。")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestoreError("备份清单无法解析。") from exc
        if manifest.get("format") != BACKUP_FORMAT:
            raise RestoreError("备份格式不受支持。", format=manifest.get("format"))
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise RestoreError(
                "备份格式版本不受支持。", format_version=manifest.get("format_version")
            )
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise RestoreError(
                "备份的 schema 版本与当前实现不兼容。",
                backup_schema=manifest.get("schema_version"),
                supported_schema=SCHEMA_VERSION,
            )
        return manifest

    def _verify_members(
        self, archive: zipfile.ZipFile, manifest: dict[str, Any]
    ) -> None:
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise RestoreError("备份清单没有文件条目。")
        members = archive.namelist()
        names = set(members)
        declared = [entry.get("path") for entry in entries if isinstance(entry, dict)]
        if (len(declared) != len(entries) or any(not isinstance(path, str) for path in declared)
                or len(set(declared)) != len(declared) or len(names) != len(members)
                or names - {"manifest.json"} - set(declared)):
            raise RestoreError("备份文件清单与实际成员不一致。")
        issues: list[dict[str, Any]] = []
        for entry in entries:
            path = entry.get("path", "")
            if not self._safe_member(path):
                issues.append(
                    _issue("unsafe_member_path", SEVERITY_ERROR, str(path), "备份包含不安全的路径。")
                )
                continue
            if path not in names:
                issues.append(
                    _issue("backup_member_missing", SEVERITY_ERROR, path, "备份包缺少清单中的文件。")
                )
                continue
            digest, size = _hash_member(archive, path)
            if size != entry.get("bytes") or digest != entry.get("sha256"):
                issues.append(
                    _issue(
                        "backup_member_mismatch",
                        SEVERITY_ERROR,
                        path,
                        "备份文件的大小或 SHA-256 与清单不一致。",
                    )
                )
        if "archive.db" not in {entry.get("path") for entry in entries}:
            issues.append(
                _issue("backup_database_missing", SEVERITY_ERROR, "archive.db", "备份清单未包含数据库。")
            )
        if issues:
            raise RestoreError("备份包完整性校验未通过。", issues=issues)

    @staticmethod
    def _safe_member(path: str) -> bool:
        return path == "archive.db" or re.fullmatch(r"objects/[A-Za-z0-9][A-Za-z0-9_-]{0,79}", path) is not None

    def _extract(
        self, archive: zipfile.ZipFile, staging_dir: Path, manifest: dict[str, Any]
    ) -> None:
        for entry in manifest["files"]:
            path = entry["path"]
            destination = staging_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(path) as source, destination.open("wb") as output:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
        for folder in ("objects", "staging", "quarantine"):
            (staging_dir / folder).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cleanup(path: Path) -> None:
        if not path.exists():
            return
        for child in [item for item in path.rglob("*") if item.is_file()]:
            child.unlink(missing_ok=True)
        directories = [item for item in path.rglob("*") if item.is_dir()]
        for child in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            child.rmdir()
        path.rmdir()


def audit_data_dir(data_dir: str | Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only audit of a restored archive against its backup manifest."""
    data_dir = Path(data_dir)
    db_path = data_dir / "archive.db"
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not db_path.is_file():
        return {
            "ok": False,
            "errors": [_issue("restored_database_missing", SEVERITY_ERROR, "archive.db", "恢复目录缺少数据库。")],
            "warnings": [],
            "counts": {},
            "checks": {},
        }

    conn = connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        violations = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        content = [dict(row) for row in conn.execute("SELECT * FROM content ORDER BY id")]
        relations = [dict(row) for row in conn.execute("SELECT * FROM relations ORDER BY id")]
        attributes = [dict(row) for row in conn.execute("SELECT * FROM attributes ORDER BY id")]
        attachments = [dict(row) for row in conn.execute("SELECT * FROM attachments ORDER BY id")]
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta")}
    except sqlite3.Error as exc:
        conn.close()
        return {
            "ok": False,
            "errors": [_issue("restored_database_unreadable", SEVERITY_ERROR, "archive.db", str(exc))],
            "warnings": [],
            "counts": {},
            "checks": {},
        }
    finally:
        conn.close()

    if integrity != "ok":
        errors.append(
            _issue("database_integrity_check_failed", SEVERITY_ERROR, "archive.db", f"integrity_check：{integrity}")
        )
    missing_tables = missing_schema_tables(tables, _schema_version_from_meta(meta) or SCHEMA_VERSION)
    if missing_tables:
        errors.append(
            _issue(
                "database_tables_missing",
                SEVERITY_ERROR,
                "archive.db",
                f"缺少表：{', '.join(missing_tables)}",
            )
        )
    for row in violations:
        warnings.append(
            _issue(
                "relation_endpoint_missing",
                SEVERITY_WARNING,
                str(row.get("rowid", "relations")),
                "恢复后的数据库存在外键不一致（关系端点缺失）。",
            )
        )

    counts = {
        "content_total": len(content),
        "content_active": sum(1 for row in content if row["state"] == "active"),
        "content_deleted": sum(1 for row in content if row["state"] == "deleted"),
        "relations_total": len(relations),
        "attributes_total": len(attributes),
        "attachments_total": len(attachments),
        "attachments_ready": sum(1 for row in attachments if row["state"] == "READY"),
    }

    checks: dict[str, Any] = {
        "database_integrity": integrity,
        "foreign_key_violations": len(violations),
        "schema_version": meta.get("schema_version"),
        "content_ids": [row["id"] for row in content],
        "deleted_content_ids": [row["id"] for row in content if row["state"] == "deleted"],
        "relation_ids": [row["id"] for row in relations],
        "relation_edges": [
            {"id": row["id"], "from_id": row["from_id"], "to_id": row["to_id"], "type": row["type"]}
            for row in relations
        ],
        "attribute_ids": [row["id"] for row in attributes],
        "attribute_states": [
            {
                "id": row["id"],
                "content_id": row["content_id"],
                "provenance": row["provenance"],
                "confirmed": bool(row["confirmed"]),
            }
            for row in attributes
        ],
        "attachment_ids": [row["id"] for row in attachments],
        "attachments_verified": 0,
        "attachments_not_ready": 0,
        "attachments_missing": 0,
        "attachments_corrupted": 0,
    }

    if manifest is None:
        for row in attachments:
            if row["state"] != "READY":
                checks["attachments_not_ready"] += 1
                continue
            path = data_dir / "objects" / row["id"]
            if not path.is_file():
                checks["attachments_missing"] += 1
                errors.append(
                    _issue("attachment_file_missing", SEVERITY_ERROR, row["id"], "恢复目录缺少附件文件。")
                )
                continue
            digest, size = _hash_file(path)
            if row["sha256"] and digest.lower() != row["sha256"].lower():
                checks["attachments_corrupted"] += 1
                errors.append(
                    _issue("attachment_hash_mismatch", SEVERITY_ERROR, row["id"], "恢复后的附件 SHA-256 不匹配。")
                )
            elif row["byte_size"] is not None and size != row["byte_size"]:
                checks["attachments_corrupted"] += 1
                errors.append(
                    _issue("attachment_size_mismatch", SEVERITY_ERROR, row["id"], "恢复后的附件大小不匹配。")
                )
            else:
                checks["attachments_verified"] += 1
    else:
        database_version = _schema_version_from_meta(meta)
        manifest_version = manifest.get("schema_version")
        if database_version is None:
            errors.append(
                _issue(
                    "schema_version_missing",
                    SEVERITY_ERROR,
                    "archive.db",
                    "包内数据库缺少有效的 schema_version 元数据。",
                )
            )
        elif database_version != manifest_version:
            errors.append(
                _issue(
                    "schema_version_mismatch",
                    SEVERITY_ERROR,
                    "archive.db",
                    "包内数据库 schema 版本为 {}，清单声明为 {}。".format(
                        database_version, manifest_version
                    ),
                )
            )

        expected = manifest.get("identity", {})
        for key in (
            "content_ids",
            "deleted_content_ids",
            "relation_ids",
            "relation_edges",
            "attribute_ids",
            "attribute_states",
            "attachment_ids",
        ):
            if checks.get(key) != expected.get(key):
                errors.append(
                    _issue(
                        "identity_mismatch",
                        SEVERITY_ERROR,
                        key,
                        "恢复后的身份集合与备份清单不一致。",
                    )
                )
        for key, value in manifest.get("counts", {}).items():
            if key in counts and counts[key] != value:
                errors.append(
                    _issue(
                        "count_mismatch",
                        SEVERITY_ERROR,
                        key,
                        f"恢复后的计数 {counts[key]} 与备份清单 {value} 不一致。",
                    )
                )
        manifest_rows = manifest.get("attachments")
        if not isinstance(manifest_rows, list) or any(not isinstance(row, dict) for row in manifest_rows):
            manifest_rows = []
            errors.append(_issue("attachment_manifest_invalid", SEVERITY_ERROR, "attachments", "附件清单无效。"))
        db_rows = {row["id"]: row for row in attachments}
        declared = {row["id"]: row for row in manifest_rows if isinstance(row.get("id"), str)}
        ready_paths = {f"objects/{row['id']}" for row in attachments if row["state"] == "READY"}
        members = {entry["path"] for entry in manifest["files"] if entry["path"].startswith("objects/")}
        if len(declared) != len(manifest_rows) or declared.keys() != db_rows.keys() or ready_paths != members:
            errors.append(_issue("attachment_manifest_set_mismatch", SEVERITY_ERROR, "attachments", "数据库、清单与对象集合不一致。"))
        for row in attachments:
            entry = declared.get(row["id"], {})
            if any(row[key] != entry.get(key) for key in ("content_id", "state", "sha256", "byte_size")):
                errors.append(_issue("attachment_manifest_metadata_mismatch", SEVERITY_ERROR, row["id"], "附件归属、状态、大小或摘要不一致。"))
            if row["state"] != "READY":
                checks["attachments_not_ready"] += 1
                warnings.append(_issue("attachment_not_ready", SEVERITY_WARNING, row["id"], "只恢复未就绪附件的元数据。"))
                continue
            relative = f"objects/{row['id']}"
            path = data_dir / relative
            if not ArchiveRestorer._safe_member(relative) or not path.is_file():
                checks["attachments_missing"] += 1
                errors.append(_issue("attachment_file_missing", SEVERITY_ERROR, row["id"], "恢复目录缺少附件。"))
                continue
            digest, size = _hash_file(path)
            if any((row["sha256"] and digest != row["sha256"].lower(),
                    row["byte_size"] is not None and size != row["byte_size"])):
                checks["attachments_corrupted"] += 1
                errors.append(_issue("attachment_bytes_mismatch", SEVERITY_ERROR, row["id"], "附件字节与数据库不一致。"))
            else:
                checks["attachments_verified"] += 1

    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts, "checks": checks}
