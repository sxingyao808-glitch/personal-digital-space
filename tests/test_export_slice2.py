"""Slice 2 tests: versioned archive export (AC-12, F-04 foundation).

These tests verify export behavior only. A successful export is not backup or
restore verification (AC-13), and it is not public release.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import SCHEMA_VERSION, connect, utcnow
from archive.export import EXPORT_FORMAT, EXPORT_FORMAT_VERSION

TOKEN = "test-owner-token-export-987654321"
ZIP_MEMBERS = {
    "manifest.json",
    "content.json",
    "relations.json",
    "attributes.json",
    "attachments.json",
    "integrity_issues.json",
}


class ExportTestCase(unittest.TestCase):
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

    def create_content(self, title="", body="", visibility=None):
        response = self.owner.post(
            "/api/content",
            json={"title": title, "body": body, "visibility": visibility},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_relation(self, from_id, to_id, relation_type):
        response = self.owner.post(
            f"/api/content/{from_id}/relations",
            json={"to_id": to_id, "type": relation_type},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def add_attribute(self, content_id, key, value, provenance="user", confirmed=False, source=None):
        payload = {
            "key": key,
            "value": value,
            "provenance": provenance,
            "confirmed": confirmed,
        }
        if source is not None:
            payload["source_ref"] = source
        response = self.owner.post(
            f"/api/content/{content_id}/attributes", json=payload, headers=self.headers
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def upload(self, content_id, payload: bytes, operation_id: str, visibility="private"):
        response = self.owner.post(
            f"/api/content/{content_id}/attachments",
            data={
                "file": (io.BytesIO(payload), "note.txt"),
                "operation_id": operation_id,
                "visibility": visibility,
            },
            content_type="multipart/form-data",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]["attachment"]

    def export(self, **payload):
        return self.owner.post("/api/export", json=payload, headers=self.headers)

    def open_package(self, result: dict) -> zipfile.ZipFile:
        path = Path(result["path"])
        self.assertTrue(path.is_file(), f"export package missing: {path}")
        return zipfile.ZipFile(path)

    def read_json(self, archive: zipfile.ZipFile, name: str):
        return json.loads(archive.read(name).decode("utf-8"))

    def db_rows(self, table: str):
        conn = connect(self.data_dir / "archive.db")
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 1. Normal export and manifest verification
    # ------------------------------------------------------------------
    def test_export_package_and_manifest_are_self_describing(self):
        public_content = self.create_content("公开记录", "公开正文", visibility="public")
        private_content = self.create_content("私有记录", "私有正文", visibility="private")
        relation = self.add_relation(private_content["id"], public_content["id"], "REFERENCES")
        self.add_attribute(
            public_content["id"],
            "标签",
            "归档",
            provenance="user",
            confirmed=True,
            source="手动输入",
        )
        self.add_attribute(
            private_content["id"], "推断", "可能相关", provenance="ai_suggested", confirmed=False
        )
        attachment_bytes = b"exported attachment bytes\n"
        attachment = self.upload(
            public_content["id"], attachment_bytes, "op_export_normal_1", visibility="public"
        )

        response = self.export()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertTrue(data["complete"])
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["format"], EXPORT_FORMAT)
        self.assertEqual(data["format_version"], EXPORT_FORMAT_VERSION)
        self.assertEqual(data["counts"]["content_total"], 2)
        self.assertEqual(data["counts"]["content_public"], 1)
        self.assertEqual(data["counts"]["relations_total"], 1)
        self.assertEqual(data["counts"]["attributes_total"], 2)
        self.assertEqual(data["counts"]["attachments_included"], 1)

        with self.open_package(data) as archive:
            self.assertTrue(ZIP_MEMBERS.issubset(set(archive.namelist())))
            manifest = self.read_json(archive, "manifest.json")
            self.assertEqual(manifest["format"], EXPORT_FORMAT)
            self.assertEqual(manifest["format_version"], EXPORT_FORMAT_VERSION)
            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            self.assertTrue(manifest["completeness"]["complete"])
            self.assertFalse(manifest["boundaries"]["publishes_content"])
            self.assertFalse(manifest["boundaries"]["changes_visibility"])

            # Every declared file must exist in the package with the recorded identity.
            for entry in manifest["files"]:
                actual = archive.read(entry["path"])
                self.assertEqual(len(actual), entry["bytes"], entry["path"])
                self.assertEqual(hashlib.sha256(actual).hexdigest(), entry["sha256"], entry["path"])

            content_items = {row["id"]: row for row in self.read_json(archive, "content.json")["items"]}
            self.assertEqual(set(content_items), {public_content["id"], private_content["id"]})
            self.assertTrue(content_items[public_content["id"]]["public"])
            self.assertFalse(content_items[private_content["id"]]["public"])
            self.assertTrue(content_items[private_content["id"]]["visibility_selected"])

            relations = self.read_json(archive, "relations.json")["items"]
            self.assertEqual(len(relations), 1)
            self.assertEqual(relations[0]["id"], relation["id"])
            self.assertEqual(relations[0]["from_id"], private_content["id"])
            self.assertEqual(relations[0]["to_id"], public_content["id"])
            self.assertEqual(relations[0]["type"], "REFERENCES")
            self.assertEqual(relations[0]["direction"], "from->to")
            self.assertTrue(relations[0]["intact"])

            attributes = {
                row["id"]: row for row in self.read_json(archive, "attributes.json")["items"]
            }
            self.assertEqual(len(attributes), 2)
            confirmed = next(a for a in attributes.values() if a["key"] == "标签")
            self.assertEqual(confirmed["provenance"], "user")
            self.assertTrue(confirmed["confirmed"])
            self.assertEqual(confirmed["source_ref"], "手动输入")
            inferred = next(a for a in attributes.values() if a["key"] == "推断")
            self.assertEqual(inferred["provenance"], "ai_suggested")
            self.assertFalse(inferred["confirmed"])

            attachments = self.read_json(archive, "attachments.json")["items"]
            self.assertEqual(len(attachments), 1)
            exported = attachments[0]
            self.assertEqual(exported["id"], attachment["id"])
            self.assertTrue(exported["included"])
            self.assertEqual(exported["verification"], "match")
            self.assertEqual(
                exported["export_path"], f"files/attachments/{attachment['id']}"
            )
            self.assertEqual(archive.read(exported["export_path"]), attachment_bytes)
            self.assertEqual(
                exported["exported_sha256"], hashlib.sha256(attachment_bytes).hexdigest()
            )

            payload = self.read_json(archive, "integrity_issues.json")
            self.assertEqual(payload["dataset"], "integrity_issues")

        package_bytes = Path(data["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(package_bytes).hexdigest(), data["sha256"])
        download = self.owner.get(data["download_path"], headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(hashlib.sha256(download.data).hexdigest(), data["sha256"])
        download.close()

    # ------------------------------------------------------------------
    # 2. Empty archive
    # ------------------------------------------------------------------
    def test_export_empty_archive(self):
        response = self.export()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertTrue(data["complete"])
        self.assertEqual(data["counts"]["content_total"], 0)
        self.assertEqual(data["counts"]["attachments_total"], 0)

        with self.open_package(data) as archive:
            self.assertTrue(ZIP_MEMBERS.issubset(set(archive.namelist())))
            for name in ("content.json", "relations.json", "attributes.json", "attachments.json"):
                self.assertEqual(self.read_json(archive, name)["count"], 0)
            manifest = self.read_json(archive, "manifest.json")
            self.assertTrue(manifest["completeness"]["complete"])
            self.assertEqual(manifest["issues"], [])
            self.assertFalse(any(name.startswith("files/") for name in archive.namelist()))

    # ------------------------------------------------------------------
    # 3. Stable identity, relations, attributes, deleted records
    # ------------------------------------------------------------------
    def test_export_preserves_deleted_content_relations_and_attributes(self):
        source = self.create_content("来源", "来源正文")
        deleted = self.create_content("将被删除", "删除正文")
        relation = self.add_relation(source["id"], deleted["id"], "EVOLVED_INTO")
        attribute = self.add_attribute(source["id"], "阶段", "验证", confirmed=True)

        removed = self.owner.delete(
            f"/api/content/{deleted['id']}",
            json={"revision": deleted["revision"]},
            headers=self.headers,
        )
        self.assertEqual(removed.status_code, 200)

        response = self.export()
        data = response.get_json()["data"]
        self.assertTrue(data["complete"], data["issues"])

        with self.open_package(data) as archive:
            content_items = {row["id"]: row for row in self.read_json(archive, "content.json")["items"]}
            self.assertEqual(content_items[deleted["id"]]["state"], "deleted")
            self.assertIsNotNone(content_items[deleted["id"]]["deleted_at"])
            self.assertEqual(content_items[source["id"]]["state"], "active")

            relation_row = self.read_json(archive, "relations.json")["items"][0]
            self.assertEqual(relation_row["id"], relation["id"])
            self.assertEqual(relation_row["from_id"], source["id"])
            self.assertEqual(relation_row["to_id"], deleted["id"])
            self.assertTrue(relation_row["intact"])
            self.assertEqual(relation_row["to_state"], "deleted")

            attributes = self.read_json(archive, "attributes.json")["items"]
            self.assertEqual(attributes[0]["id"], attribute["id"])
            self.assertEqual(attributes[0]["content_id"], source["id"])
            self.assertTrue(attributes[0]["confirmed"])

        # Export must not restore or otherwise change the deleted record.
        remaining = self.owner.get(
            f"/api/content/{deleted['id']}?include_deleted=1", headers=self.headers
        ).get_json()["data"]
        self.assertEqual(remaining["state"], "deleted")

    # ------------------------------------------------------------------
    # 4. Multiple attachments, duplicate content and checksums
    # ------------------------------------------------------------------
    def test_export_multiple_attachments_and_duplicate_content(self):
        first = self.create_content("同名", "完全相同的正文")
        second = self.create_content("同名", "完全相同的正文")
        alpha = self.upload(first["id"], b"alpha-bytes", "op_export_multi_1")
        beta = self.upload(first["id"], b"beta-bytes-different", "op_export_multi_2")

        response = self.export()
        data = response.get_json()["data"]
        self.assertTrue(data["complete"], data["issues"])
        self.assertEqual(data["counts"]["content_total"], 2)
        self.assertEqual(data["counts"]["attachments_included"], 2)

        with self.open_package(data) as archive:
            content_items = self.read_json(archive, "content.json")["items"]
            self.assertEqual(len(content_items), 2)
            self.assertNotEqual(content_items[0]["id"], content_items[1]["id"])
            self.assertEqual(content_items[0]["body"], content_items[1]["body"])

            attachments = {
                row["id"]: row for row in self.read_json(archive, "attachments.json")["items"]
            }
            for attachment, expected in ((alpha, b"alpha-bytes"), (beta, b"beta-bytes-different")):
                row = attachments[attachment["id"]]
                self.assertTrue(row["included"])
                self.assertEqual(archive.read(row["export_path"]), expected)
                self.assertEqual(
                    row["exported_sha256"], hashlib.sha256(expected).hexdigest()
                )
                self.assertNotEqual(row["exported_sha256"], attachment["id"])

    # ------------------------------------------------------------------
    # 5. Missing or corrupted attachments: report and partial status
    # ------------------------------------------------------------------
    def test_export_reports_missing_and_corrupted_attachments(self):
        content = self.create_content("附件故障")
        missing = self.upload(content["id"], b"missing-bytes", "op_export_fault_1")
        corrupted = self.upload(content["id"], b"original-bytes", "op_export_fault_2")

        (self.data_dir / "objects" / missing["id"]).unlink()
        (self.data_dir / "objects" / corrupted["id"]).write_bytes(b"corrupted")

        response = self.export()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()["data"]
        self.assertFalse(data["complete"])
        self.assertEqual(data["status"], "partial")
        codes = {issue["code"] for issue in data["issues"]}
        self.assertIn("attachment_file_missing", codes)
        self.assertIn("attachment_hash_mismatch", codes)
        self.assertGreaterEqual(data["counts"]["issues_error"], 2)
        self.assertEqual(data["counts"]["attachments_included"], 1)

        with self.open_package(data) as archive:
            attachments = {
                row["id"]: row for row in self.read_json(archive, "attachments.json")["items"]
            }
            self.assertFalse(attachments[missing["id"]]["included"])
            self.assertIsNone(attachments[missing["id"]]["export_path"])
            self.assertFalse(
                any(name.endswith(missing["id"]) for name in archive.namelist())
            )
            self.assertTrue(attachments[corrupted["id"]]["included"])
            self.assertEqual(
                attachments[corrupted["id"]]["verification"], "sha256_mismatch"
            )
            self.assertEqual(
                archive.read(attachments[corrupted["id"]]["export_path"]), b"corrupted"
            )
            manifest = self.read_json(archive, "manifest.json")
            self.assertFalse(manifest["completeness"]["complete"])
            self.assertEqual(manifest["completeness"]["status"], "partial")

    def test_export_reports_missing_relation_endpoint(self):
        content = self.create_content("关系端点")
        conn = connect(self.data_dir / "archive.db")
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO relations(id, from_id, to_id, type, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                ("r_dangling_export", content["id"], "c_missing_endpoint", "PART_OF", utcnow()),
            )
            conn.commit()
        finally:
            conn.close()

        data = self.export().get_json()["data"]
        self.assertFalse(data["complete"])
        self.assertIn("relation_endpoint_missing", {i["code"] for i in data["issues"]})
        with self.open_package(data) as archive:
            relation = self.read_json(archive, "relations.json")["items"][0]
            self.assertEqual(relation["to_id"], "c_missing_endpoint")
            self.assertFalse(relation["to_exists"])
            self.assertFalse(relation["intact"])
            self.assertIsNone(relation["to_state"])

    # ------------------------------------------------------------------
    # 6. Export is not publication and does not mutate the archive
    # ------------------------------------------------------------------
    def test_export_does_not_publish_or_mutate_archive(self):
        content = self.create_content("私有草稿", "不应公开的正文", visibility="private")
        attachment = self.upload(content["id"], b"private-bytes", "op_export_private_1")

        before = {
            table: self.db_rows(table)
            for table in ("content", "relations", "attributes", "attachments", "operations")
        }
        object_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.data_dir / "objects").iterdir()
        }

        response = self.export()
        data = response.get_json()["data"]
        self.assertTrue(data["complete"], data["issues"])

        after = {
            table: self.db_rows(table)
            for table in ("content", "relations", "attributes", "attachments", "operations")
        }
        self.assertEqual(before, after)
        self.assertEqual(
            object_hashes,
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.data_dir / "objects").iterdir()
            },
        )

        self.assertEqual(self.anonymous.get(f"/p/{content['id']}").status_code, 404)
        self.assertEqual(
            self.anonymous.get(f"/api/public/content/{content['id']}").status_code, 404
        )
        self.assertEqual(
            self.anonymous.get(f"/api/attachments/{attachment['id']}/file").status_code, 404
        )

        with self.open_package(data) as archive:
            manifest = self.read_json(archive, "manifest.json")
            self.assertTrue(manifest["boundaries"]["includes_private_content"])
            self.assertFalse(manifest["boundaries"]["publishes_content"])
            self.assertFalse(manifest["boundaries"]["changes_visibility"])
            content_row = self.read_json(archive, "content.json")["items"][0]
            self.assertFalse(content_row["public"])
            self.assertEqual(content_row["visibility"], "private")
            attachment_row = self.read_json(archive, "attachments.json")["items"][0]
            self.assertEqual(attachment_row["visibility"], "private")

    # ------------------------------------------------------------------
    # 7. Failure and invalid output paths are reported honestly
    # ------------------------------------------------------------------
    def test_export_rejects_invalid_output_paths(self):
        payload = self.create_content("路径测试")

        missing_parent = Path(self.tempdir.name) / "no-such-dir" / "out.zip"
        response = self.export(output_path=str(missing_parent))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "validation_error")
        self.assertFalse(missing_parent.exists())

        existing = Path(self.tempdir.name) / "already.zip"
        existing.write_bytes(b"keep me")
        response = self.export(output_path=str(existing))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(existing.read_bytes(), b"keep me")

        wrong_suffix = Path(self.tempdir.name) / "out.txt"
        response = self.export(output_path=str(wrong_suffix))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(wrong_suffix.exists())

        bad_type = self.export(output_path=123)
        self.assertEqual(bad_type.status_code, 422)

        # A rejected export must not leave partial packages behind.
        exports_dir = self.data_dir / "exports"
        leftovers = list(exports_dir.glob("*")) if exports_dir.exists() else []
        self.assertEqual(leftovers, [])
        self.assertEqual(self.export().status_code, 200)

    def test_export_write_failure_reports_error_and_leaves_no_partial_file(self):
        self.create_content("写入失败")
        output = Path(self.tempdir.name) / "failure.zip"
        with mock.patch("archive.export.publish_new_file", side_effect=OSError("disk unavailable")):
            response = self.export(output_path=str(output))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"]["code"], "export_failed")
        self.assertFalse(output.exists())
        leftovers = [p.name for p in Path(self.tempdir.name).glob(".*.tmp")]
        self.assertEqual(leftovers, [])
        # The archive itself is still readable and exportable afterwards.
        self.assertEqual(self.export().status_code, 200)

    # ------------------------------------------------------------------
    # 8. Owner-only access
    # ------------------------------------------------------------------
    def test_export_requires_owner_and_download_is_guarded(self):
        response = self.anonymous.post("/api/export", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            self.anonymous.get("/api/export/archive-export-x.zip").status_code, 401
        )

        data = self.export().get_json()["data"]
        self.assertEqual(self.anonymous.get(data["download_path"]).status_code, 401)
        self.assertEqual(
            self.owner.get("/api/export/../../../archive.db", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            self.owner.get("/api/export/not-a-package.txt", headers=self.headers).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
