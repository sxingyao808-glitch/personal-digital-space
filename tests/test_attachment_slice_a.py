"""Slice A: attachment write, commit and retry safety."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
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
from archive.attachments import CHUNK_SIZE, AttachmentStore
from archive.db import connect, utcnow
from archive.store import (
    ArchiveStore,
    ConflictError,
    UncertainOutcomeError,
    ValidationError,
)

TOKEN = "test-owner-token-0123456789"


class AttachmentSliceATestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.content_a = self.store.create_content("附件甲")
        self.content_b = self.store.create_content("附件乙")

    def tearDown(self) -> None:
        os.environ.pop("ARCHIVE_TEST_MODE", None)
        os.environ.pop("ARCHIVE_FAULT_INJECT", None)
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    def upload(self, content_id, payload, operation_id, attachment_id):
        return self.attachments.upload(
            io.BytesIO(payload),
            filename="slice-a.bin",
            content_id=content_id,
            operation_id=operation_id,
            attachment_id=attachment_id,
        )

    def attachment_row(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            return dict(
                conn.execute(
                    "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
                ).fetchone()
            )
        finally:
            conn.close()

    def operation_row(self, operation_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            return dict(
                conn.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
            )
        finally:
            conn.close()

    def object_bytes(self, attachment_id):
        return (self.data_dir / "objects" / attachment_id).read_bytes()

    def staging_files(self):
        return sorted(p.name for p in (self.data_dir / "staging").iterdir())

    def quarantine_bytes(self):
        return b"".join(
            path.read_bytes()
            for path in (self.data_dir / "quarantine").iterdir()
            if path.is_file()
        )

    def completed_operation_count(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM operations "
                "WHERE resource_id = ? AND state = 'COMPLETED'",
                (attachment_id,),
            ).fetchone()[0]
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

    def _run_fault(self, content_id, fault, payload, operation_id, attachment_id):
        payload_path = Path(self.tempdir.name) / f"{operation_id}.payload"
        payload_path.write_bytes(payload)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tests" / "fault_worker.py"),
                "--data-dir",
                str(self.data_dir),
                "--file",
                str(payload_path),
                "--content-id",
                content_id,
                "--operation-id",
                operation_id,
                "--attachment-id",
                attachment_id,
                "--fault",
                fault,
                "--sha256",
                hashlib.sha256(payload).hexdigest(),
                "--size",
                str(len(payload)),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        self.assertEqual(
            process.returncode,
            73,
            f"expected hard crash, got {process.returncode}: {process.stderr}",
        )

    # ------------------------------------------------------------------
    # 1. Same operation / attachment / owner / payload is a true no-op
    # ------------------------------------------------------------------
    def test_same_payload_retry_does_not_disturb_ready_state(self):
        payload = b"slice-a-payload"
        first = self.upload(self.content_a["id"], payload, "op_a1", "f_a1")
        self.assertEqual(first["attachment"]["state"], "READY")
        self.assertFalse(first["idempotent"])

        attachment_before = self.attachment_row("f_a1")
        operation_before = self.operation_row("op_a1")
        object_path = self.data_dir / "objects" / "f_a1"
        mtime_before = object_path.stat().st_mtime_ns

        repeated = self.upload(self.content_a["id"], payload, "op_a1", "f_a1")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["attachment"]["id"], "f_a1")
        self.assertEqual(repeated["attachment"]["state"], "READY")
        self.assertEqual(repeated["operation"]["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_a1"), payload)
        self.assertEqual(self.staging_files(), [])
        self.assertEqual(
            sorted(p.name for p in (self.data_dir / "objects").iterdir()), ["f_a1"]
        )

        # The fast path must not perform the PENDING -> promote -> READY cycle.
        # updated_at is the discriminator: the pre-fix implementation rewrote
        # both rows, while the committed fast path performs no write at all.
        attachment_after = self.attachment_row("f_a1")
        operation_after = self.operation_row("op_a1")
        self.assertEqual(attachment_after["updated_at"], attachment_before["updated_at"])
        self.assertEqual(operation_after["updated_at"], operation_before["updated_at"])
        self.assertEqual(object_path.stat().st_mtime_ns, mtime_before)

    # ------------------------------------------------------------------
    # 2. Owner conflict rejects without changing bytes or ownership
    # ------------------------------------------------------------------
    def test_owner_conflict_keeps_existing_attachment_intact(self):
        payload = b"owner-conflict-payload"
        self.upload(self.content_a["id"], payload, "op_a1", "f_a1")

        with self.assertRaises(ConflictError):
            self.upload(self.content_b["id"], payload, "op_a1", "f_a1")

        row = self.attachment_row("f_a1")
        self.assertEqual(row["content_id"], self.content_a["id"])
        self.assertEqual(row["state"], "READY")
        self.assertEqual(self.object_bytes("f_a1"), payload)
        self.assertEqual(self.staging_files(), [])
        operation = self.operation_row("op_a1")
        self.assertEqual(operation["owner_content_id"], self.content_a["id"])
        self.assertEqual(operation["state"], "COMPLETED")

    # ------------------------------------------------------------------
    # 3. Payload conflict rejects without replacing committed bytes
    # ------------------------------------------------------------------
    def test_payload_conflict_keeps_committed_bytes(self):
        original = b"original-payload"
        digest = hashlib.sha256(original).hexdigest()
        self.upload(self.content_a["id"], original, "op_a1", "f_a1")

        with self.assertRaises(ConflictError):
            self.upload(self.content_a["id"], b"replacement", "op_a1", "f_a1")

        row = self.attachment_row("f_a1")
        self.assertEqual(row["sha256"], digest)
        self.assertEqual(row["byte_size"], len(original))
        self.assertEqual(row["state"], "READY")
        self.assertEqual(self.object_bytes("f_a1"), original)
        self.assertEqual(self.staging_files(), [])
        operation = self.operation_row("op_a1")
        self.assertEqual(operation["payload_sha256"], digest)
        self.assertEqual(operation["state"], "COMPLETED")

    # ------------------------------------------------------------------
    # 4. Object missing on retry: heal instead of reporting a stale READY
    # ------------------------------------------------------------------
    def test_retry_after_object_loss_recreates_bytes(self):
        payload = b"lost-object-payload"
        self.upload(self.content_a["id"], payload, "op_a1", "f_a1")
        (self.data_dir / "objects" / "f_a1").unlink()

        retried = self.upload(self.content_a["id"], payload, "op_a1", "f_a1")
        self.assertEqual(retried["attachment"]["state"], "READY")
        self.assertFalse(retried["idempotent"])
        self.assertEqual(self.object_bytes("f_a1"), payload)
        self.assertEqual(self.staging_files(), [])

    # ------------------------------------------------------------------
    # 5. Promotion succeeds but READY commit fails -> uncertain, not success
    # ------------------------------------------------------------------
    def test_ready_commit_failure_is_uncertain_and_retryable(self):
        payload = b"promoted-but-not-ready"
        os.environ["ARCHIVE_TEST_MODE"] = "1"
        os.environ["ARCHIVE_FAULT_INJECT"] = "fail_ready_commit"

        with self.assertRaises(UncertainOutcomeError):
            self.upload(self.content_a["id"], payload, "op_a1", "f_a1")

        self.assertEqual(self.object_bytes("f_a1"), payload)
        pending = self.attachment_row("f_a1")
        self.assertEqual(pending["state"], "PENDING")
        self.assertEqual(pending["note"], "资源已就位但元数据未确认")
        operation = self.operation_row("op_a1")
        self.assertEqual(operation["state"], "UNCERTAIN")
        self.assertEqual(operation["result"], "metadata commit failed")
        self.assertEqual(self.staging_files(), [])

        os.environ.pop("ARCHIVE_TEST_MODE", None)
        os.environ.pop("ARCHIVE_FAULT_INJECT", None)
        retried = self.upload(self.content_a["id"], payload, "op_a1", "f_a1")
        self.assertEqual(retried["attachment"]["state"], "READY")
        self.assertEqual(retried["operation"]["state"], "COMPLETED")
        self.assertFalse(retried["idempotent"])
        self.assertEqual(self.object_bytes("f_a1"), payload)
        self.assertEqual(self.staging_files(), [])


    # ------------------------------------------------------------------
    # 6. A crash during the staging write must not promote unverified bytes
    # ------------------------------------------------------------------
    def test_crash_during_staging_is_quarantined_without_journaled_hash(self):
        payload = b"P" * (CHUNK_SIZE + 37)
        self._run_fault(
            self.content_a["id"],
            "during_staging",
            payload,
            "op_mid_stage",
            "f_mid_stage",
        )

        durable = self.attachment_row("f_mid_stage")
        operation = self.operation_row("op_mid_stage")
        self.assertEqual(durable["state"], "PENDING")
        self.assertIsNone(durable["sha256"])
        self.assertEqual(operation["state"], "PENDING")
        self.assertIsNone(operation["payload_sha256"])

        staged = [path for path in (self.data_dir / "staging").iterdir() if path.is_file()]
        self.assertEqual(len(staged), 1)
        staged_bytes = staged[0].read_bytes()
        self.assertGreater(len(staged_bytes), 0)
        self.assertLess(len(staged_bytes), len(payload))
        self.assertTrue(payload.startswith(staged_bytes))

        report = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(report["ok"])
        self.assertIn(
            "staging_without_journaled_payload",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertNotIn(
            "resumed_staging_promotion",
            {action["action"] for action in report["actions"]},
        )
        self.assertFalse((self.data_dir / "objects" / "f_mid_stage").exists())
        self.assertEqual(self.attachment_row("f_mid_stage")["state"], "BROKEN")
        self.assertEqual(self.operation_row("op_mid_stage")["state"], "UNCERTAIN")
        self.assertEqual(self.staging_files(), [])
        self.assertEqual(self.quarantine_bytes(), staged_bytes)
        open_kinds = {
            issue["kind"]
            for issue in AttachmentStore(self.data_dir).get_integrity()["open_issues"]
        }
        self.assertIn("staging_without_journaled_payload", open_kinds)

        recovered = self.upload(
            self.content_a["id"], payload, "op_mid_stage", "f_mid_stage"
        )
        self.assertEqual(recovered["attachment"]["state"], "READY")
        self.assertFalse(recovered["idempotent"])
        self.assertEqual(self.object_bytes("f_mid_stage"), payload)
        self.assertEqual(
            recovered["attachment"]["sha256"], hashlib.sha256(payload).hexdigest()
        )
        self.assertEqual(recovered["attachment"]["byte_size"], len(payload))
        self.assertEqual(self.staging_files(), [])

        final_report = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(final_report["ok"])

    # ------------------------------------------------------------------
    # 7. A new operation may supersede a pre-payload failure and stays bound
    # ------------------------------------------------------------------
    def test_new_operation_rebinds_after_pre_payload_failure(self):
        with self.assertRaises(ValidationError):
            self.attachments.upload(
                io.BytesIO(b"first"),
                filename="slice-a.bin",
                content_id=self.content_a["id"],
                operation_id="op_old",
                attachment_id="f_rebind",
                expected_size=999,
            )
        failed = self.attachment_row("f_rebind")
        self.assertEqual(failed["state"], "BROKEN")
        self.assertIsNone(failed["sha256"])
        self.assertEqual(failed["operation_id"], "op_old")
        self.assertEqual(self.operation_row("op_old")["state"], "FAILED")

        payload = b"corrected-payload"
        result = self.upload(self.content_a["id"], payload, "op_new", "f_rebind")
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertFalse(result["idempotent"])
        bound = self.attachment_row("f_rebind")
        self.assertEqual(bound["operation_id"], "op_new")
        self.assertEqual(self.operation_row("op_new")["state"], "COMPLETED")
        self.assertEqual(self.operation_row("op_old")["state"], "FAILED")
        self.assertIn(
            "superseded by op_new", self.operation_row("op_old")["result"] or ""
        )
        self.assertEqual(self.completed_operation_count("f_rebind"), 1)

        object_path = self.data_dir / "objects" / "f_rebind"
        mtime = object_path.stat().st_mtime_ns
        repeated = self.upload(self.content_a["id"], payload, "op_new", "f_rebind")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(self.object_bytes("f_rebind"), payload)
        self.assertEqual(object_path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.completed_operation_count("f_rebind"), 1)

    # ------------------------------------------------------------------
    # 8. A crash after a rebind must be recovered by reconcile, not orphaned
    # ------------------------------------------------------------------
    def test_reconcile_recovers_new_operation_crash_after_rebind(self):
        with self.assertRaises(ValidationError):
            self.attachments.upload(
                io.BytesIO(b"first"),
                filename="slice-a.bin",
                content_id=self.content_a["id"],
                operation_id="op_old_c",
                attachment_id="f_rebind_c",
                expected_size=999,
            )

        payload = b"rebound-crash-payload"
        self._run_fault(
            self.content_a["id"],
            "after_staging",
            payload,
            "op_new_c",
            "f_rebind_c",
        )

        durable = self.attachment_row("f_rebind_c")
        self.assertEqual(durable["operation_id"], "op_new_c")
        self.assertEqual(durable["state"], "PENDING")
        self.assertEqual(durable["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.operation_row("op_new_c")["state"], "PENDING")

        report = AttachmentStore(self.data_dir).reconcile()
        issue_kinds = {issue["kind"] for issue in report["issues"]}
        self.assertNotIn("operation_without_attachment", issue_kinds)
        self.assertNotIn("stale_staging_quarantined", issue_kinds)
        self.assertIn(
            "resumed_staging_promotion",
            {action["action"] for action in report["actions"]},
        )
        self.assertEqual(self.attachment_row("f_rebind_c")["state"], "READY")
        self.assertEqual(self.object_bytes("f_rebind_c"), payload)
        self.assertEqual(self.operation_row("op_new_c")["state"], "COMPLETED")
        self.assertEqual(self.operation_row("op_old_c")["state"], "FAILED")
        self.assertIn(
            "superseded by op_new_c",
            self.operation_row("op_old_c")["result"] or "",
        )

        repeated = self.upload(
            self.content_a["id"], payload, "op_new_c", "f_rebind_c"
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(self.object_bytes("f_rebind_c"), payload)

    # ------------------------------------------------------------------
    # 9. Superseding an UNCERTAIN attempt keeps it uncertain and audit-visible
    # ------------------------------------------------------------------
    def test_superseded_uncertain_operation_keeps_state_and_is_not_an_orphan(self):
        payload = b"U" * (CHUNK_SIZE + 11)
        self._run_fault(
            self.content_a["id"],
            "during_staging",
            payload,
            "op_old_u",
            "f_old_u",
        )
        first_report = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(first_report["ok"])
        self.assertEqual(self.operation_row("op_old_u")["state"], "UNCERTAIN")
        self.assertEqual(self.attachment_row("f_old_u")["state"], "BROKEN")
        self.assertIsNone(self.attachment_row("f_old_u")["sha256"])

        recovered = self.upload(self.content_a["id"], payload, "op_new_u", "f_old_u")
        self.assertEqual(recovered["attachment"]["state"], "READY")
        self.assertEqual(self.attachment_row("f_old_u")["operation_id"], "op_new_u")
        old_operation = self.operation_row("op_old_u")
        self.assertEqual(old_operation["state"], "UNCERTAIN")
        self.assertIn("superseded by op_new_u", old_operation["result"] or "")

        second_report = AttachmentStore(self.data_dir).reconcile()
        self.assertNotIn(
            "operation_without_attachment",
            {issue["kind"] for issue in second_report["issues"]},
        )
        self.assertTrue(second_report["ok"])
        self.assertEqual(self.operation_row("op_old_u")["state"], "UNCERTAIN")

    # ------------------------------------------------------------------
    # 10. Corrupt committed bytes stay visible across reconciles
    # ------------------------------------------------------------------
    def test_corrupt_final_object_issue_stays_visible_across_reconciles(self):
        payload = b"original-bytes"
        self.upload(self.content_a["id"], payload, "op_corrupt", "f_corrupt")
        object_path = self.data_dir / "objects" / "f_corrupt"
        object_path.write_bytes(b"corrupted")

        with self.assertRaises(ConflictError):
            self.upload(self.content_a["id"], payload, "op_corrupt", "f_corrupt")

        row = self.attachment_row("f_corrupt")
        self.assertEqual(row["state"], "BROKEN")
        self.assertEqual(self.object_bytes("f_corrupt"), b"corrupted")
        open_kinds = {
            issue["kind"]
            for issue in AttachmentStore(self.data_dir).get_integrity()["open_issues"]
        }
        self.assertIn("ready_hash_mismatch", open_kinds)

        report_one = AttachmentStore(self.data_dir).reconcile()
        report_two = AttachmentStore(self.data_dir).reconcile()
        for report in (report_one, report_two):
            self.assertFalse(report["ok"])
            self.assertIn(
                "ready_hash_mismatch",
                {issue["kind"] for issue in report["issues"]},
            )
        self.assertEqual(self.object_bytes("f_corrupt"), b"corrupted")
        open_kinds = {
            issue["kind"]
            for issue in AttachmentStore(self.data_dir).get_integrity()["open_issues"]
        }
        self.assertIn("ready_hash_mismatch", open_kinds)

    # ------------------------------------------------------------------
    # 11. MIME is fixed when the payload is first journaled
    # ------------------------------------------------------------------
    def test_mime_is_fixed_when_payload_is_first_journaled(self):
        payload = b"mime-payload"
        first = self.attachments.upload(
            io.BytesIO(payload),
            filename="mime.bin",
            content_id=self.content_a["id"],
            operation_id="op_mime",
            attachment_id="f_mime",
            declared_mime="application/octet-stream",
        )
        self.assertEqual(first["attachment"]["mime"], "application/octet-stream")

        fast = self.attachments.upload(
            io.BytesIO(payload),
            filename="mime.bin",
            content_id=self.content_a["id"],
            operation_id="op_mime",
            attachment_id="f_mime",
            declared_mime="text/plain",
        )
        self.assertEqual(fast["attachment"]["mime"], "application/octet-stream")

        (self.data_dir / "objects" / "f_mime").unlink()
        healed = self.attachments.upload(
            io.BytesIO(payload),
            filename="mime.bin",
            content_id=self.content_a["id"],
            operation_id="op_mime",
            attachment_id="f_mime",
            declared_mime="text/plain",
        )
        self.assertEqual(healed["attachment"]["mime"], "application/octet-stream")
        self.assertEqual(healed["attachment"]["state"], "READY")
        self.assertFalse(healed["idempotent"])
        self.assertEqual(self.object_bytes("f_mime"), payload)


    # ------------------------------------------------------------------
    # 12. A stale reconcile snapshot must not overwrite a committed upload
    # ------------------------------------------------------------------
    def test_stale_reconcile_cannot_overwrite_committed_ready(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_stale",
            attachment_id="f_stale",
            filename="slice-a.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        entered = threading.Event()
        release = threading.Event()
        original = AttachmentStore._mark_broken_reconciliation

        def delayed(store, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("reconcile did not receive the release signal")
            return original(store, *args, **kwargs)

        holder = {}
        with mock.patch.object(AttachmentStore, "_mark_broken_reconciliation", delayed):
            def run_reconcile():
                holder["report"] = self.attachments.reconcile()

            thread = threading.Thread(target=run_reconcile, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                payload = b"committed-during-reconcile"
                result = self.upload(self.content_a["id"], payload, "op_stale", "f_stale")
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")

        report = holder["report"]
        self.assertTrue(report["ok"])
        self.assertNotIn(
            "pending_resource_missing",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertIn(
            "skipped_stale_reconciliation",
            {action["action"] for action in report["actions"]},
        )
        row = self.attachment_row("f_stale")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(self.object_bytes("f_stale"), payload)
        self.assertEqual(self.operation_row("op_stale")["state"], "COMPLETED")

    # ------------------------------------------------------------------
    # 13. A stale final-resource confirmation must not overwrite READY
    # ------------------------------------------------------------------
    def test_stale_completion_cannot_overwrite_ready_after_promotion_crash(self):
        payload = b"stale-completion-payload"
        self._run_fault(
            self.content_a["id"],
            "after_promotion",
            payload,
            "op_stale_complete",
            "f_stale_complete",
        )
        before = self.attachment_row("f_stale_complete")
        self.assertEqual(before["state"], "PENDING")
        self.assertTrue((self.data_dir / "objects" / "f_stale_complete").exists())

        entered = threading.Event()
        release = threading.Event()
        original = AttachmentStore._complete_reconciliation

        def delayed(store, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("reconcile did not receive the release signal")
            return original(store, *args, **kwargs)

        holder = {}
        with mock.patch.object(AttachmentStore, "_complete_reconciliation", delayed):
            def run_reconcile():
                holder["report"] = self.attachments.reconcile()

            thread = threading.Thread(target=run_reconcile, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                result = self.upload(
                    self.content_a["id"],
                    payload,
                    "op_stale_complete",
                    "f_stale_complete",
                )
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")

        report = holder["report"]
        self.assertTrue(report["ok"])
        action_names = {action["action"] for action in report["actions"]}
        self.assertNotIn("confirmed_final_resource", action_names)
        self.assertIn("skipped_stale_reconciliation", action_names)
        row = self.attachment_row("f_stale_complete")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(self.object_bytes("f_stale_complete"), payload)
        self.assertEqual(self.operation_row("op_stale_complete")["state"], "COMPLETED")

    # ------------------------------------------------------------------
    # 14. A stale READY audit must not mark a repaired attachment BROKEN
    # ------------------------------------------------------------------
    def test_stale_ready_audit_cannot_mark_repaired_attachment_broken(self):
        payload = b"stale-audit-payload"
        self.upload(self.content_a["id"], payload, "op_stale_audit", "f_stale_audit")
        (self.data_dir / "objects" / "f_stale_audit").unlink()

        entered = threading.Event()
        release = threading.Event()
        original = AttachmentStore._mark_attachment_broken

        def delayed(store, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("reconcile did not receive the release signal")
            return original(store, *args, **kwargs)

        holder = {}
        with mock.patch.object(AttachmentStore, "_mark_attachment_broken", delayed):
            def run_reconcile():
                holder["report"] = self.attachments.reconcile()

            thread = threading.Thread(target=run_reconcile, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                result = self.upload(
                    self.content_a["id"],
                    payload,
                    "op_stale_audit",
                    "f_stale_audit",
                )
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertFalse(result["idempotent"])

        report = holder["report"]
        self.assertTrue(report["ok"])
        issue_kinds = {issue["kind"] for issue in report["issues"]}
        self.assertNotIn("ready_file_missing", issue_kinds)
        self.assertNotIn("ready_hash_mismatch", issue_kinds)
        self.assertIn(
            "skipped_stale_audit",
            {action["action"] for action in report["actions"]},
        )
        row = self.attachment_row("f_stale_audit")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.object_bytes("f_stale_audit"), payload)


    # ------------------------------------------------------------------
    # 15. P1: an issue persisted with its state transition is not re-stamped
    #     after a concurrent upload heals the attachment.
    # ------------------------------------------------------------------
    def test_stale_issue_registration_cannot_survive_concurrent_heal(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_p1",
            attachment_id="f_p1",
            filename="slice-a.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        entered = threading.Event()
        release = threading.Event()
        original = AttachmentStore._record_issues

        def delayed(store, *args, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("reconcile did not receive the release signal")
            return original(store, *args, **kwargs)

        holder = {}
        with mock.patch.object(AttachmentStore, "_record_issues", delayed):
            def run_reconcile():
                holder["report"] = self.attachments.reconcile()

            thread = threading.Thread(target=run_reconcile, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                # The issue was written in the same transaction as BROKEN /
                # UNCERTAIN, before _record_issues is reached.
                before = self.issue_row("pending_resource_missing", "f_p1")
                self.assertIsNotNone(before)
                self.assertEqual(before["state"], "open")
                self.assertEqual(self.attachment_row("f_p1")["state"], "BROKEN")
                self.assertEqual(self.operation_row("op_p1")["state"], "UNCERTAIN")

                payload = b"healed-during-reconcile"
                result = self.upload(
                    self.content_a["id"], payload, "op_p1", "f_p1"
                )
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "reconcile thread did not finish")

        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")
        row = self.attachment_row("f_p1")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(row["operation_id"], "op_p1")
        self.assertEqual(self.operation_row("op_p1")["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_p1"), payload)

        report = holder["report"]
        self.assertIn(
            "pending_resource_missing",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertFalse(report["ok"])
        # No second registration or reopened stamp after the heal.
        after = self.issue_row("pending_resource_missing", "f_p1")
        self.assertEqual(after["state"], "open")
        self.assertEqual(after["first_seen"], before["first_seen"])
        self.assertEqual(after["last_seen"], before["last_seen"])
        self.assertEqual(after["detail"], before["detail"])

        final_report = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(final_report["ok"])
        open_kinds = {
            issue["kind"]
            for issue in AttachmentStore(self.data_dir).get_integrity()["open_issues"]
        }
        self.assertNotIn("pending_resource_missing", open_kinds)

    # ------------------------------------------------------------------
    # 16. P2: reconcile must not move or quarantine the staging file of an
    #     upload that is still running.
    # ------------------------------------------------------------------
    def test_reconcile_cannot_move_staging_of_active_upload(self):
        payload = b"A" * (CHUNK_SIZE + 123)
        staged = threading.Event()
        release = threading.Event()
        original_fault = attachments_module._fault

        def controlled_fault(name):
            if name == "after_staging" and threading.current_thread().name == "active-upload":
                staged.set()
                if not release.wait(timeout=10):
                    raise AssertionError("upload did not receive the release signal")
                return None
            return original_fault(name)

        holder = {}
        with mock.patch.object(attachments_module, "_fault", controlled_fault):
            def run_upload():
                holder["result"] = self.upload(
                    self.content_a["id"], payload, "op_p2", "f_p2"
                )

            thread = threading.Thread(target=run_upload, name="active-upload", daemon=True)
            thread.start()
            try:
                self.assertTrue(staged.wait(timeout=10))
                staging_before = self.staging_files()
                self.assertEqual(len(staging_before), 1)
                report = AttachmentStore(self.data_dir).reconcile()
                self.assertEqual(self.staging_files(), staging_before)
            finally:
                release.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "upload thread did not finish")

        result = holder["result"]
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")
        self.assertEqual(result["attachment"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["attachment"]["byte_size"], len(payload))
        self.assertEqual(result["attachment"]["id"], "f_p2")
        self.assertEqual(self.object_bytes("f_p2"), payload)
        self.assertEqual(self.staging_files(), [])

        self.assertTrue(report["ok"])
        self.assertEqual(report["quarantined"], [])
        self.assertNotIn(
            "stale_staging_quarantined",
            {issue["kind"] for issue in report["issues"]},
        )
        actions = report["actions"]
        self.assertTrue(
            any(
                action.get("staging_file") == staging_before[0]
                for action in actions
                if action["action"] == "skipped_active_upload"
            )
        )
        # The in-process registry must not leak after the upload finishes.
        self.assertNotIn(
            str(self.data_dir.resolve()), attachments_module._ACTIVE_UPLOADS
        )

    # ------------------------------------------------------------------
    # 17. P2: a failed quarantine must be reported as failure, and the next
    #     scan must still be able to isolate exactly once.
    # ------------------------------------------------------------------
    def test_failed_quarantine_is_not_reported_as_success(self):
        payload = b"Q" * (CHUNK_SIZE + 7)
        self._run_fault(
            self.content_a["id"], "during_staging", payload, "op_quar", "f_quar"
        )
        staged = [path for path in (self.data_dir / "staging").iterdir() if path.is_file()]
        self.assertEqual(len(staged), 1)
        stage = staged[0]
        real_replace = os.replace

        def failing_replace(src, dst):
            if "quarantine" in str(dst):
                raise OSError("injected quarantine failure")
            return real_replace(src, dst)

        with mock.patch.object(
            attachments_module.os, "replace", side_effect=failing_replace
        ):
            report = AttachmentStore(self.data_dir).reconcile()

        self.assertFalse(report["ok"])
        self.assertEqual(report["quarantined"], [])
        self.assertTrue(stage.exists())
        issue = next(
            issue
            for issue in report["issues"]
            if issue["kind"] == "staging_without_journaled_payload"
        )
        self.assertIn("隔离失败", issue["detail"])
        self.assertNotIn(
            "stale_staging_quarantine_failed",
            {issue["kind"] for issue in report["issues"]},
        )
        self.assertEqual(self.attachment_row("f_quar")["state"], "BROKEN")
        self.assertEqual(self.operation_row("op_quar")["state"], "UNCERTAIN")
        ledger = self.issue_row("staging_without_journaled_payload", "f_quar")
        self.assertEqual(ledger["detail"], issue["detail"])

        second = AttachmentStore(self.data_dir).reconcile()
        self.assertEqual(len(second["quarantined"]), 1)
        self.assertIn(stage.name, second["quarantined"][0])
        self.assertFalse(stage.exists())
        self.assertEqual(
            sum(1 for item in second["quarantined"] if stage.name in item), 1
        )

    # ------------------------------------------------------------------
    # 18. P3: a failing duplicate must not downgrade a concurrent success.
    # ------------------------------------------------------------------
    def test_failed_duplicate_upload_cannot_downgrade_concurrent_success(self):
        payload = b"D" * 4096
        failing_name = "failing-upload"
        mark_entered = threading.Event()
        release_mark = threading.Event()
        real_replace = os.replace
        original_mark_uncertain = AttachmentStore._mark_uncertain_retained

        def thread_replace(src, dst):
            if (
                threading.current_thread().name == failing_name
                and str(src).endswith(".part")
            ):
                raise OSError("injected promotion failure")
            return real_replace(src, dst)

        def delayed_mark_failed(store, attachment_id, operation_id, note):
            if threading.current_thread().name == failing_name:
                mark_entered.set()
                if not release_mark.wait(timeout=10):
                    raise AssertionError("_mark_failed did not receive the release signal")
            return original_mark_uncertain(store, attachment_id, operation_id, note)

        holder = {}

        def run_failing():
            try:
                self.upload(self.content_a["id"], payload, "op_p3_fail", "f_p3_fail")
            except BaseException as exc:
                holder["error"] = exc

        with mock.patch.object(
            attachments_module.os, "replace", side_effect=thread_replace
        ), mock.patch.object(
            AttachmentStore, "_mark_uncertain_retained", delayed_mark_failed
        ):
            thread = threading.Thread(target=run_failing, name=failing_name, daemon=True)
            thread.start()
            try:
                self.assertTrue(mark_entered.wait(timeout=10))
                result = self.upload(
                    self.content_a["id"], payload, "op_p3_fail", "f_p3_fail"
                )
            finally:
                release_mark.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "failing upload did not finish")

        self.assertIsInstance(holder.get("error"), OSError)
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")
        row = self.attachment_row("f_p3_fail")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(row["operation_id"], "op_p3_fail")
        self.assertEqual(self.operation_row("op_p3_fail")["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_p3_fail"), payload)
        # The failing attempt kept its verified staging bytes. A later scan
        # quarantines them instead of deleting them silently.
        self.attachments.reconcile()
        self.assertEqual(self.staging_files(), [])
        self.assertEqual(self.completed_operation_count("f_p3_fail"), 1)

    # ------------------------------------------------------------------
    # 19. P3: the READY-commit failure handler must not downgrade a success.
    # ------------------------------------------------------------------
    def test_ready_failure_handler_cannot_downgrade_concurrent_success(self):
        payload = b"R" * 8192
        failing_name = "ready-failure-thread"
        handler_entered = threading.Event()
        release_handler = threading.Event()
        original_fault = attachments_module._fault
        original_handler = AttachmentStore._mark_uncertain_after_ready_failure

        def controlled_fault(name):
            if (
                name == "fail_ready_commit"
                and threading.current_thread().name == failing_name
            ):
                raise sqlite3.OperationalError("injected READY metadata failure")
            return original_fault(name)

        def delayed_handler(store, attachment_id, operation_id):
            if threading.current_thread().name == failing_name:
                handler_entered.set()
                if not release_handler.wait(timeout=10):
                    raise AssertionError("handler did not receive the release signal")
            return original_handler(store, attachment_id, operation_id)

        holder = {}

        def run_failing():
            try:
                self.upload(self.content_a["id"], payload, "op_p3_ready", "f_p3_ready")
            except BaseException as exc:
                holder["error"] = exc

        with mock.patch.object(
            attachments_module, "_fault", controlled_fault
        ), mock.patch.object(
            AttachmentStore,
            "_mark_uncertain_after_ready_failure",
            delayed_handler,
        ):
            thread = threading.Thread(
                target=run_failing, name=failing_name, daemon=True
            )
            thread.start()
            try:
                self.assertTrue(handler_entered.wait(timeout=10))
                result = self.upload(
                    self.content_a["id"], payload, "op_p3_ready", "f_p3_ready"
                )
            finally:
                release_handler.set()
                thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "failing upload did not finish")

        self.assertIsInstance(holder.get("error"), UncertainOutcomeError)
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")
        row = self.attachment_row("f_p3_ready")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(row["operation_id"], "op_p3_ready")
        self.assertEqual(self.operation_row("op_p3_ready")["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_p3_ready"), payload)
        self.assertEqual(self.staging_files(), [])


    # ------------------------------------------------------------------
    # 20. P1-A: an active upload skip must not resolve an open issue
    # ------------------------------------------------------------------
    def test_active_upload_skip_does_not_resolve_open_issue(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_active_ok",
            attachment_id="f_active_ok",
            filename="slice-a.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        before = self.issue_row("pending_resource_missing", "f_active_ok")
        self.assertIsNotNone(before)
        self.assertEqual(before["state"], "open")

        entered = threading.Event()
        release = threading.Event()
        errors = {}
        holder = {}

        class PausedStream:
            def __init__(self):
                self.served = False

            def read(self, size):
                if not self.served:
                    self.served = True
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("upload did not receive the release signal")
                    return b"active-upload-payload"
                return b""

        def run_upload():
            try:
                holder["result"] = self.attachments.upload(
                    PausedStream(),
                    filename="active.bin",
                    content_id=self.content_a["id"],
                    operation_id="op_active_ok",
                    attachment_id="f_active_ok",
                )
            except BaseException as exc:
                errors["upload"] = exc

        thread = threading.Thread(target=run_upload, name="p1a-success", daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            during = AttachmentStore(self.data_dir).reconcile()
            during_issue = self.issue_row("pending_resource_missing", "f_active_ok")
            self.assertEqual(during_issue["state"], "open")
            self.assertEqual(during_issue["first_seen"], before["first_seen"])
            self.assertEqual(during_issue["last_seen"], before["last_seen"])
            self.assertEqual(during_issue["detail"], before["detail"])
            self.assertIn(
                "skipped_active_upload",
                {action["action"] for action in during["actions"]},
            )
        finally:
            release.set()
            thread.join(15)
        self.assertFalse(thread.is_alive(), "upload thread did not finish")
        self.assertNotIn("upload", errors)
        self.assertEqual(holder["result"]["attachment"]["state"], "READY")

        still_open = self.issue_row("pending_resource_missing", "f_active_ok")
        self.assertEqual(still_open["state"], "open")

        final = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(final["ok"])
        resolved = self.issue_row("pending_resource_missing", "f_active_ok")
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["first_seen"], before["first_seen"])
        self.assertEqual(resolved["last_seen"], before["last_seen"])
        self.assertEqual(resolved["detail"], before["detail"])

    # ------------------------------------------------------------------
    # 21. P1-A: a failed active upload leaves the issue open
    # ------------------------------------------------------------------
    def test_failed_active_upload_keeps_issue_open(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_active_fail",
            attachment_id="f_active_fail",
            filename="slice-a.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        before = self.issue_row("pending_resource_missing", "f_active_fail")
        self.assertEqual(before["state"], "open")

        entered = threading.Event()
        release = threading.Event()
        errors = {}

        class FailingStream:
            def read(self, size):
                entered.set()
                if not release.wait(10):
                    raise AssertionError("upload did not receive the release signal")
                raise OSError("injected staging read failure")

        def run_upload():
            try:
                self.attachments.upload(
                    FailingStream(),
                    filename="active-fail.bin",
                    content_id=self.content_a["id"],
                    operation_id="op_active_fail",
                    attachment_id="f_active_fail",
                )
            except BaseException as exc:
                errors["upload"] = exc

        thread = threading.Thread(target=run_upload, name="p1a-failure", daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            during = AttachmentStore(self.data_dir).reconcile()
            during_issue = self.issue_row("pending_resource_missing", "f_active_fail")
            self.assertEqual(during_issue["state"], "open")
            self.assertEqual(during_issue["first_seen"], before["first_seen"])
            self.assertEqual(during_issue["last_seen"], before["last_seen"])
            self.assertEqual(during_issue["detail"], before["detail"])
            self.assertIn(
                "skipped_active_upload",
                {action["action"] for action in during["actions"]},
            )
        finally:
            release.set()
            thread.join(15)
        self.assertFalse(thread.is_alive(), "upload thread did not finish")
        self.assertIsInstance(errors.get("upload"), OSError)

        after = self.issue_row("pending_resource_missing", "f_active_fail")
        self.assertEqual(after["state"], "open")
        self.assertEqual(self.attachment_row("f_active_fail")["state"], "BROKEN")
        # The scan before this upload had already recorded the attempt as
        # UNCERTAIN. A later retry that fails again must not rewrite that
        # history as a definite FAILED (R-07).
        self.assertEqual(self.operation_row("op_active_fail")["state"], "UNCERTAIN")

    # ------------------------------------------------------------------
    # 22. P1-B: a later reconcile's issue cannot be resolved by an earlier
    #     stale scan; reconcile is serialized per Archive.
    # ------------------------------------------------------------------
    def test_concurrent_reconcile_cannot_resolve_later_issue(self):
        a_paused = threading.Event()
        release_a = threading.Event()
        b_started = threading.Event()
        b_entered = threading.Event()
        holder = {}
        original_record = AttachmentStore._record_issues
        original_reconcile = AttachmentStore._reconcile

        def delayed_record(store_obj, issues, checked_at, **kwargs):
            if threading.current_thread().name == "p1b-a":
                a_paused.set()
                if not release_a.wait(10):
                    raise AssertionError("reconcile A did not receive the release signal")
            return original_record(store_obj, issues, checked_at, **kwargs)

        def marked_reconcile(store_obj):
            if threading.current_thread().name == "p1b-b":
                b_entered.set()
            return original_reconcile(store_obj)

        with mock.patch.object(
            AttachmentStore, "_record_issues", delayed_record
        ), mock.patch.object(AttachmentStore, "_reconcile", marked_reconcile):
            def run_a():
                holder["a"] = AttachmentStore(self.data_dir).reconcile()

            thread_a = threading.Thread(target=run_a, name="p1b-a", daemon=True)
            thread_a.start()
            self.assertTrue(a_paused.wait(10))

            def run_b():
                b_started.set()
                holder["b"] = AttachmentStore(self.data_dir).reconcile()

            thread_b = threading.Thread(target=run_b, name="p1b-b", daemon=True)
            thread_b.start()
            self.assertTrue(b_started.wait(10))
            self.assertFalse(b_entered.wait(0.2), "reconcile B entered while A held the lock")

            # This row appears after A's snapshot but before A writes its ledger.
            self.attachments._prepare_pending(
                content_id=self.content_a["id"],
                operation_id="op_late_issue",
                attachment_id="f_late_issue",
                filename="late.bin",
                mime="application/octet-stream",
                visibility="private",
            )
            release_a.set()
            thread_a.join(15)
            self.assertTrue(b_entered.wait(10))
            thread_b.join(15)

        self.assertFalse(thread_a.is_alive(), "reconcile A did not finish")
        self.assertFalse(thread_b.is_alive(), "reconcile B did not finish")
        self.assertTrue(holder["a"]["ok"])
        self.assertFalse(holder["b"]["ok"])
        issue = self.issue_row("pending_resource_missing", "f_late_issue")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["state"], "open")
        self.assertEqual(self.attachment_row("f_late_issue")["state"], "BROKEN")
        self.assertEqual(self.operation_row("op_late_issue")["state"], "UNCERTAIN")

    # ------------------------------------------------------------------
    # 23. P1-B: an issue written after the scan began must not be resolved by
    #     the older scan, even when its target was checked healthy.
    # ------------------------------------------------------------------
    def test_record_issues_does_not_resolve_newer_issue(self):
        payload = b"newer-issue-payload"
        self.upload(self.content_a["id"], payload, "op_newer_issue", "f_newer_issue")

        entered = threading.Event()
        release = threading.Event()
        holder = {}
        original_record = AttachmentStore._record_issues

        def delayed_record(store_obj, issues, checked_at, **kwargs):
            if threading.current_thread().name == "p1b-newer":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("reconcile did not receive the release signal")
            return original_record(store_obj, issues, checked_at, **kwargs)

        with mock.patch.object(AttachmentStore, "_record_issues", delayed_record):
            def run_reconcile():
                holder["report"] = AttachmentStore(self.data_dir).reconcile()

            thread = threading.Thread(target=run_reconcile, name="p1b-newer", daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                conn = connect(self.data_dir / "archive.db")
                try:
                    now = utcnow()
                    conn.execute(
                        "INSERT INTO integrity_issues(fingerprint, kind, target, detail, "
                        "state, first_seen, last_seen) "
                        "VALUES(?, ?, ?, ?, 'open', ?, ?) "
                        "ON CONFLICT(fingerprint) DO UPDATE SET state = 'open', "
                        "last_seen = excluded.last_seen, resolved_at = NULL",
                        (
                            "synthetic-newer-issue",
                            "ready_hash_mismatch",
                            "f_newer_issue",
                            "synthetic issue written after the scan began",
                            now,
                            now,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            finally:
                release.set()
                thread.join(15)
        self.assertFalse(thread.is_alive(), "reconcile did not finish")
        self.assertTrue(holder["report"]["ok"])
        issue = self.issue_row("ready_hash_mismatch", "f_newer_issue")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["state"], "open")

    # ------------------------------------------------------------------
    # 24. P1-C: BROKEN + FAILED + no payload + no object must be reported
    # ------------------------------------------------------------------
    def test_broken_failed_without_payload_is_not_ok(self):
        with self.assertRaises(ValidationError):
            self.attachments.upload(
                io.BytesIO(b"payload"),
                filename="broken.bin",
                content_id=self.content_a["id"],
                operation_id="op_broken_meta",
                attachment_id="f_broken_meta",
                expected_size=999,
            )
        attachment = self.attachment_row("f_broken_meta")
        self.assertEqual(attachment["state"], "BROKEN")
        self.assertIsNone(attachment["sha256"])
        self.assertIsNone(attachment["byte_size"])
        self.assertEqual(self.operation_row("op_broken_meta")["state"], "FAILED")
        self.assertFalse((self.data_dir / "objects" / "f_broken_meta").exists())

        report = self.attachments.reconcile()
        self.assertFalse(report["ok"])
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("attachment_metadata_incomplete", kinds)
        self.assertIn("broken_resource_missing", kinds)
        open_issues = {
            issue["kind"]: issue
            for issue in self.attachments.get_integrity()["open_issues"]
            if issue["target"] == "f_broken_meta"
        }
        self.assertIn("attachment_metadata_incomplete", open_issues)
        self.assertIn("broken_resource_missing", open_issues)
        self.assertTrue(open_issues["attachment_metadata_incomplete"]["detail"])
        self.assertTrue(open_issues["broken_resource_missing"]["detail"])

    # ------------------------------------------------------------------
    # 25. P1-C: metadata-incomplete with an existing object is unverifiable,
    #     not a missing-resource issue.
    # ------------------------------------------------------------------
    def test_broken_without_payload_but_with_object_is_unverifiable(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_meta_object",
            attachment_id="f_meta_object",
            filename="meta-object.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        object_path = self.data_dir / "objects" / "f_meta_object"
        object_path.write_bytes(b"object-without-payload-metadata")
        self.attachments._mark_failed(
            "f_meta_object",
            "op_meta_object",
            "synthetic pre-payload failure",
        )

        report = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(report["ok"])
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("attachment_metadata_incomplete", kinds)
        self.assertNotIn("broken_resource_missing", kinds)
        self.assertTrue(object_path.is_file())
        self.assertEqual(self.attachment_row("f_meta_object")["state"], "BROKEN")

    # ------------------------------------------------------------------
    # 26. P1-C: a BROKEN row with payload metadata and a missing object has
    #     a distinct issue, not a metadata-incomplete issue.
    # ------------------------------------------------------------------
    def test_broken_with_payload_and_missing_object_has_distinct_issue(self):
        payload = b"payload-for-broken-state"
        self.upload(self.content_a["id"], payload, "op_broken_file", "f_broken_file")
        (self.data_dir / "objects" / "f_broken_file").unlink()

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        self.assertEqual(self.attachment_row("f_broken_file")["state"], "BROKEN")
        first_issue = self.issue_row("ready_file_missing", "f_broken_file")
        self.assertIsNotNone(first_issue)

        second = self.attachments.reconcile()
        self.assertFalse(second["ok"])
        kinds = {issue["kind"] for issue in second["issues"]}
        self.assertIn("broken_resource_missing", kinds)
        self.assertNotIn("attachment_metadata_incomplete", kinds)
        second_issue = self.issue_row("broken_resource_missing", "f_broken_file")
        self.assertIsNotNone(second_issue)
        self.assertEqual(second_issue["state"], "open")
        # The original issue described the earlier READY→BROKEN observation and
        # remains open until the attachment is actually healthy again.
        self.assertEqual(
            self.issue_row("ready_file_missing", "f_broken_file")["state"],
            "open",
        )

    # ------------------------------------------------------------------
    # 27. P1-C: a healthy READY attachment remains a clean scan.
    # ------------------------------------------------------------------
    def test_healthy_ready_attachment_is_not_reported(self):
        payload = b"healthy-ready-payload"
        self.upload(self.content_a["id"], payload, "op_healthy", "f_healthy")
        report = self.attachments.reconcile()
        self.assertTrue(report["ok"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(self.attachments.get_integrity()["open_issues"], [])

    # ------------------------------------------------------------------
    # 28. P2: two reconciles for the same Archive serialize; a reconcile for
    #     another Archive does not block behind them.
    # ------------------------------------------------------------------
    def test_concurrent_reconcile_promotes_staging_once(self):
        payload = b"P" * (CHUNK_SIZE + 19)
        self._run_fault(
            self.content_a["id"],
            "after_staging",
            payload,
            "op_p2_lock",
            "f_p2_lock",
        )

        first_paused = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        other_done = threading.Event()
        holder = {}
        errors = {}
        original_hash = attachments_module._hash_file
        original_reconcile = AttachmentStore._reconcile

        def controlled_hash(path, *args, **kwargs):
            if (
                threading.current_thread().name == "p2-first"
                and Path(path).parent == self.attachments.staging
            ):
                first_paused.set()
                if not release_first.wait(10):
                    raise AssertionError("first reconcile did not receive the release signal")
            return original_hash(path, *args, **kwargs)

        def marked_reconcile(store_obj):
            if threading.current_thread().name == "p2-second":
                second_entered.set()
            return original_reconcile(store_obj)

        with mock.patch.object(
            attachments_module, "_hash_file", controlled_hash
        ), mock.patch.object(AttachmentStore, "_reconcile", marked_reconcile):
            def run_first():
                try:
                    holder["first"] = AttachmentStore(self.data_dir).reconcile()
                except BaseException as exc:
                    errors["first"] = exc

            first_thread = threading.Thread(target=run_first, name="p2-first", daemon=True)
            first_thread.start()
            self.assertTrue(first_paused.wait(10))

            def run_second():
                second_started.set()
                try:
                    holder["second"] = AttachmentStore(self.data_dir).reconcile()
                except BaseException as exc:
                    errors["second"] = exc

            second_thread = threading.Thread(target=run_second, name="p2-second", daemon=True)
            second_thread.start()
            self.assertTrue(second_started.wait(10))
            self.assertFalse(
                second_entered.wait(0.2),
                "second reconcile entered while the first held the lock",
            )

            other_data = Path(self.tempdir.name) / "other-data"
            create_app(other_data, bootstrap_token=TOKEN, testing=True)

            def run_other():
                try:
                    holder["other"] = AttachmentStore(other_data).reconcile()
                finally:
                    other_done.set()

            other_thread = threading.Thread(target=run_other, name="p2-other", daemon=True)
            other_thread.start()
            self.assertTrue(other_done.wait(10), "different Archive was blocked")
            other_thread.join(10)

            release_first.set()
            first_thread.join(15)
            self.assertTrue(second_entered.wait(10))
            second_thread.join(15)

        self.assertFalse(first_thread.is_alive(), "first reconcile did not finish")
        self.assertFalse(second_thread.is_alive(), "second reconcile did not finish")
        self.assertEqual(errors, {})
        self.assertTrue(holder["first"]["ok"])
        self.assertTrue(holder["second"]["ok"])
        self.assertTrue(holder["other"]["ok"])
        self.assertIn(
            "resumed_staging_promotion",
            {action["action"] for action in holder["first"]["actions"]},
        )
        self.assertEqual(
            self.attachment_row("f_p2_lock")["state"], "READY"
        )
        self.assertEqual(
            self.operation_row("op_p2_lock")["state"], "COMPLETED"
        )
        self.assertEqual(self.object_bytes("f_p2_lock"), payload)
        self.assertEqual(self.staging_files(), [])

    # ------------------------------------------------------------------
    # 29. P2: an exception in one reconcile must not leave the Archive lock held.
    # ------------------------------------------------------------------
    def test_reconcile_lock_is_released_after_exception(self):
        original = AttachmentStore._reconcile
        calls = {"count": 0}

        def controlled(store_obj):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("injected reconcile failure")
            return original(store_obj)

        with mock.patch.object(AttachmentStore, "_reconcile", controlled):
            with self.assertRaises(RuntimeError):
                self.attachments.reconcile()
            report = self.attachments.reconcile()
        self.assertTrue(report["ok"])


    # ------------------------------------------------------------------
    # 30. SA-N-02: restoring identical bytes must not report a clean scan
    #     while the attachment is still BROKEN.
    # ------------------------------------------------------------------
    def test_broken_attachment_with_restored_bytes_is_not_reported_healthy(self):
        payload = b"sa-n-02-restored-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        self.upload(self.content_a["id"], payload, "op_sa2", "f_sa2")
        object_path = self.data_dir / "objects" / "f_sa2"
        object_path.unlink()

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        self.assertIn(
            "ready_file_missing", {issue["kind"] for issue in first["issues"]}
        )
        self.assertEqual(self.attachment_row("f_sa2")["state"], "BROKEN")
        self.assertEqual(
            self.issue_row("ready_file_missing", "f_sa2")["state"], "open"
        )

        object_path.write_bytes(payload)

        second = AttachmentStore(self.data_dir).reconcile()
        # A matching object is not a completed recovery: the scan must not
        # report success nor an empty issue list.
        self.assertFalse(second["ok"])
        self.assertNotEqual(second["issues"], [])
        self.assertIn(
            "broken_state_unrecovered",
            {issue["kind"] for issue in second["issues"]},
        )
        row = self.attachment_row("f_sa2")
        self.assertEqual(row["state"], "BROKEN")
        self.assertEqual(row["sha256"], digest)
        self.assertEqual(row["byte_size"], len(payload))
        self.assertEqual(self.object_bytes("f_sa2"), payload)
        self.assertEqual(self.operation_row("op_sa2")["state"], "COMPLETED")
        # The missing-object finding itself is genuinely gone ...
        self.assertEqual(
            self.issue_row("ready_file_missing", "f_sa2")["state"], "resolved"
        )
        # ... but the outstanding recovery is recorded and stays open.
        recovery = self.issue_row("broken_state_unrecovered", "f_sa2")
        self.assertIsNotNone(recovery)
        self.assertEqual(recovery["state"], "open")
        self.assertTrue(recovery["detail"])

        # Repeated scans stay truthful and idempotent.
        third = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(third["ok"])
        self.assertEqual(
            [issue["kind"] for issue in third["issues"]],
            ["broken_state_unrecovered"],
        )
        again = self.issue_row("broken_state_unrecovered", "f_sa2")
        self.assertEqual(again["state"], "open")
        self.assertEqual(again["first_seen"], recovery["first_seen"])
        self.assertEqual(self.attachment_row("f_sa2")["state"], "BROKEN")

    # ------------------------------------------------------------------
    # 31. SA-N-02: a BROKEN attachment whose object bytes do not match is
    #     reported as a mismatch and the object is never replaced.
    # ------------------------------------------------------------------
    def test_broken_attachment_with_mismatching_bytes_is_reported(self):
        payload = b"expected-broken-bytes"
        self.upload(self.content_a["id"], payload, "op_sa2_bad", "f_sa2_bad")
        object_path = self.data_dir / "objects" / "f_sa2_bad"
        object_path.unlink()
        self.attachments.reconcile()
        self.assertEqual(self.attachment_row("f_sa2_bad")["state"], "BROKEN")

        tampered = b"tampered-bytes"
        object_path.write_bytes(tampered)
        report = AttachmentStore(self.data_dir).reconcile()

        self.assertFalse(report["ok"])
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("ready_hash_mismatch", kinds)
        self.assertNotIn("broken_state_unrecovered", kinds)
        self.assertEqual(self.attachment_row("f_sa2_bad")["state"], "BROKEN")
        # Non-destructive: the differing bytes are preserved for diagnosis.
        self.assertEqual(self.object_bytes("f_sa2_bad"), tampered)
        self.assertEqual(
            self.issue_row("ready_hash_mismatch", "f_sa2_bad")["state"], "open"
        )

    # ------------------------------------------------------------------
    # 32. SA-N-02: an object that matches unverifiable bytes stays
    #     unverifiable; byte equality alone is not recovery evidence.
    # ------------------------------------------------------------------
    def test_broken_without_metadata_and_matching_object_is_unverifiable(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_sa2_meta",
            attachment_id="f_sa2_meta",
            filename="meta.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        object_path = self.data_dir / "objects" / "f_sa2_meta"
        object_path.write_bytes(b"unverifiable-bytes")
        self.attachments._mark_failed(
            "f_sa2_meta", "op_sa2_meta", "synthetic pre-payload failure"
        )

        report = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(report["ok"])
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("attachment_metadata_incomplete", kinds)
        self.assertNotIn("broken_state_unrecovered", kinds)
        self.assertEqual(self.attachment_row("f_sa2_meta")["state"], "BROKEN")
        self.assertEqual(self.object_bytes("f_sa2_meta"), b"unverifiable-bytes")

    # ------------------------------------------------------------------
    # 33. Per-cause resolution: closing the missing-object finding must not
    #     close the separate metadata finding for the same attachment.
    # ------------------------------------------------------------------
    def test_one_cause_resolving_does_not_close_another_cause(self):
        self.attachments._prepare_pending(
            content_id=self.content_a["id"],
            operation_id="op_multi_cause",
            attachment_id="f_multi_cause",
            filename="multi.bin",
            mime="application/octet-stream",
            visibility="private",
        )
        self.attachments._mark_failed(
            "f_multi_cause", "op_multi_cause", "synthetic pre-payload failure"
        )

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        kinds = {issue["kind"] for issue in first["issues"]}
        self.assertIn("attachment_metadata_incomplete", kinds)
        self.assertIn("broken_resource_missing", kinds)

        # The object now exists, so the missing-object cause is gone, but the
        # payload metadata is still absent and nothing can be verified.
        (self.data_dir / "objects" / "f_multi_cause").write_bytes(b"late-object")

        second = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(second["ok"])
        self.assertEqual(
            self.issue_row("broken_resource_missing", "f_multi_cause")["state"],
            "resolved",
        )
        metadata_issue = self.issue_row(
            "attachment_metadata_incomplete", "f_multi_cause"
        )
        self.assertEqual(metadata_issue["state"], "open")
        self.assertEqual(self.attachment_row("f_multi_cause")["state"], "BROKEN")
        self.assertIn(
            "attachment_metadata_incomplete",
            {issue["kind"] for issue in second["issues"]},
        )

    # ------------------------------------------------------------------
    # 34. The recovery finding must close once a legitimate recovery path
    #     (same payload, journaled first) actually restores READY.
    # ------------------------------------------------------------------
    def test_broken_state_issue_resolves_after_legitimate_reupload(self):
        payload = b"legitimate-recovery-bytes"
        self.upload(self.content_a["id"], payload, "op_rec", "f_rec")
        object_path = self.data_dir / "objects" / "f_rec"
        object_path.unlink()
        self.attachments.reconcile()
        object_path.write_bytes(payload)
        restored = self.attachments.reconcile()
        self.assertFalse(restored["ok"])
        self.assertEqual(
            self.issue_row("broken_state_unrecovered", "f_rec")["state"], "open"
        )

        recovered = self.upload(self.content_a["id"], payload, "op_rec", "f_rec")
        self.assertEqual(recovered["attachment"]["state"], "READY")
        self.assertEqual(self.operation_row("op_rec")["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_rec"), payload)

        final = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(final["ok"])
        self.assertEqual(final["issues"], [])
        self.assertEqual(self.attachment_row("f_rec")["state"], "READY")
        self.assertEqual(
            self.issue_row("broken_state_unrecovered", "f_rec")["state"],
            "resolved",
        )
        self.assertEqual(
            AttachmentStore(self.data_dir).get_integrity()["open_issues"], []
        )

    # ------------------------------------------------------------------
    # 35. operation_without_attachment: a terminal operation is not proof
    #     that the attachment exists.
    # ------------------------------------------------------------------
    def _insert_orphan_operation(self, operation_id, resource_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            now = utcnow()
            conn.execute(
                "INSERT INTO operations(id, kind, resource_id, owner_content_id, "
                "state, result, created_at, updated_at) "
                "VALUES(?, 'attachment_write', ?, ?, 'PENDING', NULL, ?, ?)",
                (operation_id, resource_id, self.content_a["id"], now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def _set_operation_state_sql(self, operation_id, state, result):
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

    def test_terminal_operation_without_attachment_keeps_issue_open(self):
        self._insert_orphan_operation("op_orphan", "f_orphan_never")

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        self.assertIn(
            "operation_without_attachment",
            {issue["kind"] for issue in first["issues"]},
        )
        self.assertEqual(self.issue_row("operation_without_attachment", "op_orphan")["state"], "open")
        self.assertEqual(self.operation_row("op_orphan")["state"], "UNCERTAIN")

        self._set_operation_state_sql("op_orphan", "FAILED", "manual terminal")

        second = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(second["ok"])
        self.assertIn(
            "operation_without_attachment",
            {issue["kind"] for issue in second["issues"]},
        )
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_orphan")["state"],
            "open",
        )
        self.assertEqual(self.operation_row("op_orphan")["state"], "FAILED")
        conn = connect(self.data_dir / "archive.db")
        try:
            orphan_rows = conn.execute(
                "SELECT COUNT(*) FROM attachments WHERE id = 'f_orphan_never'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(orphan_rows, 0)

        # Only removing the operation record ends the finding; the missing
        # attachment is never silently treated as resolved.
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute("DELETE FROM operations WHERE id = 'op_orphan'")
            conn.commit()
        finally:
            conn.close()

        third = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(third["ok"])
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_orphan")["state"],
            "resolved",
        )

    # ------------------------------------------------------------------
    # 36. operation_without_attachment: a resource that appears through the
    #     real upload path resolves the finding.
    # ------------------------------------------------------------------
    def test_terminal_operation_resource_appearing_later_resolves_issue(self):
        payload = b"late-resource-bytes"
        self._insert_orphan_operation("op_late_res", "f_late_res")
        self.attachments.reconcile()
        self._set_operation_state_sql("op_late_res", "FAILED", "manual terminal")
        stuck = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(stuck["ok"])
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_late_res")["state"],
            "open",
        )

        result = self.upload(self.content_a["id"], payload, "op_late_res", "f_late_res")
        self.assertEqual(result["attachment"]["state"], "READY")
        self.assertEqual(result["operation"]["state"], "COMPLETED")
        self.assertEqual(self.object_bytes("f_late_res"), payload)

        final = AttachmentStore(self.data_dir).reconcile()
        self.assertTrue(final["ok"])
        self.assertEqual(final["issues"], [])
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_late_res")["state"],
            "resolved",
        )
        self.assertEqual(self.attachment_row("f_late_res")["state"], "READY")

    # ------------------------------------------------------------------
    # 37. Resolving one operation's finding must not close an independent
    #     resource finding that is still open.
    # ------------------------------------------------------------------
    def test_resolving_operation_issue_keeps_other_resource_issue_open(self):
        self._insert_orphan_operation("op_orphan_other", None)
        self.upload(self.content_b["id"], b"independent-bytes", "op_indep", "f_indep")
        (self.data_dir / "objects" / "f_indep").unlink()

        first = self.attachments.reconcile()
        self.assertFalse(first["ok"])
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_orphan_other")["state"],
            "open",
        )
        self.assertEqual(
            self.issue_row("ready_file_missing", "f_indep")["state"], "open"
        )

        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute("DELETE FROM operations WHERE id = 'op_orphan_other'")
            conn.commit()
        finally:
            conn.close()

        second = AttachmentStore(self.data_dir).reconcile()
        self.assertFalse(second["ok"])
        self.assertEqual(
            self.issue_row("operation_without_attachment", "op_orphan_other")["state"],
            "resolved",
        )
        self.assertEqual(
            self.issue_row("broken_resource_missing", "f_indep")["state"], "open"
        )
        self.assertEqual(self.attachment_row("f_indep")["state"], "BROKEN")
        self.assertFalse((self.data_dir / "objects" / "f_indep").exists())
        self.assertIn(
            "broken_resource_missing",
            {issue["kind"] for issue in second["issues"]},
        )

    # ------------------------------------------------------------------
    # 38. A stale scan must not resolve a newer operation finding.
    # ------------------------------------------------------------------
    def test_stale_scan_cannot_resolve_newer_operation_issue(self):
        entered = threading.Event()
        release = threading.Event()
        holder = {}
        original_record = AttachmentStore._record_issues

        def delayed_record(store_obj, issues, checked_at, **kwargs):
            if threading.current_thread().name == "sa2-newer":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("reconcile did not receive the release signal")
            return original_record(store_obj, issues, checked_at, **kwargs)

        with mock.patch.object(AttachmentStore, "_record_issues", delayed_record):
            def run_reconcile():
                holder["report"] = AttachmentStore(self.data_dir).reconcile()

            thread = threading.Thread(target=run_reconcile, name="sa2-newer", daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                conn = connect(self.data_dir / "archive.db")
                try:
                    now = utcnow()
                    conn.execute(
                        "INSERT INTO integrity_issues(fingerprint, kind, target, detail, "
                        "state, first_seen, last_seen) "
                        "VALUES(?, 'operation_without_attachment', ?, ?, 'open', ?, ?) "
                        "ON CONFLICT(fingerprint) DO UPDATE SET state = 'open', "
                        "last_seen = excluded.last_seen, resolved_at = NULL",
                        (
                            "synthetic-newer-operation-issue",
                            "op_newer",
                            "synthetic operation finding written after the scan began",
                            now,
                            now,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            finally:
                release.set()
                thread.join(15)

        self.assertFalse(thread.is_alive(), "reconcile did not finish")
        issue = self.issue_row("operation_without_attachment", "op_newer")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["state"], "open")


if __name__ == "__main__":
    unittest.main()
