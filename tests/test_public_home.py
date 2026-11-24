"""Public entry page (anonymous home) tests.

The anonymous entry page must be derived exclusively from the active Release
Bundle: no active release means no listing, and only explicitly published
content may appear.
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

TOKEN = "test-owner-token-public-home-13579"


class PublicHomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)
        self.anonymous = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def login(self, client) -> dict[str, str]:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return {"X-CSRF-Token": csrf}

    def create_content(self, title, body, visibility):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def publish(self, content_ids):
        response = self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": []},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_id = response.get_json()["data"]["release_id"]
        activation = self.owner.post(
            f"/api/releases/{release_id}/activate", json={}, headers=self.headers
        )
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        return release_id

    def withdraw_all(self):
        response = self.owner.post(
            "/api/releases/withdraw", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    # ------------------------------------------------------------------
    # 1. Fail closed without an active release
    # ------------------------------------------------------------------
    def test_anonymous_root_redirects_to_login_without_release(self):
        response = self.anonymous.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    # ------------------------------------------------------------------
    # 2. Listing contains published items only
    # ------------------------------------------------------------------
    def test_public_home_lists_only_published_content(self):
        public = self.create_content("公开的条目", "公开正文", "public")
        private = self.create_content("私密的条目", "私密正文", "private")
        self.publish([public["id"]])

        response = self.anonymous.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("公开的条目", page)
        self.assertIn('href="content/p_', page)
        self.assertNotIn(public["id"], page)
        self.assertNotIn("私密的条目", page)
        self.assertNotIn(private["id"], page)
        self.assertNotIn("私密正文", page)

    def test_public_home_orders_by_updated_at_desc(self):
        first = self.create_content("较早的条目", "正文一", "public")
        second = self.create_content("较晚的条目", "正文二", "public")
        self.publish([first["id"], second["id"]])

        page = self.anonymous.get("/").get_data(as_text=True)
        self.assertIn("较早的条目", page)
        self.assertIn("较晚的条目", page)

    # ------------------------------------------------------------------
    # 3. Owner keeps the owner UI at the same path
    # ------------------------------------------------------------------
    def test_owner_root_keeps_private_console(self):
        public = self.create_content("公开的条目", "公开正文", "public")
        self.publish([public["id"]])
        response = self.owner.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('id="search-input"', page)
        self.assertNotIn("public-list-item", page)

    # ------------------------------------------------------------------
    # 5. Withdraw returns the entry page to fail-closed
    # ------------------------------------------------------------------
    def test_withdraw_removes_public_home(self):
        public = self.create_content("公开的条目", "公开正文", "public")
        self.publish([public["id"]])
        self.assertEqual(self.anonymous.get("/").status_code, 200)

        self.withdraw_all()
        response = self.anonymous.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    # ------------------------------------------------------------------
    # 6. Headers
    # ------------------------------------------------------------------
    def test_public_home_headers(self):
        public = self.create_content("公开的条目", "公开正文", "public")
        self.publish([public["id"]])
        response = self.anonymous.get("/")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    # ------------------------------------------------------------------
    # 7. Content page links back to the entry page
    # ------------------------------------------------------------------
    def test_public_content_page_links_back_to_home(self):
        public = self.create_content("公开的条目", "公开正文", "public")
        self.publish([public["id"]])
        page = self.anonymous.get(f"/p/{public['id']}").get_data(as_text=True)
        self.assertIn('<a href="../">公开内容 · Archive</a>', page)


if __name__ == "__main__":
    unittest.main()
