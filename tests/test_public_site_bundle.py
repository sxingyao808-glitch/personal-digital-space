"""Public site bundle (Batch A) tests.

The active Release Bundle must contain a browsable public site: a root
``index.html``, one page per published space, content reading pages with
home/space/related navigation, and strictly no private content. Anonymous
readers never need to log in; the owner-only preview serves the same files with
a banner.
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
from archive.release import public_content_id

TOKEN = "test-owner-token-public-site-24680"


class PublicSiteBundleTestCase(unittest.TestCase):
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

    def create_content(self, title, body, visibility="public", space="ME"):
        response = self.owner.post(
            "/api/content",
            json={
                "title": title,
                "body": body,
                "visibility": visibility,
                "primary_space": space,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_relation(self, from_id, to_id, relation_type="REFERENCES"):
        response = self.owner.post(
            f"/api/content/{from_id}/relations",
            json={"to_id": to_id, "type": relation_type},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

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
    # Home and space navigation
    # ------------------------------------------------------------------
    def test_home_lists_published_items_and_spaces(self):
        me_item = self.create_content("ME 条目", "ME 正文", space="ME")
        build_item = self.create_content("BUILD 条目", "BUILD 正文", space="BUILD")
        self.publish([me_item["id"], build_item["id"]])

        response = self.anonymous.get("/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('class="public-shell"', page)
        self.assertIn("ME 条目", page)
        self.assertIn("BUILD 条目", page)
        self.assertIn('href="content/p_', page)
        self.assertIn('href="spaces/ME/"', page)
        self.assertIn('href="spaces/BUILD/"', page)
        self.assertIn('href="assets/style.css"', page)
        self.assertNotIn('id="search-input"', page)

        self.assertEqual(self.anonymous.get("/index.html").status_code, 200)

    def test_space_page_lists_only_that_space(self):
        me_item = self.create_content("属于 ME", "正文", space="ME")
        build_item = self.create_content("属于 BUILD", "正文", space="BUILD")
        self.publish([me_item["id"], build_item["id"]])

        response = self.anonymous.get("/spaces/ME/")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("属于 ME", page)
        self.assertNotIn("属于 BUILD", page)
        self.assertIn('href="../../"', page)
        self.assertIn('href="../../assets/style.css"', page)
        self.assertIn('aria-current="page"', page)
        self.assertEqual(self.anonymous.get("/spaces/BUILD").status_code, 200)

    # ------------------------------------------------------------------
    # Content reading pages
    # ------------------------------------------------------------------
    def test_content_page_links_home_space_and_related(self):
        first = self.create_content("起点内容", "第一段", space="ME")
        second = self.create_content("终点内容", "第二段", space="BUILD")
        self.add_relation(first["id"], second["id"], "LED_TO")
        self.publish([first["id"], second["id"]])

        page = self.anonymous.get(f"/p/{first['id']}").get_data(as_text=True)
        self.assertIn("起点内容", page)
        self.assertIn('href="../"', page)
        self.assertIn('href="../spaces/ME/"', page)
        self.assertIn("相关内容", page)
        self.assertIn("终点内容", page)
        self.assertIn(f'href="{public_content_id(second["id"])}.html"', page)

        direct = self.anonymous.get(
            f"/content/{public_content_id(first['id'])}.html"
        )
        self.assertEqual(direct.status_code, 200)
        self.assertIn("起点内容", direct.get_data(as_text=True))

    def test_refresh_returns_content_page(self):
        item = self.create_content("可刷新条目", "正文")
        self.publish([item["id"]])
        for _ in range(2):
            response = self.anonymous.get(f"/p/{item['id']}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("可刷新条目", response.get_data(as_text=True))

    # ------------------------------------------------------------------
    # Isolation and failure modes
    # ------------------------------------------------------------------
    def test_private_content_and_relations_never_appear(self):
        public = self.create_content("公开条目", "公开正文", space="ME")
        private = self.create_content("内部草稿", "内部正文", visibility="private")
        self.add_relation(public["id"], private["id"], "REFERENCES")
        self.publish([public["id"]])

        for path in ("/", "/spaces/ME/", f"/p/{public['id']}"):
            page = self.anonymous.get(path).get_data(as_text=True)
            self.assertNotIn("内部草稿", page, path)
            self.assertNotIn("内部正文", page, path)
            self.assertNotIn(private["id"], page, path)
            self.assertNotIn(public_content_id(private["id"]), page, path)

    def test_unknown_paths_are_not_found(self):
        item = self.create_content("公开条目", "正文")
        self.publish([item["id"]])

        self.assertEqual(self.anonymous.get("/p/p_does_not_exist").status_code, 404)
        self.assertEqual(
            self.anonymous.get("/content/p_does_not_exist.html").status_code, 404
        )
        self.assertEqual(self.anonymous.get("/spaces/NOPE/").status_code, 404)
        self.assertEqual(self.anonymous.get("/assets/missing.css").status_code, 404)

        styles = self.anonymous.get("/assets/style.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn("text/css", styles.headers["Content-Type"])

    def test_no_active_release_has_no_public_site(self):
        self.assertEqual(self.anonymous.get("/index.html").status_code, 404)
        self.assertEqual(self.anonymous.get("/content/p_x.html").status_code, 404)
        self.assertEqual(self.anonymous.get("/spaces/ME/").status_code, 404)
        root = self.anonymous.get("/")
        self.assertEqual(root.status_code, 302)
        self.assertIn("/login", root.headers["Location"])

    def test_withdraw_closes_the_public_site(self):
        item = self.create_content("公开条目", "正文")
        self.publish([item["id"]])
        self.assertEqual(self.anonymous.get("/").status_code, 200)

        self.withdraw_all()
        self.assertEqual(self.anonymous.get("/").status_code, 302)
        self.assertEqual(self.anonymous.get("/index.html").status_code, 404)
        self.assertEqual(self.anonymous.get("/spaces/ME/").status_code, 404)

    def test_bundle_is_static_host_ready(self):
        item = self.create_content("静态站点条目", "正文", space="ME")
        self.publish([item["id"]])
        root = next(
            path
            for path in (self.data_dir / "releases").iterdir()
            if (path / "release.json").is_file()
        )
        for relative in (
            "index.html",
            "spaces/ME/index.html",
            f"content/{public_content_id(item['id'])}.html",
            "assets/style.css",
        ):
            self.assertTrue((root / relative).is_file(), relative)

        home = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="assets/style.css"', home)
        self.assertIn('href="spaces/ME/"', home)
        self.assertIn(f'href="content/{public_content_id(item["id"])}.html"', home)

        content_page = (
            root / "content" / f"{public_content_id(item['id'])}.html"
        ).read_text(encoding="utf-8")
        self.assertIn('href="../assets/style.css"', content_page)
        self.assertIn('href="../"', content_page)
        self.assertIn('href="../spaces/ME/"', content_page)

    # ------------------------------------------------------------------
    # Owner-only local preview
    # ------------------------------------------------------------------
    def test_preview_is_owner_only_and_marked(self):
        item = self.create_content("预览条目", "正文", space="ME")
        self.publish([item["id"]])

        anonymous = self.anonymous.get("/preview/")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login", anonymous.headers["Location"])

        home = self.owner.get("/preview/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("当前本机网站预览", home.get_data(as_text=True))

        page = self.owner.get(f"/preview/content/{public_content_id(item['id'])}.html")
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        self.assertIn("预览条目", markup)
        self.assertIn("当前本机网站预览", markup)

        styles = self.owner.get("/preview/assets/style.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn("text/css", styles.headers["Content-Type"])


if __name__ == "__main__":
    unittest.main()
