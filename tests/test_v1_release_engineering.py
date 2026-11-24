"""V1.0 release engineering: withdrawal guarantees and Payload v2 boundaries.

All fixtures are synthetic and live in a temporary data directory. The real
archive database is never opened by these tests.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.release as release_module
from archive.app import create_app
from archive.release import (
    PUBLIC_ATTACHMENT_FIELDS,
    PUBLIC_PAYLOAD_FIELDS,
    ReleaseRegistry,
    public_attachment_id,
    public_content_id,
)

TOKEN = "test-owner-token-v1-release-engineering"

INTERNAL_CONTENT_PATTERN = re.compile(r"c_[0-9a-f]{32}")
INTERNAL_ATTACHMENT_PATTERN = re.compile(r"att_[0-9a-f]{32}")


class V1ReleaseEngineeringTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        self.headers = self.login(self.owner)
        self.anonymous = self.app.test_client()
        self.registry: ReleaseRegistry = self.app.extensions["release_registry"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def login(self, client) -> dict[str, str]:
        client.get(f"/auth?token={TOKEN}")
        with client.session_transaction() as session:
            return {"X-CSRF-Token": session["csrf"]}

    def create_content(self, title, body, visibility="public", **fields):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility, **fields},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def patch_content(self, content_id, revision, **fields):
        response = self.owner.patch(
            f"/api/content/{content_id}",
            json={"revision": revision, **fields},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def upload(self, content_id, payload: bytes, filename="note.txt"):
        response = self.owner.post(
            f"/api/content/{content_id}/attachments",
            data={
                "file": (io.BytesIO(payload), filename),
                "operation_id": f"op_{os.urandom(12).hex()}",
                "visibility": "public",
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

    def activate(self, release_id, *, confirm=False):
        payload = {"confirm_republish_withdrawn": True} if confirm else {}
        return self.owner.post(
            f"/api/releases/{release_id}/activate", json=payload, headers=self.headers
        )

    def publish(self, content_ids, attachment_ids=None, *, confirm=False):
        response = self.prepare(content_ids, attachment_ids)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_id = response.get_json()["data"]["release_id"]
        activation = self.activate(release_id, confirm=confirm)
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        return release_id

    def withdraw(self, payload=None):
        return self.owner.post(
            "/api/releases/withdraw", json=payload or {}, headers=self.headers
        )

    def restore(self, body):
        return self.owner.post("/api/releases/restore", json=body, headers=self.headers)

    def drift(self):
        response = self.owner.get("/api/publication/drift")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["data"]

    def index(self):
        path = self.data_dir / "releases" / "index.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def bundle(self, release_id):
        return self.data_dir / "releases" / release_id

    def marker(self):
        return self.data_dir / "releases" / release_module.WITHDRAWAL_MARKER

    def category_of(self, report, content_id):
        return [
            row["category"]
            for row in report["contents"]
            if row["content_id"] == content_id
        ]

    # ------------------------------------------------------------------
    # Payload v2
    # ------------------------------------------------------------------
    def test_payload_v2_is_whitelisted_and_hides_internal_ids(self):
        content = self.create_content(
            "合成公开条目",
            "第一行\n第二行",
            summary="这是公开摘要。",
            type="PROJECT",
            primary_space="BUILD",
        )
        attachment = self.upload(content["id"], b"PUBLIC-BYTES", "note.txt")
        release_id = self.publish([content["id"]], [attachment["id"]])

        response = self.anonymous.get(f"/api/public/content/{content['id']}")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()["data"]

        self.assertEqual(set(payload), PUBLIC_PAYLOAD_FIELDS)
        self.assertEqual(payload["id"], public_content_id(content["id"]))
        self.assertEqual(payload["title"], "合成公开条目")
        self.assertEqual(payload["summary"], "这是公开摘要。")
        self.assertEqual(payload["body"], "第一行\n第二行")
        self.assertEqual(payload["type"], "PROJECT")
        self.assertEqual(payload["primary_space"], "BUILD")
        self.assertEqual(payload["payload_version"], 2)
        self.assertEqual(len(payload["attachments"]), 1)
        self.assertEqual(set(payload["attachments"][0]), PUBLIC_ATTACHMENT_FIELDS)
        self.assertEqual(
            payload["attachments"][0]["id"],
            public_attachment_id(attachment["id"]),
        )
        self.assertEqual(
            payload["attachments"][0]["url"],
            f"attachments/{public_attachment_id(attachment['id'])}/note.txt",
        )

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(content["id"], serialized)
        self.assertNotIn(attachment["id"], serialized)
        self.assertNotIn("revision", payload)
        self.assertNotIn("relations", payload)
        self.assertNotIn("attributes", payload)
        self.assertNotIn("related_spaces", payload)

        page = self.anonymous.get(f"/p/{payload['id']}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("合成公开条目", html)
        self.assertIn("这是公开摘要。", html)
        self.assertIn("PROJECT", html)
        self.assertIn("BUILD", html)
        self.assertNotIn(content["id"], html)
        self.assertNotIn(attachment["id"], html)

        home = self.anonymous.get("/").get_data(as_text=True)
        self.assertIn(payload["id"], home)
        self.assertIn("这是公开摘要。", home)
        self.assertNotIn(content["id"], home)

        bundle = self.bundle(release_id)
        self.assertTrue((bundle / "content" / f"{payload['id']}.json").is_file())
        self.assertFalse((bundle / "content" / f"{content['id']}.json").exists())
        self.assertFalse(
            (bundle / "attachments" / attachment["id"]).exists()
        )

        # release.json is an internal manifest, never part of deployed files.
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        declared = {entry["path"] for entry in manifest["files"]}
        self.assertNotIn("release.json", declared)

        for path in bundle.rglob("*"):
            if not path.is_file() or path.name == "release.json":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(INTERNAL_CONTENT_PATTERN.search(text), path)
            self.assertIsNone(INTERNAL_ATTACHMENT_PATTERN.search(text), path)
            for forbidden in ("revision", "related_spaces", "archive.db", "objects/"):
                self.assertNotIn(forbidden, text, path)

    def test_private_content_cannot_be_built_or_read_publicly(self):
        content = self.create_content("私密合成条目", "secret", visibility="private")
        response = self.prepare([content["id"]])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 404
        )

    # ------------------------------------------------------------------
    # Withdrawal
    # ------------------------------------------------------------------
    def test_withdraw_subset_stops_public_access_and_updates_ledger(self):
        keep = self.create_content("保留条目", "keep")
        drop = self.create_content("撤回条目", "drop", summary="将撤回")
        attachment = self.upload(drop["id"], b"DROP", "drop.txt")
        first = self.publish([keep["id"], drop["id"]], [attachment["id"]])
        drop_public = public_content_id(drop["id"])
        self.assertEqual(self.anonymous.get(f"/p/{drop_public}").status_code, 200)

        updated = self.patch_content(drop["id"], drop["revision"], visibility="private")
        report = self.drift()
        self.assertIn(
            "PUBLISHED_BUT_NOW_PRIVATE", self.category_of(report, drop["id"])
        )

        response = self.withdraw({"content_ids": [drop["id"]]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        successor = data["release_id"]
        self.assertNotEqual(successor, first)
        self.assertEqual(self.index()["active_release_id"], successor)
        self.assertFalse(self.marker().exists())

        home = self.anonymous.get("/").get_data(as_text=True)
        self.assertNotIn("撤回条目", home)
        self.assertIn("保留条目", home)
        self.assertEqual(self.anonymous.get(f"/p/{drop_public}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{drop['id']}").status_code, 404
        )
        self.assertEqual(
            self.anonymous.get(
                f"/attachments/{public_attachment_id(attachment['id'])}/drop.txt"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.anonymous.get(f"/p/{public_content_id(keep['id'])}").status_code, 200
        )

        index = self.index()
        ledger = index["withdrawals"]["content"][drop["id"]]
        self.assertIsNone(ledger["reauthorized_at"])
        self.assertEqual(ledger["release_id"], successor)
        records = {row["release_id"]: row for row in index["releases"]}
        self.assertEqual(records[first]["status"], "superseded")
        self.assertEqual(records[successor]["withdrawn_content_ids"], [drop["id"]])

        # The withdrawn content is still PRIVATE in the source database.
        self.assertEqual(updated["visibility"], "private")

        repeated = self.withdraw({"content_ids": [drop["id"]]})
        self.assertEqual(repeated.status_code, 422)

    def test_withdraw_all_is_idempotent_and_fail_closed_marker_blocks_public(self):
        content = self.create_content("整体撤回", "body")
        self.publish([content["id"]])

        self.marker().write_text("{}", encoding="utf-8")
        self.assertEqual(self.anonymous.get("/").status_code, 503)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 503
        )
        self.marker().unlink()

        first = self.withdraw({})
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.index()["active_release_id"], None)

        again = self.withdraw({})
        self.assertEqual(again.status_code, 200, again.get_data(as_text=True))
        self.assertTrue(again.get_json()["data"]["already_withdrawn"])

    def test_failed_withdraw_leaves_public_entry_fail_closed(self):
        content = self.create_content("失败关闭", "body")
        keep = self.create_content("失败关闭保留", "keep")
        self.publish([content["id"], keep["id"]])

        with mock.patch.object(
            self.registry,
            "_build_successor_locked",
            side_effect=RuntimeError("synthetic bundle failure"),
        ):
            failed = self.withdraw({"content_ids": [content["id"]]})
        self.assertGreaterEqual(failed.status_code, 400)
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.anonymous.get("/").status_code, 503)
        self.assertEqual(
            self.anonymous.get(f"/p/{public_content_id(content['id'])}").status_code,
            503,
        )
        # Emergency fail-closed may already clear the pointer; the marker keeps
        # the public entry at 503 either way.
        self.assertIsNone(self.index()["active_release_id"])

        recovered = self.withdraw({})
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        self.assertFalse(self.marker().exists())
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

    def test_registry_write_failure_keeps_withdrawal_fail_closed(self):
        content = self.create_content("注册表失败", "body")
        keep = self.create_content("注册表失败保留", "keep")
        self.publish([content["id"], keep["id"]])
        real_atomic_write = release_module._atomic_write

        def flaky(path, data):
            if Path(path).name == "index.json":
                raise OSError("synthetic registry write failure")
            return real_atomic_write(path, data)

        with mock.patch.object(release_module, "_atomic_write", side_effect=flaky):
            failed = self.withdraw({"content_ids": [content["id"]]})
        self.assertGreaterEqual(failed.status_code, 400)
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.anonymous.get("/").status_code, 503)

        # Recovery is an explicit owner action; it also clears active.
        self.marker().unlink()
        recovered = self.withdraw({})
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

    def test_rollback_and_reactivation_cannot_bypass_withdrawal(self):
        content = self.create_content("回滚防护", "body")
        first = self.publish([content["id"]])
        self.withdraw({})
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

        # Re-activating the old (now superseded/withdrawn) bundle is rejected.
        rollback = self.activate(first, confirm=True)
        self.assertIn(rollback.status_code, (409, 422), rollback.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

        # A new Release is required; activation still needs explicit consent.
        prepared = self.prepare([content["id"]])
        self.assertEqual(prepared.status_code, 201)
        new_release = prepared.get_json()["data"]["release_id"]
        blocked = self.activate(new_release)
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

        allowed = self.activate(new_release, confirm=True)
        self.assertEqual(allowed.status_code, 200, allowed.get_data(as_text=True))
        self.assertEqual(
            self.anonymous.get(f"/p/{public_content_id(content['id'])}").status_code,
            200,
        )
        ledger = self.index()["withdrawals"]["content"][content["id"]]
        self.assertIsNotNone(ledger["reauthorized_at"])

    def test_restore_respects_current_visibility_and_withdrawal_ledger(self):
        content = self.create_content("恢复门禁", "body")
        source = self.publish([content["id"]])
        self.withdraw({})
        # Content is still PUBLIC but the ledger blocks silent restoration.
        blocked = self.restore({"source_release_id": source})
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

        self.patch_content(content["id"], content["revision"], visibility="private")
        denied = self.restore(
            {"source_release_id": source, "confirm_republish_withdrawn": True}
        )
        self.assertEqual(denied.status_code, 422, denied.get_data(as_text=True))
        self.assertIn(self.anonymous.get("/").status_code, (302, 404))

    # ------------------------------------------------------------------
    # Full isolated lifecycle drill
    # ------------------------------------------------------------------
    def test_release_lifecycle_isolation_drill(self):
        a = self.create_content(
            "演练 A",
            "A 正文",
            summary="A 摘要",
            type="THOUGHT",
            primary_space="THINK",
        )
        b = self.create_content(
            "演练 B", "B 正文", type="EXPERIENCE", primary_space="JOURNEY"
        )
        attachment = self.upload(a["id"], b"A-FILE", "a.txt")
        original = self.publish([a["id"], b["id"]], [attachment["id"]])
        a_public = public_content_id(a["id"])
        b_public = public_content_id(b["id"])

        home = self.anonymous.get("/").get_data(as_text=True)
        self.assertIn("演练 A", home)
        self.assertIn("演练 B", home)
        payload = self.anonymous.get(f"/api/public/content/{a_public}").get_json()["data"]
        self.assertEqual(set(payload), PUBLIC_PAYLOAD_FIELDS)
        self.assertEqual(payload["type"], "THOUGHT")

        self.patch_content(a["id"], a["revision"], visibility="private")
        self.assertIn("PUBLISHED_BUT_NOW_PRIVATE", self.category_of(self.drift(), a["id"]))

        withdrawn = self.withdraw({"content_ids": [a["id"]]})
        self.assertEqual(withdrawn.status_code, 200, withdrawn.get_data(as_text=True))
        successor = withdrawn.get_json()["data"]["release_id"]
        self.assertNotEqual(successor, original)

        self.assertNotIn("演练 A", self.anonymous.get("/").get_data(as_text=True))
        self.assertEqual(self.anonymous.get(f"/p/{a_public}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{a['id']}").status_code, 404
        )
        self.assertEqual(self.anonymous.get(f"/p/{b_public}").status_code, 200)
        # Old bundle remains on disk (immutable evidence), but is not served.
        self.assertTrue(self.bundle(original).is_dir())
        self.assertEqual(self.anonymous.get(f"/p/{a_public}").status_code, 404)

        # Cache invalidation: the active index change is observed immediately.
        self.registry_ext = self.app.extensions["release_reader"]
        self.assertEqual(self.registry_ext.snapshot().release_id, successor)

        # Rollback attempts cannot resurrect the withdrawn content.
        self.assertIn(self.activate(original, confirm=True).status_code, (409, 422))
        self.assertIn(
            self.restore({"source_release_id": original}).status_code, (409, 422)
        )
        self.assertEqual(self.anonymous.get(f"/p/{a_public}").status_code, 404)

        # Explicit reauthorization: restore produces a new Release, not a
        # resurrection of the old bundle.
        self.patch_content(a["id"], self.fetch_revision(a["id"]), visibility="public")
        restored = self.restore(
            {
                "source_release_id": original,
                "confirm_republish_withdrawn": True,
                "reason": "drill reauthorization",
            }
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        restored_id = restored.get_json()["data"]["release_id"]
        self.assertNotEqual(restored_id, original)
        self.assertNotEqual(restored_id, successor)
        self.assertEqual(self.anonymous.get(f"/p/{a_public}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{b_public}").status_code, 200)
        ledger = self.index()["withdrawals"]["content"][a["id"]]
        self.assertIsNotNone(ledger["reauthorized_at"])

    def fetch_revision(self, content_id):
        response = self.owner.get(f"/api/content/{content_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["data"]["revision"]

    # ------------------------------------------------------------------
    # Static hosting isolation
    # ------------------------------------------------------------------
    def _deployable_files(self, release_id):
        bundle = self.bundle(release_id)
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        return bundle, [entry["path"] for entry in manifest["files"]]

    def test_static_deployment_subset_excludes_manifest_database_and_internal_ids(self):
        content = self.create_content("静态部署", "body", summary="摘要")
        release_id = self.publish([content["id"]])
        bundle, paths = self._deployable_files(release_id)
        self.assertTrue(paths)
        self.assertNotIn("release.json", paths)
        for path in paths:
            self.assertTrue(
                path == "index.html"
                or path.startswith(("content/", "attachments/", "assets/", "spaces/")),
                path,
            )

        deployed = Path(self.tempdir.name) / "static" / "current"
        for path in paths:
            target = deployed / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((bundle / path).read_bytes())

        self.assertFalse((deployed / "release.json").exists())
        self.assertFalse(list(deployed.rglob("*.db")))
        self.assertFalse(list(deployed.rglob("*.py")))
        self.assertFalse(list(deployed.rglob("objects")))
        for file in deployed.rglob("*"):
            if not file.is_file():
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(INTERNAL_CONTENT_PATTERN.search(text), file)
            self.assertIsNone(INTERNAL_ATTACHMENT_PATTERN.search(text), file)
            self.assertNotIn("archive.db", text)
            self.assertNotIn("private", text.lower())

    def test_static_current_pointer_switch_drops_withdrawn_content(self):
        keep = self.create_content("静态保留", "keep")
        drop = self.create_content("静态撤回", "drop")
        first = self.publish([keep["id"], drop["id"]])
        _, first_paths = self._deployable_files(first)
        drop_public = public_content_id(drop["id"])
        self.assertIn(f"content/{drop_public}.html", first_paths)

        self.withdraw({"content_ids": [drop["id"]]})
        successor = self.index()["active_release_id"]
        _, next_paths = self._deployable_files(successor)
        self.assertNotIn(f"content/{drop_public}.html", next_paths)
        self.assertNotIn(f"content/{drop_public}.json", next_paths)
        self.assertIn(
            f"content/{public_content_id(keep['id'])}.html", next_paths
        )

        # Immutable old version directory still exists on disk; this is the
        # documented CDN/static-hosting limitation (must be deleted/purged by
        # the hosting layer, not by the application).
        old_dir = Path(self.tempdir.name) / "static" / "versions" / first
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "content").mkdir(exist_ok=True)
        self.assertTrue(self.bundle(first).is_dir())

    def test_public_routes_set_security_headers_and_block_traversal(self):
        content = self.create_content("响应头", "body")
        self.publish([content["id"]])
        response = self.anonymous.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

        traversal = self.anonymous.get("/assets/../release.json")
        self.assertIn(traversal.status_code, (400, 404))
        self.assertNotIn("bundle_sha256", traversal.get_data(as_text=True))

    def test_disallowed_host_is_rejected(self):
        response = self.anonymous.get("/", base_url="http://evil.example")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "host_rejected")


if __name__ == "__main__":
    unittest.main()
