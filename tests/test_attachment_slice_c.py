"""ADR-03 Slice C tests: restore truthfulness and integrity observability.

These tests cover only the Slice C boundary:
- restore reports complete vs partial outcomes truthfully;
- excluded backup inventory is disclosed without being described as restored;
- restore and explicit reconciliation stay separate;
- the integrity API exposes only non-READY attachment metadata to the owner;
- one Archive's integrity view never includes another Archive's attachments.

They do not reopen Slice A/B upload, retry, reconciliation or quarantine rules.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.backup as backup_module
from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.backup import ArchiveBackup, ArchiveRestorer
from archive.db import connect, get_meta, init_db
from archive.store import ArchiveStore

TOKEN = "test-owner-token-slice-c-864209753"


class SliceCTestCase(unittest.TestCase):
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

    def create_content(self, title="", body="", visibility="private"):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
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

    def run_sql(self, data_dir: Path, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(data_dir / "archive.db")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def db_row(self, data_dir: Path, sql: str, params: tuple = ()):
        conn = connect(data_dir / "archive.db")
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def db_rows(self, data_dir: Path, sql: str) -> list[dict]:
        conn = connect(data_dir / "archive.db")
        try:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def mark_non_ready(self, attachment_id: str, state: str, note: str) -> None:
        self.run_sql(
            self.data_dir,
            "UPDATE attachments SET state = ?, note = ? WHERE id = ?",
            (state, note, attachment_id),
        )
        (self.data_dir / "objects" / attachment_id).unlink(missing_ok=True)

    def backup(self, name="backup.zip") -> dict:
        return ArchiveBackup(self.data_dir).create(self.root / name)

    def restore(self, backup: dict, target_name="restored") -> dict:
        return ArchiveRestorer(backup["path"]).restore(self.root / target_name)

    def manifest_of(self, backup_path: str) -> dict:
        with zipfile.ZipFile(backup_path) as archive:
            return json.loads(archive.read("manifest.json").decode("utf-8"))

    def warning_codes(self, result: dict) -> set[str]:
        return {issue["code"] for issue in result.get("warnings", [])}

    # ------------------------------------------------------------------
    # AC-C1: complete restore keeps verified=true
    # ------------------------------------------------------------------
    def test_complete_restore_remains_verified(self):
        content = self.create_content("完整恢复", "正文", "private")
        payload = b"complete payload"
        attachment = self.upload(content["id"], payload, "op_slice_c_complete")

        backup = self.backup("complete.zip")
        restored = self.restore(backup, "restored-complete")

        self.assertTrue(restored["verified"], restored)
        self.assertEqual(restored["status"], "verified")
        self.assertEqual(restored["checks"]["attachments_not_ready"], 0)
        self.assertEqual(restored["checks"]["attachments_verified"], 1)
        self.assertTrue(
            (self.root / "restored-complete" / "objects" / attachment["id"]).is_file()
        )
        self.assertEqual(
            (self.root / "restored-complete" / "objects" / attachment["id"]).read_bytes(),
            payload,
        )
        self.assertNotIn(
            "backup_excluded_storage_not_restored", self.warning_codes(restored)
        )

    # ------------------------------------------------------------------
    # AC-C2: BROKEN attachment makes the restore partial, not verified
    # ------------------------------------------------------------------
    def test_restore_with_broken_attachment_is_published_partial(self):
        content = self.create_content("损坏附件恢复", "正文", "private")
        attachment = self.upload(content["id"], b"broken payload", "op_slice_c_broken")
        self.mark_non_ready(attachment["id"], "BROKEN", "测试损坏状态")

        backup = self.backup("broken.zip")
        restored = self.restore(backup, "restored-broken")

        self.assertFalse(restored["verified"], restored)
        self.assertEqual(restored["status"], "published_partial")
        self.assertTrue((self.root / "restored-broken").is_dir())
        self.assertEqual(restored["checks"]["attachments_not_ready"], 1)
        self.assertEqual(restored["checks"]["attachments_verified"], 0)
        self.assertIn("attachment_not_ready", self.warning_codes(restored))
        row = self.db_row(
            self.root / "restored-broken",
            "SELECT state, note FROM attachments WHERE id = ?",
            (attachment["id"],),
        )
        self.assertEqual(row["state"], "BROKEN")
        self.assertEqual(row["note"], "测试损坏状态")
        self.assertFalse(
            (self.root / "restored-broken" / "objects" / attachment["id"]).exists()
        )

    # ------------------------------------------------------------------
    # AC-C3: PENDING and QUARANTINED are itemized partial outcomes
    # ------------------------------------------------------------------
    def test_restore_with_pending_and_quarantined_attachments_is_partial(self):
        content = self.create_content("多状态恢复", "正文", "private")
        ready = self.upload(content["id"], b"ready bytes", "op_slice_c_ready")
        pending = self.upload(content["id"], b"pending bytes", "op_slice_c_pending")
        quarantined = self.upload(
            content["id"], b"quarantined bytes", "op_slice_c_quarantined"
        )
        self.mark_non_ready(pending["id"], "PENDING", "写入未完成")
        self.mark_non_ready(quarantined["id"], "QUARANTINED", "隔离待人工处理")

        backup = self.backup("multi-state.zip")
        restored = self.restore(backup, "restored-multi-state")

        self.assertFalse(restored["verified"], restored)
        self.assertEqual(restored["status"], "published_partial")
        self.assertEqual(restored["checks"]["attachments_not_ready"], 2)
        self.assertEqual(restored["checks"]["attachments_verified"], 1)
        warning_targets = {
            issue["target"]
            for issue in restored["warnings"]
            if issue["code"] == "attachment_not_ready"
        }
        self.assertEqual(warning_targets, {pending["id"], quarantined["id"]})
        states = {
            row["id"]: row["state"]
            for row in self.db_rows(
                self.root / "restored-multi-state",
                "SELECT id, state FROM attachments ORDER BY id",
            )
        }
        self.assertEqual(states.get(pending["id"]), "PENDING")
        self.assertEqual(states.get(quarantined["id"]), "QUARANTINED")
        self.assertTrue(
            (self.root / "restored-multi-state" / "objects" / ready["id"]).is_file()
        )

    # ------------------------------------------------------------------
    # Published target with a final audit error is a distinct status
    # ------------------------------------------------------------------
    def test_restore_reports_published_with_audit_errors_distinctly(self):
        content = self.create_content("审计错误恢复", "正文", "private")
        self.upload(content["id"], b"audit payload", "op_slice_c_audit")

        backup = self.backup("audit-error.zip")
        real_audit = backup_module.audit_data_dir
        calls = {"count": 0}

        def fake_audit(data_dir, manifest=None):
            calls["count"] += 1
            result = real_audit(data_dir, manifest)
            if calls["count"] == 2:
                result = dict(result)
                result["ok"] = False
                result["errors"] = [
                    {
                        "code": "forced_final_audit_error",
                        "severity": "error",
                        "target": "archive.db",
                        "detail": "测试注入的最终审计错误",
                    }
                ]
                result["warnings"] = list(result.get("warnings", []))
            return result

        with patch.object(backup_module, "audit_data_dir", side_effect=fake_audit):
            restored = ArchiveRestorer(backup["path"]).restore(
                self.root / "restored-audit-error"
            )

        self.assertTrue(restored["restored"])
        self.assertTrue((self.root / "restored-audit-error").is_dir())
        self.assertFalse(restored["verified"], restored)
        self.assertEqual(restored["status"], "published_with_audit_errors")
        self.assertEqual(restored["checks"]["attachments_not_ready"], 0)
        self.assertEqual(restored["checks"]["attachments_verified"], 1)
        self.assertTrue(
            any(
                issue["code"] == "forced_final_audit_error"
                for issue in restored["errors"]
            )
        )

    # ------------------------------------------------------------------
    # AC-C5: excluded inventory is disclosed, not described as restored
    # ------------------------------------------------------------------
    def test_restore_discloses_excluded_staging_and_quarantine(self):
        content = self.create_content("排除清单", "正文", "private")
        self.upload(content["id"], b"ready payload", "op_slice_c_excluded")

        staging_file = self.data_dir / "staging" / "partial-example.part"
        quarantine_file = self.data_dir / "quarantine" / "orphan-example.bin"
        orphan_file = self.data_dir / "objects" / "f_orphan_slice_c"
        staging_file.parent.mkdir(parents=True, exist_ok=True)
        quarantine_file.parent.mkdir(parents=True, exist_ok=True)
        orphan_file.parent.mkdir(parents=True, exist_ok=True)
        staging_file.write_bytes(b"partial staging")
        quarantine_file.write_bytes(b"quarantined bytes")
        orphan_file.write_bytes(b"unreferenced object")

        backup = self.backup("excluded.zip")
        manifest = self.manifest_of(backup["path"])
        self.assertEqual(manifest["excluded"]["staging"][0]["name"], staging_file.name)
        self.assertEqual(
            manifest["excluded"]["quarantine"][0]["name"], quarantine_file.name
        )
        self.assertEqual(
            manifest["excluded"]["unreferenced_objects"][0]["name"], orphan_file.name
        )
        with zipfile.ZipFile(backup["path"]) as archive:
            names = archive.namelist()
        self.assertFalse(any(name.startswith("staging/") for name in names))
        self.assertFalse(any(name.startswith("quarantine/") for name in names))

        restored = self.restore(backup, "restored-excluded")

        # The READY archive data is verified; excluded uncommitted storage is
        # disclosed separately and is not counted as restored.
        self.assertTrue(restored["verified"], restored)
        self.assertEqual(restored["status"], "verified")
        self.assertIn("backup_excluded_storage_not_restored", self.warning_codes(restored))
        self.assertEqual(restored["excluded"], manifest["excluded"])
        self.assertEqual(
            list((self.root / "restored-excluded" / "staging").iterdir()), []
        )
        self.assertEqual(
            list((self.root / "restored-excluded" / "quarantine").iterdir()), []
        )
        self.assertFalse(
            (self.root / "restored-excluded" / "objects" / orphan_file.name).exists()
        )
        self.assertEqual(staging_file.read_bytes(), b"partial staging")
        self.assertEqual(quarantine_file.read_bytes(), b"quarantined bytes")
        self.assertEqual(orphan_file.read_bytes(), b"unreferenced object")

    # ------------------------------------------------------------------
    # AC-C5: a manifest without excluded inventory is not fabricated
    # ------------------------------------------------------------------
    def test_restore_does_not_fabricate_missing_excluded_inventory(self):
        content = self.create_content("无排除清单", "正文", "private")
        self.upload(content["id"], b"ready payload", "op_slice_c_no_excluded")
        backup = self.backup("no-excluded.zip")
        tampered = self.root / "no-excluded-manifest.zip"

        with zipfile.ZipFile(backup["path"]) as original, zipfile.ZipFile(
            tampered, "w", compression=zipfile.ZIP_DEFLATED
        ) as updated:
            for info in original.infolist():
                payload = original.read(info.filename)
                if info.filename == "manifest.json":
                    manifest = json.loads(payload.decode("utf-8"))
                    manifest.pop("excluded", None)
                    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode(
                        "utf-8"
                    )
                updated.writestr(info, payload)

        restored = ArchiveRestorer(tampered).restore(self.root / "restored-no-excluded")
        self.assertTrue(restored["verified"], restored)
        self.assertIsNone(restored["excluded"])
        self.assertNotIn(
            "backup_excluded_storage_not_restored", self.warning_codes(restored)
        )

    # ------------------------------------------------------------------
    # AC-C6: restore does not reconcile; explicit reconcile does
    # ------------------------------------------------------------------
    def test_restore_does_not_reconcile_and_explicit_reconcile_creates_report(self):
        content = self.create_content("恢复与对账分离", "正文", "private")
        attachment = self.upload(content["id"], b"broken payload", "op_slice_c_separation")
        self.mark_non_ready(attachment["id"], "BROKEN", "测试损坏状态")
        self.assertIsNone(get_meta(self.data_dir / "archive.db", "last_reconcile_report"))

        backup = self.backup("separation.zip")
        restored = self.restore(backup, "restored-separation")
        restored_dir = self.root / "restored-separation"

        self.assertIsNone(get_meta(restored_dir / "archive.db", "last_reconcile_report"))
        self.assertEqual(
            (restored_dir / "objects" / attachment["id"]).exists(), False
        )
        self.assertEqual(list((restored_dir / "quarantine").iterdir()), [])

        report = AttachmentStore(restored_dir).reconcile()
        self.assertIsNotNone(get_meta(restored_dir / "archive.db", "last_reconcile_report"))
        integrity = AttachmentStore(restored_dir).get_integrity()
        self.assertIsNotNone(integrity["last_report"])
        self.assertEqual(integrity["last_report"]["checked_at"], report["checked_at"])
        self.assertEqual(list((restored_dir / "quarantine").iterdir()), [])
        row = self.db_row(
            restored_dir,
            "SELECT state FROM attachments WHERE id = ?",
            (attachment["id"],),
        )
        self.assertEqual(row["state"], "BROKEN")
        self.assertEqual(
            self.db_row(
                self.data_dir,
                "SELECT state FROM attachments WHERE id = ?",
                (attachment["id"],),
            )["state"],
            "BROKEN",
        )

    # ------------------------------------------------------------------
    # AC-C7: integrity API fields, privacy and owner gate
    # ------------------------------------------------------------------
    def test_integrity_lists_non_ready_attachments_without_private_body(self):
        secret_body = "SECRET BODY MUST NOT APPEAR IN INTEGRITY"
        content = self.create_content("私密损坏附件", secret_body, "private")
        ready = self.upload(content["id"], b"ready", "op_slice_c_api_ready")
        broken = self.upload(content["id"], b"broken", "op_slice_c_api_broken")
        self.mark_non_ready(broken["id"], "BROKEN", "API 可见备注")

        response = self.owner.get("/api/integrity")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertEqual(len(data["non_ready_attachments"]), 1)
        entry = data["non_ready_attachments"][0]
        self.assertEqual(
            set(entry),
            {
                "id",
                "content_id",
                "filename",
                "state",
                "note",
                "operation_id",
                "updated_at",
            },
        )
        self.assertEqual(entry["id"], broken["id"])
        self.assertEqual(entry["content_id"], content["id"])
        self.assertEqual(entry["filename"], "note.txt")
        self.assertEqual(entry["state"], "BROKEN")
        self.assertEqual(entry["note"], "API 可见备注")
        self.assertEqual(entry["operation_id"], "op_slice_c_api_broken")
        self.assertNotIn(secret_body, response.get_data(as_text=True))
        self.assertFalse(
            any(
                attachment["id"] == ready["id"]
                for attachment in data["non_ready_attachments"]
            )
        )

    def test_integrity_requires_owner_and_is_scoped_to_one_archive(self):
        anonymous = self.app.test_client()
        response = anonymous.get("/api/integrity")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "unauthorized")

        content = self.create_content("本归档", "正文", "private")
        local = self.upload(content["id"], b"local", "op_slice_c_local")
        self.mark_non_ready(local["id"], "BROKEN", "本归档损坏")

        other_dir = self.root / "other-archive"
        init_db(other_dir / "archive.db")
        other_store = ArchiveStore(other_dir)
        other_attachments = AttachmentStore(other_dir)
        other_content = other_store.create_content("其他归档", "正文", "private")
        other_result = other_attachments.upload(
            io.BytesIO(b"other"),
            filename="other.txt",
            content_id=other_content["id"],
            operation_id="op_slice_c_other",
        )
        other_id = other_result["attachment"]["id"]
        conn = sqlite3.connect(other_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE attachments SET state = 'BROKEN', note = '其他归档损坏' WHERE id = ?",
                (other_id,),
            )
            conn.commit()
        finally:
            conn.close()

        local_entries = AttachmentStore(self.data_dir).get_integrity()[
            "non_ready_attachments"
        ]
        other_entries = other_attachments.get_integrity()["non_ready_attachments"]
        self.assertEqual([entry["id"] for entry in local_entries], [local["id"]])
        self.assertEqual([entry["id"] for entry in other_entries], [other_id])
        self.assertEqual([entry["filename"] for entry in local_entries], ["note.txt"])
        self.assertEqual([entry["filename"] for entry in other_entries], ["other.txt"])


if __name__ == "__main__":
    unittest.main()
