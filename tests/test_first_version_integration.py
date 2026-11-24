"""P5 first-version integration checks.

Cross-page checks for HOME, the owner release console, the public page and the
404 page, plus one end-to-end lifecycle that crosses those pages. All state is
synthetic and isolated in a temporary data directory; nothing touches real
owner data and no real publication happens.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app

TOKEN = "test-owner-token-p5-1357924680"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FirstVersionIntegrationTestCase(unittest.TestCase):
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

    def create_content(self, title="", body="", visibility="public"):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def upload(self, content_id, payload: bytes, filename="note.txt", visibility="public"):
        response = self.owner.post(
            f"/api/content/{content_id}/attachments",
            data={
                "file": (io.BytesIO(payload), filename),
                "operation_id": f"op_{os.urandom(12).hex()}",
                "visibility": visibility,
            },
            content_type="multipart/form-data",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]["attachment"]

    def prepare(self, content_ids, attachment_ids=None):
        return self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids or []},
            headers=self.headers,
        )

    def activate(self, release_id):
        return self.owner.post(
            f"/api/releases/{release_id}/activate", json={}, headers=self.headers
        )

    def publish(self, content_ids, attachment_ids=None):
        response = self.prepare(content_ids, attachment_ids)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_id = response.get_json()["data"]["release_id"]
        activation = self.activate(release_id)
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        return release_id

    def withdraw(self, payload=None):
        return self.owner.post(
            "/api/releases/withdraw", json=payload or {}, headers=self.headers
        )

    def restore(self, body):
        return self.owner.post("/api/releases/restore", json=body, headers=self.headers)

    def bundle_hashes(self, release_id):
        root = self.data_dir / "releases" / release_id
        return {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in root.rglob("*")
            if path.is_file()
        }

    # ------------------------------------------------------------------
    # P5-A: pages, navigation and return paths
    # ------------------------------------------------------------------
    def test_pages_share_navigation_and_return_paths(self):
        home = self.owner.get("/")
        self.assertEqual(home.status_code, 200)
        home_html = home.get_data(as_text=True)
        self.assertIn('class="top-nav"', home_html)
        self.assertIn('href="/releases"', home_html)
        self.assertIn('href="/"', home_html)
        self.assertIn('aria-current="page"', home_html)

        console = self.owner.get("/releases")
        self.assertEqual(console.status_code, 200)
        console_html = console.get_data(as_text=True)
        self.assertIn('class="top-nav"', console_html)
        self.assertIn('href="/"', console_html)
        self.assertIn('href="/releases"', console_html)
        self.assertIn("releases.js", console_html)
        self.assertIn("发布管理", console_html)

        not_found = self.anonymous.get("/p/c_missing_page")
        self.assertEqual(not_found.status_code, 404)
        missing_html = not_found.get_data(as_text=True)
        self.assertIn("notfound-shell", missing_html)
        self.assertIn('href="/"', missing_html)
        self.assertIn("返回 Archive", missing_html)

        for path in ("/", "/releases", "/p/c_missing_page"):
            response = self.owner.get(path) if path != "/p/c_missing_page" else self.anonymous.get(path)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store", path)

    def test_visitors_are_kept_out_of_owner_pages(self):
        for path in ("/", "/releases"):
            response = self.anonymous.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/login", response.headers.get("Location", ""), path)

    # ------------------------------------------------------------------
    # P5-A: end-to-end lifecycle across owner page and public page
    # ------------------------------------------------------------------
    def test_first_version_end_to_end_release_lifecycle(self):
        first = self.create_content("第一篇", "第一行\n第二行")
        second = self.create_content("第二篇", "另一篇正文")
        private = self.create_content("私密草稿", "不应公开", visibility="private")
        attachment = self.upload(first["id"], b"PUBLIC-ATTACHMENT")

        release_id = self.publish([first["id"], second["id"]], [attachment["id"]])

        page = self.anonymous.get(f"/p/{first['id']}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("第一篇", html)
        self.assertIn("note.txt", html)
        self.assertNotIn(private["id"], html)
        self.assertNotIn("不应公开", html)

        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{private['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{private['id']}").status_code, 404
        )
        download = self.anonymous.get(f"/attachments/{attachment['id']}/note.txt")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, b"PUBLIC-ATTACHMENT")
        download.close()

        detail = self.owner.get(f"/api/releases/{release_id}")
        self.assertEqual(detail.status_code, 200)
        detail_data = detail.get_json()["data"]
        self.assertEqual(
            detail_data["manifest"]["content_ids"], [first["id"], second["id"]]
        )
        self.assertEqual(
            detail_data["manifest"]["attachment_ids"], [attachment["id"]]
        )
        detail_text = detail.get_data(as_text=True)
        self.assertNotIn(private["id"], detail_text)
        self.assertNotIn("不应公开", detail_text)

        preview = self.owner.post(
            "/api/releases/preview",
            json={
                "content_ids": [first["id"], second["id"]],
                "attachment_ids": [attachment["id"]],
            },
            headers=self.headers,
        )
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        preview_data = preview.get_json()["data"]
        self.assertTrue(preview_data["read_only"])
        self.assertTrue(preview_data["valid"])
        self.assertTrue(preview_data["delta"]["same_as_active"])

        before_hashes = self.bundle_hashes(release_id)
        withdrawn = self.withdraw({"content_ids": [first["id"]]})
        self.assertEqual(withdrawn.status_code, 200, withdrawn.get_data(as_text=True))
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code, 404
        )
        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 200)

        blocked = self.restore({"source_release_id": release_id})
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertEqual(
            blocked.get_json()["error"]["code"], "restore_requires_confirmation"
        )
        self.assertEqual(
            blocked.get_json()["error"]["details"]["previously_withdrawn"][
                "content_ids"
            ],
            [first["id"]],
        )

        restored = self.restore(
            {
                "source_release_id": release_id,
                "confirm_republish_withdrawn": True,
            }
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 200)
        restored_page = self.anonymous.get(f"/p/{first['id']}").get_data(as_text=True)
        self.assertIn("第一篇", restored_page)
        self.assertIn("note.txt", restored_page)
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code,
            200,
        )
        self.assertEqual(self.anonymous.get(f"/p/{private['id']}").status_code, 404)

        self.assertEqual(
            self.bundle_hashes(release_id), before_hashes, "historical bundle changed"
        )
        for path in (self.data_dir / "releases").rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    private["id"],
                    path.read_text(encoding="utf-8", errors="ignore"),
                    f"private content leaked into {path.name}",
                )

    # ------------------------------------------------------------------
    # P5-D: public routes never fall back to the private database
    # ------------------------------------------------------------------
    def test_public_routes_have_no_private_fallback(self):
        unpublished = self.create_content("未发布公开", "只在数据库中")
        attachment = self.upload(unpublished["id"], b"NOT-PUBLIC-YET")

        response = self.anonymous.get(f"/p/{unpublished['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{unpublished['id']}").status_code, 404
        )
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code, 404
        )

        page = self.anonymous.get("/p/c_missing_page")
        html = page.get_data(as_text=True)
        self.assertNotIn(str(self.data_dir), html)
        self.assertNotIn("archive.db", html)
        self.assertNotIn("objects/", html)

    def test_anonymous_cannot_use_owner_release_endpoints(self):
        release_id = self.publish([self.create_content("公开", "正文")["id"]])
        attempts = [
            ("post", "/api/releases/preview", {"content_ids": []}),
            ("post", "/api/releases/prepare", {"content_ids": []}),
            ("post", "/api/releases/withdraw", {}),
            ("post", "/api/releases/restore", {"source_release_id": release_id}),
            ("post", f"/api/releases/{release_id}/activate", {}),
            ("get", "/api/releases", None),
            ("get", "/api/releases/history", None),
        ]
        for method, path, payload in attempts:
            if method == "get":
                response = self.anonymous.get(path)
            else:
                response = self.anonymous.post(path, json=payload or {})
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual(
            self.anonymous.post("/api/content", json={"title": "x"}).status_code, 401
        )


if __name__ == "__main__":
    unittest.main()
