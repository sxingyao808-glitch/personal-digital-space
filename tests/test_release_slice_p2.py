"""P2 tests: activation, public active-bundle reads, withdrawal, audit.

Every test uses an isolated temporary data directory and never touches real
user data. Public routes must resolve the active Release Bundle only; the
private database and object store are never a public fallback.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import connect
from archive.release import (
    RELEASE_FORMAT,
    ReleaseError,
    ReleaseRegistry,
    _RegistryLock,
    public_attachment_id,
    public_content_id,
)
from archive.store import ArchiveError

TOKEN = "test-owner-token-p2-9753108642"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReleaseP2TestCase(unittest.TestCase):
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

    def prepare(self, content_ids, attachment_ids=None, *, token_headers=None):
        return self.owner.post(
            "/api/releases/prepare",
            json={"content_ids": content_ids, "attachment_ids": attachment_ids or []},
            headers=token_headers or self.headers,
        )

    def activate(self, release_id, *, confirm_republish_withdrawn=False):
        payload = {}
        if confirm_republish_withdrawn:
            payload["confirm_republish_withdrawn"] = True
        return self.owner.post(
            f"/api/releases/{release_id}/activate", json=payload, headers=self.headers
        )

    def publish(
        self, content_ids, attachment_ids=None, *, confirm_republish_withdrawn=False
    ):
        response = self.prepare(content_ids, attachment_ids)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        release_id = response.get_json()["data"]["release_id"]
        activation = self.activate(
            release_id, confirm_republish_withdrawn=confirm_republish_withdrawn
        )
        self.assertEqual(activation.status_code, 200, activation.get_data(as_text=True))
        return release_id

    def withdraw(self, payload=None):
        return self.owner.post(
            "/api/releases/withdraw", json=payload or {}, headers=self.headers
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

    def bundle_dir(self, release_id):
        return self.data_dir / "releases" / release_id

    def object_path(self, attachment_id):
        return self.data_dir / "objects" / attachment_id

    def db_snapshot(self):
        db_path = self.data_dir / "archive.db"
        conn = connect(db_path)
        try:
            operations = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
            attachments = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
            content = conn.execute("SELECT COUNT(*) FROM content").fetchone()[0]
        finally:
            conn.close()
        objects = {
            path.name: sha256_file(path)
            for path in (self.data_dir / "objects").iterdir()
            if path.is_file()
        }
        return {
            "db": sha256_file(db_path),
            "operations": operations,
            "attachments": attachments,
            "content": content,
            "objects": objects,
        }

    # ------------------------------------------------------------------
    # Bundle v2 / activation
    # ------------------------------------------------------------------
    def test_no_active_release_serves_nothing(self):
        content = self.create_content("公开", "公开正文")
        attachment = self.upload(content["id"], b"PUBLIC-BYTES")
        self.assertIsNone(self.release_index())

        for path in (
            f"/p/{content['id']}",
            f"/api/public/content/{content['id']}",
            "/assets/style.css",
            f"/attachments/{attachment['id']}/note.txt",
            f"/api/attachments/{attachment['id']}/file",
        ):
            self.assertEqual(self.anonymous.get(path).status_code, 404, path)
        self.assertEqual(self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 404)
        self.assertFalse((self.data_dir / "releases" / "index.json").exists())

    def test_activate_serves_page_payload_assets_and_attachment(self):
        content = self.create_content("公开标题", "第一行\n第二行")
        attachment = self.upload(content["id"], b"PUBLIC-ATTACHMENT")
        release_id = self.publish([content["id"]], [attachment["id"]])

        index = self.release_index()
        self.assertEqual(index["format_version"], 3)
        self.assertEqual(index["active_release_id"], release_id)
        record = next(item for item in index["releases"] if item["release_id"] == release_id)
        self.assertEqual(record["status"], "active")
        self.assertIsNotNone(record["activated_at"])
        manifest = json.loads(
            (self.bundle_dir(release_id) / "release.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["format_version"], 3)
        self.assertTrue(manifest["boundaries"]["includes_public_payload"])
        self.assertEqual(
            {entry["kind"] for entry in manifest["files"]},
            {"payload", "content", "asset", "attachment", "site"},
        )

        page = self.anonymous.get(f"/p/{content['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers.get("Cache-Control"), "no-store")
        html = page.get_data(as_text=True)
        for marker in (
            'class="public-shell"',
            'class="brand-mark"',
            'class="public-title"',
            'class="public-body"',
            'class="public-attachments"',
            'class="attachment-link"',
            "../assets/style.css",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('class="panel"', html)
        self.assertIn("公开标题", html)
        self.assertIn("更新于", html)

        payload_response = self.anonymous.get(f"/api/public/content/{content['id']}")
        self.assertEqual(payload_response.status_code, 200)
        self.assertEqual(payload_response.headers.get("Cache-Control"), "no-store")
        payload = payload_response.get_json()["data"]
        self.assertEqual(payload["id"], public_content_id(content["id"]))
        self.assertEqual(payload["body"], "第一行\n第二行")
        self.assertEqual(
            payload["attachments"][0]["id"], public_attachment_id(attachment["id"])
        )
        self.assertEqual(payload["attachments"][0]["filename"], "note.txt")
        self.assertEqual(payload["attachments"][0]["byte_size"], len(b"PUBLIC-ATTACHMENT"))
        self.assertNotIn("relations", payload)
        self.assertNotIn("attributes", payload)

        styles = self.anonymous.get("/assets/style.css")
        self.assertEqual(styles.status_code, 200)
        self.assertEqual(styles.headers.get("Cache-Control"), "no-store")
        self.assertIn("--brass", styles.get_data(as_text=True))

        download = self.anonymous.get(f"/attachments/{attachment['id']}/note.txt")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers.get("Cache-Control"), "no-store")
        self.assertEqual(download.data, b"PUBLIC-ATTACHMENT")
        download.close()

        api_download = self.anonymous.get(f"/api/attachments/{attachment['id']}/file")
        self.assertEqual(api_download.status_code, 200)
        self.assertEqual(api_download.data, b"PUBLIC-ATTACHMENT")
        api_download.close()

        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/other.txt").status_code, 404
        )
        self.assertEqual(self.anonymous.get("/assets/../archive.db").status_code, 404)

    def test_content_missing_from_active_bundle_is_404(self):
        published = self.create_content("已发布", "公开")
        hidden = self.create_content("未发布", "也公开")
        self.publish([published["id"]])
        self.assertEqual(self.anonymous.get(f"/p/{published['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{hidden['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{hidden['id']}").status_code, 404
        )

    def test_v1_prepared_bundle_is_not_activatable_and_record_is_kept(self):
        release_id = "rel_v1_legacy"
        release_dir = self.data_dir / "releases" / release_id
        release_dir.mkdir(parents=True)
        manifest = {
            "format": RELEASE_FORMAT,
            "format_version": 1,
            "release_id": release_id,
            "created_at": "2026-01-01T00:00:00Z",
            "content_ids": ["c_legacy"],
            "attachment_ids": [],
            "items": [],
            "files": [],
            "bundle_sha256": sha256_bytes(b"[]"),
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        (release_dir / "release.json").write_bytes(manifest_bytes)
        index = {
            "format": "personal-archive-release-registry",
            "format_version": 1,
            "active_release_id": None,
            "releases": [
                {
                    "release_id": release_id,
                    "created_at": manifest["created_at"],
                    "parent_release_id": None,
                    "action": "prepare",
                    "status": "prepared",
                    "content_ids": ["c_legacy"],
                    "attachment_ids": [],
                    "bundle_sha256": manifest["bundle_sha256"],
                    "manifest_sha256": sha256_bytes(manifest_bytes),
                    "manifest_bytes": len(manifest_bytes),
                    "file_count": 0,
                    "total_bytes": 0,
                }
            ],
        }
        index_path = self.data_dir / "releases" / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")

        response = self.activate(release_id)
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        data = response.get_json()["error"]
        self.assertIn(
            "release_format_unsupported",
            [issue["code"] for issue in data["details"]["issues"]],
        )
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["format_version"], 1)
        self.assertIsNone(raw["active_release_id"])
        self.assertEqual(raw["releases"][0]["release_id"], release_id)
        self.assertTrue(release_dir.is_dir())
        self.assertEqual(self.anonymous.get("/p/c_legacy").status_code, 404)

    def test_activation_rejects_stale_release_and_keeps_old_active(self):
        a = self.create_content("A", "A-BODY")
        b = self.create_content("B", "B-BODY")
        active_a = self.publish([a["id"]])
        prepared = self.prepare([b["id"]])
        self.assertEqual(prepared.status_code, 201)
        b_release = prepared.get_json()["data"]["release_id"]

        changed = self.patch_content(b["id"], b["revision"], body="B-CHANGED")
        self.assertEqual(changed.status_code, 200)

        response = self.activate(b_release)
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        self.assertEqual(self.release_index()["active_release_id"], active_a)
        self.assertEqual(self.anonymous.get(f"/p/{a['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{b['id']}").status_code, 404)

    def test_activation_rejects_tampered_bundle(self):
        a = self.create_content("A", "A-BODY")
        b = self.create_content("B", "B-BODY")
        active_a = self.publish([a["id"]])
        prepared = self.prepare([b["id"]])
        self.assertEqual(prepared.status_code, 201)
        b_release = prepared.get_json()["data"]["release_id"]
        page = self.bundle_dir(b_release) / "content" / f"{public_content_id(b['id'])}.html"
        page.write_bytes(page.read_bytes() + b"tampered")

        response = self.activate(b_release)
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        self.assertEqual(self.release_index()["active_release_id"], active_a)
        self.assertEqual(self.anonymous.get(f"/p/{a['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{b['id']}").status_code, 404)

    def test_duplicate_activation_is_idempotent(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        before = self.release_index()
        before_events = len(self.events())
        second = self.activate(release_id)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["data"]["already_active"])
        after = self.release_index()
        self.assertEqual(after["registry_revision"], before["registry_revision"])
        self.assertEqual(after["active_release_id"], release_id)
        self.assertEqual(len(self.events()), before_events)

    def test_activation_is_blocked_while_registry_lock_is_held(self):
        content = self.create_content("A", "A-BODY")
        prepared = self.prepare([content["id"]])
        release_id = prepared.get_json()["data"]["release_id"]
        lock = _RegistryLock(self.registry.releases_dir)
        with lock:
            response = self.activate(release_id)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIsNone(self.release_index()["active_release_id"])
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 404)

    def test_concurrent_activation_yields_single_active_release(self):
        content = self.create_content("A", "A-BODY")
        prepared = self.prepare([content["id"]])
        release_id = prepared.get_json()["data"]["release_id"]
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def worker() -> None:
            barrier.wait()
            try:
                outcomes.append(("ok", self.registry.activate(release_id)))
            except ArchiveError as exc:
                outcomes.append(("error", exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 2)
        oks = [value for kind, value in outcomes if kind == "ok"]
        self.assertGreaterEqual(len(oks), 1)
        for kind, value in outcomes:
            if kind == "error":
                self.assertEqual(value.status, 409)
        self.assertEqual(
            self.registry.list()["active_release_id"], release_id, self.release_index()
        )
        index = self.release_index()
        self.assertEqual(
            [item["status"] for item in index["releases"]].count("active"), 1
        )

    def test_event_failure_after_commit_is_pending_and_reconciled(self):
        a = self.create_content("A", "A-BODY")
        b = self.create_content("B", "B-BODY")
        self.publish([a["id"]])
        prepared = self.prepare([b["id"]])
        b_release = prepared.get_json()["data"]["release_id"]

        original = self.registry._append_event

        def failing_append(event):
            if event.get("action") == "activate":
                raise OSError("simulated post-commit event failure")
            return original(event)

        self.registry._append_event = failing_append
        try:
            response = self.activate(b_release)
        finally:
            self.registry._append_event = original

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        self.assertTrue(result["audit_event_recorded"] is False)
        self.assertTrue(result["audit_event_pending"])
        self.assertEqual(self.release_index()["active_release_id"], b_release)
        self.assertEqual(self.anonymous.get(f"/p/{b['id']}").status_code, 200)
        self.assertNotIn(
            f"ev_activate_{b_release}", [event["event_id"] for event in self.events()]
        )
        report = self.registry.audit(write_report=False)
        self.assertIn(f"ev_activate_{b_release}", report["audit_event_pending"])

        reconciled = self.registry.reconcile_events()
        self.assertGreaterEqual(reconciled["appended"], 1)
        event_ids = [event["event_id"] for event in self.events()]
        self.assertIn(f"ev_activate_{b_release}", event_ids)
        self.assertEqual(self.release_index()["active_release_id"], b_release)
        self.assertEqual(self.registry.audit(write_report=False)["audit_event_pending"], [])

    # ------------------------------------------------------------------
    # Corrupt active state
    # ------------------------------------------------------------------
    def test_corrupt_active_bundle_returns_503_without_db_fallback(self):
        content = self.create_content("A", "A-BODY")
        attachment = self.upload(content["id"], b"ATTACH")
        release_id = self.publish([content["id"]], [attachment["id"]])

        page = self.bundle_dir(release_id) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_bytes(b"tampered-page")
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 503)

        payload_path = self.bundle_dir(release_id) / "content" / f"{public_content_id(content['id'])}.json"
        payload_path.write_bytes(b"tampered-payload")
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 503
        )

        style = self.bundle_dir(release_id) / "assets" / "style.css"
        style.write_bytes(b"tampered-style")
        self.assertEqual(self.anonymous.get("/assets/style.css").status_code, 503)

        attachment_file = next(
            path
            for path in (self.bundle_dir(release_id) / "attachments").rglob("*")
            if path.is_file()
        )
        attachment_file.write_bytes(b"tampered-attachment")
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code, 503
        )
        self.assertEqual(
            self.anonymous.get(f"/api/attachments/{attachment['id']}/file").status_code, 503
        )
        # Private database still holds the content but is never used publicly.
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 503)

    def test_corrupt_registry_returns_503(self):
        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        (self.data_dir / "releases" / "index.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 503)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 503
        )
        self.assertEqual(self.anonymous.get("/assets/style.css").status_code, 503)

    # ------------------------------------------------------------------
    # Withdrawal
    # ------------------------------------------------------------------
    def test_withdraw_all_removes_public_access_and_keeps_history(self):
        content = self.create_content("A", "A-BODY")
        attachment = self.upload(content["id"], b"ATTACH")
        release_id = self.publish([content["id"]], [attachment["id"]])
        before_db = self.db_snapshot()
        old_bundle_hash = sha256_bytes(
            (self.bundle_dir(release_id) / "release.json").read_bytes()
        )

        response = self.withdraw({})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        self.assertTrue(result["changed"])
        self.assertIsNone(result["active_release_id"])

        index = self.release_index()
        self.assertIsNone(index["active_release_id"])
        self.assertIsNone(index["active_since"])
        record = next(item for item in index["releases"] if item["release_id"] == release_id)
        self.assertEqual(record["status"], "withdrawn")
        self.assertEqual(record["withdrawal_scope"], "all")
        self.assertIsNotNone(record["withdrawn_at"])
        self.assertIn(f"ev_withdraw_{release_id}", [e["event_id"] for e in self.events()])

        for path in (
            f"/p/{content['id']}",
            f"/api/public/content/{content['id']}",
            "/assets/style.css",
            f"/attachments/{attachment['id']}/note.txt",
            f"/api/attachments/{attachment['id']}/file",
        ):
            self.assertEqual(self.anonymous.get(path).status_code, 404, path)

        self.assertTrue((self.bundle_dir(release_id) / "release.json").is_file())
        self.assertEqual(
            sha256_bytes((self.bundle_dir(release_id) / "release.json").read_bytes()),
            old_bundle_hash,
        )
        self.assertEqual(self.db_snapshot(), before_db)

        owner_download = self.owner.get(f"/api/attachments/{attachment['id']}/file")
        self.assertEqual(owner_download.status_code, 200)
        self.assertEqual(owner_download.data, b"ATTACH")
        owner_download.close()

        repeated = self.withdraw({})
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.get_json()["data"]["already_withdrawn"])

    def test_withdraw_all_is_allowed_when_active_bundle_is_corrupt(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        page = self.bundle_dir(release_id) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_bytes(b"tampered")
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 503)

        response = self.withdraw({})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIsNone(self.release_index()["active_release_id"])
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 404)

    def test_subset_withdrawal_builds_successor_bundle(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        attachment = self.upload(first["id"], b"A-ATTACHMENT")
        old_release = self.publish([first["id"], second["id"]], [attachment["id"]])
        old_dir = self.bundle_dir(old_release)
        old_files = {
            path.relative_to(old_dir).as_posix(): sha256_file(path)
            for path in old_dir.rglob("*")
            if path.is_file()
        }

        response = self.withdraw({"content_ids": [first["id"]], "reason": "retract A"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        successor = result["release_id"]
        self.assertNotEqual(successor, old_release)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["previous_active_release_id"], old_release)
        self.assertEqual(result["withdrawn_content_ids"], [first["id"]])
        self.assertEqual(result["withdrawn_attachment_ids"], [attachment["id"]])
        self.assertEqual(result["content_ids"], [second["id"]])

        index = self.release_index()
        self.assertEqual(index["active_release_id"], successor)
        old_record = next(item for item in index["releases"] if item["release_id"] == old_release)
        self.assertEqual(old_record["status"], "superseded")
        self.assertEqual(old_record["superseded_by_release_id"], successor)
        new_record = next(item for item in index["releases"] if item["release_id"] == successor)
        self.assertEqual(new_record["status"], "active")
        self.assertEqual(new_record["action"], "withdraw")
        self.assertEqual(new_record["parent_release_id"], old_release)
        self.assertEqual(new_record["withdrawal_scope"], "content_subset")

        manifest = json.loads((self.bundle_dir(successor) / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["action"], "withdraw")
        self.assertEqual(manifest["parent_release_id"], old_release)
        self.assertEqual(manifest["withdrawn_content_ids"], [first["id"]])
        self.assertEqual(manifest["attachment_ids"], [])
        self.assertTrue((self.bundle_dir(successor) / "assets" / "style.css").is_file())
        self.assertFalse((self.bundle_dir(successor) / "content" / f"{public_content_id(first['id'])}.html").exists())

        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{first['id']}").status_code, 404
        )
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code, 404
        )
        self.assertEqual(
            self.anonymous.get(f"/api/attachments/{attachment['id']}/file").status_code, 404
        )
        self.assertEqual(self.anonymous.get(f"/p/{old_release}").status_code, 404)

        current_files = {
            path.relative_to(old_dir).as_posix(): sha256_file(path)
            for path in old_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(current_files, old_files)
        self.assertIn(f"ev_activate_{successor}", [e["event_id"] for e in self.events()])

    def test_subset_withdrawal_rejects_invalid_selection(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        attachment = self.upload(first["id"], b"ATTACH")
        self.publish([first["id"], second["id"]], [attachment["id"]])

        unknown = self.withdraw({"content_ids": ["c_does_not_exist"]})
        self.assertEqual(unknown.status_code, 422, unknown.get_data(as_text=True))

        attachment_only = self.withdraw({"attachment_ids": [attachment["id"]]})
        self.assertEqual(attachment_only.status_code, 422)
        codes = [
            issue["code"]
            for issue in attachment_only.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("attachment_only_withdrawal_not_supported", codes)

        removes_all = self.withdraw({"content_ids": [first["id"], second["id"]]})
        self.assertEqual(removes_all.status_code, 422)
        self.assertEqual(self.release_index()["active_release_id"] is not None, True)

        empty_subset = self.withdraw({"content_ids": []})
        self.assertEqual(empty_subset.status_code, 422)
        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 200)

        withdrawn = self.withdraw({})
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 404)

    def test_subset_withdrawal_uses_active_bundle_not_private_database(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "PUBLISHED-BODY")
        self.publish([first["id"], second["id"]])

        changed = self.patch_content(second["id"], second["revision"], body="EDITED-AFTER-PUBLISH")
        self.assertEqual(changed.status_code, 200)
        operator_db = self.db_snapshot()

        response = self.withdraw({"content_ids": [first["id"]]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        successor = response.get_json()["data"]["release_id"]

        page = self.anonymous.get(f"/p/{second['id']}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("PUBLISHED-BODY", html)
        self.assertNotIn("EDITED-AFTER-PUBLISH", html)
        manifest = json.loads((self.bundle_dir(successor) / "release.json").read_text(encoding="utf-8"))
        payload_file = manifest["items"][0]["payload_path"]
        payload = json.loads((self.bundle_dir(successor) / payload_file).read_text(encoding="utf-8"))
        self.assertEqual(payload["body"], "PUBLISHED-BODY")
        self.assertEqual(self.db_snapshot(), operator_db)

    def test_reader_cache_is_invalidated_by_state_changes(self):
        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 200)
        self.withdraw({})
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 404)
        self.publish([content["id"]], confirm_republish_withdrawn=True)
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 200)

    # ------------------------------------------------------------------
    # Audit / backup / private data
    # ------------------------------------------------------------------
    def test_audit_classifies_orphans_without_mutation(self):
        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        orphan = self.data_dir / "releases" / "rel_orphan_test"
        orphan.mkdir()
        (orphan / "release.json").write_text("{}", encoding="utf-8")
        invalid = self.data_dir / "releases" / "rel_orphan_invalid"
        invalid.mkdir()
        (invalid / "release.json").write_text("{bad", encoding="utf-8")
        staging = self.data_dir / "releases" / ".rel_staged.abc.tmp"
        staging.mkdir()

        response = self.owner.get("/api/releases/audit")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        report = response.get_json()["data"]
        codes = [finding["code"] for finding in report["findings"]]
        self.assertIn("UNREFERENCED_BUNDLE", codes)
        self.assertIn("UNREFERENCED_MANIFEST_INVALID", codes)
        self.assertIn("STAGING_LEFTOVER", codes)
        self.assertEqual(report["action_taken"], "none")
        self.assertFalse(report["blocking_public"])
        self.assertTrue(orphan.is_dir())
        self.assertTrue(invalid.is_dir())
        self.assertTrue(staging.is_dir())
        self.assertTrue(Path(report["report_path"]).is_file())

    def test_audit_detects_missing_referenced_bundle_and_active_corruption(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        index_path = self.data_dir / "releases" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["releases"].append(
            {
                "release_id": "rel_missing_test",
                "created_at": "2026-01-01T00:00:00Z",
                "parent_release_id": None,
                "action": "prepare",
                "status": "prepared",
                "content_ids": [],
                "attachment_ids": [],
                "bundle_sha256": None,
                "manifest_sha256": None,
                "manifest_bytes": 0,
                "file_count": 0,
                "total_bytes": 0,
            }
        )
        index_path.write_text(json.dumps(index), encoding="utf-8")

        report = self.registry.audit(write_report=False)
        codes = [finding["code"] for finding in report["findings"]]
        self.assertIn("REFERENCED_BUNDLE_MISSING", codes)
        self.assertFalse(report["blocking_public"])

        page = self.bundle_dir(release_id) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_bytes(b"tampered")
        report = self.registry.audit(write_report=False)
        codes = [finding["code"] for finding in report["findings"]]
        self.assertIn("ACTIVE_BUNDLE_INVALID", codes)
        self.assertTrue(report["blocking_public"])
        self.assertTrue(page.is_file())

    def test_audit_detects_event_only_release(self):
        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        with (self.data_dir / "releases" / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"event_id": "ev_prepare_rel_event_only", "action": "prepare"}
                )
                + "\n"
            )
        report = self.registry.audit(write_report=False)
        codes = [finding["code"] for finding in report["findings"]]
        self.assertIn("EVENT_ONLY_RELEASE", codes)
        self.assertEqual(report["action_taken"], "none")

    def test_startup_audit_is_recorded_and_non_blocking(self):
        audit = self.app.extensions["release_startup_audit"]
        self.assertEqual(audit["action_taken"], "none")
        self.assertFalse(audit["blocking_public"])
        self.assertEqual(audit["finding_counts"], {})
        self.assertEqual(audit["audit_event_pending"], [])
        self.assertTrue(Path(audit["report_path"]).is_file())
        self.assertEqual(self.anonymous.get("/api/health").status_code, 200)

    def test_registry_upgrade_preserves_v1_records(self):
        legacy_id = "rel_legacy_record"
        legacy_dir = self.data_dir / "releases" / legacy_id
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "release.json").write_text("{}", encoding="utf-8")
        index = {
            "format": "personal-archive-release-registry",
            "format_version": 1,
            "active_release_id": None,
            "releases": [
                {
                    "release_id": legacy_id,
                    "created_at": "2026-01-01T00:00:00Z",
                    "parent_release_id": None,
                    "action": "prepare",
                    "status": "prepared",
                    "content_ids": [],
                    "attachment_ids": [],
                    "bundle_sha256": None,
                    "manifest_sha256": None,
                    "manifest_bytes": 0,
                    "file_count": 0,
                    "total_bytes": 0,
                }
            ],
        }
        index_path = self.data_dir / "releases" / "index.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")

        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])

        raw = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["format_version"], 3)
        self.assertEqual(raw["releases"][0]["release_id"], legacy_id)
        self.assertEqual(raw["releases"][0]["status"], "prepared")
        self.assertTrue(legacy_dir.is_dir())
        self.assertIsNotNone(raw["active_release_id"])

    def test_registry_backup_includes_active_pointer_and_verifies_bundles(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        result = self.registry.backup()
        with zipfile.ZipFile(result["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            names = set(archive.namelist())
        self.assertEqual(manifest["active_release_id"], release_id)
        self.assertEqual(manifest["registry_revision"], self.release_index()["registry_revision"])
        self.assertEqual(len(manifest["index_sha256"]), 64)
        self.assertIn("registry/index.json", names)
        self.assertIn(f"registry/{release_id}/release.json", names)

        page = self.bundle_dir(release_id) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_bytes(b"tampered")
        with self.assertRaises(ReleaseError):
            self.registry.backup()

    def test_activation_and_withdrawal_do_not_modify_private_data(self):
        content = self.create_content("A", "A-BODY")
        attachment = self.upload(content["id"], b"ATTACH")
        before = self.db_snapshot()
        self.publish([content["id"]], [attachment["id"]])
        self.assertEqual(self.db_snapshot(), before)
        self.assertIsNotNone(self.registry.list()["active_release_id"])
        withdrawn = self.withdraw({})
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(self.db_snapshot(), before)
        self.assertEqual(
            self.object_path(attachment["id"]).read_bytes(), b"ATTACH"
        )
        self.assertIsNone(self.registry.list()["active_release_id"])


if __name__ == "__main__":
    unittest.main()
