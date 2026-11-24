"""Slice B: reconciliation, staging and quarantine convergence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.attachments as attachments_module
from archive.app import create_app
from archive.attachments import AttachmentStore, AttachmentUnverifiedError
from archive.db import connect, get_meta, set_meta, utcnow
from archive.store import ArchiveStore, ConflictError, NotFoundError, ValidationError

TOKEN = "test-owner-token-0123456789"


class AttachmentSliceBTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content_a = self.store.create_content("Slice B 内容甲")
        self.content_b = self.store.create_content("Slice B 内容乙")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    def upload(self, content_id, payload, operation_id, attachment_id):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="slice-b.bin",
            content_id=content_id,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def attachment_row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def operation_row(self, operation_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def issue_row(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def attachment_count(self):
        conn = connect(self.data_dir / "archive.db")
        try:
            return conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        finally:
            conn.close()

    def object_bytes(self, attachment_id):
        return (self.data_dir / "objects" / attachment_id).read_bytes()

    def staging_files(self):
        return sorted(p.name for p in (self.data_dir / "staging").iterdir())

    def quarantine_files(self):
        return sorted(
            p for p in (self.data_dir / "quarantine").iterdir() if p.is_file()
        )

    def set_operation_state(self, operation_id, state, result):
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE operations SET state = ?, result = ?, updated_at = ? "
                "WHERE id = ?",
                (state, result, utcnow(), operation_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_attachment_quarantined(self, attachment_id, payload):
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE attachments SET state = 'QUARANTINED', sha256 = ?, "
                "byte_size = ?, updated_at = ? WHERE id = ?",
                (
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    utcnow(),
                    attachment_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 1. A stale staging file is quarantined with a durable metadata record.
    # ------------------------------------------------------------------
    def test_stale_staging_is_quarantined_with_metadata_and_repeatable(self):
        payload = b"STALE-STAGING-SLICE-B"
        stage = self.data_dir / "staging" / "dead-op.0001.part"
        stage.write_bytes(payload)

        report = self.attachments.reconcile()
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["quarantined"]), 1)
        # Reports publish the quarantine file name, never the absolute path.
        quarantine_name = report["quarantined"][0]
        self.assertEqual(quarantine_name, Path(quarantine_name).name)
        quarantined_path = self.data_dir / "quarantine" / quarantine_name
        self.assertTrue(quarantined_path.name.startswith("staging-dead-op.0001.part."))
        self.assertEqual(quarantined_path.read_bytes(), payload)
        self.assertFalse(stage.exists())
        self.assertEqual(self.staging_files(), [])

        ledger = self.issue_row("quarantine_file_isolated", quarantined_path.name)
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["state"], "open")
        detail = json.loads(ledger["detail"])
        self.assertEqual(detail["origin"], "staging")
        self.assertEqual(detail["reason"], "stale_staging")
        self.assertEqual(detail["original_name"], "dead-op.0001.part")
        self.assertEqual(detail["bytes"], len(payload))
        self.assertEqual(detail["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(detail["moved_at"])

        self.assertEqual(len(report["quarantine_inventory"]), 1)
        entry = report["quarantine_inventory"][0]
        self.assertEqual(entry["name"], quarantined_path.name)
        self.assertEqual(entry["origin"], "staging")
        self.assertEqual(entry["reason"], "stale_staging")
        self.assertEqual(entry["original_name"], "dead-op.0001.part")
        self.assertEqual(entry["bytes"], len(payload))
        self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(entry["tracked_before_scan"])
        self.assertFalse(entry["registered_this_run"])

        second = self.attachments.reconcile()
        self.assertEqual(second["quarantined"], [])
        self.assertEqual(second["quarantine_inventory_count"], 1)
        self.assertEqual(quarantined_path.read_bytes(), payload)
        again = self.issue_row("quarantine_file_isolated", quarantined_path.name)
        self.assertEqual(again["first_seen"], ledger["first_seen"])
        self.assertEqual(again["last_seen"], ledger["last_seen"])
        self.assertEqual(again["detail"], ledger["detail"])
        self.assertEqual(
            self.issue_row("stale_staging_quarantined", "dead-op.0001.part")["state"],
            "resolved",
        )
        open_kinds = {
            issue["kind"]
            for issue in self.attachments.get_integrity()["open_issues"]
        }
        self.assertIn("quarantine_file_isolated", open_kinds)
        # ok covers new active-area findings; the isolated inventory is
        # reported separately and is intentionally not a new scan failure.
        self.assertTrue(second["ok"])

        # Reconcile never deletes. If a human removes the isolated file, the
        # inventory record closes without touching any other evidence.
        quarantined_path.unlink()
        third = self.attachments.reconcile()
        self.assertEqual(third["quarantine_inventory_count"], 0)
        self.assertEqual(
            self.issue_row("quarantine_file_isolated", quarantined_path.name)["state"],
            "resolved",
        )

    # ------------------------------------------------------------------
    # 2. An orphan object is quarantined without creating an attachment row.
    # ------------------------------------------------------------------
    def test_orphan_object_is_quarantined_without_attachment_row(self):
        payload = b"ORPHAN-OBJECT-SLICE-B"
        orphan = self.data_dir / "objects" / "orphan.bin"
        orphan.write_bytes(payload)
        attachments_before = self.attachment_count()

        report = self.attachments.reconcile()
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["quarantined"]), 1)
        quarantined_path = self.data_dir / "quarantine" / report["quarantined"][0]
        self.assertTrue(quarantined_path.name.startswith("objects-orphan.bin."))
        self.assertEqual(quarantined_path.read_bytes(), payload)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.attachment_count(), attachments_before)

        issue = self.issue_row("orphan_object_quarantined", "orphan.bin")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["state"], "open")

        ledger = self.issue_row("quarantine_file_isolated", quarantined_path.name)
        detail = json.loads(ledger["detail"])
        self.assertEqual(detail["origin"], "objects")
        self.assertEqual(detail["reason"], "orphan_object")
        self.assertEqual(detail["original_name"], "orphan.bin")
        self.assertEqual(detail["bytes"], len(payload))
        self.assertEqual(detail["sha256"], hashlib.sha256(payload).hexdigest())

        second = self.attachments.reconcile()
        self.assertEqual(second["quarantined"], [])
        self.assertEqual(len(self.quarantine_files()), 1)
        self.assertEqual(quarantined_path.read_bytes(), payload)
        self.assertEqual(self.attachment_count(), attachments_before)

    # ------------------------------------------------------------------
    # 3. A successful move is recorded even when the attachment CAS is stale.
    # ------------------------------------------------------------------
    def test_quarantine_move_is_recorded_when_attachment_cas_is_stale(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_race",
            attachment_id="f_race",
            filename="slice-b.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        stale_bytes = b"STALE-STAGING-RACE"
        stage = self.data_dir / "staging" / "op_race.aaaaaaaa.part"
        stage.write_bytes(stale_bytes)

        entered = threading.Event()
        release = threading.Event()
        original = AttachmentStore._mark_broken_reconciliation

        def delayed(store_obj, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("reconcile did not receive the release signal")
            return original(store_obj, *args, **kwargs)

        holder = {}
        with mock.patch.object(
            AttachmentStore, "_mark_broken_reconciliation", delayed
        ):
            def run_reconcile():
                holder["report"] = AttachmentStore(self.data_dir).reconcile()

            thread = threading.Thread(target=run_reconcile, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                # The move and its inventory record precede the paused CAS.
                moved = self.quarantine_files()
                self.assertEqual(len(moved), 1)
                self.assertEqual(moved[0].read_bytes(), stale_bytes)
                ledger = self.issue_row("quarantine_file_isolated", moved[0].name)
                self.assertIsNotNone(ledger)
                self.assertEqual(
                    json.loads(ledger["detail"])["reason"],
                    "staging_without_journaled_payload",
                )
                self.assertIsNotNone(
                    self.issue_row("staging_without_journaled_payload", "f_race")
                )

                # A concurrent upload wins the attachment row while the stale
                # reconciliation is paused before its conditional update.
                result = self.upload(
                    self.content_a["id"], b"CONCURRENT-GOOD-PAYLOAD", "op_race", "f_race"
                )
                self.assertEqual(result["attachment"]["state"], "READY")
            finally:
                release.set()
                thread.join(timeout=15)

        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")
        report = holder["report"]
        self.assertFalse(report["ok"])
        self.assertIn(
            "staging_without_journaled_payload",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertIn(
            "skipped_stale_reconciliation",
            {action["action"] for action in report["actions"]},
        )
        self.assertEqual(self.attachment_row("f_race")["state"], "READY")
        self.assertEqual(self.object_bytes("f_race"), b"CONCURRENT-GOOD-PAYLOAD")
        self.assertEqual(self.staging_files(), [])
        self.assertEqual(len(self.quarantine_files()), 1)

        # The next scan resolves the attachment finding but keeps the durable
        # quarantine inventory row: the file is still isolated.
        final = self.attachments.reconcile()
        self.assertTrue(final["ok"])
        self.assertEqual(
            self.issue_row("staging_without_journaled_payload", "f_race")["state"],
            "resolved",
        )
        ledger = self.issue_row(
            "quarantine_file_isolated", self.quarantine_files()[0].name
        )
        self.assertEqual(ledger["state"], "open")
        self.assertEqual(final["quarantine_inventory_count"], 1)

    # ------------------------------------------------------------------
    # 4. QUARANTINED rows are never revived; active objects are moved out.
    # ------------------------------------------------------------------
    def test_quarantined_attachment_is_not_revived_and_active_object_is_moved(self):
        payload = b"QUARANTINED-ATTACHMENT-BYTES"
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_quarantined",
            attachment_id="f_quarantined",
            filename="slice-b.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        self.set_attachment_quarantined("f_quarantined", payload)
        object_path = self.data_dir / "objects" / "f_quarantined"
        object_path.write_bytes(payload)

        report = self.attachments.reconcile()
        self.assertFalse(report["ok"])
        self.assertFalse(object_path.exists())
        self.assertEqual(self.attachment_row("f_quarantined")["state"], "QUARANTINED")
        self.assertEqual(len(self.quarantine_files()), 1)
        self.assertEqual(self.quarantine_files()[0].read_bytes(), payload)
        ledger = self.issue_row(
            "quarantine_file_isolated", self.quarantine_files()[0].name
        )
        self.assertEqual(
            json.loads(ledger["detail"])["reason"], "quarantined_attachment_object"
        )

        self.assertIsNotNone(
            self.issue_row("quarantined_attachment_object_moved", "f_quarantined")
        )
        pending_issue = self.issue_row(
            "quarantined_attachment_pending_operation", "f_quarantined"
        )
        self.assertIsNotNone(pending_issue)
        self.assertEqual(pending_issue["state"], "open")
        self.assertIn(
            "skipped_quarantined_attachment",
            {action["action"] for action in report["actions"]},
        )
        self.assertEqual(len(report["quarantined_attachments"]), 1)
        entry = report["quarantined_attachments"][0]
        self.assertEqual(entry["id"], "f_quarantined")
        self.assertEqual(entry["disposition"], "moved_to_quarantine")
        self.assertTrue(entry["active_object_present"])

        # A quarantined attachment is not served through either access path.
        with self.assertRaises(NotFoundError):
            self.attachments.download_target("f_quarantined", owner=True)
        with self.assertRaises(NotFoundError):
            self.attachments.download_target("f_quarantined", owner=False)

        second = self.attachments.reconcile()
        self.assertEqual(self.attachment_row("f_quarantined")["state"], "QUARANTINED")
        self.assertEqual(len(self.quarantine_files()), 1)
        self.assertEqual(
            self.issue_row("quarantined_attachment_object_moved", "f_quarantined")[
                "state"
            ],
            "resolved",
        )
        self.assertEqual(len(second["quarantined_attachments"]), 1)
        self.assertEqual(
            second["quarantined_attachments"][0]["disposition"], "no_active_object"
        )
        self.assertEqual(second["quarantine_inventory_count"], 1)

        # Once the linked operation is terminal, the "pending operation"
        # finding closes; the attachment and inventory record remain.
        self.set_operation_state("op_quarantined", "FAILED", "manual terminal")
        third = self.attachments.reconcile()
        self.assertTrue(third["ok"])
        self.assertEqual(
            self.issue_row(
                "quarantined_attachment_pending_operation", "f_quarantined"
            )["state"],
            "resolved",
        )
        self.assertEqual(self.attachment_row("f_quarantined")["state"], "QUARANTINED")
        self.assertEqual(third["quarantine_inventory_count"], 1)

    # ------------------------------------------------------------------
    # 5. An untracked quarantine file is registered, never moved or deleted.
    # ------------------------------------------------------------------
    def test_untracked_quarantine_file_is_registered_and_repeatable(self):
        payload = b"MANUAL-QUARANTINE-BYTES"
        manual = self.data_dir / "quarantine" / "manual.bin"
        manual.write_bytes(payload)

        report = self.attachments.reconcile()
        self.assertFalse(report["ok"])
        self.assertIn(
            "quarantine_file_untracked",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertTrue(manual.is_file())
        self.assertEqual(manual.read_bytes(), payload)

        ledger = self.issue_row("quarantine_file_isolated", "manual.bin")
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["state"], "open")
        detail = json.loads(ledger["detail"])
        self.assertEqual(detail["origin"], "unknown")
        self.assertEqual(detail["reason"], "untracked")
        self.assertIsNone(detail["original_name"])
        self.assertEqual(detail["bytes"], len(payload))
        self.assertEqual(detail["sha256"], hashlib.sha256(payload).hexdigest())

        entry = report["quarantine_inventory"][0]
        self.assertFalse(entry["tracked_before_scan"])
        self.assertTrue(entry["registered_this_run"])

        second = self.attachments.reconcile()
        self.assertTrue(manual.is_file())
        self.assertEqual(manual.read_bytes(), payload)
        self.assertEqual(
            self.issue_row("quarantine_file_untracked", "manual.bin")["state"],
            "resolved",
        )
        again = self.issue_row("quarantine_file_isolated", "manual.bin")
        self.assertEqual(again["first_seen"], ledger["first_seen"])
        self.assertEqual(again["last_seen"], ledger["last_seen"])
        self.assertEqual(again["detail"], ledger["detail"])
        self.assertTrue(second["quarantine_inventory"][0]["tracked_before_scan"])
        self.assertFalse(second["quarantine_inventory"][0]["registered_this_run"])

    # ------------------------------------------------------------------
    # 6. READY missing and hash-mismatch findings stay visible and preserve
    #    all bytes across repeated reconciles.
    # ------------------------------------------------------------------
    def test_ready_missing_and_hash_mismatch_remain_non_destructive(self):
        self.upload(self.content_a["id"], b"MISSING-BYTES", "op_missing", "f_missing")
        self.upload(self.content_a["id"], b"CORRUPT-BYTES", "op_corrupt", "f_corrupt")
        (self.data_dir / "objects" / "f_missing").unlink()
        (self.data_dir / "objects" / "f_corrupt").write_bytes(b"TAMPERED-BYTES")

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        first_kinds = {issue["kind"] for issue in first["issues"]}
        self.assertIn("ready_file_missing", first_kinds)
        self.assertIn("ready_hash_mismatch", first_kinds)
        self.assertEqual(self.attachment_row("f_missing")["state"], "BROKEN")
        self.assertEqual(self.attachment_row("f_corrupt")["state"], "BROKEN")
        self.assertFalse((self.data_dir / "objects" / "f_missing").exists())
        self.assertEqual(self.object_bytes("f_corrupt"), b"TAMPERED-BYTES")
        self.assertEqual(self.quarantine_files(), [])

        second = self.attachments.reconcile()
        self.assertFalse(second["ok"])
        second_kinds = {issue["kind"] for issue in second["issues"]}
        self.assertIn("broken_resource_missing", second_kinds)
        self.assertIn("ready_hash_mismatch", second_kinds)
        self.assertFalse((self.data_dir / "objects" / "f_missing").exists())
        self.assertEqual(self.object_bytes("f_corrupt"), b"TAMPERED-BYTES")
        self.assertEqual(self.quarantine_files(), [])
        self.assertEqual(
            self.issue_row("ready_file_missing", "f_missing")["state"], "open"
        )
        self.assertEqual(
            self.issue_row("ready_hash_mismatch", "f_corrupt")["state"], "open"
        )
        self.assertEqual(second["quarantine_inventory_count"], 0)

    # ------------------------------------------------------------------
    # 7. B-REV-01: a quarantine target collision must not overwrite evidence.
    # ------------------------------------------------------------------
    def test_quarantine_target_collision_preserves_existing_file(self):
        old_payload = b"OLD-QUARANTINE-EVIDENCE"
        new_payload = b"NEW-STAGING-BYTES"

        # First reconcile creates a legitimate isolated file and its ledger.
        first_stage = self.data_dir / "staging" / "stale.part"
        first_stage.write_bytes(old_payload)
        self.attachments.reconcile()
        existing_files = self.quarantine_files()
        self.assertEqual(len(existing_files), 1)
        existing_path = existing_files[0]
        self.assertEqual(existing_path.read_bytes(), old_payload)
        existing_ledger = self.issue_row(
            "quarantine_file_isolated", existing_path.name
        )
        self.assertIsNotNone(existing_ledger)
        self.assertEqual(existing_ledger["state"], "open")
        existing_ledger_before = dict(existing_ledger)

        collision_hex = existing_path.name.rsplit(".", 1)[-1]
        self.assertEqual(len(collision_hex), 32)
        fresh_hex = "0" * 32
        if fresh_hex == collision_hex:
            fresh_hex = "f" * 32

        # A second stale staging file has the same original name. The first
        # candidate target is forced to collide with the existing file; the
        # retry must use a different name.
        second_stage = self.data_dir / "staging" / "stale.part"
        second_stage.write_bytes(new_payload)

        class FakeUUID:
            def __init__(self, hex_value):
                self.hex = hex_value

        generated = [FakeUUID(collision_hex), FakeUUID(fresh_hex)]
        with mock.patch.object(
            attachments_module.uuid, "uuid4", side_effect=generated
        ):
            report = AttachmentStore(self.data_dir).reconcile()

        # The pre-existing isolated bytes and ledger are untouched.
        self.assertEqual(existing_path.read_bytes(), old_payload)
        self.assertEqual(len(existing_path.read_bytes()), len(old_payload))
        existing_ledger_after = self.issue_row(
            "quarantine_file_isolated", existing_path.name
        )
        self.assertIsNotNone(existing_ledger_after)
        self.assertEqual(existing_ledger_after["state"], "open")
        self.assertEqual(
            existing_ledger_after["first_seen"], existing_ledger_before["first_seen"]
        )
        self.assertEqual(
            existing_ledger_after["last_seen"], existing_ledger_before["last_seen"]
        )
        self.assertEqual(
            existing_ledger_after["detail"], existing_ledger_before["detail"]
        )

        # The new file is isolated under the non-colliding second candidate.
        expected_new_name = f"staging-stale.part.{fresh_hex}"
        new_path = self.data_dir / "quarantine" / expected_new_name
        self.assertTrue(new_path.is_file())
        self.assertEqual(new_path.read_bytes(), new_payload)
        self.assertEqual(len(self.quarantine_files()), 2)
        self.assertEqual(self.staging_files(), [])
        self.assertIn(expected_new_name, report["quarantined"])
        self.assertNotIn(existing_path.name, report["quarantined"])
        self.assertEqual(report["quarantine_inventory_count"], 2)

        new_ledger = self.issue_row("quarantine_file_isolated", expected_new_name)
        self.assertIsNotNone(new_ledger)
        detail = json.loads(new_ledger["detail"])
        self.assertEqual(detail["origin"], "staging")
        self.assertEqual(detail["reason"], "stale_staging")
        self.assertEqual(detail["original_name"], "stale.part")
        self.assertEqual(detail["bytes"], len(new_payload))
        self.assertEqual(detail["sha256"], hashlib.sha256(new_payload).hexdigest())


class ReconcileResilienceTestCase(unittest.TestCase):
    """B1 / R-01 + R-16: one bad item must not hide the rest of the audit."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B1 韧性测试")["id"]
        self.upload("att-a", "op-a", b"RESILIENCE-A")
        self.upload("att-b", "op-b", b"RESILIENCE-B")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, attachment_id, operation_id, payload):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b1.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def issue_row(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def test_directory_object_is_reported_and_scan_continues(self):
        object_a = self.data_dir / "objects" / "att-a"
        saved = object_a.read_bytes()
        object_a.unlink()
        object_a.mkdir()

        report = self.attachments.reconcile()

        self.assertTrue(report["partial"])
        self.assertFalse(report["ok"])
        pairs = {(issue["kind"], issue["target"]) for issue in report["issues"]}
        self.assertIn(("attachment_object_not_regular", "att-a"), pairs)
        self.assertNotIn("att-b", {issue["target"] for issue in report["issues"]})
        error = report["errors"][0]
        self.assertEqual(error["target"], "att-a")
        self.assertEqual(error["status"], "not_regular")
        # The error record must not leak the data directory location.
        self.assertNotIn(str(self.data_dir), json.dumps(error))
        # The uninspectable item is left exactly as it was.
        self.assertEqual(self.row("att-a")["state"], "READY")
        self.assertEqual(self.row("att-b")["state"], "READY")

        stored = json.loads(
            get_meta(self.data_dir / "archive.db", "last_reconcile_report")
        )
        self.assertTrue(stored["partial"])
        self.assertFalse(stored["ok"])

        # Once the object is a readable regular file again, the finding closes.
        object_a.rmdir()
        object_a.write_bytes(saved)
        second = self.attachments.reconcile()
        self.assertFalse(second["partial"])
        self.assertTrue(second["ok"])
        self.assertEqual(
            self.issue_row("attachment_object_not_regular", "att-a")["state"],
            "resolved",
        )

    def test_hash_failure_is_isolated_to_one_item(self):
        real_open = Path.open

        def flaky_open(self_path, *args, **kwargs):
            if self_path.name == "att-a":
                raise PermissionError(13, "injected hash failure")
            return real_open(self_path, *args, **kwargs)

        with mock.patch.object(Path, "open", flaky_open):
            report = self.attachments.reconcile()

        self.assertTrue(report["partial"])
        self.assertFalse(report["ok"])
        pairs = {(issue["kind"], issue["target"]) for issue in report["issues"]}
        self.assertIn(("attachment_object_hash_failed", "att-a"), pairs)
        self.assertEqual(report["errors"][0]["error_class"], "PermissionError")
        self.assertEqual(report["errors"][0]["errno"], 13)
        self.assertEqual(self.row("att-a")["state"], "READY")
        self.assertEqual(self.row("att-b")["state"], "READY")

    def test_report_write_failure_rolls_back_its_issues(self):
        # A stale staging file is judged only by the final _record_issues()
        # transaction, so a report failure there must roll its finding back.
        stage = self.data_dir / "staging" / "b1-stale.0001.part"
        stage.write_bytes(b"STALE-B1")
        real_dumps = json.dumps

        def failing(value, *args, **kwargs):
            if isinstance(value, dict) and "checked_at" in value:
                raise ValueError("injected report failure")
            return real_dumps(value, *args, **kwargs)

        with mock.patch.object(
            attachments_module.json, "dumps", side_effect=failing
        ):
            with self.assertRaises(ValueError):
                self.attachments.reconcile()

        # The finding and the report are one transaction: neither is visible
        # and the caller sees an error instead of a silently stale report.
        self.assertIsNone(
            get_meta(self.data_dir / "archive.db", "last_reconcile_report")
        )
        self.assertIsNone(
            self.issue_row("stale_staging_quarantined", "b1-stale.0001.part")
        )

    # ------------------------------------------------------------------
    # B10-B / R-16: recoverable report commit protocol
    # ------------------------------------------------------------------
    def _force_report_failure(self):
        real_dumps = json.dumps

        def failing(value, *args, **kwargs):
            if isinstance(value, dict) and "checked_at" in value:
                raise ValueError("injected report failure")
            return real_dumps(value, *args, **kwargs)

        return mock.patch.object(attachments_module.json, "dumps", side_effect=failing)

    def test_successful_reconcile_records_completed_attempt(self):
        report = self.attachments.reconcile()
        self.assertTrue(report["ok"])
        self.assertEqual(report["commit"]["phase"], "committed")
        self.assertIs(report["commit"]["report_atomic"], True)

        integrity = self.attachments.get_integrity()
        self.assertFalse(integrity["last_report_stale"])
        attempt = integrity["reconcile_attempt"]
        self.assertEqual(attempt["state"], "completed")
        self.assertEqual(attempt["phase"], "completed")
        self.assertEqual(
            attempt["report_attempt_id"], report["commit"]["attempt_id"]
        )

    def test_report_failure_marks_attempt_failed_and_report_stale(self):
        stage = self.data_dir / "staging" / "b10-stale.0001.part"
        stage.write_bytes(b"STALE-B10")

        with self._force_report_failure():
            with self.assertRaises(ValueError):
                self.attachments.reconcile()

        integrity = self.attachments.get_integrity()
        self.assertTrue(integrity["last_report_stale"])
        self.assertIsNone(integrity["last_report"])
        attempt = integrity["reconcile_attempt"]
        self.assertEqual(attempt["phase"], "failed")
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(attempt["failure_phase"], "report_commit")
        self.assertEqual(attempt["error_class"], "ValueError")
        self.assertFalse(attempt["report_committed"])

    def test_early_commits_survive_report_failure(self):
        # A journal-less staging file is quarantined and its finding is written
        # *before* the final report transaction, so a report failure leaves
        # durable state that must stay visible instead of being rolled back.
        self.attachments._prepare_pending(
            content_id=self.content,
            operation_id="op_b10_early",
            attachment_id="f_b10_early",
            filename="b10.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        stage = self.data_dir / "staging" / "op_b10_early.aaaaaaaa.part"
        stage.write_bytes(b"EARLY-COMMIT-BYTES")

        with self._force_report_failure():
            with self.assertRaises(ValueError):
                self.attachments.reconcile()

        self.assertIsNotNone(
            self.issue_row("staging_without_journaled_payload", "f_b10_early")
        )
        moved = sorted(
            path
            for path in (self.data_dir / "quarantine").iterdir()
            if path.is_file()
        )
        self.assertEqual(len(moved), 1)
        self.assertIsNotNone(
            self.issue_row("quarantine_file_isolated", moved[0].name)
        )
        self.assertEqual(self.row("f_b10_early")["state"], "BROKEN")
        self.assertIsNone(
            get_meta(self.data_dir / "archive.db", "last_reconcile_report")
        )
        integrity = self.attachments.get_integrity()
        self.assertTrue(integrity["last_report_stale"])
        self.assertEqual(integrity["reconcile_attempt"]["phase"], "failed")
        self.assertGreaterEqual(
            integrity["reconcile_attempt"]["early_commits"], 1
        )

    def test_process_exit_during_report_commit_stays_stale_until_reconciled(self):
        first = self.attachments.reconcile()
        self.assertTrue(first["ok"])

        def crash(store_obj, *args, **kwargs):
            raise SystemExit("simulated process exit during report commit")

        with mock.patch.object(AttachmentStore, "_record_issues", crash):
            with self.assertRaises(SystemExit):
                AttachmentStore(self.data_dir).reconcile()

        restarted = AttachmentStore(self.data_dir)
        integrity = restarted.get_integrity()
        self.assertTrue(integrity["last_report_stale"])
        self.assertEqual(integrity["reconcile_attempt"]["phase"], "failed")
        self.assertEqual(
            integrity["reconcile_attempt"]["failure_phase"], "report_commit"
        )

        recovered = restarted.reconcile()
        self.assertTrue(recovered["ok"])
        integrity = restarted.get_integrity()
        self.assertFalse(integrity["last_report_stale"])
        self.assertEqual(integrity["reconcile_attempt"]["phase"], "completed")

    def test_crash_leftover_commit_marker_is_stale_then_recovered(self):
        first = self.attachments.reconcile()
        self.assertTrue(first["ok"])
        # The marker a hard process death would leave between the phase switch
        # and the final transaction.
        set_meta(
            self.data_dir / "archive.db",
            "reconcile_attempt",
            json.dumps(
                {
                    "attempt_id": "rec_crash_leftover",
                    "started_at": utcnow(),
                    "phase": "committing_report",
                    "state": "in_progress",
                    "early_commits": 0,
                }
            ),
        )

        restarted = AttachmentStore(self.data_dir)
        integrity = restarted.get_integrity()
        self.assertTrue(integrity["last_report_stale"])
        self.assertEqual(
            integrity["reconcile_attempt"]["phase"], "committing_report"
        )
        self.assertTrue(integrity["last_report"]["ok"])

        recovered = restarted.reconcile()
        self.assertTrue(recovered["ok"])
        integrity = restarted.get_integrity()
        self.assertFalse(integrity["last_report_stale"])
        self.assertEqual(
            integrity["reconcile_attempt"]["report_attempt_id"],
            integrity["last_report"]["commit"]["attempt_id"],
        )


class RecoveryStateMachineTestCase(unittest.TestCase):
    """B2 / R-02 + R-07 + R-14: retained bytes and an honest state machine."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B2 恢复测试")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, operation_id, attachment_id, payload):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b2.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def operation_row(self, operation_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def issue_row(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def staging_files(self, operation_id):
        return sorted((self.data_dir / "staging").glob(f"{operation_id}.*.part"))

    def test_promotion_failure_retains_verified_staging_bytes(self):
        payload = b"B2-PROMOTION-FAILURE"
        real_replace = os.replace

        def failing_replace(src, dst):
            if Path(dst).parent.name == "objects":
                raise PermissionError(13, "injected promotion failure")
            return real_replace(src, dst)

        with mock.patch.object(
            attachments_module.os, "replace", side_effect=failing_replace
        ):
            with self.assertRaises(PermissionError):
                self.upload("op_promote", "att-promote", payload)

        staged = self.staging_files("op_promote")
        self.assertEqual(len(staged), 1)
        # The bytes were verified before promotion and must be byte-identical.
        self.assertEqual(staged[0].read_bytes(), payload)
        self.assertEqual(
            hashlib.sha256(staged[0].read_bytes()).hexdigest(),
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertFalse((self.data_dir / "objects" / "att-promote").exists())
        self.assertEqual(self.operation_row("op_promote")["state"], "UNCERTAIN")
        self.assertEqual(self.row("att-promote")["state"], "PENDING")
        issue = self.issue_row(
            "staging_retained_after_promotion_failure", staged[0].name
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue["state"], "open")
        self.assertNotIn(str(self.data_dir), issue["detail"])

        # A restart/scan recovers the journaled payload without re-upload.
        report = self.attachments.reconcile()
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(self.row("att-promote")["state"], "READY")
        self.assertEqual(self.operation_row("op_promote")["state"], "COMPLETED")
        self.assertEqual(
            (self.data_dir / "objects" / "att-promote").read_bytes(), payload
        )
        self.assertEqual(self.staging_files("op_promote"), [])

    def test_journal_failure_retains_staging_and_reports_it(self):
        payload = b"B2-JOURNAL-FAILURE"
        real_transaction = attachments_module.transaction
        calls = {"count": 0}

        def flaky_transaction(path):
            calls["count"] += 1
            if calls["count"] == 2:
                raise sqlite3.OperationalError("injected journal failure")
            return real_transaction(path)

        with mock.patch.object(
            attachments_module, "transaction", side_effect=flaky_transaction
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.upload("op_journal", "att-journal", payload)

        staged = self.staging_files("op_journal")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_bytes(), payload)
        self.assertEqual(self.operation_row("op_journal")["state"], "UNCERTAIN")
        self.assertEqual(self.row("att-journal")["state"], "PENDING")
        issue = self.issue_row("staging_unjournaled_retained", staged[0].name)
        self.assertIsNotNone(issue)
        detail = json.loads(issue["detail"])
        self.assertEqual(detail["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(detail["bytes"], len(payload))
        self.assertEqual(detail["error_class"], "OperationalError")
        self.assertNotIn(str(self.data_dir), issue["detail"])

        # Reconcile never promotes unjournaled bytes; it quarantines them.
        self.attachments.reconcile()
        self.assertEqual(self.staging_files("op_journal"), [])
        self.assertFalse((self.data_dir / "objects" / "att-journal").exists())

    def test_retry_failure_does_not_downgrade_uncertain(self):
        payload = b"B2-UNCERTAIN-KEPT"
        real_replace = os.replace

        def failing_replace(src, dst):
            if Path(dst).parent.name == "objects":
                raise PermissionError(13, "injected promotion failure")
            return real_replace(src, dst)

        with mock.patch.object(
            attachments_module.os, "replace", side_effect=failing_replace
        ):
            with self.assertRaises(PermissionError):
                self.upload("op_keep", "att-keep", payload)
        self.assertEqual(self.operation_row("op_keep")["state"], "UNCERTAIN")

        class BrokenStream(io.BytesIO):
            def read(self, size=-1):
                raise OSError("injected retry read failure")

        with self.assertRaises(OSError):
            self.attachments.upload(
                BrokenStream(),
                filename="b2.bin",
                content_id=self.content,
                operation_id="op_keep",
                attachment_id="att-keep",
            )
        self.assertEqual(self.operation_row("op_keep")["state"], "UNCERTAIN")

    def test_bookkeeping_failure_is_logged_not_swallowed(self):
        with mock.patch.object(
            attachments_module,
            "transaction",
            side_effect=sqlite3.OperationalError("injected bookkeeping failure"),
        ):
            with self.assertLogs("archive.attachments", level="WARNING") as logs:
                self.attachments._mark_failed("att-x", "op-x", "note")
        self.assertTrue(
            any("bookkeeping failed" in message for message in logs.output)
        )


class RepairAndOperationRecoveryTestCase(unittest.TestCase):
    """B3 / R-03 + R-08: corrupt objects are quarantined, retries resume."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B3 修复测试")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, operation_id, attachment_id, payload):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b3.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def issue_row(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def attachment_count(self):
        conn = connect(self.data_dir / "archive.db")
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
        finally:
            conn.close()

    def quarantine_ledger(self):
        conn = connect(self.data_dir / "archive.db")
        try:
            rows = conn.execute(
                "SELECT target, detail FROM integrity_issues "
                "WHERE kind = 'quarantine_file_isolated'"
            ).fetchall()
            return [(row["target"], json.loads(row["detail"])) for row in rows]
        finally:
            conn.close()

    def test_broken_corrupt_object_is_quarantined_then_repaired(self):
        payload = b"B3-ORIGINAL-BYTES"
        self.upload("op_repair", "att-repair", payload)
        target = self.data_dir / "objects" / "att-repair"
        target.write_bytes(b"Z" * len(payload))

        report = self.attachments.reconcile()
        self.assertEqual(self.row("att-repair")["state"], "BROKEN")
        self.assertIn(
            "ready_hash_mismatch",
            {issue["kind"] for issue in report["issues"]},
        )

        result = self.upload("op_repair", "att-repair", payload)

        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(target.read_bytes(), payload)
        # The corrupt object was moved aside, never deleted or overwritten.
        ledger = self.quarantine_ledger()
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger[0][0].startswith("object-att-repair."))
        self.assertEqual(ledger[0][1]["original_name"], "att-repair")
        self.assertEqual(ledger[0][1]["reason"], "corrupt_object_replaced")
        self.assertEqual(
            ledger[0][1]["sha256"], hashlib.sha256(b"Z" * len(payload)).hexdigest()
        )
        repair_issue = self.issue_row(
            "broken_object_quarantined_for_repair", "att-repair"
        )
        self.assertIsNotNone(repair_issue)
        self.assertEqual(repair_issue["state"], "open")

        self.attachments.reconcile()
        self.assertEqual(
            self.issue_row("broken_object_quarantined_for_repair", "att-repair")[
                "state"
            ],
            "resolved",
        )

    def test_ready_object_is_never_replaced_by_upload(self):
        payload = b"B3-READY-BYTES"
        self.upload("op_ready", "att-ready", payload)
        target = self.data_dir / "objects" / "att-ready"
        corrupt = b"Q" * len(payload)
        target.write_bytes(corrupt)

        with self.assertRaises(ConflictError):
            self.upload("op_ready", "att-ready", payload)

        # Nothing was overwritten and nothing moved while the claim was READY.
        self.assertEqual(target.read_bytes(), corrupt)
        self.assertEqual(self.quarantine_ledger(), [])
        self.assertEqual(self.row("att-ready")["state"], "BROKEN")

        # Only the follow-up retry, on a BROKEN row, may repair.
        result = self.upload("op_ready", "att-ready", payload)
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(target.read_bytes(), payload)

    def test_retry_without_attachment_id_reuses_operation_resource(self):
        payload = b"B3-RESUME-OP"
        first = self.upload("op_resume", None, payload)
        first_id = first["attachment"]["id"]
        (self.data_dir / "objects" / first_id).unlink()
        self.attachments.reconcile()
        self.assertEqual(self.row(first_id)["state"], "BROKEN")

        second = self.upload("op_resume", None, payload)

        self.assertEqual(second["attachment"]["id"], first_id)
        self.assertEqual(self.attachment_count(), 1)
        self.assertEqual(self.row(first_id)["state"], "READY")
        self.assertEqual(
            (self.data_dir / "objects" / first_id).read_bytes(), payload
        )


