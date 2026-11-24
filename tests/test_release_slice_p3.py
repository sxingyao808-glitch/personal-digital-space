"""P3 slice tests: publishing history, rollback/restore, lineage audit, backup v3.

All tests use isolated temporary data directories and synthetic content. No
real user data, no real publication and no real restore is performed.

Coverage follows the approved P3 plan:
history query/filter/sort/pagination, events-missing history, publish and
withdraw trails, subset withdrawal and restore, full and partial restore,
withdrawn-content confirmation, source restrictions, corrupt/missing source
bundles, private-database publication gate, dry_run, pre/post-commit failure,
idempotency/concurrency/expected_active, event reconciliation, historical
bundle immutability, old URLs and reader cache, lineage audit, Registry v3
backup, v1/v2 compatibility, payload whitelist and acyclic restore chains.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import connect
from archive.release import (
    REGISTRY_BACKUP_FORMAT_VERSION,
    REGISTRY_FORMAT_VERSION,
    RELEASE_FORMAT,
    ReleaseError,
    ReleaseRegistry,
    _RegistryLock,
    public_content_id,
)

TOKEN = "test-owner-token-p3-2468013579"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(payload) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class ReleaseP3TestCase(unittest.TestCase):
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
        return self.owner.post(
            "/api/releases/restore", json=body, headers=self.headers
        )

    def history(self, **params):
        return self.owner.get("/api/releases/history", query_string=params)

    def release_history(self, release_id, **params):
        return self.owner.get(
            f"/api/releases/{release_id}/history", query_string=params
        )

    def release_index(self):
        path = self.data_dir / "releases" / "index.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_index(self, index) -> None:
        path = self.data_dir / "releases" / "index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(index))

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

    def bundle_hashes(self, release_id):
        root = self.bundle_dir(release_id)
        return {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in root.rglob("*")
            if path.is_file()
        }

    def manifest(self, release_id):
        return json.loads(
            (self.bundle_dir(release_id) / "release.json").read_text(encoding="utf-8")
        )

    def payload(self, release_id, content_id):
        manifest = self.manifest(release_id)
        item = next(
            entry for entry in manifest["items"] if entry["content_id"] == content_id
        )
        return json.loads(
            (self.bundle_dir(release_id) / item["payload_path"]).read_text(
                encoding="utf-8"
            )
        )

    def record(self, release_id, index=None):
        index = index or self.release_index()
        return next(
            item for item in index["releases"] if item["release_id"] == release_id
        )

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

    def rewrite_payload(self, release_id, content_id, mutate) -> None:
        """Tamper a source payload while keeping every hash internally valid."""
        release_dir = self.bundle_dir(release_id)
        manifest_path = release_dir / "release.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = next(
            entry for entry in manifest["items"] if entry["content_id"] == content_id
        )
        payload_path = release_dir / item["payload_path"]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        mutate(payload)
        data = canonical(payload)
        payload_path.write_bytes(data)
        digest = sha256_bytes(data)
        for entry in manifest["files"]:
            if entry["path"] == item["payload_path"]:
                entry["sha256"] = digest
                entry["bytes"] = len(data)
        item["payload_sha256"] = digest
        item["payload_bytes"] = len(data)
        manifest["bundle_sha256"] = sha256_bytes(
            canonical({"files": manifest["files"]})
        )
        manifest_bytes = canonical(manifest)
        manifest_path.write_bytes(manifest_bytes)
        index = self.release_index()
        target = self.record(release_id, index)
        target["manifest_sha256"] = sha256_bytes(manifest_bytes)
        target["manifest_bytes"] = len(manifest_bytes)
        target["bundle_sha256"] = manifest["bundle_sha256"]
        self.write_index(index)

    def v1_source(self, release_id="rel_v1_source") -> str:
        release_dir = self.bundle_dir(release_id)
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
        manifest_bytes = canonical(manifest)
        (release_dir / "release.json").write_bytes(manifest_bytes)
        self.write_index(
            {
                "format": "personal-archive-release-registry",
                "format_version": 1,
                "active_release_id": None,
                "releases": [
                    {
                        "release_id": release_id,
                        "created_at": manifest["created_at"],
                        "parent_release_id": None,
                        "action": "prepare",
                        "status": "superseded",
                        "activated_at": "2026-01-01T00:00:00Z",
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
        )
        return release_id

    # ------------------------------------------------------------------
    # P3-B history queries
    # ------------------------------------------------------------------
    def test_history_query_filters_sorts_and_paginates(self):
        first_content = self.create_content("A", "A-BODY")
        second_content = self.create_content("B", "B-BODY")
        first = self.publish([first_content["id"]])
        second = self.publish([first_content["id"], second_content["id"]])

        response = self.history()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertEqual(data["active_release_id"], second)
        self.assertEqual(data["total"], 2)
        self.assertEqual(
            [item["release_id"] for item in data["items"]], [second, first]
        )
        for item in data["items"]:
            self.assertIsInstance(item["sequence"], int)
            self.assertEqual(item["operation"], "publish")
            self.assertEqual(item["actor"], "owner")
            self.assertIn(
                "activate", [entry["action"] for entry in item["history"]]
            )
        self.assertEqual(
            data["registry_revision"], self.release_index()["registry_revision"]
        )

        ascending = self.history(order="asc").get_json()["data"]["items"]
        self.assertEqual([item["release_id"] for item in ascending], [first, second])

        active = self.history(status="active").get_json()["data"]
        self.assertEqual([item["release_id"] for item in active["items"]], [second])
        self.assertEqual(active["total"], 1)

        publish_only = self.history(action="publish").get_json()["data"]
        self.assertEqual(publish_only["total"], 2)

        by_content = self.history(content_id=second_content["id"]).get_json()["data"]
        self.assertEqual([item["release_id"] for item in by_content["items"]], [second])

        page = self.history(limit=1).get_json()["data"]
        self.assertEqual(page["returned"], 1)
        self.assertEqual(page["total"], 2)
        self.assertIsNotNone(page["next_cursor"])
        tail = self.history(limit=1, cursor=page["next_cursor"]).get_json()["data"]
        self.assertEqual([item["release_id"] for item in tail["items"]], [first])
        self.assertIsNone(tail["next_cursor"])

        future = self.history(since="2030-01-01T00:00:00Z").get_json()["data"]
        self.assertEqual(future["total"], 0)

        bad_limit = self.history(limit=0)
        self.assertEqual(bad_limit.status_code, 422)
        bad_order = self.history(order="sideways")
        self.assertEqual(bad_order.status_code, 422)
        bad_status = self.history(status="exploded")
        self.assertEqual(bad_status.status_code, 422)
        bad_action = self.history(action="exploded")
        self.assertEqual(bad_action.status_code, 422)

    def test_history_survives_missing_events_file(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        (self.data_dir / "releases" / "events.jsonl").unlink()

        listing = self.history()
        self.assertEqual(listing.status_code, 200, listing.get_data(as_text=True))
        self.assertEqual(listing.get_json()["data"]["total"], 1)

        detail = self.release_history(release_id, verify="1")
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        payload = detail.get_json()["data"]
        self.assertEqual(payload["release_id"], release_id)
        self.assertTrue(payload["active"])
        self.assertEqual(payload["operation"], "publish")
        self.assertIn("activate", [entry["action"] for entry in payload["history"]])
        self.assertTrue(payload["verification"]["ok"])

        missing = self.release_history("rel_does_not_exist")
        self.assertEqual(missing.status_code, 404)

    def test_publish_and_withdraw_all_history_trail(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        withdrawn = self.withdraw({})
        self.assertEqual(withdrawn.status_code, 200)

        data = self.history().get_json()["data"]
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["status"], "withdrawn")
        self.assertEqual(item["operation"], "withdraw_all")
        self.assertEqual(item["release_id"], release_id)
        self.assertEqual(item["withdrawal_scope"], "all")
        self.assertIsNotNone(item["withdrawn_at"])
        actions = [entry["action"] for entry in item["history"]]
        self.assertEqual(actions, ["build", "activate", "withdraw"])
        entries = item["history"]
        self.assertEqual([entry["seq"] for entry in entries], [1, 2, 3])
        self.assertEqual(
            [entry["origin"] for entry in entries],
            ["recorded", "recorded", "recorded"],
        )
        self.assertEqual(entries[-1]["scope"], "all")
        self.assertIn(
            f"ev_withdraw_{release_id}", [event["event_id"] for event in self.events()]
        )

    def test_subset_withdrawal_and_restore_history_trail(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        source = self.publish([first["id"], second["id"]])

        withdrawal = self.withdraw({"content_ids": [first["id"], "c_ghost"]})
        self.assertEqual(withdrawal.status_code, 422)
        withdrawal = self.withdraw({"content_ids": [first["id"]]})
        self.assertEqual(withdrawal.status_code, 200, withdrawal.get_data(as_text=True))
        successor = withdrawal.get_json()["data"]["release_id"]

        data = self.history(action="withdraw_subset").get_json()["data"]
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["release_id"], successor)
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["parent_release_id"], source)
        self.assertEqual(item["withdrawn_content_ids"], [first["id"]])
        self.assertEqual(item["history"][0]["scope"], "content_subset")
        self.assertEqual(item["history"][0]["content_ids"], [first["id"]])

        restored = self.restore(
            {"source_release_id": source, "content_ids": [second["id"]]}
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        restored_id = restored.get_json()["data"]["release_id"]
        detail = self.release_history(restored_id).get_json()["data"]
        self.assertEqual(detail["operation"], "restore_subset")
        actions = [entry["action"] for entry in detail["history"]]
        self.assertEqual(actions, ["restore_request", "activate"])
        self.assertEqual(detail["record"]["restored_from_release_id"], source)
        self.assertEqual(detail["record"]["restore_scope"], "content_subset")

    # ------------------------------------------------------------------
    # P3-C restore
    # ------------------------------------------------------------------
    def test_full_restore_creates_new_release_from_historical_bundle(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-ORIGINAL")
        attachment = self.upload(first["id"], b"ATTACH-ORIGINAL")
        source = self.publish([first["id"], second["id"]], [attachment["id"]])
        source_hashes = self.bundle_hashes(source)

        changed = self.patch_content(second["id"], second["revision"], body="B-EDITED")
        self.assertEqual(changed.status_code, 200)
        current = self.publish([first["id"], second["id"]], [attachment["id"]])
        before_db = self.db_snapshot()
        self.assertIn("B-EDITED", self.anonymous.get(f"/p/{second['id']}").get_data(as_text=True))

        response = self.restore({"source_release_id": source, "reason": "rollback"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        restored = result["release_id"]
        self.assertNotEqual(restored, source)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["previous_active_release_id"], current)
        self.assertEqual(result["restored_from_release_id"], source)
        self.assertEqual(result["restore_scope"], "full")
        self.assertEqual(result["active_release_id"], restored)

        index = self.release_index()
        source_record = self.record(source, index)
        self.assertEqual(source_record["status"], "superseded")
        current_record = self.record(current, index)
        self.assertEqual(current_record["status"], "superseded")
        self.assertEqual(current_record["superseded_by_release_id"], restored)
        restored_record = self.record(restored, index)
        self.assertEqual(restored_record["status"], "active")
        self.assertEqual(restored_record["action"], "restore")
        self.assertEqual(restored_record["parent_release_id"], source)
        self.assertEqual(restored_record["restored_from_release_id"], source)
        self.assertEqual(restored_record["restore_scope"], "full")
        self.assertEqual(
            [entry["action"] for entry in restored_record["history"]],
            ["restore_request", "activate"],
        )

        manifest = self.manifest(restored)
        self.assertEqual(manifest["action"], "restore")
        self.assertEqual(manifest["restored_from_release_id"], source)
        self.assertEqual(manifest["restore_scope"], "full")
        self.assertEqual(manifest["parent_release_id"], source)
        self.assertEqual(self.payload(restored, second["id"])["body"], "B-ORIGINAL")

        page = self.anonymous.get(f"/p/{second['id']}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("B-ORIGINAL", html)
        self.assertNotIn("B-EDITED", html)
        public_api = self.anonymous.get(f"/api/public/content/{second['id']}")
        self.assertEqual(public_api.status_code, 200)
        self.assertEqual(public_api.get_json()["data"]["body"], "B-ORIGINAL")

        self.assertEqual(self.bundle_hashes(source), source_hashes)
        self.assertEqual(self.db_snapshot(), before_db)
        self.assertIn(
            f"ev_prepare_{restored}", [event["event_id"] for event in self.events()]
        )
        self.assertIn(
            f"ev_activate_{restored}", [event["event_id"] for event in self.events()]
        )

    def test_partial_restore_selects_content_and_attachments(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        attachment = self.upload(first["id"], b"ATTACH")
        source = self.publish([first["id"], second["id"]], [attachment["id"]])
        self.publish([first["id"], second["id"]], [attachment["id"]])

        without_content = self.restore(
            {"source_release_id": source, "attachment_ids": [attachment["id"]]}
        )
        self.assertEqual(without_content.status_code, 422)

        response = self.restore(
            {
                "source_release_id": source,
                "content_ids": [first["id"]],
                "attachment_ids": [attachment["id"]],
            }
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        restored = result["release_id"]
        self.assertEqual(result["scope"], "content_subset")
        self.assertEqual(result["content_ids"], [first["id"]])
        self.assertEqual(result["attachment_ids"], [attachment["id"]])
        manifest = self.manifest(restored)
        self.assertEqual(manifest["content_ids"], [first["id"]])
        self.assertEqual(manifest["attachment_ids"], [attachment["id"]])
        self.assertTrue(
            (self.bundle_dir(restored) / "content" / f"{public_content_id(first['id'])}.json").is_file()
        )
        self.assertFalse(
            (self.bundle_dir(restored) / "content" / f"{public_content_id(second['id'])}.json").exists()
        )
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/attachments/{attachment['id']}/note.txt").status_code,
            200,
        )

        before_invalid = self.db_snapshot()
        invalid = self.restore(
            {
                "source_release_id": source,
                "content_ids": [second["id"]],
                "attachment_ids": [attachment["id"]],
            }
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.db_snapshot(), before_invalid)

    def test_restore_requires_confirmation_for_previously_withdrawn_content(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        source = self.publish([first["id"], second["id"]])
        withdrawal = self.withdraw({"content_ids": [first["id"]]})
        self.assertEqual(withdrawal.status_code, 200)

        blocked = self.restore({"source_release_id": source})
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        error = blocked.get_json()["error"]
        self.assertEqual(error["code"], "restore_requires_confirmation")
        self.assertEqual(
            error["details"]["previously_withdrawn"]["content_ids"], [first["id"]]
        )

        dry = self.restore({"source_release_id": source, "dry_run": True})
        self.assertEqual(dry.status_code, 200)
        self.assertTrue(dry.get_json()["data"]["requires_confirmation"])

        confirmed = self.restore(
            {"source_release_id": source, "confirm_republish_withdrawn": True}
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.get_data(as_text=True))
        self.assertEqual(self.anonymous.get(f"/p/{first['id']}").status_code, 200)
        self.assertEqual(self.anonymous.get(f"/p/{second['id']}").status_code, 200)

    def test_restore_source_status_restrictions(self):
        content = self.create_content("A", "A-BODY")
        prepared = self.prepare([content["id"]])
        self.assertEqual(prepared.status_code, 201)
        prepared_id = prepared.get_json()["data"]["release_id"]
        response = self.restore({"source_release_id": prepared_id})
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        codes = [
            issue["code"]
            for issue in response.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("source_never_published", codes)

        active = self.publish([content["id"]])
        already = self.restore({"source_release_id": active})
        self.assertEqual(already.status_code, 200)
        result = already.get_json()["data"]
        self.assertTrue(result["already_active"])
        self.assertFalse(result["changed"])

        v1 = self.v1_source()
        legacy = self.restore({"source_release_id": v1})
        self.assertEqual(legacy.status_code, 422, legacy.get_data(as_text=True))
        codes = [
            issue["code"]
            for issue in legacy.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("source_format_unsupported", codes)

        missing = self.restore({"source_release_id": "rel_does_not_exist"})
        self.assertEqual(missing.status_code, 404)

    def test_restore_rejects_missing_or_corrupt_source_bundle(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        current = self.publish([content["id"]])
        manifest_bytes = (self.bundle_dir(source) / "release.json").read_bytes()

        (self.bundle_dir(source) / "release.json").unlink()
        missing = self.restore({"source_release_id": source})
        self.assertEqual(missing.status_code, 422, missing.get_data(as_text=True))
        codes = [
            issue["code"]
            for issue in missing.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("source_bundle_missing", codes)
        self.assertEqual(self.release_index()["active_release_id"], current)

        (self.bundle_dir(source) / "release.json").write_bytes(manifest_bytes)
        page = self.bundle_dir(source) / "content" / f"{public_content_id(content['id'])}.html"
        page.write_bytes(b"tampered-source-page")
        tampered = self.restore({"source_release_id": source})
        self.assertEqual(tampered.status_code, 422, tampered.get_data(as_text=True))
        self.assertEqual(self.release_index()["active_release_id"], current)
        self.assertEqual(
            [
                record
                for record in self.release_index()["releases"]
                if record.get("action") == "restore"
            ],
            [],
        )

    def test_restore_public_gate_blocks_private_or_deleted_content(self):
        first = self.create_content("A", "A-BODY")
        second = self.create_content("B", "B-BODY")
        source = self.publish([first["id"], second["id"]])
        self.publish([first["id"], second["id"]])

        private = self.patch_content(
            second["id"], second["revision"], visibility="private"
        )
        self.assertEqual(private.status_code, 200)

        blocked = self.restore({"source_release_id": source})
        self.assertEqual(blocked.status_code, 422, blocked.get_data(as_text=True))
        codes = [
            issue["code"]
            for issue in blocked.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("content_not_public", codes)

        partial = self.restore(
            {"source_release_id": source, "content_ids": [first["id"]]}
        )
        self.assertEqual(partial.status_code, 200, partial.get_data(as_text=True))

        deleted = self.owner.delete(
            f"/api/content/{first['id']}",
            json={"revision": first["revision"]},
            headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        blocked_deleted = self.restore(
            {
                "source_release_id": source,
                "content_ids": [first["id"]],
            }
        )
        self.assertEqual(blocked_deleted.status_code, 422)

    def test_restore_dry_run_previews_without_mutation(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        current = self.publish([content["id"]])
        index_before = self.release_index()
        bundles_before = {
            path.name for path in (self.data_dir / "releases").iterdir() if path.is_dir()
        }

        preview = self.restore({"source_release_id": source, "dry_run": True})
        self.assertEqual(preview.status_code, 200, preview.get_data(as_text=True))
        data = preview.get_json()["data"]
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["changed"])
        self.assertEqual(data["source_release_id"], source)
        self.assertEqual(data["scope"], "full")
        self.assertEqual(data["previous_active_release_id"], current)
        self.assertEqual(data["plan"]["will_supersede_release_id"], current)
        self.assertNotIn("release_id", data)

        self.assertEqual(self.release_index(), index_before)
        bundles_after = {
            path.name for path in (self.data_dir / "releases").iterdir() if path.is_dir()
        }
        self.assertEqual(bundles_after, bundles_before)

        real = self.restore({"source_release_id": source})
        self.assertEqual(real.status_code, 200)
        self.assertTrue(real.get_json()["data"]["changed"])

    def test_restore_commit_failures_keep_old_active(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        current = self.publish([content["id"]])

        original = self.registry._activate_locked

        def failing_activate(*args, **kwargs):
            raise ReleaseError("simulated activation failure")

        self.registry._activate_locked = failing_activate
        try:
            failed = self.restore({"source_release_id": source})
        finally:
            self.registry._activate_locked = original
        self.assertEqual(failed.status_code, 500)
        index = self.release_index()
        self.assertEqual(index["active_release_id"], current)
        prepared = [
            record
            for record in index["releases"]
            if record.get("action") == "restore" and record["status"] == "prepared"
        ]
        self.assertEqual(len(prepared), 1)

        resumed = self.restore({"source_release_id": source})
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        result = resumed.get_json()["data"]
        self.assertTrue(result["resumed"])
        self.assertEqual(result["release_id"], prepared[0]["release_id"])
        self.assertEqual(self.release_index()["active_release_id"], result["release_id"])

    def test_restore_is_idempotent_and_honours_expected_active(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        current = self.publish([content["id"]])

        conflict = self.restore(
            {"source_release_id": source, "expected_active_release_id": "rel_other"}
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["error"]["code"], "release_conflict")

        first = self.restore(
            {"source_release_id": source, "expected_active_release_id": current}
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        restored = first.get_json()["data"]["release_id"]

        again = self.restore({"source_release_id": source})
        self.assertEqual(again.status_code, 200)
        result = again.get_json()["data"]
        self.assertTrue(result["already_restored"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["release_id"], restored)
        self.assertEqual(
            len(
                [
                    record
                    for record in self.release_index()["releases"]
                    if record.get("restored_from_release_id") == source
                ]
            ),
            1,
        )

    def test_restore_is_blocked_while_registry_lock_is_held(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        self.publish([content["id"]])
        releases_dir = self.data_dir / "releases"
        with _RegistryLock(releases_dir):
            response = self.restore({"source_release_id": source})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["error"]["code"], "release_locked")

    def test_restore_event_failure_is_pending_and_reconciled(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        self.publish([content["id"]])

        original = self.registry._append_event

        def failing_append(event):
            raise OSError("simulated event failure")

        self.registry._append_event = failing_append
        try:
            response = self.restore({"source_release_id": source})
        finally:
            self.registry._append_event = original
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()["data"]
        self.assertFalse(result["audit_event_recorded"])
        self.assertTrue(result["audit_event_pending"])
        restored = result["release_id"]
        self.assertEqual(self.release_index()["active_release_id"], restored)
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 200)

        reconciled = self.registry.reconcile_events()
        self.assertGreaterEqual(reconciled["appended"], 1)
        event_ids = [event["event_id"] for event in self.events()]
        self.assertIn(f"ev_prepare_{restored}", event_ids)
        self.assertIn(f"ev_activate_{restored}", event_ids)
        self.assertEqual(
            self.registry.audit(write_report=False)["audit_event_pending"], []
        )

    def test_restore_attachments_only_without_content_is_rejected(self):
        content = self.create_content("A", "A-BODY")
        attachment = self.upload(content["id"], b"ATTACH")
        source = self.publish([content["id"]], [attachment["id"]])
        self.publish([content["id"]], [attachment["id"]])
        response = self.restore(
            {"source_release_id": source, "attachment_ids": [attachment["id"]]}
        )
        self.assertEqual(response.status_code, 422)

    def test_restore_old_urls_and_reader_cache(self):
        content = self.create_content("A", "B-ORIGINAL")
        source = self.publish([content["id"]])
        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 200)
        changed = self.patch_content(content["id"], content["revision"], body="B-NEW")
        self.assertEqual(changed.status_code, 200)
        current = self.publish([content["id"]])
        self.assertIn(
            "B-NEW", self.anonymous.get(f"/p/{content['id']}").get_data(as_text=True)
        )

        response = self.restore({"source_release_id": source})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        restored = response.get_json()["data"]["release_id"]
        page = self.anonymous.get(f"/p/{content['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("B-ORIGINAL", page.get_data(as_text=True))
        self.assertNotIn("B-NEW", page.get_data(as_text=True))
        self.assertEqual(self.anonymous.get(f"/p/{source}").status_code, 404)
        self.assertEqual(self.anonymous.get(f"/p/{current}").status_code, 404)
        self.assertEqual(self.anonymous.get(f"/p/{restored}").status_code, 404)

    # ------------------------------------------------------------------
    # P3-D lineage audit
    # ------------------------------------------------------------------
    def test_audit_detects_dangling_cycle_multiple_successors_and_sequence_gap(self):
        def base_record(release_id, *, parent=None, seqs=None, **extra):
            record = {
                "release_id": release_id,
                "created_at": "2026-01-01T00:00:00Z",
                "parent_release_id": parent,
                "action": "prepare",
                "status": "prepared",
                "content_ids": ["c_a"],
                "attachment_ids": [],
                "bundle_sha256": None,
                "manifest_sha256": None,
                "manifest_bytes": 0,
                "file_count": 0,
                "total_bytes": 0,
                "sequence": seqs[0] if seqs else None,
                "history": [
                    {
                        "seq": seq,
                        "registry_revision": 1,
                        "at": "2026-01-01T00:00:00Z",
                        "actor": "owner",
                        "action": "build",
                        "origin": "recorded",
                        "content_ids": ["c_a"],
                        "attachment_ids": [],
                    }
                    for seq in (seqs or [])
                ],
            }
            record.update(extra)
            return record

        index = {
            "format": "personal-archive-release-registry",
            "format_version": 3,
            "history_format": {
                "format": "personal-archive-release-history",
                "version": 3,
            },
            "sequence": 6,
            "registry_revision": 1,
            "active_release_id": None,
            "releases": [
                base_record("rel_a", seqs=[1]),
                base_record("rel_b", parent="rel_a", seqs=[1], activated_from_release_id="rel_a"),
                base_record("rel_c", parent="rel_a", seqs=[3], activated_from_release_id="rel_a"),
                base_record("rel_d", parent="rel_e", seqs=[5]),
                base_record("rel_e", parent="rel_d", seqs=[4]),
                base_record(
                    "rel_f",
                    parent="rel_a",
                    seqs=[6],
                    action="restore",
                    restored_from_release_id="rel_a",
                    restore_scope="full",
                    content_ids=["c_not_in_source"],
                ),
                base_record("rel_g", parent="rel_missing", seqs=[7]),
                base_record(
                    "rel_h",
                    seqs=[8],
                    action="restore",
                    restored_from_release_id="rel_missing_source",
                    restore_scope="full",
                ),
            ],
        }
        self.write_index(index)
        report = self.registry.audit(write_report=False)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("DANGLING_PARENT_REFERENCE", codes)
        self.assertIn("DANGLING_RESTORE_SOURCE", codes)
        self.assertIn("LINEAGE_CYCLE", codes)
        self.assertIn("MULTIPLE_SUCCESSORS", codes)
        self.assertIn("SEQUENCE_DUPLICATE", codes)
        self.assertIn("SEQUENCE_GAP", codes)
        self.assertIn("RESTORE_LINEAGE_MISMATCH", codes)
        self.assertEqual(report["action_taken"], "none")
        # Audit must never repair or delete bundle directories.
        self.assertTrue(index["releases"])

    def test_audit_accepts_healthy_restore_chain_without_cycles(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        self.publish([content["id"]])
        restored = self.restore({"source_release_id": source}).get_json()["data"][
            "release_id"
        ]
        report = self.registry.audit(write_report=False)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertNotIn("LINEAGE_CYCLE", codes)
        self.assertNotIn("MULTIPLE_SUCCESSORS", codes)
        self.assertNotIn("RESTORE_LINEAGE_MISMATCH", codes)
        self.assertNotIn("SEQUENCE_DUPLICATE", codes)
        self.assertNotIn("SEQUENCE_GAP", codes)
        self.assertEqual(report["audit_event_pending"], [])

        follow = []
        current = restored
        while current:
            follow.append(current)
            item = self.record(current)
            current = item.get("parent_release_id")
            self.assertLessEqual(len(follow), 10)
        self.assertEqual(follow[-1], source)

    def test_payload_whitelist_blocks_restore_of_private_fields(self):
        content = self.create_content("A", "A-BODY")
        source = self.publish([content["id"]])
        self.publish([content["id"]])

        def add_private_field(payload):
            payload["private_note"] = "must-not-be-published"

        self.rewrite_payload(source, content["id"], add_private_field)
        response = self.restore({"source_release_id": source})
        self.assertEqual(response.status_code, 422, response.get_data(as_text=True))
        codes = [
            issue["code"]
            for issue in response.get_json()["error"]["details"]["issues"]
        ]
        self.assertIn("source_payload_private_field", codes)
        self.assertIsNotNone(self.release_index()["active_release_id"])

    # ------------------------------------------------------------------
    # P3-A/P3-E compatibility and backup
    # ------------------------------------------------------------------
    def test_registry_v3_upgrade_and_history_ledger(self):
        content = self.create_content("A", "A-BODY")
        release_id = self.publish([content["id"]])
        index = self.release_index()
        self.assertEqual(index["format_version"], REGISTRY_FORMAT_VERSION)
        self.assertEqual(index["history_format"]["version"], 3)
        self.assertIsInstance(index["sequence"], int)
        record = self.record(release_id, index)
        self.assertEqual(record["actor"], "owner")
        self.assertIsNone(record["restored_from_release_id"])
        self.assertIsNone(record["restore_scope"])
        self.assertEqual(record["sequence"], record["history"][0]["seq"])
        self.assertEqual(
            [entry["action"] for entry in record["history"]], ["build", "activate"]
        )
        for entry in record["history"]:
            self.assertEqual(entry["actor"], "owner")
            self.assertEqual(entry["origin"], "recorded")
            self.assertIsInstance(entry["registry_revision"], int)
            self.assertLessEqual(
                entry["registry_revision"], index["registry_revision"]
            )
        self.assertEqual(
            record["history"][-1]["registry_revision"], index["registry_revision"]
        )

    def test_v2_registry_is_read_compatible_and_upgraded_on_write(self):
        legacy_id = "rel_v2_readonly"
        (self.bundle_dir(legacy_id)).mkdir(parents=True)
        (self.bundle_dir(legacy_id) / "release.json").write_text(
            "{}", encoding="utf-8"
        )
        index = {
            "format": "personal-archive-release-registry",
            "format_version": 2,
            "registry_revision": 1,
            "active_release_id": None,
            "active_since": None,
            "updated_at": None,
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
        path = self.data_dir / "releases" / "index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        raw_before = path.read_bytes()

        data = self.history().get_json()["data"]
        self.assertEqual(data["format_version"], 2)
        self.assertEqual(data["total"], 1)
        item = data["items"][0]
        self.assertEqual(item["release_id"], legacy_id)
        self.assertEqual(item["operation"], "prepared")
        self.assertEqual(item["sequence"], 1)
        self.assertEqual(item["history"][0]["origin"], "derived")
        self.assertEqual(path.read_bytes(), raw_before)

        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["format_version"], 3)
        legacy = self.record(legacy_id, raw)
        self.assertEqual(legacy["sequence"], 1)
        self.assertEqual(legacy["history"][0]["origin"], "derived")
        self.assertEqual(raw["history_format"]["version"], 3)
        self.assertGreaterEqual(raw["sequence"], 3)

    def test_registry_v3_backup_includes_history_and_lineage(self):
        content = self.create_content("A", "A-BODY")
        self.publish([content["id"]])
        self.publish([content["id"]])
        result = self.registry.backup()
        self.assertEqual(result["registry_format_version"], 3)
        self.assertEqual(result["history_format_version"], 3)
        self.assertEqual(result["lineage_count"], result["release_count"])
        with zipfile.ZipFile(result["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            names = set(archive.namelist())
        self.assertEqual(manifest["format_version"], REGISTRY_BACKUP_FORMAT_VERSION)
        self.assertEqual(manifest["registry_format_version"], 3)
        self.assertEqual(manifest["history_format"]["version"], 3)
        self.assertIsInstance(manifest["registry_sequence"], int)
        self.assertEqual(len(manifest["lineage"]), manifest["release_count"])
        for entry in manifest["lineage"]:
            self.assertIn("release_id", entry)
            self.assertIn("sequence", entry)
            self.assertIn("restored_from_release_id", entry)
        index = self.release_index()
        self.assertEqual(
            [entry["release_id"] for entry in manifest["lineage"]],
            [record["release_id"] for record in index["releases"]],
        )
        self.assertIn("registry/index.json", names)
        for record in index["releases"]:
            self.assertIn(f"registry/{record['release_id']}/release.json", names)


if __name__ == "__main__":
    unittest.main()
