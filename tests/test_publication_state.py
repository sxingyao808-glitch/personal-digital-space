"""Batch C unified publication-state tests.

Every UI reads one authoritative backend view: private, selected-but-unpublished,
published, published-with-local-edits, withdrawn, and an incomplete/failed last
publish. States are derived from the drift report and the withdrawal ledger.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.store import ArchiveStore

TOKEN = "test-owner-token-publication-state-11223"


class PublicationStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.store = ArchiveStore(self.data_dir)
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

    def state(self):
        response = self.owner.get("/api/publication/state")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def by_id(self, state):
        return {entry["content_id"]: entry for entry in state["contents"]}

    def one_click(self):
        response = self.owner.post(
            "/api/publish/one-click", json={"confirm": True}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def test_private_and_selected_states(self):
        private = self.store.create_content("私有内容", "正文", visibility="private")
        selected = self.store.create_content("待公开内容", "正文", visibility="public")

        state = self.state()
        entries = self.by_id(state)
        self.assertEqual(entries[private["id"]]["state"], "private")
        self.assertEqual(entries[private["id"]]["label"], "私有")
        self.assertEqual(entries[selected["id"]]["state"], "selected_not_published")
        self.assertEqual(
            entries[selected["id"]]["label"], "已选择公开，尚未发布"
        )
        self.assertEqual(state["counts"]["private"], 1)
        self.assertEqual(state["counts"]["selected_not_published"], 1)
        self.assertEqual(state["deployment"]["status"], "not_configured")

    def test_published_then_stale_then_withdrawn(self):
        content = self.store.create_content("状态内容", "初版", visibility="public")
        published = self.one_click()

        state = self.state()
        entry = self.by_id(state)[content["id"]]
        self.assertEqual(entry["state"], "published")
        self.assertEqual(entry["label"], "已发布到本机")
        self.assertEqual(entry["public_url"], f"/p/{content['id']}")
        self.assertEqual(state["release"]["active_release_id"], published["release_id"])

        self.store.update_content(content["id"], content["revision"], body="修改后")
        entry = self.by_id(self.state())[content["id"]]
        self.assertEqual(entry["state"], "published_stale")
        self.assertEqual(entry["label"], "内容已修改，等待更新")
        self.assertNotEqual(entry["published_revision"], entry["current_revision"])

        response = self.owner.post(
            "/api/releases/withdraw", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        state = self.state()
        entry = self.by_id(state)[content["id"]]
        self.assertEqual(entry["state"], "withdrawn")
        self.assertEqual(entry["label"], "已从本机撤回")
        self.assertIsNone(entry["public_url"])
        self.assertTrue(entry["withdrawn_before"])
        self.assertIsNone(state["release"]["active_release_id"])

    def test_incomplete_publish_is_reported(self):
        content = self.store.create_content("未完成内容", "正文", visibility="public")
        prepared = self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": [content["id"]], "attachment_ids": []},
            headers=self.headers,
        )
        self.assertEqual(prepared.status_code, 201)
        release_id = prepared.get_json()["data"]["release_id"]

        state = self.state()
        self.assertIsNotNone(state["last_failed_attempt"])
        self.assertEqual(state["last_failed_attempt"]["release_id"], release_id)
        self.assertIn("没有完成", state["last_failed_attempt"]["message"])
        # The content itself is still "selected but not published".
        self.assertEqual(
            self.by_id(state)[content["id"]]["state"], "selected_not_published"
        )

        self.one_click()
        state = self.state()
        self.assertIsNone(state["last_failed_attempt"])
        self.assertEqual(self.by_id(state)[content["id"]]["state"], "published")

    def test_state_requires_owner_session(self):
        response = self.anonymous.get("/api/publication/state")
        self.assertEqual(response.status_code, 401)

    def test_state_is_backed_by_release_data_not_frontend_guess(self):
        content = self.store.create_content("数据来源内容", "正文", visibility="public")
        self.one_click()
        state = self.state()
        self.assertEqual(state["release"]["state"], "verified")
        self.assertEqual(state["release"]["bundle_verified"], True)
        self.assertEqual(len(state["contents"]), 1)
        entry = self.by_id(state)[content["id"]]
        self.assertEqual(entry["current_revision"], entry["published_revision"])


if __name__ == "__main__":
    unittest.main()