class DownloadIntegrityTestCase(unittest.TestCase):
    """B4 / R-04 + R-15: READY is verified on read and never escapes the store."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B4 下载校验")["id"]
        self.payload = b"B4-VERIFIED-BYTES"
        result = self.attachments.upload(
            io.BytesIO(self.payload),
            filename="b4.bin",
            content_id=self.content,
            operation_id="op_download",
            attachment_id="att-download",
        )
        self.attachment_id = result["attachment"]["id"]
        self.target = self.data_dir / "objects" / self.attachment_id
        self.client = self.app.test_client()
        self.client.get(f"/auth?token={TOKEN}")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_intact_download_still_works(self):
        path, filename, mime = self.attachments.download_target(
            self.attachment_id, owner=True
        )
        self.assertEqual(path.read_bytes(), self.payload)
        self.assertEqual(filename, "b4.bin")
        response = self.client.get(f"/api/attachments/{self.attachment_id}/file")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, self.payload)

    def test_tampered_ready_object_is_refused(self):
        self.target.write_bytes(b"X" * len(self.payload))
        with self.assertRaises(AttachmentUnverifiedError):
            self.attachments.download_target(self.attachment_id, owner=True)
        response = self.client.get(f"/api/attachments/{self.attachment_id}/file")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "attachment_unverified")
        self.assertNotIn(b"XXXX", response.data)

    def test_size_mismatch_is_refused(self):
        self.target.write_bytes(self.payload + b"EXTRA")
        with self.assertRaises(AttachmentUnverifiedError):
            self.attachments.download_target(self.attachment_id, owner=True)

    def test_directory_at_object_path_is_refused(self):
        self.target.unlink()
        self.target.mkdir()
        with self.assertRaises(AttachmentUnverifiedError):
            self.attachments.download_target(self.attachment_id, owner=True)

    def test_symlink_or_junction_is_never_followed(self):
        outside = Path(self.tempdir.name) / "outside.bin"
        outside.write_bytes(self.payload)
        original = self.target.read_bytes()
        self.target.unlink()
        try:
            os.symlink(outside, self.target)
        except (OSError, NotImplementedError) as exc:
            self.target.write_bytes(original)
            self.skipTest(f"symlink creation is not available: {exc}")
        try:
            if not self.target.is_symlink():
                self.skipTest("object path is not reported as a symlink")
            with self.assertRaises(AttachmentUnverifiedError):
                self.attachments.download_target(self.attachment_id, owner=True)
            response = self.client.get(f"/api/attachments/{self.attachment_id}/file")
            self.assertEqual(response.status_code, 409)
        finally:
            if self.target.is_symlink() or self.target.exists():
                self.target.unlink(missing_ok=True)
            self.target.write_bytes(original)


    def test_windows_directory_junction_is_never_followed(self):
        if not sys.platform.startswith("win"):
            self.skipTest("junction creation is Windows-only")
        outside = Path(self.tempdir.name) / "outside_dir"
        outside.mkdir()
        (outside / "payload.bin").write_bytes(self.payload)
        original = self.target.read_bytes()
        self.target.unlink()
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.target), str(outside)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.target.write_bytes(original)
            self.skipTest(f"junction creation failed: {created.stderr or created.stdout}")
        try:
            # The junction resolves to a directory holding a byte-identical
            # file. It must still be rejected: the object path itself is not a
            # regular file.
            with self.assertRaises(AttachmentUnverifiedError):
                self.attachments.download_target(self.attachment_id, owner=True)
            response = self.client.get(f"/api/attachments/{self.attachment_id}/file")
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(self.payload, response.data)
        finally:
            # A junction is a directory reparse point: it must be removed with
            # rmdir, and path.exists() reports it as absent.
            if os.path.lexists(self.target):
                os.rmdir(self.target)
            self.target.write_bytes(original)


class OperationIssueClosureTestCase(unittest.TestCase):
    """B10-C / AUD-01: operation-level staging findings can be closed."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B10-C 内容")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    class _UnstatPath:
        """Path-like object whose stat() fails, as a locked staging file would."""

        def __init__(self, name: str) -> None:
            self.name = name

        def stat(self):
            raise OSError(13, "access denied")

    def prepare(self, operation_id, attachment_id, payload=b"B10C"):
        self.attachments._prepare_pending(
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
            filename="b10c.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE operations SET payload_sha256 = ?, payload_size = ? WHERE id = ?",
                (hashlib.sha256(payload).hexdigest(), len(payload), operation_id),
            )
            conn.commit()
        finally:
            conn.close()

    def issue_row(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def issue_count(self, kind, target):
        conn = connect(self.data_dir / "archive.db")
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM integrity_issues WHERE kind = ? AND target = ?",
                (kind, target),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_operation_issue_closes_when_staging_settles(self):
        self.prepare("op_b10c_one", "f_b10c_one")
        fake = self._UnstatPath("op_b10c_one.dead.part")
        original = AttachmentStore._staging_snapshot
        with mock.patch.object(
            AttachmentStore,
            "_staging_snapshot",
            lambda store, op: [fake] if op == "op_b10c_one" else original(store, op),
        ):
            first = self.attachments.reconcile()

        self.assertFalse(first["ok"])
        row = self.issue_row("staging_file_uninspectable", "op_b10c_one")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "open")
        self.assertEqual(first["errors"][0]["target"], "op_b10c_one")

        # A restart plus a scan that settles the same staging area closes the
        # finding while keeping its audit history.
        restarted = AttachmentStore(self.data_dir)
        restarted.reconcile()
        row = self.issue_row("staging_file_uninspectable", "op_b10c_one")
        self.assertEqual(row["state"], "resolved")
        self.assertIsNotNone(row["resolved_at"])

        # Repeating the reconciled state is idempotent.
        restarted.reconcile()
        self.assertEqual(
            self.issue_count("staging_file_uninspectable", "op_b10c_one"), 1
        )
        self.assertEqual(
            self.issue_row("staging_file_uninspectable", "op_b10c_one")["state"],
            "resolved",
        )

    def test_operation_issues_do_not_cross_close(self):
        self.prepare("op_b10c_a", "f_b10c_a")
        self.prepare("op_b10c_b", "f_b10c_b")
        fake_a = self._UnstatPath("op_b10c_a.dead.part")
        fake_b = self._UnstatPath("op_b10c_b.dead.part")
        original = AttachmentStore._staging_snapshot

        def both_failing(store, op):
            if op == "op_b10c_a":
                return [fake_a]
            if op == "op_b10c_b":
                return [fake_b]
            return original(store, op)

        with mock.patch.object(AttachmentStore, "_staging_snapshot", both_failing):
            self.attachments.reconcile()
        self.assertEqual(
            self.issue_row("staging_file_uninspectable", "op_b10c_a")["state"], "open"
        )
        self.assertEqual(
            self.issue_row("staging_file_uninspectable", "op_b10c_b")["state"], "open"
        )

        def only_a_failing(store, op):
            if op == "op_b10c_a":
                return [fake_a]
            return original(store, op)

        with mock.patch.object(AttachmentStore, "_staging_snapshot", only_a_failing):
            AttachmentStore(self.data_dir).reconcile()
        self.assertEqual(
            self.issue_row("staging_file_uninspectable", "op_b10c_a")["state"], "open"
        )
        self.assertEqual(
            self.issue_row("staging_file_uninspectable", "op_b10c_b")["state"],
            "resolved",
        )

    @unittest.skipUnless(os.name == "nt", "junction requires Windows")
    def test_junction_staging_file_is_reported_and_closes_after_removal(self):
        self.prepare("op_b10c_j", "f_b10c_j")
        target = self.data_dir / "junction-target-c"
        target.mkdir()
        (target / "payload.bin").write_bytes(b"X")
        stage = self.data_dir / "staging" / "op_b10c_j.dead.part"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(stage), str(target)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest(f"junction creation failed: {created.stderr or created.stdout}")
        try:
            report = self.attachments.reconcile()
            row = self.issue_row("staging_file_uninspectable", "op_b10c_j")
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "open")
            self.assertIn(
                "staging_file_uninspectable",
                {issue["kind"] for issue in report["issues"]},
            )
        finally:
            os.rmdir(stage)

        restarted = AttachmentStore(self.data_dir)
        restarted.reconcile()
        row = self.issue_row("staging_file_uninspectable", "op_b10c_j")
        self.assertEqual(row["state"], "resolved")

    def test_legacy_operation_targeted_finding_closes_on_settled_staging(self):
        self.prepare("op_b10c_legacy", "f_b10c_legacy")
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "INSERT INTO integrity_issues(fingerprint, kind, target, detail, "
                "state, first_seen, last_seen) VALUES(?, ?, ?, ?, 'open', ?, ?)",
                (
                    "legacy-b10c-fingerprint",
                    "attachment_object_unreadable",
                    "op_b10c_legacy",
                    "legacy operation-targeted finding",
                    "2020-01-01T00:00:00Z",
                    "2020-01-01T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self.attachments.reconcile()
        row = self.issue_row("attachment_object_unreadable", "op_b10c_legacy")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "resolved")


