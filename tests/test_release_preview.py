"""P4 read-only release preview tests.

Every test uses an isolated temporary data directory and synthetic content.
No real publication, withdrawal or restore is performed against user data.
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

TOKEN = "test-owner-token-p4-preview-9753108642"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReleasePreviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.csrf = self.login(self.owner)
        self.headers = {"X-CSRF-Token": self.csrf}
        self.anonymous = self.app.test_client()
        self.registry = self.app.extensions["release_registry"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def login(self, client) -> str:
        response = client.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            return session["csrf"]

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

    def preview(self, content_ids, attachment_ids):
        return self.owner.post(
            "/api/releases/preview",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids},
            headers=self.headers,
        )

    def prepare_and_activate(self, content_ids, attachment_ids):
        prepared = self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids},
            headers=self.headers,
        )
        self.assertEqual(prepared.status_code, 201, prepared.get_data(as_text=True))
        release_id = prepared.get_json()["data"]["release_id"]
        activated = self.owner.post(
            f"/api/releases/{release_id}/activate",
            json={},
            headers=self.headers,
        )
        self.assertEqual(activated.status_code, 200, activated.get_data(as_text=True))
        return release_id

    # ------------------------------------------------------------------
    # Read-only behaviour
    # ------------------------------------------------------------------
    def test_preview_returns_projected_public_facts(self):
        first = self.create_content("第一篇", "正文一", visibility="public")
        second = self.create_content("第二篇", "正文二", visibility="public")
        attachment = self.upload(first["id"], b"hello preview", filename="note.txt")
        result = self.registry.preview_build(
            [first["id"], second["id"]], [attachment["id"]]
        )
        self.assertTrue(result["read_only"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["totals"]["content_count"], 2)
        self.assertEqual(result["totals"]["attachment_count"], 1)
        self.assertGreater(result["totals"]["attachment_bytes"], 0)
        self.assertEqual(
            result["boundaries"]["publishes_content"], False
        )
        self.assertEqual(result["boundaries"]["activated"], False)
        self.assertEqual(
            result["boundaries"]["includes_private_content"], False
        )
        self.assertEqual(
            result["boundaries"]["includes_local_paths"], False
        )
        for content in result["contents"]:
            self.assertEqual(
                content["payload_fields"],
                [
                    "attachments",
                    "body",
                    "id",
                    "payload_version",
                    "primary_space",
                    "summary",
                    "title",
                    "type",
                    "updated_at",
                ],
            )
            self.assertNotIn("path", content)
        self.assertEqual(result["attachments"][0]["content_id"], first["id"])
        self.assertNotIn("path", result["attachments"][0])
        self.assertNotIn("sha256", result["attachments"][0])

    def test_preview_does_not_write_registry_bundle_or_events(self):
        content = self.create_content("只读", "不应产生 Bundle", visibility="public")
        attachment = self.upload(content["id"], b"read-only bytes")
        releases_dir = self.data_dir / "releases"

        def snapshot() -> dict:
            return {
                "db": sha256_file(self.data_dir / "archive.db"),
                "object": sha256_file(self.data_dir / "objects" / attachment["id"]),
                "entries": sorted(
                    str(path.relative_to(self.data_dir))
                    for path in self.data_dir.rglob("*")
                    if path.is_file()
                ),
                "bundles": sorted(
                    path.name
                    for path in releases_dir.iterdir()
                    if path.is_dir() and path.name.startswith("rel_")
                )
                if releases_dir.is_dir()
                else [],
                "index": (releases_dir / "index.json").read_bytes()
                if (releases_dir / "index.json").is_file()
                else None,
                "events": (releases_dir / "events.jsonl").read_bytes()
                if (releases_dir / "events.jsonl").is_file()
                else None,
            }

        before = snapshot()
        result = self.registry.preview_build([content["id"]], [attachment["id"]])
        self.assertTrue(result["valid"])
        after = snapshot()
        self.assertEqual(after["bundles"], [])
        self.assertEqual(after["entries"], before["entries"])
        self.assertEqual(after["index"], before["index"])
        self.assertEqual(after["events"], before["events"])
        listed = self.registry.list()
        self.assertEqual(listed["release_count"], 0)
        self.assertEqual(listed["active_release_id"], None)
        self.assertEqual(sha256_file(self.data_dir / "archive.db"), before["db"])
        self.assertEqual(
            sha256_file(self.data_dir / "objects" / attachment["id"]),
            before["object"],
        )
        again = self.registry.preview_build([content["id"]], [attachment["id"]])
        self.assertEqual(result["totals"], again["totals"])

    def test_preview_reports_selection_issues_without_writing(self):
        private = self.create_content("私密", "不可公开", visibility="private")
        hidden_attachment = self.upload(
            private["id"], b"private", filename="secret.txt", visibility="private"
        )
        result = self.registry.preview_build([private["id"]], [])
        self.assertFalse(result["valid"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("content_not_public", codes)

        result = self.registry.preview_build(
            [private["id"]], [hidden_attachment["id"]]
        )
        self.assertFalse(result["valid"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("attachment_private", codes)

        missing = self.registry.preview_build(["c_missing"], [])
        self.assertFalse(missing["valid"])
        self.assertIn(
            "content_not_found", {issue["code"] for issue in missing["issues"]}
        )
        self.assertEqual(self.registry.list()["release_count"], 0)

    def test_preview_rejects_empty_or_malformed_selection(self):
        content = self.create_content("正常", "", visibility="public")
        with self.assertRaises(Exception) as empty:
            self.registry.preview_build([], [])
        self.assertEqual(
            getattr(empty.exception, "code", None), "release_validation_error"
        )
        with self.assertRaises(Exception) as missing_attachments:
            self.registry.preview_build([content["id"]], None)
        self.assertEqual(
            getattr(missing_attachments.exception, "code", None),
            "release_validation_error",
        )
        response = self.preview(["../escape"], [])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.get_json()["error"]["code"], "release_validation_error"
        )

    def test_preview_delta_tracks_active_release(self):
        first = self.create_content("一", "a", visibility="public")
        second = self.create_content("二", "b", visibility="public")
        active = self.prepare_and_activate([first["id"]], [])
        same = self.registry.preview_build([first["id"]], [])
        self.assertEqual(same["active_release_id"], active)
        self.assertTrue(same["delta"]["same_as_active"])
        self.assertEqual(same["delta"]["added_content_ids"], [])
        self.assertEqual(same["delta"]["removed_content_ids"], [])

        expanded = self.registry.preview_build([first["id"], second["id"]], [])
        self.assertFalse(expanded["delta"]["same_as_active"])
        self.assertEqual(expanded["delta"]["added_content_ids"], [second["id"]])
        self.assertEqual(expanded["delta"]["removed_content_ids"], [])

        narrowed = self.registry.preview_build([second["id"]], [])
        self.assertEqual(narrowed["delta"]["added_content_ids"], [second["id"]])
        self.assertEqual(narrowed["delta"]["removed_content_ids"], [first["id"]])

    def test_preview_attachment_delta_tracks_active_release(self):
        content = self.create_content("附件", "body", visibility="public")
        attachment = self.upload(content["id"], b"delta bytes")
        active = self.prepare_and_activate([content["id"]], [attachment["id"]])
        withheld = self.registry.preview_build([content["id"]], [])
        self.assertEqual(withheld["delta"]["active_release_id"], active)
        self.assertEqual(
            withheld["delta"]["removed_attachment_ids"], [attachment["id"]]
        )
        included = self.registry.preview_build([content["id"]], [attachment["id"]])
        self.assertTrue(included["delta"]["same_as_active"])

    # ------------------------------------------------------------------
    # Owner / security boundary
    # ------------------------------------------------------------------
    def test_preview_endpoint_requires_owner_csrf_and_post(self):
        response = self.anonymous.post(
            "/api/releases/preview",
            json={"content_ids": ["c_1"], "attachment_ids": []},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "unauthorized")

        without_csrf = self.owner.post(
            "/api/releases/preview",
            json={"content_ids": ["c_1"], "attachment_ids": []},
        )
        self.assertEqual(without_csrf.status_code, 403)
        self.assertEqual(
            without_csrf.get_json()["error"]["code"], "csrf_failed"
        )

        wrong_origin = self.owner.post(
            "/api/releases/preview",
            json={"content_ids": ["c_1"], "attachment_ids": []},
            headers={**self.headers, "Origin": "https://evil.example"},
        )
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(
            wrong_origin.get_json()["error"]["code"], "origin_rejected"
        )

        # ``/api/releases/<release_id>`` owns GET; "preview" is not a release.
        get_response = self.owner.get("/api/releases/preview")
        self.assertEqual(get_response.status_code, 404)

        rejected_host = self.owner.post(
            "/api/releases/preview",
            json={"content_ids": ["c_1"], "attachment_ids": []},
            headers={**self.headers, "Host": "evil.example"},
        )
        self.assertEqual(rejected_host.status_code, 403)
        self.assertEqual(
            rejected_host.get_json()["error"]["code"], "host_rejected"
        )

    def test_release_page_is_owner_only_and_no_store(self):
        login_page = self.anonymous.get("/releases")
        self.assertEqual(login_page.status_code, 302)
        self.assertIn("/login", login_page.headers.get("Location", ""))

        page = self.owner.get("/releases")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("releases.js", body)
        self.assertIn("发布管理", body)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")

    def test_preview_response_is_no_store_and_json(self):
        content = self.create_content("预览", "body", visibility="public")
        response = self.preview([content["id"]], [])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.mimetype, "application/json")
        data = response.get_json()["data"]
        self.assertTrue(data["read_only"])
        self.assertTrue(data["valid"])

    def test_preview_does_not_expose_private_paths_in_json(self):
        content = self.create_content("路径", "body", visibility="public")
        response = self.preview([content["id"]], [])
        text = response.get_data(as_text=True)
        self.assertNotIn(str(self.data_dir), text)
        self.assertNotIn("objects/", text)
        self.assertNotIn("archive.db", text)
        self.assertNotIn("release.json", text)


if __name__ == "__main__":
    unittest.main()
