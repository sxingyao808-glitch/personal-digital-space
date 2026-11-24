"""Acceptance-slice tests for the Windows 11 archive implementation v0.1.

These tests exercise implemented behavior only. They do not claim backup,
export, public release activation, or History verification.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.db import connect
from archive.store import ArchiveStore

TOKEN = "test-owner-token-0123456789"
ALLOWED_TYPES = {"PART_OF", "DERIVED_FROM", "LED_TO", "EVOLVED_INTO", "REFERENCES"}


class ArchiveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)
        self.anonymous = self.app.test_client()

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

    def create_content(self, title="", body="", visibility=None, client=None, headers=None):
        client = client or self.owner
        headers = headers if headers is not None else self.headers
        response = client.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def patch_content(self, content_id, revision, **fields):
        return self.owner.patch(
            f"/api/content/{content_id}",
            json={"revision": revision, **fields},
            headers=self.headers,
        )

    def publish(self, content_ids, attachment_ids=None):
        """Prepare and activate a release so public routes can serve it."""
        response = self.owner.post(
            "/api/releases/prepare",
            json={
                "content_ids": content_ids,
                "attachment_ids": attachment_ids or [],
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_id = response.get_json()["data"]["release_id"]
        activation = self.owner.post(
            f"/api/releases/{release_id}/activate",
            json={},
            headers=self.headers,
        )
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        return release_id

    def withdraw_all(self):
        response = self.owner.post(
            "/api/releases/withdraw", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_relation(self, from_id, to_id, relation_type):
        return self.owner.post(
            f"/api/content/{from_id}/relations",
            json={"to_id": to_id, "type": relation_type},
            headers=self.headers,
        )

    def add_attribute(self, content_id, key, value, provenance="user", confirmed=False):
        return self.owner.post(
            f"/api/content/{content_id}/attributes",
            json={
                "key": key,
                "value": value,
                "provenance": provenance,
                "confirmed": confirmed,
            },
            headers=self.headers,
        )

    def upload(
        self,
        content_id,
        payload: bytes,
        operation_id: str,
        attachment_id: str | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        visibility: str = "private",
        client=None,
        headers=None,
    ):
        client = client or self.owner
        headers = headers if headers is not None else self.headers
        data = {
            "file": (io.BytesIO(payload), "note.txt"),
            "operation_id": operation_id,
            "visibility": visibility,
        }
        if attachment_id:
            data["attachment_id"] = attachment_id
        if expected_sha256:
            data["expected_sha256"] = expected_sha256
        if expected_size is not None:
            data["expected_size"] = str(expected_size)
        return client.post(
            f"/api/content/{content_id}/attachments",
            data=data,
            content_type="multipart/form-data",
            headers=headers,
        )

    # ------------------------------------------------------------------
    # AC-01 / AC-02 / AC-03: creation, persistence and stable identity
    # ------------------------------------------------------------------
    def test_content_identity_persists_across_edits_and_restart(self):
        created = self.create_content("初始标题", "初始正文")
        content_id = created["id"]
        self.assertTrue(content_id.startswith("c_"))
        self.assertEqual(created["revision"], 1)
        self.assertFalse(created["visibility_selected"])
        self.assertFalse(created["public"])

        response = self.patch_content(content_id, 1, title="改名之后", body="更新正文")
        self.assertEqual(response.status_code, 200)
        updated = response.get_json()["data"]
        self.assertEqual(updated["id"], content_id)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["title"], "改名之后")

        app2 = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        client2 = app2.test_client()
        headers2 = self.login(client2)
        reread = client2.get(f"/api/content/{content_id}", headers=headers2)
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(reread.get_json()["data"]["body"], "更新正文")

    def test_same_text_does_not_collapse_identity(self):
        first = self.create_content("同名", "完全相同的正文")
        second = self.create_content("同名", "完全相同的正文")
        self.assertNotEqual(first["id"], second["id"])
        listing = self.owner.get("/api/content", headers=self.headers).get_json()["data"]["items"]
        self.assertEqual(len(listing), 2)

    def test_stale_revision_is_rejected_without_overwrite(self):
        created = self.create_content("并发", "原始")
        first = self.patch_content(created["id"], 1, body="第一次更新")
        self.assertEqual(first.status_code, 200)
        stale = self.patch_content(created["id"], 1, body="过期更新")
        self.assertEqual(stale.status_code, 409)
        details = stale.get_json()["error"]["details"]
        self.assertEqual(details["current_revision"], 2)
        current = self.owner.get(f"/api/content/{created['id']}").get_json()["data"]
        self.assertEqual(current["body"], "第一次更新")

    def test_deleted_content_is_not_editable_or_uploadable(self):
        created = self.create_content("将删除", "内容")
        deleted = self.owner.delete(
            f"/api/content/{created['id']}",
            json={"revision": created["revision"]},
            headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200)
        edit = self.patch_content(created["id"], deleted.get_json()["data"]["content"]["revision"], body="x")
        self.assertEqual(edit.status_code, 409)
        upload = self.upload(created["id"], b"x", "op_deleted_1")
        self.assertEqual(upload.status_code, 409)

    # ------------------------------------------------------------------
    # AC-04 / AC-05 / AC-06: visibility and public access isolation
    # ------------------------------------------------------------------
    def test_visibility_requires_explicit_selection(self):
        implicit = self.create_content("未选择", "默认私有")
        private = self.create_content("私有", "私有正文", visibility="private")
        public = self.create_content("公开", "公开正文", visibility="public")
        self.publish([public["id"]])

        self.assertEqual(self.anonymous.get(f"/p/{implicit['id']}").status_code, 404)
        self.assertEqual(self.anonymous.get(f"/p/{private['id']}").status_code, 404)
        self.assertEqual(self.anonymous.get(f"/p/{public['id']}").status_code, 200)

        to_public = self.patch_content(implicit["id"], implicit["revision"], visibility="public")
        self.assertEqual(to_public.status_code, 200)
        self.assertTrue(to_public.get_json()["data"]["public"])
        self.publish([public["id"], implicit["id"]])
        self.assertEqual(self.anonymous.get(f"/p/{implicit['id']}").status_code, 200)

        back_to_unselected = self.patch_content(
            implicit["id"], to_public.get_json()["data"]["revision"], visibility=None
        )
        self.assertEqual(back_to_unselected.status_code, 200)
        self.assertFalse(back_to_unselected.get_json()["data"]["visibility_selected"])
        self.publish([public["id"]])
        self.assertEqual(self.anonymous.get(f"/p/{implicit['id']}").status_code, 404)

    def test_public_payload_omits_relations_and_attributes(self):
        private = self.create_content("私有端点", "PRIVATE-CANARY-BODY", visibility="private")
        public = self.create_content("公开端点", "PUBLIC-BODY", visibility="public")
        self.add_relation(public["id"], private["id"], "REFERENCES")
        self.add_attribute(public["id"], "秘密属性", "SECRET-ATTRIBUTE-VALUE")
        self.publish([public["id"]])

        response = self.anonymous.get(f"/api/public/content/{public['id']}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertNotIn("relations", payload)
        self.assertNotIn("attributes", payload)
        text = response.get_data(as_text=True)
        self.assertNotIn("PRIVATE-CANARY-BODY", text)
        self.assertNotIn("SECRET-ATTRIBUTE-VALUE", text)

        private_response = self.anonymous.get(f"/api/public/content/{private['id']}")
        self.assertEqual(private_response.status_code, 404)
        self.assertNotIn("PRIVATE-CANARY-BODY", private_response.get_data(as_text=True))

    def test_public_page_escapes_user_html(self):
        created = self.create_content(
            '<script>alert("t")</script>',
            "正文 <img src=x onerror=alert(2)> 结束",
            visibility="public",
        )
        self.publish([created["id"]])
        response = self.anonymous.get(f"/p/{created['id']}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('<script>alert("t")</script>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<img src=x", html)

    # ------------------------------------------------------------------
    # AC-07 / AC-08 / AC-09: relations and attributes
    # ------------------------------------------------------------------
    def test_relation_types_endpoints_and_duplicates(self):
        first = self.create_content("关系一")
        second = self.create_content("关系二")
        for relation_type in sorted(ALLOWED_TYPES):
            response = self.add_relation(first["id"], second["id"], relation_type)
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

        duplicate = self.add_relation(first["id"], second["id"], "PART_OF")
        self.assertEqual(duplicate.status_code, 409)
        invalid = self.add_relation(first["id"], second["id"], "RELATED_TO")
        self.assertEqual(invalid.status_code, 422)
        self_relation = self.add_relation(first["id"], first["id"], "REFERENCES")
        self.assertEqual(self_relation.status_code, 422)

        detail = self.owner.get(f"/api/content/{first['id']}").get_json()["data"]
        self.assertEqual(len(detail["relations"]), len(ALLOWED_TYPES))
        self.assertTrue(all(r["direction"] == "outgoing" for r in detail["relations"]))

    def test_relation_survives_delete_and_reports_broken_endpoint(self):
        first = self.create_content("端点甲")
        second = self.create_content("端点乙")
        self.add_relation(first["id"], second["id"], "LED_TO")
        deleted = self.owner.delete(
            f"/api/content/{second['id']}", json={"revision": second["revision"]}, headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200)

        detail = self.owner.get(
            f"/api/content/{first['id']}?include_deleted=1"
        ).get_json()["data"]
        self.assertEqual(len(detail["relations"]), 1)
        self.assertFalse(detail["relations"][0]["other_resolvable"])

        new_relation = self.add_relation(first["id"], second["id"], "REFERENCES")
        self.assertEqual(new_relation.status_code, 409)

    def test_ai_attributes_never_auto_confirm_and_confirmation_changes_provenance(self):
        created = self.create_content("属性")
        ai = self.add_attribute(created["id"], "推断主题", "某种解释", "ai_inferred", confirmed=True)
        self.assertEqual(ai.status_code, 201)
        attribute = ai.get_json()["data"]
        self.assertFalse(attribute["confirmed"])
        self.assertEqual(attribute["provenance"], "ai_inferred")

        confirmed = self.owner.patch(
            f"/api/attributes/{attribute['id']}", json={"confirmed": True}, headers=self.headers
        )
        self.assertEqual(confirmed.status_code, 200)
        updated = confirmed.get_json()["data"]
        self.assertTrue(updated["confirmed"])
        self.assertEqual(updated["provenance"], "user_confirmed")
        self.assertEqual(updated["value"], "某种解释")

    def test_attributes_do_not_rewrite_body(self):
        created = self.create_content("原文", "原始正文")
        self.add_attribute(created["id"], "标签", "测试", "user", confirmed=True)
        detail = self.owner.get(f"/api/content/{created['id']}").get_json()["data"]
        self.assertEqual(detail["body"], "原始正文")
        self.assertEqual(detail["revision"], 1)

    # ------------------------------------------------------------------
    # AC-10 / AC-11: deletion impacts and recovery
    # ------------------------------------------------------------------
    def test_delete_and_restore_preserve_identity_relations_attributes(self):
        first = self.create_content("将被删除", "原始正文", visibility="public")
        second = self.create_content("保留端点")
        relation = self.add_relation(first["id"], second["id"], "PART_OF")
        self.assertEqual(relation.status_code, 201)
        self.add_attribute(first["id"], "来源", "本地", "user", confirmed=True)

        response = self.owner.delete(
            f"/api/content/{first['id']}", json={"revision": first["revision"]}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertEqual(payload["content"]["state"], "deleted")
        self.assertEqual(payload["impacts"]["direct_relation_count"], 1)
        self.assertFalse(payload["impacts"]["cascading_deletion"])
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 404)
        self.assertEqual(self.owner.get(f"/api/content/{first['id']}").status_code, 404)

        listing = self.owner.get("/api/content").get_json()["data"]["items"]
        self.assertNotIn(first["id"], [item["id"] for item in listing])
        trash = self.owner.get("/api/content?deleted=1").get_json()["data"]["items"]
        self.assertIn(first["id"], [item["id"] for item in trash])

        restored = self.owner.post(
            f"/api/content/{first['id']}/restore", headers=self.headers
        )
        self.assertEqual(restored.status_code, 200)
        data = restored.get_json()["data"]
        self.assertEqual(data["content"]["id"], first["id"])
        self.assertEqual(data["content"]["state"], "active")
        self.assertEqual(data["report"]["relations"], 1)
        self.assertEqual(data["report"]["attributes"], 1)
        self.assertTrue(data["report"]["restored_identity"])
        self.assertTrue(data["report"]["complete"])

        detail = self.owner.get(f"/api/content/{first['id']}").get_json()["data"]
        self.assertEqual(detail["title"], "将被删除")
        self.assertEqual(detail["body"], "原始正文")
        self.assertTrue(detail["relations"][0]["other_resolvable"])

    def test_restore_reports_not_ready_attachment_truthfully(self):
        created = self.create_content("带损坏附件")
        failed = self.upload(
            created["id"],
            b"payload",
            "op_bad_hash_1",
            expected_sha256="0" * 64,
        )
        self.assertEqual(failed.status_code, 422)
        detail = self.owner.get(f"/api/content/{created['id']}").get_json()["data"]
        self.assertEqual(len(detail["attachments"]), 1)
        self.assertNotEqual(detail["attachments"][0]["state"], "READY")

        deleted = self.owner.delete(
            f"/api/content/{created['id']}", json={"revision": detail["revision"]}, headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200)
        restored = self.owner.post(
            f"/api/content/{created['id']}/restore", headers=self.headers
        )
        report = restored.get_json()["data"]["report"]
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["attachments_not_ready"]), 1)

    # ------------------------------------------------------------------
    # AC-12 / AC-14: authorization boundaries
    # ------------------------------------------------------------------
    def test_owner_api_requires_session_csrf_and_same_origin(self):
        self.assertEqual(self.anonymous.get("/api/content").status_code, 401)
        self.assertEqual(self.anonymous.get("/").status_code, 302)

        no_csrf = self.owner.post("/api/content", json={"title": "x"})
        self.assertEqual(no_csrf.status_code, 403)
        self.assertEqual(no_csrf.get_json()["error"]["code"], "csrf_failed")

        bad_origin = self.owner.post(
            "/api/content",
            json={"title": "x"},
            headers={**self.headers, "Origin": "https://evil.example"},
        )
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_origin.get_json()["error"]["code"], "origin_rejected")

        wrong_login = self.anonymous.post("/login", data={"token": "wrong-token"})
        self.assertEqual(wrong_login.status_code, 403)

    def test_host_guard_rejects_foreign_host_headers(self):
        response = self.anonymous.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "host_rejected")

    def test_login_page_does_not_expose_bootstrap_token(self):
        html = self.anonymous.get("/login").get_data(as_text=True)
        self.assertNotIn(TOKEN, html)

    # ------------------------------------------------------------------
    # AC-13 / AC-15 / AC-20: attachment integrity and truthful states
    # ------------------------------------------------------------------
    def test_attachment_roundtrip_idempotency_and_conflict_rejection(self):
        first = self.create_content("附件甲")
        second = self.create_content("附件乙")
        payload = b"first payload bytes"
        digest = hashlib.sha256(payload).hexdigest()

        created = self.upload(
            first["id"],
            payload,
            "op_attach_1",
            attachment_id="f_attach_1",
            expected_sha256=digest,
            expected_size=len(payload),
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        result = created.get_json()["data"]
        attachment = result["attachment"]
        self.assertEqual(attachment["state"], "READY")
        self.assertEqual(attachment["sha256"], digest)
        self.assertEqual(attachment["content_id"], first["id"])
        self.assertFalse(result["idempotent"])

        downloaded = self.owner.get(f"/api/attachments/{attachment['id']}/file")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, payload)
        downloaded.close()

        repeated = self.upload(
            first["id"],
            payload,
            "op_attach_1",
            attachment_id="f_attach_1",
            expected_sha256=digest,
            expected_size=len(payload),
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.get_json()["data"]["idempotent"])

        different_payload = self.upload(
            first["id"],
            b"different bytes",
            "op_attach_1",
            attachment_id="f_attach_1",
            expected_size=len(b"different bytes"),
        )
        self.assertEqual(different_payload.status_code, 409)

        different_owner = self.upload(
            second["id"],
            payload,
            "op_attach_2",
            attachment_id="f_attach_1",
            expected_size=len(payload),
        )
        self.assertEqual(different_owner.status_code, 409)

        same_operation_other_owner = self.upload(
            second["id"],
            payload,
            "op_attach_1",
            attachment_id="f_attach_2",
            expected_size=len(payload),
        )
        self.assertEqual(same_operation_other_owner.status_code, 409)

        still_intact = self.owner.get(f"/api/attachments/{attachment['id']}/file")
        self.assertEqual(still_intact.data, payload)
        still_intact.close()

    def test_attachment_visibility_and_public_gating(self):
        content = self.create_content("带附件公开", "正文", visibility="public")
        public_payload = b"PUBLIC-ATTACHMENT"
        private_payload = b"PRIVATE-ATTACHMENT-CANARY"

        public_upload = self.upload(
            content["id"],
            public_payload,
            "op_pub_att",
            attachment_id="f_pub_att",
            visibility="public",
        )
        self.assertEqual(public_upload.status_code, 201)
        private_upload = self.upload(
            content["id"],
            private_payload,
            "op_priv_att",
            attachment_id="f_priv_att",
            visibility="private",
        )
        self.assertEqual(private_upload.status_code, 201)
        self.publish([content["id"]], ["f_pub_att"])

        public_page = self.anonymous.get(f"/p/{content['id']}")
        html = public_page.get_data(as_text=True)
        self.assertIn("note.txt", html)
        self.assertNotIn("f_pub_att", html)
        self.assertNotIn("f_priv_att", html)
        self.assertNotIn("PRIVATE-ATTACHMENT-CANARY", html)

        public_download = self.anonymous.get("/api/attachments/f_pub_att/file")
        self.assertEqual(public_download.status_code, 200)
        self.assertEqual(public_download.data, public_payload)
        public_download.close()
        self.assertEqual(
            self.anonymous.get("/api/attachments/f_priv_att/file").status_code, 404
        )

        self.patch_content(content["id"], content["revision"], visibility="private")
        self.withdraw_all()
        self.assertEqual(
            self.anonymous.get("/api/attachments/f_pub_att/file").status_code, 404
        )
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 404)

    def test_hash_mismatch_is_reported_as_broken_not_success(self):
        content = self.create_content("哈希不符")
        response = self.upload(
            content["id"],
            b"actual bytes",
            "op_hash_mismatch",
            attachment_id="f_hash_bad",
            expected_sha256="f" * 64,
            expected_size=len(b"actual bytes"),
        )
        self.assertEqual(response.status_code, 422)
        detail = self.owner.get(f"/api/content/{content['id']}").get_json()["data"]
        attachment = detail["attachments"][0]
        self.assertEqual(attachment["state"], "BROKEN")
        self.assertIn("SHA-256", attachment["note"])
        self.assertEqual(self.owner.get("/api/attachments/f_hash_bad/file").status_code, 404)

    def test_reconciliation_quarantines_orphans_without_deleting(self):
        store = AttachmentStore(self.data_dir)
        orphan_object = store.objects / "orphan.bin"
        orphan_object.write_bytes(b"ORPHAN-OBJECT")
        stale_staging = store.staging / "stale.part"
        stale_staging.write_bytes(b"STALE-STAGING")

        report = store.reconcile()
        self.assertFalse(report["ok"])
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("orphan_object_quarantined", kinds)
        self.assertIn("stale_staging_quarantined", kinds)
        self.assertFalse(orphan_object.exists())
        self.assertFalse(stale_staging.exists())
        quarantined_bytes = b"".join(
            path.read_bytes() for path in store.quarantine.iterdir() if path.is_file()
        )
        self.assertIn(b"ORPHAN-OBJECT", quarantined_bytes)
        self.assertIn(b"STALE-STAGING", quarantined_bytes)

        integrity = store.get_integrity()
        self.assertGreaterEqual(len(integrity["open_issues"]), 2)

    def test_missing_ready_file_is_reported_as_broken(self):
        content = self.create_content("缺失文件")
        uploaded = self.upload(
            content["id"], b"ready payload", "op_ready_missing", attachment_id="f_ready_missing"
        )
        self.assertEqual(uploaded.status_code, 201)
        (AttachmentStore(self.data_dir).objects / "f_ready_missing").unlink()

        report = AttachmentStore(self.data_dir).reconcile()
        self.assertIn("ready_file_missing", {issue["kind"] for issue in report["issues"]})
        detail = self.owner.get(f"/api/content/{content['id']}").get_json()["data"]
        self.assertEqual(detail["attachments"][0]["state"], "BROKEN")

    # ------------------------------------------------------------------
    # AEF-T03: crash left-overs and reconciliation
    # ------------------------------------------------------------------
    def _run_fault(self, content_id: str, fault: str, payload: bytes, operation_id: str, attachment_id: str):
        payload_path = Path(self.tempdir.name) / "crash-payload.bin"
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
            timeout=90,
            env=env,
        )
        self.assertEqual(
            process.returncode,
            73,
            f"expected hard crash, got {process.returncode}: {process.stderr}",
        )

    def test_crash_after_staging_resumes_promotion(self):
        content = self.create_content("崩溃暂存")
        payload = b"STAGING-CRASH-PAYLOAD"
        self._run_fault(content["id"], "after_staging", payload, "op_crash_stage", "f_crash_stage")

        before = AttachmentStore(self.data_dir).get_attachment("f_crash_stage")
        self.assertEqual(before["state"], "PENDING")
        report = AttachmentStore(self.data_dir).reconcile()
        self.assertIn(
            "resumed_staging_promotion", {action["action"] for action in report["actions"]}
        )
        after = AttachmentStore(self.data_dir).get_attachment("f_crash_stage")
        self.assertEqual(after["state"], "READY")
        self.assertEqual(
            (AttachmentStore(self.data_dir).objects / "f_crash_stage").read_bytes(), payload
        )

    def test_crash_after_promotion_confirms_resource_and_completes_operation(self):
        content = self.create_content("崩溃提升")
        payload = b"PROMOTION-CRASH-PAYLOAD"
        self._run_fault(content["id"], "after_promotion", payload, "op_crash_promote", "f_crash_promote")

        before = AttachmentStore(self.data_dir).get_attachment("f_crash_promote")
        self.assertEqual(before["state"], "PENDING")
        self.assertTrue(
            (AttachmentStore(self.data_dir).objects / "f_crash_promote").exists()
        )

        report = AttachmentStore(self.data_dir).reconcile()
        self.assertIn(
            "confirmed_final_resource", {action["action"] for action in report["actions"]}
        )
        after = AttachmentStore(self.data_dir).get_attachment("f_crash_promote")
        self.assertEqual(after["state"], "READY")

        conn = connect(self.data_dir / "archive.db")
        try:
            operation = conn.execute(
                "SELECT * FROM operations WHERE id = 'op_crash_promote'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(operation["owner_content_id"], content["id"])

    # ------------------------------------------------------------------
    # Search and retention safety
    # ------------------------------------------------------------------
    def test_search_treats_like_wildcards_literally(self):
        percent = self.create_content("完成度 100%", "百分比")
        plain = self.create_content("普通记录", "无特殊字符")
        response = self.owner.get(
            "/api/content", query_string={"q": "%"}, headers=self.headers
        )
        ids = [item["id"] for item in response.get_json()["data"]["items"]]
        self.assertIn(percent["id"], ids)
        self.assertNotIn(plain["id"], ids)


class LifecycleContractTests(unittest.TestCase):
    """Direct-store checks for API-independent invariants."""

    def test_integrity_reconciliation_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder) / "data"
            app = create_app(data_dir, bootstrap_token=TOKEN, testing=True)
            client = app.test_client()
            login_response = client.get(f"/auth?token={TOKEN}")
            self.assertEqual(login_response.status_code, 302)
            with client.session_transaction() as session:
                csrf = session["csrf"]
            store = ArchiveStore(data_dir)
            content = store.create_content("保留", "原始", visibility="private")
            self.assertEqual(content["id"], store.get_content(content["id"])["id"])

            store_dir = AttachmentStore(data_dir)
            (store_dir.objects / "unknown.bin").write_bytes(b"keep me")
            report = store_dir.reconcile()
            self.assertFalse(report["ok"])
            self.assertTrue(any("unknown.bin" in path for path in report["quarantined"]))
            preserved = [path.read_bytes() for path in store_dir.quarantine.iterdir() if path.is_file()]
            self.assertIn(b"keep me", preserved)
            self.assertTrue(csrf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
