"""Content Model v2: Summary / Type / Primary Space / Related Spaces / Lifecycle.

Covers the v1 -> v2 migration, store validation and filtering, export
projection, and the privacy boundary for the new owner-only fields.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import (
    SCHEMA_VERSION,
    connect,
    extract_legacy_summary,
    init_db,
    utcnow,
)
from archive.export import ArchiveExporter
from archive.store import ArchiveStore, ValidationError
from tests.test_migrations import V1_SCHEMA

TOKEN = "test-owner-token-content-model-55667788"

SAMPLE_ROWS = (
    ("c_fixture_kite", "风筝站日志", "风筝站记录了三次虚构观测。"),
    ("c_fixture_garden", "月面花园", "月面花园需要一套虚构的灌溉设计。"),
    ("c_fixture_clock", "水下钟表", "水下钟表使用虚构的潮汐刻度。"),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seed_v1_with_synthetic_content(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    now = utcnow()
    for content_id, title, summary in SAMPLE_ROWS:
        body = f"{title}\n\nSummary\n\n{summary}\n\n正文\n\n合成迁移样本。"
        conn.execute(
            "INSERT INTO content(id, title, body, visibility, visibility_selected, "
            "revision, state, created_at, updated_at) VALUES(?, ?, ?, 'private', 1, 1, 'active', ?, ?)",
            (content_id, title, body, now, now),
        )
    conn.executemany(
        "INSERT INTO relations(id, from_id, to_id, type, created_at) VALUES(?, ?, ?, ?, ?)",
        (
            ("r_fixture_1", "c_fixture_garden", "c_fixture_clock", "REFERENCES", now),
            ("r_fixture_2", "c_fixture_clock", "c_fixture_garden", "DERIVED_FROM", now),
            ("r_fixture_3", "c_fixture_kite", "c_fixture_garden", "REFERENCES", now),
        ),
    )
    conn.commit()
    conn.close()


class MigrationV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "archive.db"
        seed_v1_with_synthetic_content(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def snapshot(self) -> dict:
        conn = connect(self.db_path)
        try:
            content = {
                row["id"]: dict(row)
                for row in conn.execute("SELECT * FROM content").fetchall()
            }
            relations = {
                row["id"]: dict(row)
                for row in conn.execute("SELECT * FROM relations").fetchall()
            }
            return {"content": content, "relations": relations}
        finally:
            conn.close()

    def test_migration_preserves_identity_body_visibility_and_relations(self):
        before = self.snapshot()
        report = init_db(self.db_path)
        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 1)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 2)

        after = self.snapshot()
        self.assertEqual(set(after["content"]), set(before["content"]))
        self.assertEqual(set(after["relations"]), set(before["relations"]))
        for content_id, old in before["content"].items():
            new = after["content"][content_id]
            self.assertEqual(new["title"], old["title"])
            self.assertEqual(new["body"], old["body"])
            self.assertEqual(new["revision"], old["revision"])
            self.assertEqual(new["visibility"], "private")
            self.assertEqual(new["visibility_selected"], 1)
        for relation_id, old in before["relations"].items():
            new = after["relations"][relation_id]
            self.assertEqual(
                (new["from_id"], new["to_id"], new["type"]),
                (old["from_id"], old["to_id"], old["type"]),
            )

        for content_id, _, summary in SAMPLE_ROWS:
            row = after["content"][content_id]
            self.assertEqual(row["type"], "THOUGHT")
            self.assertEqual(row["primary_space"], "ME")
            self.assertEqual(row["lifecycle"], "DRAFT")
            self.assertEqual(row["summary"], summary)

        conn = connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM content_spaces").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        init_db(self.db_path)
        second = init_db(self.db_path)
        self.assertFalse(second["migrated"])
        self.assertEqual(second["applied_versions"], [])

    def test_migration_applies_same_defaults_to_renamed_rows(self):
        content_id, _, summary = SAMPLE_ROWS[0]
        with closing(sqlite3.connect(str(self.db_path))) as conn, conn:
            conn.execute("UPDATE content SET title = ? WHERE id = ?", ("改名后的观测日志", content_id))
        init_db(self.db_path)
        row = self.snapshot()["content"][content_id]
        self.assertEqual(row["title"], "改名后的观测日志")
        self.assertEqual(row["type"], "THOUGHT")
        self.assertEqual(row["primary_space"], "ME")
        self.assertEqual(row["summary"], summary)

    def test_summary_extraction_is_conservative(self):
        self.assertEqual(extract_legacy_summary("没有结构"), "")
        self.assertEqual(extract_legacy_summary("标题\n\n正文\n\n内容"), "")
        self.assertEqual(
            extract_legacy_summary("标题\n\nSummary\n\n第一段\n\n正文\n\n细节"),
            "第一段",
        )


class ContentModelStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        init_db(self.data_dir / "archive.db")
        self.store = ArchiveStore(self.data_dir)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_and_read_all_new_fields(self):
        created = self.store.create_content(
            "项目 A",
            "正文 A",
            "private",
            summary="摘要 A",
            content_type="PROJECT",
            primary_space="BUILD",
            lifecycle="COMPLETED",
            related_spaces=["THINK", "BUILD", "THINK"],
        )
        self.assertEqual(created["summary"], "摘要 A")
        self.assertEqual(created["type"], "PROJECT")
        self.assertEqual(created["primary_space"], "BUILD")
        self.assertEqual(created["lifecycle"], "COMPLETED")
        self.assertEqual(created["related_spaces"], ["THINK"])
        self.assertEqual(created["body"], "正文 A")

    def test_validation_rejects_unknown_enum_values(self):
        with self.assertRaises(ValidationError):
            self.store.create_content("x", "y", content_type="IDEA")
        with self.assertRaises(ValidationError):
            self.store.create_content("x", "y", primary_space="HOME")
        with self.assertRaises(ValidationError):
            self.store.create_content("x", "y", lifecycle="DELETED")
        with self.assertRaises(ValidationError):
            self.store.create_content("x", "y", primary_space="ME", related_spaces=["ARCHIVE"])

    def test_update_changes_fields_and_keeps_body_independent(self):
        created = self.store.create_content(
            "旧标题", "旧正文", "private", summary="旧摘要", primary_space="ME",
            content_type="THOUGHT", related_spaces=["BUILD"],
        )
        updated = self.store.update_content(
            created["id"],
            created["revision"],
            summary="新摘要",
            content_type="EXPERIENCE",
            primary_space="BUILD",
            lifecycle="ARCHIVED",
        )
        self.assertEqual(updated["summary"], "新摘要")
        self.assertEqual(updated["body"], "旧正文")
        self.assertEqual(updated["type"], "EXPERIENCE")
        self.assertEqual(updated["primary_space"], "BUILD")
        self.assertEqual(updated["lifecycle"], "ARCHIVED")
        # BUILD became primary, so it is no longer a related space.
        self.assertEqual(updated["related_spaces"], [])
        self.assertEqual(updated["revision"], created["revision"] + 1)

    def test_list_filters_space_type_lifecycle_and_summary_search(self):
        self.store.create_content(
            "项目", "无关正文", "private", summary="独特摘要词甲", content_type="PROJECT",
            primary_space="BUILD", lifecycle="DRAFT", related_spaces=["THINK"],
        )
        self.store.create_content(
            "经历", "无关正文", "private", summary="另一段", content_type="EXPERIENCE",
            primary_space="ME", lifecycle="ARCHIVED",
        )
        by_space = self.store.list_content(space="THINK")
        self.assertEqual([item["title"] for item in by_space], ["项目"])
        self.assertEqual([item["title"] for item in self.store.list_content(space="ME")], ["经历"])
        self.assertEqual(
            [item["title"] for item in self.store.list_content(content_type="PROJECT")], ["项目"]
        )
        self.assertEqual(
            [item["title"] for item in self.store.list_content(lifecycle="ARCHIVED")], ["经历"]
        )
        self.assertEqual(
            [item["title"] for item in self.store.list_content(query="独特摘要词甲")], ["项目"]
        )

    def test_delete_and_restore_keep_content_model_fields(self):
        created = self.store.create_content(
            "可删", "正文", "private", summary="保留摘要", content_type="PROJECT",
            primary_space="COMPETE", lifecycle="COMPLETED", related_spaces=["JOURNEY"],
        )
        self.store.delete_content(created["id"], created["revision"])
        deleted = self.store.get_content(created["id"], include_deleted=True)
        self.assertEqual(deleted["state"], "deleted")
        self.assertEqual(deleted["summary"], "保留摘要")
        self.assertEqual(deleted["related_spaces"], ["JOURNEY"])
        restored = self.store.restore_content(created["id"])["content"]
        self.assertEqual(restored["state"], "active")
        self.assertEqual(restored["lifecycle"], "COMPLETED")
        self.assertEqual(restored["primary_space"], "COMPETE")

    def test_export_contains_new_fields_without_mutating_database(self):
        created = self.store.create_content(
            "导出内容", "正文", "private", summary="导出摘要", content_type="PROJECT",
            primary_space="BUILD", lifecycle="DRAFT", related_spaces=["THINK"],
        )
        before = _sha(json.dumps(self.store.list_content(include_deleted=True), ensure_ascii=False, sort_keys=True))
        result = ArchiveExporter(self.data_dir).export(
            output_path=str(Path(self.tempdir.name) / "export.zip")
        )
        with zipfile.ZipFile(result["path"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            records = json.loads(archive.read("content.json"))["items"]
        self.assertTrue(manifest["completeness"]["complete"])
        record = next(item for item in records if item["id"] == created["id"])
        self.assertEqual(record["summary"], "导出摘要")
        self.assertEqual(record["type"], "PROJECT")
        self.assertEqual(record["primary_space"], "BUILD")
        self.assertEqual(record["lifecycle"], "DRAFT")
        self.assertEqual(record["related_spaces"], ["THINK"])
        after = _sha(json.dumps(self.store.list_content(include_deleted=True), ensure_ascii=False, sort_keys=True))
        self.assertEqual(before, after)


class ContentModelApiPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.owner = self.app.test_client()
        response = self.owner.get(f"/auth?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        with self.owner.session_transaction() as session:
            self.csrf = session["csrf"]
        self.headers = {"X-CSRF-Token": self.csrf}
        self.anonymous = self.app.test_client()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create(self, **payload):
        response = self.owner.post("/api/content", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def test_owner_api_roundtrip_and_filters(self):
        created = self.create(
            title="私密项目", body="正文", summary="私密摘要", visibility="private",
            type="PROJECT", primary_space="BUILD", lifecycle="DRAFT",
            related_spaces=["THINK"],
        )
        listed = self.owner.get("/api/content?space=THINK&type=PROJECT")
        self.assertEqual(listed.status_code, 200)
        items = listed.get_json()["data"]["items"]
        self.assertEqual([item["id"] for item in items], [created["id"]])
        self.assertEqual(items[0]["summary"], "私密摘要")
        patched = self.owner.patch(
            f"/api/content/{created['id']}",
            json={"revision": created["revision"], "summary": "新摘要", "lifecycle": "COMPLETED"},
            headers=self.headers,
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.get_json()["data"]["summary"], "新摘要")
        self.assertEqual(patched.get_json()["data"]["lifecycle"], "COMPLETED")

    def test_invalid_filter_is_rejected(self):
        response = self.owner.get("/api/content?space=HOME")
        self.assertEqual(response.status_code, 422)

    def test_anonymous_cannot_read_private_content_model_fields(self):
        created = self.create(
            title="极度私密标题", body="极度私密正文", summary="极度私密摘要",
            visibility="private", type="PROJECT", primary_space="BUILD",
        )
        self.assertEqual(self.anonymous.get("/api/content").status_code, 401)
        self.assertEqual(self.anonymous.get(f"/api/content/{created['id']}").status_code, 401)
        public = self.anonymous.get(f"/api/public/content/{created['id']}")
        self.assertEqual(public.status_code, 404)
        page = self.anonymous.get(f"/p/{created['id']}")
        self.assertEqual(page.status_code, 404)
        combined = public.get_data(as_text=True) + page.get_data(as_text=True)
        for secret in ("极度私密标题", "极度私密正文", "极度私密摘要", "BUILD"):
            self.assertNotIn(secret, combined)


if __name__ == "__main__":
    unittest.main()
