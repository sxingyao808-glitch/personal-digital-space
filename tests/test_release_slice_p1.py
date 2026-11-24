"""P1 tests: controlled release bundle construction (prepare/build only).

Scope: explicit selection, state/visibility validation, immutable bundle,
manifest + SHA-256, boundary inspection, Windows atomic write/locking, and
registry backup. These tests do not activate a release and do not change the
public read route.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import archive.release as release_module
from archive.app import create_app
from archive.db import connect
from archive.release import (
    RELEASE_FORMAT,
    ReleaseRegistry,
    public_attachment_id,
    public_content_id,
)

TOKEN = "test-owner-token-release-246813579"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseTestCase(unittest.TestCase):
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

    def patch_content(self, content_id, revision, **fields):
        return self.owner.patch(
            f"/api/content/{content_id}",
            json={"revision": revision, **fields},
            headers=self.headers,
        )

    def add_relation(self, from_id, to_id, relation_type="PART_OF"):
        return self.owner.post(
            f"/api/content/{from_id}/relations",
            json={"to_id": to_id, "type": relation_type},
            headers=self.headers,
        )

    def add_attribute(self, content_id, key, value):
        return self.owner.post(
            f"/api/content/{content_id}/attributes",
            json={"key": key, "value": value, "provenance": "user", "confirmed": True},
            headers=self.headers,
        )

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

    def prepare(self, content_ids, attachment_ids):
        return self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids},
            headers=self.headers,
        )

    def release_index(self):
        path = self.data_dir / "releases" / "index.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def events(self):
        path = self.data_dir / "releases" / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def release_dirs(self):
        root = self.data_dir / "releases"
        if not root.is_dir():
            return []
        return [path.name for path in root.iterdir() if path.name.startswith("rel_")]

    def object_path(self, attachment_id):
        return self.data_dir / "objects" / attachment_id

    def read_bundle_bytes(self, release_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(release_dir).as_posix(): path.read_bytes()
            for path in release_dir.rglob("*")
            if path.is_file()
        }

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_build_prepared_release_without_attachments(self):
        content = self.create_content("公开条目", "正文内容", visibility="public")
        # P2: public routes read the active bundle only; no active means 404.
        before_public = self.owner.get(f"/api/public/content/{content['id']}")
        self.assertEqual(before_public.status_code, 404)

        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.get_json()["data"]

        self.assertEqual(data["status"], "prepared")
        self.assertIsNone(data["active_release_id"])
        self.assertTrue(data["inspection"]["ok"])
        self.assertTrue(data["audit_event_recorded"])
        self.assertFalse(data["boundaries"]["activated"])
        self.assertTrue(data["boundaries"]["publishes_content"])

        release_dir = Path(data["path"])
        self.assertTrue((release_dir / "release.json").is_file())
        self.assertTrue((release_dir / "content" / f"{public_content_id(content['id'])}.html").is_file())
        self.assertTrue((release_dir / "content" / f"{public_content_id(content['id'])}.json").is_file())
        self.assertTrue((release_dir / "assets" / "style.css").is_file())

        manifest = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], RELEASE_FORMAT)
        self.assertEqual(manifest["format_version"], 3)
        self.assertEqual(manifest["release_id"], data["release_id"])
        self.assertEqual(manifest["status"], "prepared")
        self.assertEqual(manifest["content_ids"], [content["id"]])
        self.assertEqual(manifest["attachment_ids"], [])
        self.assertEqual(len(manifest["bundle_sha256"]), 64)

        index = self.release_index()
        self.assertIsNotNone(index)
        self.assertIsNone(index["active_release_id"])
        self.assertEqual(len(index["releases"]), 1)
        self.assertEqual(index["releases"][0]["status"], "prepared")

        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "owner")
        self.assertEqual(events[0]["action"], "prepare")
        self.assertEqual(events[0]["result"], "prepared")

        after_prepare_public = self.owner.get(f"/api/public/content/{content['id']}")
        self.assertEqual(after_prepare_public.status_code, 404)

        activation = self.owner.post(
            f"/api/releases/{data['release_id']}/activate",
            json={},
            headers=self.headers,
        )
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        activated = activation.get_json()["data"]
        self.assertEqual(activated["status"], "active")
        self.assertFalse(activated["already_active"])

        after_public = self.owner.get(f"/api/public/content/{content['id']}")
        self.assertEqual(after_public.status_code, 200)
        payload = after_public.get_json()["data"]
        self.assertEqual(payload["id"], public_content_id(content["id"]))
        self.assertEqual(payload["title"], "公开条目")
        self.assertEqual(payload["body"], "正文内容")

        verify = self.owner.get(f"/api/releases/{data['release_id']}/verify")
        self.assertEqual(verify.status_code, 200)
        self.assertTrue(verify.get_json()["data"]["ok"])

        listing = self.owner.get("/api/releases")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["data"]["release_count"], 1)

    def test_private_content_is_rejected(self):
        content = self.create_content("私密条目", "不可公开", visibility="private")
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "release_validation_error")
        self.assertIn(
            "content_not_public",
            {issue["code"] for issue in error["details"]["issues"]},
        )
        self.assertEqual(self.release_dirs(), [])
        self.assertIsNone(self.release_index())

    def test_content_changed_to_private_is_rejected(self):
        content = self.create_content("将变私密", "body", visibility="public")
        patched = self.patch_content(content["id"], content["revision"], visibility="private")
        self.assertEqual(patched.status_code, 200, patched.get_data(as_text=True))
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("content_not_public", codes)

    def test_public_attachment_is_copied_only_when_selected(self):
        content = self.create_content("带附件", "正文")
        payload = b"public attachment bytes\n"
        attachment = self.upload(content["id"], payload, filename="note.txt")
        self.assertEqual(attachment["visibility"], "public")

        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.get_json()["data"]
        release_dir = Path(data["path"])
        copied = (
            release_dir
            / "attachments"
            / public_attachment_id(attachment["id"])
            / "note.txt"
        )
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), payload)
        self.assertEqual(sha256_bytes(copied.read_bytes()), attachment["sha256"])

        manifest = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["attachment_ids"], [attachment["id"]])
        item = manifest["items"][0]
        self.assertEqual(len(item["attachments"]), 1)
        self.assertEqual(
            item["attachments"][0]["path"],
            f"attachments/{public_attachment_id(attachment['id'])}/note.txt",
        )

        source = self.object_path(attachment["id"])
        self.assertEqual(source.read_bytes(), payload)

    def test_private_attachment_is_rejected(self):
        content = self.create_content("公开正文", "body")
        attachment = self.upload(content["id"], b"private bytes", visibility="private")
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_private", codes)
        self.assertEqual(self.release_dirs(), [])

    def test_attachment_from_unselected_content_is_rejected(self):
        selected = self.create_content("已选择", "body")
        other = self.create_content("未选择", "body")
        attachment = self.upload(other["id"], b"bytes", visibility="public")
        response = self.prepare([selected["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_content_not_selected", codes)

    def test_attachment_not_ready_is_rejected(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"bytes")
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE attachments SET state = 'BROKEN' WHERE id = ?", (attachment["id"],)
            )
            conn.commit()
        finally:
            conn.close()
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_not_ready", codes)

    def test_attachment_hash_mismatch_is_rejected(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"original bytes")
        self.object_path(attachment["id"]).write_bytes(b"tampered bytes")
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_hash_mismatch", codes)

    def test_missing_attachment_object_is_rejected(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"bytes")
        self.object_path(attachment["id"]).unlink()
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422)
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_file_missing", codes)

    # ------------------------------------------------------------------
    # B10-A: release copy must reject reparse points and incomplete metadata
    # ------------------------------------------------------------------
    def _null_attachment_metadata(self, attachment_id):
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute(
                "UPDATE attachments SET sha256 = NULL, byte_size = NULL WHERE id = ?",
                (attachment_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_null_attachment_metadata_is_rejected(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"bytes")
        self._null_attachment_metadata(attachment["id"])
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        codes = {issue["code"] for issue in response.get_json()["error"]["details"]["issues"]}
        self.assertIn("attachment_metadata_incomplete", codes)
        self.assertEqual(self.release_dirs(), [])
        self.assertIsNone(self.release_index())

    def test_symlinked_attachment_object_is_rejected(self):
        content = self.create_content("正文", "body")
        payload = b"real bytes that must never be published via a link"
        attachment = self.upload(content["id"], payload)
        source = self.object_path(attachment["id"])
        target = self.data_dir / "objects" / "outside-link-target.bin"
        target.write_bytes(payload)
        source.unlink()
        try:
            os.symlink(target, source)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        try:
            response = self.prepare([content["id"]], [attachment["id"]])
            self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
            codes = {
                issue["code"]
                for issue in response.get_json()["error"]["details"]["issues"]
            }
            self.assertIn("attachment_file_not_regular", codes)
            self.assertEqual(self.release_dirs(), [])
        finally:
            if source.is_symlink():
                source.unlink()

    @unittest.skipUnless(os.name == "nt", "junction requires Windows")
    def test_junction_attachment_object_is_rejected(self):
        content = self.create_content("正文", "body")
        payload = b"junction payload bytes"
        attachment = self.upload(content["id"], payload)
        source = self.object_path(attachment["id"])
        target = self.data_dir / "junction-target"
        target.mkdir()
        (target / "payload.bin").write_bytes(payload)
        source.unlink()
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.skipTest(
                f"junction unavailable: {result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            response = self.prepare([content["id"]], [attachment["id"]])
            self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
            codes = {
                issue["code"]
                for issue in response.get_json()["error"]["details"]["issues"]
            }
            self.assertIn("attachment_file_not_regular", codes)
            self.assertEqual(self.release_dirs(), [])
        finally:
            os.rmdir(source)

    def test_source_replaced_between_validation_and_copy_is_rejected(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"original payload")
        attachment_id = attachment["id"]
        original = release_module.ReleaseRegistry._validate_selection

        def validate_then_swap(registry_self, *args, **kwargs):
            original(registry_self, *args, **kwargs)
            # Deterministic TOCTOU window: replace the bytes after validation
            # has passed but before the copy opens the source.
            (registry_self.objects_dir / attachment_id).write_bytes(b"swapped payload!!")

        with mock.patch.object(
            release_module.ReleaseRegistry, "_validate_selection", validate_then_swap
        ):
            response = self.prepare([content["id"]], [attachment_id])
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        error = response.get_json()["error"]
        self.assertEqual(error["code"], "release_validation_error")
        self.assertIn("SHA-256", error["message"])
        self.assertEqual(self.release_dirs(), [])
        self.assertIsNone(self.release_index())

    def test_source_modified_during_copy_is_rejected(self):
        source = Path(self.tempdir.name) / "source.bin"
        destination = Path(self.tempdir.name) / "copied.bin"
        payload = b"payload-bytes" * 1000
        source.write_bytes(payload)
        real_fstat = os.fstat
        state = {"calls": 0}

        def racing_fstat(fd):
            info = real_fstat(fd)
            state["calls"] += 1
            if state["calls"] >= 2:
                # Report different metadata for the post-read check, as if the
                # source had been written to while the copy was running.
                return os.stat_result(
                    (
                        info.st_mode,
                        info.st_ino,
                        info.st_dev,
                        info.st_nlink,
                        info.st_uid,
                        info.st_gid,
                        info.st_size + 1,
                        info.st_atime,
                        info.st_mtime,
                        info.st_ctime,
                    )
                )
            return info

        with mock.patch.object(release_module.os, "fstat", racing_fstat):
            with self.assertRaises(release_module.ReleaseValidationError) as ctx:
                self.registry._copy_verified(
                    source, destination, sha256_bytes(payload), len(payload)
                )
        self.assertIn("复制过程中", str(ctx.exception))

    def test_copy_rejects_incomplete_expected_metadata(self):
        source = Path(self.tempdir.name) / "source.bin"
        destination = Path(self.tempdir.name) / "copied.bin"
        payload = b"payload"
        source.write_bytes(payload)
        for expected_sha, expected_size in ((None, len(payload)), (sha256_bytes(payload), None)):
            with self.assertRaises(release_module.ReleaseValidationError):
                self.registry._copy_verified(source, destination, expected_sha, expected_size)
            self.assertFalse(destination.exists())

    def test_bundle_promotion_failure_leaves_no_release(self):
        content = self.create_content("正文", "body")
        real_replace = os.replace

        def failing_replace(src, dst):
            if Path(src).name.startswith(".") and Path(src).is_dir():
                raise OSError("simulated promotion failure")
            return real_replace(src, dst)

        with mock.patch.object(release_module.os, "replace", failing_replace):
            response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 500, response.get_data(as_text=True))
        self.assertEqual(self.release_dirs(), [])
        self.assertIsNone(self.release_index())
        leftovers = [
            path.name
            for path in (self.data_dir / "releases").iterdir()
            if path.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_bundle_manifest_must_match_actual_bytes(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"attachment bytes")
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.get_json()["data"]
        release_dir = Path(data["path"])
        manifest = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
        copied = release_dir / manifest["items"][0]["attachments"][0]["path"]
        copied.write_bytes(b"tampered after build")

        inspection = self.registry._inspect_bundle(release_dir, data["release_id"])
        self.assertFalse(inspection["ok"])
        codes = {issue["code"] for issue in inspection["issues"]}
        self.assertIn("bundle_file_mismatch", codes)

    def test_empty_or_missing_selection_is_rejected(self):
        content = self.create_content("正文", "body")
        cases = [
            {"content_ids": [], "attachment_ids": []},
            {"content_ids": None, "attachment_ids": []},
            {"content_ids": [content["id"]]},
            {"content_ids": "not-a-list", "attachment_ids": []},
        ]
        for payload in cases:
            response = self.owner.post("/api/releases/prepare", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422, payload)
            self.assertEqual(response.get_json()["error"]["code"], "release_validation_error")
        self.assertEqual(self.release_dirs(), [])

    def test_build_failure_does_not_change_registry_or_active_pointer(self):
        content = self.create_content("合法", "body")
        first = self.prepare([content["id"]], [])
        self.assertEqual(first.status_code, 201)
        index_before = self.release_index()
        events_before = len(self.events())
        dirs_before = self.release_dirs()

        private = self.create_content("非法", "body", visibility="private")
        failed = self.prepare([private["id"]], [])
        self.assertEqual(failed.status_code, 422)

        self.assertEqual(self.release_index(), index_before)
        self.assertEqual(len(self.events()), events_before)
        self.assertEqual(self.release_dirs(), dirs_before)
        self.assertIsNone(self.release_index()["active_release_id"])

    def test_lock_contention_and_stale_lock_recovery(self):
        content = self.create_content("正文", "body")
        releases_dir = self.data_dir / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        lock = releases_dir / ".lock"
        lock.write_text("{}", encoding="utf-8")

        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["error"]["code"], "release_locked")

        stale = time.time() - 600
        os.utime(lock, (stale, stale))
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertFalse(lock.exists())

    def test_bundle_boundaries_exclude_relations_attributes_and_paths(self):
        private_target = self.create_content("PRIVATE-RELATION-TARGET", "secret", visibility="private")
        content = self.create_content("公开条目", "正文", visibility="public")
        self.add_relation(content["id"], private_target["id"], "REFERENCES")
        self.add_attribute(content["id"], "PRIVATE-ATTRIBUTE-KEY", "PRIVATE-ATTRIBUTE-VALUE")

        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_dir = Path(response.get_json()["data"]["path"])
        bundle = self.read_bundle_bytes(release_dir)
        secret = (self.data_dir / "owner_secret").read_text(encoding="utf-8").strip()
        for forbidden in (
            "PRIVATE-RELATION-TARGET",
            "PRIVATE-ATTRIBUTE-KEY",
            "PRIVATE-ATTRIBUTE-VALUE",
            str(self.data_dir),
            secret,
        ):
            for name, data in bundle.items():
                self.assertNotIn(forbidden.encode("utf-8"), data, f"{forbidden} leaked in {name}")

        manifest = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["boundaries"]["includes_relations"])
        self.assertFalse(manifest["boundaries"]["includes_attributes"])
        self.assertFalse(manifest["boundaries"]["includes_database"])
        self.assertFalse(manifest["boundaries"]["includes_local_paths"])
        self.assertFalse(manifest["boundaries"]["includes_private_attachments"])

    def test_html_escaping_prevents_markup_injection(self):
        content = self.create_content("<script>alert(1)</script>", "<b>body</b>")
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201)
        page = (
            Path(response.get_json()["data"]["path"])
            / "content"
            / f"{public_content_id(content['id'])}.html"
        ).read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;b&gt;body&lt;/b&gt;", page)

    def test_verify_detects_tampered_bundle(self):
        content = self.create_content("正文", "body")
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201)
        data = response.get_json()["data"]
        page = Path(data["path"]) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_text("tampered", encoding="utf-8")

        verify = self.registry.verify(data["release_id"])
        self.assertFalse(verify["ok"])
        self.assertIn("bundle_file_mismatch", {issue["code"] for issue in verify["issues"]})

    def test_verify_detects_manifest_tampering(self):
        content = self.create_content("正文", "body")
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201)
        data = response.get_json()["data"]
        manifest_path = Path(data["path"]) / "release.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["items"][0]["title"] = "tampered title"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        verify = self.registry.verify(data["release_id"])
        self.assertFalse(verify["ok"])
        self.assertIn(
            "manifest_sha256_mismatch", {issue["code"] for issue in verify["issues"]}
        )

    def test_owner_and_csrf_are_required(self):
        content = self.create_content("正文", "body")
        anonymous = self.anonymous.post(
            "/api/releases/prepare",
            json={"content_ids": [content["id"]], "attachment_ids": []},
        )
        self.assertEqual(anonymous.status_code, 401)

        missing_csrf = self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": [content["id"]], "attachment_ids": []},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        self.assertEqual(missing_csrf.get_json()["error"]["code"], "csrf_failed")

    def test_all_release_endpoints_require_owner_session(self):
        content = self.create_content("正文", "body")
        prepared = self.prepare([content["id"]], [])
        self.assertEqual(prepared.status_code, 201)
        release_id = prepared.get_json()["data"]["release_id"]

        requests = [
            ("get", "/api/releases", {}),
            ("get", f"/api/releases/{release_id}", {}),
            ("get", f"/api/releases/{release_id}/verify", {}),
            (
                "post",
                "/api/releases/prepare",
                {"json": {"content_ids": [content["id"]], "attachment_ids": []}},
            ),
        ]
        for method, path, kwargs in requests:
            response = getattr(self.anonymous, method)(path, **kwargs)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.get_json()["error"]["code"], "unauthorized")

        foreign_origin = self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": [content["id"]], "attachment_ids": []},
            headers={**self.headers, "Origin": "http://evil.example"},
        )
        self.assertEqual(foreign_origin.status_code, 403)
        self.assertEqual(
            foreign_origin.get_json()["error"]["code"], "origin_rejected"
        )

    def test_private_database_and_objects_are_not_modified(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"bytes")

        def snapshot():
            conn = connect(self.data_dir / "archive.db")
            try:
                content_rows = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT id, state, visibility, visibility_selected, revision, updated_at "
                        "FROM content ORDER BY id"
                    ).fetchall()
                ]
                attachment_rows = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT id, content_id, state, visibility, byte_size, sha256 "
                        "FROM attachments ORDER BY id"
                    ).fetchall()
                ]
                operations_count = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            finally:
                conn.close()
            return (
                content_rows,
                attachment_rows,
                operations_count,
                sha256_bytes(self.object_path(attachment["id"]).read_bytes()),
            )

        before = snapshot()
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 201)
        after = snapshot()
        self.assertEqual(before, after)

    def test_index_write_is_atomic_and_leaves_no_temp_files(self):
        content = self.create_content("正文", "body")
        response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 201)
        releases_dir = self.data_dir / "releases"
        leftovers = [path.name for path in releases_dir.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        index = self.release_index()
        self.assertEqual(index["format"], "personal-archive-release-registry")
        self.assertEqual(index["format_version"], 3)

    def test_event_append_failure_is_reported_and_reconciled(self):
        content = self.create_content("正文", "body")
        original = self.registry._append_event

        def failing_append(_event):
            raise OSError("simulated audit append failure")

        self.registry._append_event = failing_append
        try:
            response = self.prepare([content["id"]], [])
        finally:
            self.registry._append_event = original

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertFalse(data["audit_event_recorded"])
        self.assertTrue(data["audit_event_pending"])
        index = self.release_index()
        self.assertEqual(len(index["releases"]), 1)
        self.assertEqual(self.events(), [])

        reconciled = self.registry.reconcile_events()
        self.assertEqual(reconciled["appended"], 1)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], f"ev_prepare_{data['release_id']}")
        self.assertTrue(events[0]["reconciled"])
        self.assertEqual(events[0]["release_id"], data["release_id"])

        repeated = self.registry.reconcile_events()
        self.assertEqual(repeated["appended"], 0)
        self.assertEqual(len(self.events()), 1)

    def test_index_write_failure_leaves_no_release(self):
        content = self.create_content("正文", "body")
        with mock.patch.object(
            release_module, "_atomic_write", side_effect=OSError("simulated index write failure")
        ):
            response = self.prepare([content["id"]], [])
        self.assertEqual(response.status_code, 500, response.get_data(as_text=True))

        self.assertEqual(self.release_dirs(), [])
        self.assertIsNone(self.release_index())
        self.assertEqual(self.events(), [])
        leftover_files = [
            path.name
            for path in (self.data_dir / "releases").iterdir()
            if path.name.endswith(".tmp")
        ]
        self.assertEqual(leftover_files, [])

    def test_registry_backup_excludes_private_archive(self):
        content = self.create_content("正文", "body")
        attachment = self.upload(content["id"], b"bytes")
        response = self.prepare([content["id"]], [attachment["id"]])
        self.assertEqual(response.status_code, 201)
        data = response.get_json()["data"]

        target = Path(self.tempdir.name) / "registry-backup.zip"
        result = self.registry.backup(target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["release_count"], 1)
        self.assertFalse(result["boundaries"]["includes_private_database"])
        self.assertFalse(result["boundaries"]["includes_attachment_objects"])
        self.assertFalse(result["boundaries"]["includes_owner_secret"])

        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("registry/index.json", names)
            self.assertIn("registry/events.jsonl", names)
            self.assertIn(f"registry/{data['release_id']}/release.json", names)
            self.assertIn(
                f"registry/{data['release_id']}/attachments/{public_attachment_id(attachment['id'])}/note.txt",
                names,
            )
            for forbidden in ("archive.db", "owner_secret", "objects/", ".lock"):
                self.assertFalse(
                    any(forbidden in name for name in names),
                    f"{forbidden} must not be in registry backup",
                )
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            for entry in manifest["files"]:
                payload = archive.read(entry["path"])
                self.assertEqual(sha256_bytes(payload), entry["sha256"])
                self.assertEqual(len(payload), entry["bytes"])


if __name__ == "__main__":
    unittest.main()
