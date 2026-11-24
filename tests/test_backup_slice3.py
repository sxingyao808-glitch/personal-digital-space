"""Slice 3 tests: whole-archive backup and restore (AC-13).

These tests exercise the real backup artifact and a restore from it. A passing
backup test is not permission to claim AC-13; the end-to-end restore check in
tests/restore_check.py is the evidence used for that claim.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
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

from archive.app import create_app
from archive.backup import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    ArchiveBackup,
    ArchiveRestorer,
    BackupError,
    RestoreError,
)
from archive.db import SCHEMA_VERSION, connect
from archive.store import ValidationError

TOKEN = "test-owner-token-backup-246813579"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data_dir = self.root / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def login(self, client) -> dict[str, str]:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return {"X-CSRF-Token": csrf}

    def create_content(self, title="", body="", visibility=None):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def delete_content(self, content_id, revision):
        response = self.owner.delete(
            f"/api/content/{content_id}",
            json={"revision": revision},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_relation(self, from_id, to_id, relation_type):
        response = self.owner.post(
            f"/api/content/{from_id}/relations",
            json={"to_id": to_id, "type": relation_type},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_attribute(self, content_id, key, value, provenance="user", confirmed=False):
        response = self.owner.post(
            f"/api/content/{content_id}/attributes",
            json={"key": key, "value": value, "provenance": provenance, "confirmed": confirmed},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def upload(self, content_id, payload: bytes, operation_id: str, filename="note.txt"):
        response = self.owner.post(
            f"/api/content/{content_id}/attachments",
            data={
                "file": (io.BytesIO(payload), filename),
                "operation_id": operation_id,
            },
            content_type="multipart/form-data",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]["attachment"]

    def backup(self, name="backup.zip") -> dict:
        return ArchiveBackup(self.data_dir).create(self.root / name)

    def restore(self, backup: dict, target_name="restored") -> dict:
        return ArchiveRestorer(backup["path"]).restore(self.root / target_name)

    def table_dump(self, data_dir: Path) -> dict:
        conn = connect(data_dir / "archive.db")
        try:
            return {
                table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("content", "relations", "attributes", "attachments")
            }
        finally:
            conn.close()

    def db_row(self, data_dir: Path, sql: str, params: tuple = ()):
        conn = connect(data_dir / "archive.db")
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def run_sql(self, data_dir: Path, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(data_dir / "archive.db")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def manifest_of(self, archive: zipfile.ZipFile) -> dict:
        return json.loads(archive.read("manifest.json").decode("utf-8"))

    def rewrite_zip(self, source: Path, destination: Path, mutate) -> None:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in original.infolist():
                payload = original.read(info.filename)
                payload = mutate(info.filename, payload) or payload
                updated.writestr(info.filename, payload)

    # ------------------------------------------------------------------
    # 1. Normal backup + manifest verification + restore
    # ------------------------------------------------------------------
    def test_backup_package_is_self_describing_and_restorable(self):
        public = self.create_content("公开条目", "公开正文", "public")
        other = self.create_content("另一条目", "另一正文", "private")
        relation = self.add_relation(other["id"], public["id"], "REFERENCES")
        attribute = self.add_attribute(other["id"], "来源", "本地", "user", True)
        attachment = self.upload(public["id"], b"attachment-payload", "op_backup_1")

        result = self.backup()
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(result["format"], BACKUP_FORMAT)
        self.assertEqual(result["format_version"], BACKUP_FORMAT_VERSION)
        self.assertEqual(result["counts"]["content_total"], 2)
        self.assertEqual(result["counts"]["relations_total"], 1)
        self.assertEqual(result["counts"]["attributes_total"], 1)
        self.assertEqual(result["counts"]["attachments_ready"], 1)

        with zipfile.ZipFile(result["path"]) as archive:
            manifest = self.manifest_of(archive)
            self.assertEqual(manifest["format"], BACKUP_FORMAT)
            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            self.assertEqual(manifest["boundaries"]["publishes_content"], False)
            self.assertEqual(manifest["boundaries"]["overwrites_source"], False)
            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("archive.db", paths)
            self.assertIn(f"objects/{attachment['id']}", paths)
            for entry in manifest["files"]:
                payload = archive.read(entry["path"])
                self.assertEqual(len(payload), entry["bytes"])
                self.assertEqual(sha256_bytes(payload), entry["sha256"])
            self.assertEqual(sha256_file(Path(result["path"])), result["sha256"])
            self.assertNotIn("owner_secret", archive.namelist())
            self.assertNotIn("exports/", archive.namelist())

        restored = self.restore(result)
        self.assertTrue(restored["restored"])
        self.assertTrue(restored["verified"], restored)
        self.assertEqual(restored["status"], "verified")
        self.assertEqual(
            restored["checks"]["content_ids"],
            sorted([public["id"], other["id"]]),
        )
        self.assertEqual(
            restored["checks"]["relation_edges"],
            [
                {
                    "id": relation["id"],
                    "from_id": other["id"],
                    "to_id": public["id"],
                    "type": "REFERENCES",
                }
            ],
        )
        self.assertEqual(
            restored["checks"]["attribute_states"],
            [
                {
                    "id": attribute["id"],
                    "content_id": other["id"],
                    "provenance": "user",
                    "confirmed": True,
                }
            ],
        )
        self.assertEqual(restored["checks"]["attachments_verified"], 1)
        self.assertEqual(sha256_file(self.root / "restored" / "objects" / attachment["id"]), sha256_bytes(b"attachment-payload"))
        self.assertEqual(self.table_dump(self.root / "restored"), self.table_dump(self.data_dir))

    # ------------------------------------------------------------------
    # 2. Empty archive
    # ------------------------------------------------------------------
    def test_empty_archive_backup_and_restore(self):
        result = self.backup("empty.zip")
        self.assertEqual(result["counts"]["content_total"], 0)
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(sorted(archive.namelist()), ["archive.db", "manifest.json"])
            manifest = self.manifest_of(archive)
            self.assertEqual(manifest["identity"]["content_ids"], [])

        restored = self.restore(result, "restored-empty")
        self.assertTrue(restored["verified"])
        self.assertEqual(restored["counts"]["content_total"], 0)
        self.assertTrue((self.root / "restored-empty" / "objects").is_dir())
        self.assertTrue((self.root / "restored-empty" / "staging").is_dir())
        self.assertTrue((self.root / "restored-empty" / "quarantine").is_dir())

    # ------------------------------------------------------------------
    # 3. Soft-deleted content, relation and attribute identity
    # ------------------------------------------------------------------
    def test_identity_preserved_for_deleted_records_relations_attributes(self):
        first = self.create_content("将被删除", "软删除内容", "private")
        second = self.create_content("保留", "保留内容", "public")
        relation = self.add_relation(first["id"], second["id"], "LED_TO")
        attribute = self.add_attribute(first["id"], "阶段", "草稿", "ai_suggested", False)
        self.delete_content(first["id"], first["revision"])

        result = self.backup()
        restored = self.restore(result)

        self.assertTrue(restored["verified"])
        self.assertEqual(restored["counts"]["content_deleted"], 1)
        self.assertEqual(restored["checks"]["deleted_content_ids"], [first["id"]])
        self.assertEqual(
            restored["checks"]["relation_edges"],
            [
                {
                    "id": relation["id"],
                    "from_id": first["id"],
                    "to_id": second["id"],
                    "type": "LED_TO",
                }
            ],
        )
        self.assertEqual(
            restored["checks"]["attribute_states"],
            [
                {
                    "id": attribute["id"],
                    "content_id": first["id"],
                    "provenance": "ai_suggested",
                    "confirmed": False,
                }
            ],
        )
        row = self.db_row(
            self.root / "restored",
            "SELECT state, deleted_at FROM content WHERE id = ?",
            (first["id"],),
        )
        self.assertEqual(row["state"], "deleted")
        self.assertIsNotNone(row["deleted_at"])

    # ------------------------------------------------------------------
    # 4. Multiple attachments, duplicate content, checksums
    # ------------------------------------------------------------------
    def test_multiple_attachments_and_duplicate_content(self):
        one = self.create_content("重复标题", "正文 A", "public")
        two = self.create_content("重复标题", "正文 B", "private")
        self.assertNotEqual(one["id"], two["id"])
        first = self.upload(one["id"], b"alpha-bytes", "op_backup_a")
        second = self.upload(two["id"], b"beta-bytes", "op_backup_b")

        result = self.backup()
        self.assertEqual(result["counts"]["attachments_ready"], 2)
        restored = self.restore(result)
        self.assertEqual(restored["checks"]["attachments_verified"], 2)
        self.assertEqual(
            set(restored["checks"]["content_ids"]), {one["id"], two["id"]}
        )
        target_objects = self.root / "restored" / "objects"
        self.assertEqual(sha256_file(target_objects / first["id"]), sha256_bytes(b"alpha-bytes"))
        self.assertEqual(sha256_file(target_objects / second["id"]), sha256_bytes(b"beta-bytes"))

    # ------------------------------------------------------------------
    # 5. Missing READY attachment -> backup fails, nothing written
    # ------------------------------------------------------------------
    def test_backup_fails_when_ready_attachment_file_is_missing(self):
        content = self.create_content("有附件", "正文", "private")
        attachment = self.upload(content["id"], b"bytes-to-lose", "op_backup_missing")
        (self.data_dir / "objects" / attachment["id"]).unlink()
        before = self.table_dump(self.data_dir)

        with self.assertRaises(BackupError) as caught:
            self.backup("missing.zip")
        self.assertEqual(caught.exception.code, "backup_failed")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("attachment_file_missing", codes)
        self.assertFalse((self.root / "missing.zip").exists())
        self.assertEqual(self.table_dump(self.data_dir), before)

    def test_backup_fails_when_attachment_bytes_are_corrupted(self):
        content = self.create_content("有附件", "正文", "private")
        attachment = self.upload(content["id"], b"original-bytes", "op_backup_corrupt")
        (self.data_dir / "objects" / attachment["id"]).write_bytes(b"tampered-bytes")

        with self.assertRaises(BackupError) as caught:
            self.backup("corrupt.zip")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("attachment_hash_mismatch", codes)
        self.assertFalse((self.root / "corrupt.zip").exists())

    # ------------------------------------------------------------------
    # 6. Non-READY attachment is reported, not silently dropped
    # ------------------------------------------------------------------
    def test_non_ready_attachment_is_reported_and_metadata_restored(self):
        content = self.create_content("损坏附件", "正文", "private")
        attachment = self.upload(content["id"], b"payload", "op_backup_broken")
        self.run_sql(
            self.data_dir,
            "UPDATE attachments SET state = 'BROKEN' WHERE id = ?",
            (attachment["id"],),
        )
        (self.data_dir / "objects" / attachment["id"]).unlink()

        result = self.backup("broken.zip")
        codes = {issue["code"] for issue in result["warnings"]}
        self.assertIn("attachment_not_ready", codes)
        self.assertEqual(result["counts"]["attachments_not_ready"], 1)
        self.assertEqual(result["counts"]["attachments_ready"], 0)

        restored = self.restore(result, "restored-broken")
        self.assertFalse(restored["verified"], restored)
        self.assertEqual(restored["status"], "published_partial")
        self.assertEqual(restored["checks"]["attachments_not_ready"], 1)
        self.assertTrue((self.root / "restored-broken").is_dir())
        self.assertFalse((self.root / "restored-broken" / "objects" / attachment["id"]).exists())
        row = self.db_row(
            self.root / "restored-broken",
            "SELECT state FROM attachments WHERE id = ?",
            (attachment["id"],),
        )
        self.assertEqual(row["state"], "BROKEN")

    # ------------------------------------------------------------------
    # 7. Restore never overwrites an existing target
    # ------------------------------------------------------------------
    def test_restore_rejects_existing_target_without_modifying_anything(self):
        self.create_content("内容", "正文", "private")
        result = self.backup()
        target = self.root / "existing"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep-me", encoding="utf-8")
        source_before = self.table_dump(self.data_dir)

        with self.assertRaises(ValidationError) as caught:
            ArchiveRestorer(result["path"]).restore(target)
        self.assertEqual(caught.exception.code, "validation_error")
        self.assertEqual((target / "sentinel.txt").read_text(encoding="utf-8"), "keep-me")
        self.assertEqual(self.table_dump(self.data_dir), source_before)

    # ------------------------------------------------------------------
    # 8. Corrupted / incomplete package cannot be published
    # ------------------------------------------------------------------
    def test_restore_rejects_corrupted_member_and_leaves_no_target(self):
        self.create_content("内容", "正文", "private")
        result = self.backup()
        tampered = self.root / "tampered.zip"

        def mutate(name, payload):
            if name == "archive.db":
                # Flip a byte in the middle: the final bytes of a SQLite page
                # can legitimately be zero padding, which would make a
                # tail-only overwrite a no-op.
                offset = len(payload) // 2
                flipped = bytearray(payload)
                flipped[offset] ^= 0xFF
                return bytes(flipped)
            return None

        self.rewrite_zip(Path(result["path"]), tampered, mutate)
        with self.assertRaises(RestoreError) as caught:
            ArchiveRestorer(tampered).restore(self.root / "corrupt-target")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("backup_member_mismatch", codes)
        self.assertFalse((self.root / "corrupt-target").exists())

    def test_restore_rejects_missing_attachment_member(self):
        content = self.create_content("内容", "正文", "private")
        attachment = self.upload(content["id"], b"payload", "op_backup_removed")
        result = self.backup("complete.zip")
        tampered = self.root / "missing-member.zip"

        with zipfile.ZipFile(result["path"]) as original, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in original.infolist():
                if info.filename == f"objects/{attachment['id']}":
                    continue
                updated.writestr(info.filename, original.read(info.filename))

        with self.assertRaises(RestoreError) as caught:
            ArchiveRestorer(tampered).restore(self.root / "missing-target")
        codes = {issue["code"] for issue in caught.exception.details["issues"]}
        self.assertIn("backup_member_missing", codes)
        self.assertFalse((self.root / "missing-target").exists())

    def test_restore_rejects_unsupported_schema_version(self):
        self.create_content("内容", "正文", "private")
        result = self.backup()
        tampered = self.root / "schema.zip"

        def mutate(name, payload):
            if name == "manifest.json":
                manifest = json.loads(payload.decode("utf-8"))
                manifest["schema_version"] = 99
                return json.dumps(manifest).encode("utf-8")
            return None

        self.rewrite_zip(Path(result["path"]), tampered, mutate)
        with self.assertRaises(RestoreError) as caught:
            ArchiveRestorer(tampered).restore(self.root / "schema-target")
        self.assertIn("schema", caught.exception.message)
        self.assertFalse((self.root / "schema-target").exists())

    # ------------------------------------------------------------------
    # 9. Broken relation (missing endpoint) is preserved and reported
    # ------------------------------------------------------------------
    def test_broken_relation_is_preserved_and_reported(self):
        first = self.create_content("端点 A", "正文", "private")
        second = self.create_content("端点 B", "正文", "private")
        relation = self.add_relation(first["id"], second["id"], "DERIVED_FROM")
        conn = sqlite3.connect(self.data_dir / "archive.db")
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM content WHERE id = ?", (second["id"],))
            conn.commit()
        finally:
            conn.close()

        result = self.backup("broken-relation.zip")
        codes = {issue["code"] for issue in result["warnings"]}
        self.assertIn("relation_endpoint_missing", codes)

        restored = self.restore(result, "restored-broken-relation")
        self.assertFalse(restored["verified"], restored)
        self.assertEqual(restored["status"], "published_with_foreign_key_warnings")
        self.assertTrue(restored["restore_fidelity"])
        self.assertTrue(restored["attachments_complete"])
        self.assertFalse(restored["foreign_keys_healthy"])
        self.assertEqual(
            restored["checks"]["relation_edges"],
            [
                {
                    "id": relation["id"],
                    "from_id": first["id"],
                    "to_id": second["id"],
                    "type": "DERIVED_FROM",
                }
            ],
        )
        warning_codes = {issue["code"] for issue in restored["warnings"]}
        self.assertIn("relation_endpoint_missing", warning_codes)

    # ------------------------------------------------------------------
    # 10. Staging / quarantine / secrets are excluded and reported
    # ------------------------------------------------------------------
    def test_uncommitted_storage_and_secrets_are_excluded(self):
        self.create_content("内容", "正文", "private")
        (self.data_dir / "owner_secret").write_text("super-secret", encoding="utf-8")
        (self.data_dir / "exports").mkdir(exist_ok=True)
        (self.data_dir / "exports" / "private-export.zip").write_bytes(b"private")
        (self.data_dir / "staging").mkdir(exist_ok=True)
        (self.data_dir / "staging" / "in-flight.part").write_bytes(b"partial")
        (self.data_dir / "objects").mkdir(exist_ok=True)
        (self.data_dir / "objects" / "unreferenced.bin").write_bytes(b"orphan")

        result = self.backup("excluded.zip")
        with zipfile.ZipFile(result["path"]) as archive:
            names = archive.namelist()
            manifest = self.manifest_of(archive)
        self.assertNotIn("owner_secret", names)
        self.assertFalse(any(name.startswith("exports/") for name in names))
        self.assertFalse(any(name.startswith("staging/") for name in names))
        self.assertFalse(any(name == "objects/unreferenced.bin" for name in names))
        self.assertEqual(manifest["excluded"]["staging"][0]["name"], "in-flight.part")
        self.assertEqual(
            manifest["excluded"]["unreferenced_objects"][0]["name"], "unreferenced.bin"
        )
        codes = {issue["code"] for issue in result["warnings"]}
        self.assertTrue(
            {"staging_files_excluded", "unreferenced_object_file"}.issubset(codes), codes
        )

    # ------------------------------------------------------------------
    # 11. Interrupted backup
    # ------------------------------------------------------------------
    def test_interrupted_backup_produces_no_package(self):
        self.create_content("内容", "正文", "private")
        target = self.root / "interrupted.zip"
        script = (
            "import sys; sys.path.insert(0, sys.argv[3]);"
            "from archive.backup import ArchiveBackup;"
            "ArchiveBackup(sys.argv[1]).create(sys.argv[2])"
        )
        env = os.environ.copy()
        env["ARCHIVE_TEST_MODE"] = "1"
        env["ARCHIVE_FAULT_INJECT"] = "backup_after_snapshot"
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.data_dir), str(target), str(ROOT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 74, completed.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.zip")), [])

    # ------------------------------------------------------------------
    # 12. Interrupted restore
    # ------------------------------------------------------------------
    def test_interrupted_restore_leaves_no_target_and_source_unchanged(self):
        self.create_content("内容", "正文", "private")
        result = self.backup("restore-source.zip")
        before = self.table_dump(self.data_dir)
        target = self.root / "interrupted-restore"
        script = (
            "import sys; sys.path.insert(0, sys.argv[3]);"
            "from archive.backup import ArchiveRestorer;"
            "ArchiveRestorer(sys.argv[1]).restore(sys.argv[2])"
        )
        env = os.environ.copy()
        env["ARCHIVE_TEST_MODE"] = "1"
        env["ARCHIVE_FAULT_INJECT"] = "restore_after_extract"
        completed = subprocess.run(
            [sys.executable, "-c", script, result["path"], str(target), str(ROOT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 74, completed.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(self.table_dump(self.data_dir), before)
        leftovers = list(self.root.glob(".interrupted-restore.restore-*"))
        self.assertTrue(leftovers, "expected an identifiable staging directory")
        self.assertFalse((leftovers[0] / "restore-complete").exists())

    # ------------------------------------------------------------------
    # 13. Restored directory is a usable archive
    # ------------------------------------------------------------------
    def test_restored_directory_serves_the_same_archive(self):
        content = self.create_content("可读内容", "恢复后应可读取", "public")
        self.add_attribute(content["id"], "键", "值", "user", True)
        result = self.backup()
        restored = self.restore(result)
        self.assertTrue(restored["verified"])

        app = create_app(self.root / "restored", bootstrap_token=TOKEN, testing=True)
        client = app.test_client()
        self.login(client)
        listed = client.get("/api/content").get_json()["data"]["items"]
        self.assertEqual([item["id"] for item in listed], [content["id"]])
        detail = client.get(f"/api/content/{content['id']}").get_json()["data"]
        self.assertEqual(detail["title"], "可读内容")
        self.assertEqual(detail["attributes"][0]["provenance"], "user")
        self.assertEqual(detail["attributes"][0]["confirmed"], True)


if __name__ == "__main__":
    unittest.main()