class UploadObjectTypeTestCase(unittest.TestCase):
    """B10-D / AUD-03: the upload path never follows a non-regular object."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B10-D 内容")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, payload, operation_id, attachment_id):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b10d.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def attachment_row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def not_regular_issue(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM integrity_issues WHERE kind = ? AND target = ?",
                ("attachment_object_not_regular", attachment_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def object_path(self, attachment_id):
        return self.data_dir / "objects" / attachment_id

    def force_ready(self, attachment_id):
        """Restore the claim to READY so download itself is the code under test."""
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE attachments SET state = 'READY' WHERE id = ?",
                (attachment_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_normal_upload_reaches_ready_as_plain_file(self):
        payload = b"normal-plain-file-bytes"
        self.upload(payload, "op_b10d_ok", "f_b10d_ok")
        self.assertEqual(self.attachment_row("f_b10d_ok")["state"], "READY")
        info = self.object_path("f_b10d_ok").lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertFalse(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        target, _filename, _mime = self.attachments.download_target(
            "f_b10d_ok", owner=True
        )
        self.assertEqual(target, self.object_path("f_b10d_ok"))
        self.assertEqual(target.read_bytes(), payload)

    def test_upload_retry_rejects_symlinked_object(self):
        payload = b"identical bytes behind a link"
        self.upload(payload, "op_b10d_link", "f_b10d_link")
        object_path = self.object_path("f_b10d_link")
        outside = self.data_dir / "outside-copy.bin"
        outside.write_bytes(payload)
        object_path.unlink()
        try:
            os.symlink(outside, object_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            with self.assertRaises(ConflictError):
                self.upload(payload, "op_b10d_link", "f_b10d_link")
            self.assertEqual(self.attachment_row("f_b10d_link")["state"], "BROKEN")
            self.assertIsNotNone(self.not_regular_issue("f_b10d_link"))
            # The link is never followed, replaced or served.
            self.assertTrue(object_path.is_symlink())
            self.force_ready("f_b10d_link")
            with self.assertRaises(AttachmentUnverifiedError):
                self.attachments.download_target("f_b10d_link", owner=True)
        finally:
            if object_path.is_symlink():
                object_path.unlink()

    @unittest.skipUnless(os.name == "nt", "junction requires Windows")
    def test_upload_retry_rejects_junction_object(self):
        payload = b"junction object bytes"
        self.upload(payload, "op_b10d_junction", "f_b10d_junction")
        object_path = self.object_path("f_b10d_junction")
        target = self.data_dir / "junction-target-d"
        target.mkdir()
        (target / "payload.bin").write_bytes(payload)
        object_path.unlink()
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(object_path), str(target)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest(f"junction creation failed: {created.stderr or created.stdout}")
        try:
            with self.assertRaises(ConflictError):
                self.upload(payload, "op_b10d_junction", "f_b10d_junction")
            self.assertEqual(
                self.attachment_row("f_b10d_junction")["state"], "BROKEN"
            )
            self.assertIsNotNone(self.not_regular_issue("f_b10d_junction"))
            self.force_ready("f_b10d_junction")
            with self.assertRaises(AttachmentUnverifiedError):
                self.attachments.download_target("f_b10d_junction", owner=True)
        finally:
            os.rmdir(object_path)

    def test_probe_rejects_swap_between_lstat_and_open(self):
        payload = b"probe source bytes"
        path = self.data_dir / "probe-subject.bin"
        path.write_bytes(payload)
        other = self.data_dir / "probe-other.bin"
        other.write_bytes(b"different")
        real_lstat = Path.lstat

        def other_lstat(self_path):
            if self_path == path:
                # Report the identity of a different file: this is exactly the
                # state a swap between the check and the open would produce.
                return real_lstat(other)
            return real_lstat(self_path)

        with mock.patch.object(Path, "lstat", other_lstat):
            status, digest, size, _error = attachments_module._probe_object(path)
        self.assertEqual(status, "not_regular")
        self.assertIsNone(digest)
        self.assertIsNone(size)


class IdentifierRulesTestCase(unittest.TestCase):
    """B6 / R-06: ids must obey the same rules as Windows file names."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B6 标识规则")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, operation_id, attachment_id):
        return self.attachments.upload(
            io.BytesIO(b"B6"),
            filename="b6.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def attachment_count(self):
        conn = connect(self.data_dir / "archive.db")
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
        finally:
            conn.close()

    def test_trailing_newline_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.upload("op_nl", "att_nl\n")
        with self.assertRaises(ValidationError):
            self.upload("op_nl\n", "att_nl2")
        self.assertEqual(self.attachment_count(), 0)

    def test_trailing_dot_or_space_is_rejected(self):
        for bad in ("att.dot.", "att.space ", " att.lead"):
            with self.assertRaises(ValidationError):
                self.upload(f"op_{abs(hash(bad))}", bad)

    def test_windows_reserved_names_are_rejected(self):
        for index, bad in enumerate(("CON", "nul", "Aux", "COM1", "lpt9")):
            with self.assertRaises(ValidationError):
                self.upload(f"op_reserved_{index}", bad)
        self.assertEqual(self.attachment_count(), 0)

    def test_case_only_collision_is_rejected(self):
        self.upload("op_case_a", "Att-Case")
        with self.assertRaises(ConflictError):
            self.upload("op_case_b", "att-case")
        self.assertEqual(self.attachment_count(), 1)
        self.assertEqual(
            sorted(p.name for p in (self.data_dir / "objects").iterdir()),
            ["Att-Case"],
        )


class OperationHistoryAndGuardsTestCase(unittest.TestCase):
    """B7 / R-09 + R-10 + R-18: history is kept, quarantine wins, kind is checked."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B7 守卫测试")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, operation_id, attachment_id, payload, **kwargs):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b7.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
            **kwargs,
        )

    def row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def operation_row(self, operation_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def execute(self, sql, params=()):
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_rejected_retry_preserves_prior_state_and_result(self):
        with self.assertRaises(ValidationError):
            self.upload("op_hist", "att-hist", b"B7-HISTORY", expected_size=999)
        first = self.operation_row("op_hist")
        self.assertEqual(first["state"], "FAILED")
        self.assertIsNotNone(first["result"])

        with self.assertRaises(ValidationError):
            self.upload("op_hist", "att-hist", b"B7-HISTORY", expected_size=999)

        second = self.operation_row("op_hist")
        self.assertEqual(second["state"], first["state"])
        self.assertEqual(second["result"], first["result"])
        self.assertNotEqual(second["state"], "PENDING")

    def test_non_attachment_operation_kind_is_rejected(self):
        self.upload("op_kind", "att-kind", b"B7-KIND")
        self.execute(
            "UPDATE operations SET kind = 'release_publish' WHERE id = ?",
            ("op_kind",),
        )
        with self.assertRaises(ConflictError):
            self.upload("op_kind", "att-kind", b"B7-KIND")
        self.assertEqual(self.row("att-kind")["state"], "READY")
        self.assertEqual(self.operation_row("op_kind")["kind"], "release_publish")

    def test_quarantine_between_reservation_and_journal_wins(self):
        original = AttachmentStore._prepare_pending

        def prepare_then_quarantine(store, **kwargs):
            result = original(store, **kwargs)
            self.execute(
                "UPDATE attachments SET state = 'QUARANTINED' WHERE id = ?",
                (kwargs["attachment_id"],),
            )
            return result

        with mock.patch.object(
            AttachmentStore, "_prepare_pending", prepare_then_quarantine
        ):
            with self.assertRaises(ConflictError):
                self.upload("op_q1", "att-q1", b"B7-QUARANTINE")

        self.assertEqual(self.row("att-q1")["state"], "QUARANTINED")
        self.assertFalse((self.data_dir / "objects" / "att-q1").exists())

    def test_quarantine_during_ready_commit_wins(self):
        real_replace = os.replace

        def replace_then_quarantine(src, dst):
            result = real_replace(src, dst)
            if Path(dst).parent.name == "objects":
                self.execute(
                    "UPDATE attachments SET state = 'QUARANTINED' WHERE id = ?",
                    ("att-q2",),
                )
            return result

        with mock.patch.object(
            attachments_module.os, "replace", side_effect=replace_then_quarantine
        ):
            with self.assertRaises(ConflictError):
                self.upload("op_q2", "att-q2", b"B7-QUARANTINE-READY")

        self.assertEqual(self.row("att-q2")["state"], "QUARANTINED")
        self.assertNotEqual(self.operation_row("op_q2")["state"], "COMPLETED")


class ObservabilityAndCountsTestCase(unittest.TestCase):
    """B8 / R-13 + R-17: reports are path-free and counts stay separated."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content = self.store.create_content("B8 可观测性")["id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def upload(self, operation_id, attachment_id, payload):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="b8.bin",
            content_id=self.content,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def attachment_row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def operation_row(self, operation_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def test_reconcile_report_contains_no_local_path(self):
        stage = self.data_dir / "staging" / "b8-dead.part"
        stage.write_bytes(b"B8-REPORT-PATH")

        report = self.attachments.reconcile()

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(len(report["quarantined"]), 1)
        name = report["quarantined"][0]
        self.assertEqual(name, Path(name).name)
        self.assertEqual(
            report["quarantine_moves"],
            [{"name": name, "origin": "staging", "reason": "stale_staging"}],
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(str(self.data_dir), serialized)
        self.assertNotIn(self.tempdir.name, serialized)
        self.assertTrue((self.data_dir / "quarantine" / name).is_file())

    def test_safe_error_text_strips_absolute_paths(self):
        exc = OSError(
            13, "Permission denied", str(self.data_dir / "staging" / "secret.part")
        )
        text = attachments_module._safe_error_text(exc)
        self.assertIn("Error", text)
        self.assertIn("errno=13", text)
        self.assertIn("secret.part", text)
        self.assertNotIn(str(self.data_dir), text)
        self.assertNotIn(self.tempdir.name, text)

    def test_failure_note_never_persists_absolute_path(self):
        secret_path = str(self.data_dir / "staging" / "leaky.part")

        def failing_fsync(_descriptor):
            raise OSError(13, "Permission denied", secret_path)

        with mock.patch.object(
            attachments_module.os, "fsync", side_effect=failing_fsync
        ):
            with self.assertRaises(OSError):
                self.upload("op_b8_note", "att-note", b"B8-NOTE")

        attachment = self.attachment_row("att-note")
        operation = self.operation_row("op_b8_note")
        for text in (attachment["note"], operation["result"]):
            self.assertIsNotNone(text)
            self.assertNotIn(secret_path, text)
            self.assertNotIn(self.tempdir.name, text)
            self.assertIn("Error", text)
            self.assertIn("errno=13", text)

    def test_active_issues_exclude_quarantine_history(self):
        result = self.upload("op_b8_ok", "att-b8-ok", b"B8-READY")
        self.assertEqual(result["attachment"]["state"], "READY")
        stage = self.data_dir / "staging" / "b8-history.part"
        stage.write_bytes(b"B8-HISTORY")
        self.attachments.reconcile()
        self.attachments.reconcile()

        integrity = self.attachments.get_integrity()
        open_kinds = {issue["kind"] for issue in integrity["open_issues"]}
        self.assertIn("quarantine_file_isolated", open_kinds)
        self.assertEqual(integrity["active_open_issues"], [])
        self.assertEqual(integrity["active_open_issue_count"], 0)
        self.assertGreaterEqual(integrity["quarantine_history_count"], 1)
        # The ledger row is retained for audit; zeroing the badge never
        # deletes it.
        conn = connect(self.data_dir / "archive.db")
        try:
            ledger = conn.execute(
                "SELECT COUNT(*) FROM integrity_issues WHERE kind = ?",
                ("quarantine_file_isolated",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ledger, 1)

        client = self.app.test_client()
        client.get(f"/auth?token={TOKEN}")
        body = client.get("/api/session").get_json()["data"]
        self.assertEqual(body["integrity"]["open_issue_count"], 0)
        self.assertEqual(body["integrity"]["quarantine_history_count"], 1)

        # A real active-area problem still counts, so the separation does not
        # hide damage.
        (self.data_dir / "objects" / "att-b8-ok").unlink()
        self.attachments.reconcile()
        self.assertEqual(
            self.attachments.get_integrity()["active_open_issue_count"], 1
        )


if __name__ == "__main__":
    unittest.main()
