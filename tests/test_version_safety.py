"""ADR-02 v0.2 fixes: cross-version backup/restore safety and diagnostics."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.backup import ArchiveBackup, ArchiveRestorer, BackupError, RestoreError
from archive.db import SCHEMA_VERSION, SchemaVersionError, init_db, utcnow


class VersionSafetyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "archive.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def seed_v1(self) -> None:
        init_db(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = utcnow()
            conn.execute(
                "INSERT INTO content(id, title, body, visibility, visibility_selected, "
                "revision, state, created_at, updated_at) "
                "VALUES('c_safety', '兼容性验证', '正文', 'private', 0, 1, 'active', ?, ?)",
                (now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def set_version(self, value: str) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (value,))
            conn.commit()
        finally:
            conn.close()

    def version(self) -> str | None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def tamper_package(self, source: Path, destination: Path, version: int) -> None:
        with zipfile.ZipFile(source) as original:
            members = {
                info.filename: original.read(info.filename) for info in original.infolist()
            }
        tampered_db = self.root / "tampered.db"
        tampered_db.write_bytes(members["archive.db"])
        conn = sqlite3.connect(str(tampered_db))
        try:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(version),)
            )
            conn.commit()
        finally:
            conn.close()
        db_bytes = tampered_db.read_bytes()
        digest = hashlib.sha256(db_bytes).hexdigest()

        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        manifest["database"]["bytes"] = len(db_bytes)
        manifest["database"]["sha256"] = digest
        manifest["database"]["meta"]["schema_version"] = str(version)
        for entry in manifest["files"]:
            if entry["path"] == "archive.db":
                entry["bytes"] = len(db_bytes)
                entry["sha256"] = digest
        members["archive.db"] = db_bytes
        members["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")

        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)

    # ------------------------------------------------------------------
    # H-1: backup must refuse a database at an unsupported version
    # ------------------------------------------------------------------
    def test_backup_refuses_unsupported_database_version(self):
        self.seed_v1()
        self.set_version("99")
        target = self.root / "refused.zip"

        with self.assertRaises(BackupError) as caught:
            ArchiveBackup(self.data_dir).create(target)
        self.assertEqual(caught.exception.code, "backup_failed")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("backup_schema_version_mismatch", codes)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.zip")), [])
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(self.version(), "99")

    # ------------------------------------------------------------------
    # H-1: restore/audit must compare database meta with the manifest
    # ------------------------------------------------------------------
    def test_restore_rejects_manifest_v1_package_with_v99_database(self):
        self.seed_v1()
        valid = ArchiveBackup(self.data_dir).create(self.root / "valid.zip")
        tampered = self.root / "tampered.zip"
        self.tamper_package(Path(valid["path"]), tampered, 99)
        target = self.root / "target"

        with self.assertRaises(RestoreError) as caught:
            ArchiveRestorer(tampered).restore(target)
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("schema_version_mismatch", codes)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".target.restore-*")), [])
        self.assertFalse((self.root / "target" / "archive.db").exists())

    def test_restore_rejects_missing_schema_version_in_database(self):
        self.seed_v1()
        valid = ArchiveBackup(self.data_dir).create(self.root / "valid-missing.zip")
        tampered = self.root / "tampered-missing.zip"

        with zipfile.ZipFile(valid["path"]) as original:
            members = {
                info.filename: original.read(info.filename) for info in original.infolist()
            }
        missing_db = self.root / "missing.db"
        missing_db.write_bytes(members["archive.db"])
        conn = sqlite3.connect(str(missing_db))
        try:
            conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()
        db_bytes = missing_db.read_bytes()
        digest = hashlib.sha256(db_bytes).hexdigest()
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        manifest["database"]["bytes"] = len(db_bytes)
        manifest["database"]["sha256"] = digest
        manifest["database"]["meta"].pop("schema_version", None)
        for entry in manifest["files"]:
            if entry["path"] == "archive.db":
                entry["bytes"] = len(db_bytes)
                entry["sha256"] = digest
        members["archive.db"] = db_bytes
        members["manifest.json"] = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
        with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)

        with self.assertRaises(RestoreError) as caught:
            ArchiveRestorer(tampered).restore(self.root / "missing-target")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("schema_version_missing", codes)
        self.assertFalse((self.root / "missing-target").exists())

    # ------------------------------------------------------------------
    # G-1: corrupted database is refused with a diagnosable error
    # ------------------------------------------------------------------
    def test_corrupt_database_is_refused_with_reason(self):
        self.data_dir.mkdir(parents=True)
        self.db_path.write_bytes(b"this is not a sqlite database\n" * 8)

        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path)
        self.assertIn("reason", caught.exception.details)
        self.assertIn(
            caught.exception.details["reason"], {"DatabaseError", "OperationalError"}
        )
        self.assertIn("detail", caught.exception.details)
        self.assertFalse((self.data_dir / "owner_secret").exists())

    def test_cli_refuses_corrupt_database_with_exit_two(self):
        self.data_dir.mkdir(parents=True)
        self.db_path.write_bytes(b"not a database at all\n" * 8)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "--data-dir",
                str(self.data_dir),
                "--no-open",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("schema 不兼容", completed.stderr)
        self.assertIn("原因=", completed.stderr)
        self.assertFalse((self.data_dir / "owner_secret").exists())

    # ------------------------------------------------------------------
    # G-2: CLI reports existing version diagnostics without secrets
    # ------------------------------------------------------------------
    def test_cli_reports_database_and_supported_versions(self):
        self.seed_v1()
        self.set_version("99")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "--data-dir",
                str(self.data_dir),
                "--no-open",
                "--bootstrap-token",
                "should-not-appear-token",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("数据库版本=99", completed.stderr)
        self.assertIn(f"当前支持版本={SCHEMA_VERSION}", completed.stderr)
        self.assertNotIn("should-not-appear-token", completed.stderr)
        self.assertNotIn("should-not-appear-token", completed.stdout)

    # ------------------------------------------------------------------
    # Supplementary: meta exists but schema_version key is missing
    # ------------------------------------------------------------------
    def test_missing_schema_version_key_is_refused(self):
        self.seed_v1()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path)
        self.assertIn("schema_version", caught.exception.message)
        self.assertIsNone(self.version())


if __name__ == "__main__":
    unittest.main()
