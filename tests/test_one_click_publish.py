"""Batch B one-click local publishing tests.

The one-click plan must inherit the owner's existing public selection, the
publish call must require explicit confirmation, and the result must state
clearly that only a local static site was produced (no online deployment).
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.attachments import AttachmentStore
from archive.release import public_content_id
from archive.store import ArchiveStore

TOKEN = "test-owner-token-one-click-13579"


class OneClickPublishTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
        self.attachments = AttachmentStore(self.data_dir)
        self.owner = self.app.test_client()
        self.anonymous = self.app.test_client()
        self.headers = self.login(self.owner)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def login(self, client) -> dict[str, str]:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return {"X-CSRF-Token": csrf}

    def plan(self):
        response = self.owner.get("/api/publish/plan")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def one_click(self, confirm=True):
        return self.owner.post(
            "/api/publish/one-click", json={"confirm": confirm}, headers=self.headers
        )

    def upload(self, content_id, visibility="public", suffix="1"):
        result = self.attachments.upload(
            io.BytesIO(b"one-click attachment"),
            filename=f"one-click-{suffix}.txt",
            content_id=content_id,
            operation_id=f"op_one_click_{suffix}",
            attachment_id=f"a_one_click_{suffix}",
            visibility=visibility,
        )
        return result["attachment"]

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------
    def test_plan_inherits_public_selection_and_attachments(self):
        public = self.store.create_content("公开内容", "正文", visibility="public")
        not_selected = self.store.create_content("仅公开未选择", "正文", visibility=None)
        private = self.store.create_content("私有内容", "正文", visibility="private")
        public_attachment = self.upload(public["id"], "public", "pub")
        self.upload(public["id"], "private", "priv")

        data = self.plan()
        self.assertEqual(data["content_ids"], [public["id"]])
        self.assertNotIn(not_selected["id"], data["content_ids"])
        self.assertNotIn(private["id"], data["content_ids"])
        self.assertEqual(data["attachment_ids"], [public_attachment["id"]])
        self.assertEqual(len(data["skipped_attachments"]), 1)
        self.assertIn("私有", data["skipped_attachments"][0]["reason"])
        self.assertTrue(data["valid"])
        self.assertIsNone(data["active_release_id"])
        self.assertTrue(data["delta"]["has_changes"])

    def test_plan_does_not_create_a_release(self):
        self.store.create_content("公开内容", "正文", visibility="public")
        self.plan()
        response = self.owner.get("/api/releases")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["data"]["active_release_id"])
        self.assertEqual(response.get_json()["data"]["releases"], [])

    def test_plan_reports_content_changed_since_publish(self):
        content = self.store.create_content("初版标题", "初版正文", visibility="public")
        self.assertEqual(self.one_click().status_code, 200)

        self.store.update_content(content["id"], content["revision"], body="修改后的正文")
        data = self.plan()
        self.assertEqual(data["delta"]["changed_content_ids"], [content["id"]])
        self.assertTrue(data["delta"]["has_changes"])
        self.assertEqual(data["delta"]["added_content_ids"], [])
        self.assertEqual(data["delta"]["removed_content_ids"], [])

    # ------------------------------------------------------------------
    # One-click publishing
    # ------------------------------------------------------------------
    def test_one_click_requires_explicit_confirmation(self):
        self.store.create_content("公开内容", "正文", visibility="public")
        response = self.one_click(confirm=False)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.get_json()["error"]["code"], "release_validation_error"
        )
        self.assertEqual(self.plan()["active_release_id"], None)

    def test_one_click_publishes_and_serves_the_local_site(self):
        content = self.store.create_content("一键发布内容", "正文", visibility="public")

        response = self.one_click()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertEqual(data["content_count"], 1)
        self.assertEqual(data["local_publish"], "succeeded")
        self.assertTrue(data["bundle_generated"])
        self.assertEqual(data["deployment"]["status"], "not_configured")
        self.assertIn("线上部署尚未配置", data["deployment"]["message"])
        self.assertEqual(data["active_release_id"], data["release_id"])

        home = self.anonymous.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("一键发布内容", home.get_data(as_text=True))
        page = self.anonymous.get(
            f"/content/{public_content_id(content['id'])}.html"
        )
        self.assertEqual(page.status_code, 200)

        followed = self.plan()
        self.assertEqual(followed["active_release_id"], data["release_id"])
        self.assertFalse(followed["delta"]["has_changes"])

    def test_one_click_republishes_edited_content(self):
        content = self.store.create_content("旧正文标题", "旧正文", visibility="public")
        self.assertEqual(self.one_click().status_code, 200)
        self.store.update_content(content["id"], content["revision"], body="新正文")
        self.assertEqual(self.one_click().status_code, 200)

        page = self.anonymous.get(f"/p/{content['id']}")
        self.assertIn("新正文", page.get_data(as_text=True))

    def test_one_click_without_public_content_is_rejected(self):
        self.store.create_content("私有内容", "正文", visibility="private")
        response = self.one_click()
        self.assertEqual(response.status_code, 422)
        error = response.get_json()["error"]
        self.assertIn("没有已选择公开的内容", error["message"])

    def test_one_click_result_is_honest_about_online_deployment(self):
        self.store.create_content("公开内容", "正文", visibility="public")
        data = self.one_click().get_json()["data"]
        self.assertEqual(data["preview_url"], "/preview/")
        self.assertEqual(data["deployment"]["status"], "not_configured")

    def test_release_page_exposes_one_click_entry_and_preview(self):
        page = self.owner.get("/releases")
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        self.assertIn("待发布的内容变化", markup)
        self.assertIn('id="one-click-publish"', markup)
        self.assertIn('href="/preview/"', markup)
        self.assertIn("发布到网站", markup)
        self.assertIn('id="release-advanced"', markup)
        self.assertIn('id="one-click-preview-draft"', markup)

    def test_draft_preview_renders_a_navigable_owner_only_site_without_publishing(self):
        item = self.store.create_content("待发布网站", "完整正文", visibility="public")
        self.store.create_content("私有条目", "NEVER-PUBLIC-PREVIEW", visibility="private")
        plan = self.plan()
        created = self.owner.post(
            "/api/publish/preview-site", json={"confirmation": plan["confirmation"]},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 200)
        url = created.get_json()["data"]["url"]
        with self.owner.get(url) as page:
            self.assertIn("待发布网站", page.get_data(as_text=True))
            self.assertNotIn("NEVER-PUBLIC-PREVIEW", page.get_data(as_text=True))
        with self.owner.get(url + "content/" + public_content_id(item["id"]) + ".html") as page:
            self.assertIn("完整正文", page.get_data(as_text=True))
        self.assertEqual(self.anonymous.get(url).status_code, 302)
        self.assertEqual(self.owner.get(url + "release.json").status_code, 404)
        self.assertIsNone(self.plan()["active_release_id"])

    def test_removing_last_private_copy_requires_confirmation(self):
        item = self.store.create_content("曾公开", "正文", visibility="public")
        self.assertEqual(self.one_click().status_code, 200)
        active = self.plan()["active_release_id"]
        changed = self.owner.patch(
            f"/api/content/{item['id']}",
            json={"revision": item["revision"], "visibility": "private"},
            headers=self.headers,
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.plan()["delta"]["removed_content_ids"], [item["id"]])
        for confirm, expected, status in [(False, active, 422), (True, None, 409)]:
            response = self.owner.post(
                "/api/publish/one-click", headers=self.headers,
                json={"remove": True, "confirm": confirm, "expected_active": expected},
            )
            self.assertEqual(response.status_code, status)
            self.assertEqual(self.plan()["active_release_id"], active)
        response = self.owner.post(
            "/api/publish/one-click", headers=self.headers,
            json={"remove": True, "confirm": True, "expected_active": active},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["local_publish"], "withdrawn")
        self.assertIsNone(self.plan()["active_release_id"])

    def test_no_change_republish_is_signposted_in_ui(self):
        """A no-op republish stays possible (it can rebuild a damaged bundle),
        but the UI must say so instead of silently creating a duplicate."""
        self.store.create_content("公开内容", "正文", visibility="public")
        self.assertEqual(self.one_click().status_code, 200)
        data = self.plan()
        self.assertTrue(data["delta"]["same_as_active"])
        self.assertFalse(data["delta"]["has_changes"])

        script = self.owner.get("/static/releases.js").get_data(as_text=True)
        self.assertIn("has_changes === false", script)
        self.assertIn("内容相同的新版本", script)
        self.assertIn("仍要重新发布", script)
        self.assertNotIn("勾选『选择公开』", script)


if __name__ == "__main__":
    unittest.main()
