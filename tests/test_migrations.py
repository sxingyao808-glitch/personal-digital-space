"""Schema migration and upgrade-safety tests (ADR-02 slice).

``V1_SCHEMA`` is a frozen fixture of the shipped v1 baseline. Synthetic
migrations 2 and 3 in this module are test-only fixtures; the production
registry contains only the real v2 Content Model migration.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.app import create_app
from archive.db import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SchemaVersionError,
    connect,
    init_db,
    utcnow,
)

TOKEN = "test-owner-token-migration-11223344"

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE content (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    visibility_selected INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK (visibility IN ('private', 'public')),
    CHECK (visibility_selected IN (0, 1)),
    CHECK (state IN ('active', 'deleted'))
);
CREATE TABLE relations (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL REFERENCES content(id),
    to_id TEXT NOT NULL REFERENCES content(id),
    type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (type IN ('PART_OF', 'DERIVED_FROM', 'LED_TO', 'EVOLVED_INTO', 'REFERENCES')),
    CHECK (from_id <> to_id),
    UNIQUE (from_id, to_id, type)
);
CREATE TABLE attributes (
    id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    provenance TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    source_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content(id),
    filename TEXT NOT NULL,
    byte_size INTEGER,
    sha256 TEXT,
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    visibility TEXT NOT NULL DEFAULT 'private',
    state TEXT NOT NULL DEFAULT 'PENDING',
    operation_id TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource_id TEXT,
    owner_content_id TEXT,
    payload_sha256 TEXT,
    payload_size INTEGER,
    state TEXT NOT NULL DEFAULT 'PENDING',
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE integrity_issues (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT
);
"""


def seed_v1(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    now = utcnow()
    rows = (
        ("c_a", "条目 A", "active", None),
        ("c_b", "条目 B", "deleted", now),
        ("c_c", "条目 C", "active", None),
    )
    for content_id, title, state, deleted_at in rows:
        conn.execute(
            "INSERT INTO content(id, title, body, visibility, visibility_selected, "
            "revision, state, created_at, updated_at, deleted_at) "
            "VALUES(?, ?, ?, 'private', 0, 1, ?, ?, ?, ?)",
            (content_id, title, "正文 " + content_id, state, now, now, deleted_at),
        )
    conn.execute(
        "INSERT INTO relations(id, from_id, to_id, type, created_at) "
        "VALUES('r_1', 'c_a', 'c_c', 'REFERENCES', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO attributes(id, content_id, key, value, provenance, confirmed, "
        "created_at, updated_at) VALUES('a_1', 'c_a', '来源', '本地', "
        "'ai_suggested', 0, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO attachments(id, content_id, filename, byte_size, sha256, mime, "
        "visibility, state, operation_id, created_at, updated_at) "
        "VALUES('f_1', 'c_b', 'note.txt', 3, 'abc', 'text/plain', 'private', "
        "'READY', 'op_1', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()


def migration_two(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE content ADD COLUMN migrated_note TEXT NOT NULL DEFAULT ''")


def migration_three(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS migration_probe (id TEXT PRIMARY KEY)")


class MigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        self.db_path = self.data_dir / "archive.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def version(self) -> int:
        conn = connect(self.db_path)
        try:
            return int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
        finally:
            conn.close()

    def set_version(self, value: str) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (value,))
            conn.commit()
        finally:
            conn.close()

    def dump(self) -> dict[str, list[dict]]:
        queries = {
            "content": (
                "SELECT id, title, body, visibility, visibility_selected, revision, "
                "state, created_at, updated_at, deleted_at FROM content ORDER BY id"
            ),
            "relations": "SELECT * FROM relations ORDER BY id",
            "attributes": "SELECT * FROM attributes ORDER BY id",
            "attachments": "SELECT * FROM attachments ORDER BY id",
        }
        conn = connect(self.db_path)
        try:
            return {
                table: [dict(row) for row in conn.execute(query)]
                for table, query in queries.items()
            }
        finally:
            conn.close()

    def columns(self, table: str) -> set[str]:
        conn = connect(self.db_path)
        try:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 1. Fresh database
    # ------------------------------------------------------------------
    def test_new_database_initializes_at_current_version(self):
        report = init_db(self.db_path)
        self.assertTrue(report["created"])
        self.assertFalse(report["migrated"])
        self.assertEqual(report["from_version"], SCHEMA_VERSION)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        self.assertEqual(report["applied_versions"], [])
        self.assertEqual(self.version(), SCHEMA_VERSION)
        conn = connect(self.db_path)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        self.assertEqual(integrity, "ok")
        self.assertTrue({"content", "relations", "attributes", "attachments", "meta"} <= tables)

    # ------------------------------------------------------------------
    # 2. Existing v1 database
    # ------------------------------------------------------------------
    def test_existing_v1_is_migrated_in_place_without_data_loss(self):
        seed_v1(self.db_path)
        before = self.dump()
        report = init_db(self.db_path)
        self.assertFalse(report["created"])
        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 1)
        self.assertEqual(report["to_version"], 2)
        self.assertEqual(report["applied_versions"], [2])
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.dump(), before)
        self.assertTrue(
            {"summary", "type", "primary_space", "lifecycle"}
            <= self.columns("content")
        )

    # ------------------------------------------------------------------
    # 3. Sequential migrations
    # ------------------------------------------------------------------
    def test_sequential_migrations_run_in_order(self):
        seed_v1(self.db_path)
        calls: list[int] = []

        def step_two(conn):
            calls.append(2)
            migration_two(conn)

        def step_three(conn):
            calls.append(3)
            migration_three(conn)

        report = init_db(
            self.db_path, target_version=3, migrations={2: step_two, 3: step_three}
        )
        self.assertEqual(calls, [2, 3])
        self.assertEqual(report["from_version"], 1)
        self.assertEqual(report["to_version"], 3)
        self.assertEqual(report["applied_versions"], [2, 3])
        self.assertEqual(self.version(), 3)
        self.assertIn("migrated_note", self.columns("content"))
        self.assertTrue((self.data_dir / "archive.db").is_file())

    def test_new_database_can_migrate_from_baseline(self):
        report = init_db(
            self.db_path,
            target_version=3,
            migrations={2: migration_two, 3: migration_three},
        )
        self.assertTrue(report["created"])
        self.assertEqual(report["applied_versions"], [3])
        self.assertEqual(self.version(), 3)

    # ------------------------------------------------------------------
    # 4. Failure in the middle
    # ------------------------------------------------------------------
    def test_intermediate_failure_keeps_previous_version_and_rolls_back(self):
        seed_v1(self.db_path)
        before = self.dump()

        def failing_three(conn):
            conn.execute("ALTER TABLE content ADD COLUMN partial_probe TEXT")
            raise RuntimeError("synthetic migration failure")

        with self.assertRaises(SchemaVersionError) as caught:
            init_db(
                self.db_path,
                target_version=3,
                migrations={2: migration_two, 3: failing_three},
            )
        self.assertEqual(caught.exception.details["failed_version"], 3)
        self.assertEqual(caught.exception.details["database_version"], 2)
        self.assertEqual(self.version(), 2)
        self.assertIn("migrated_note", self.columns("content"))
        self.assertNotIn("partial_probe", self.columns("content"))
        self.assertEqual(self.dump(), before)

    def test_failed_first_migration_keeps_version_one(self):
        seed_v1(self.db_path)
        before = self.dump()

        def failing_two(conn):
            conn.execute("ALTER TABLE content ADD COLUMN partial_two TEXT")
            raise RuntimeError("boom")

        with self.assertRaises(SchemaVersionError):
            init_db(self.db_path, target_version=2, migrations={2: failing_two})
        self.assertEqual(self.version(), 1)
        self.assertNotIn("partial_two", self.columns("content"))
        self.assertEqual(self.dump(), before)

    def test_foreign_key_violation_rolls_back_migration(self):
        seed_v1(self.db_path)

        def breaking_two(conn):
            conn.execute("PRAGMA defer_foreign_keys = ON")
            conn.execute("DELETE FROM content WHERE id = 'c_c'")

        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path, target_version=2, migrations={2: breaking_two})
        self.assertEqual(caught.exception.details["failed_version"], 2)
        self.assertEqual(self.version(), 1)
        conn = connect(self.db_path)
        try:
            content_ids = {row["id"] for row in conn.execute("SELECT id FROM content")}
            relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()
        self.assertIn("c_c", content_ids)
        self.assertEqual(relation_count, 1)

    # ------------------------------------------------------------------
    # 5. Missing migration / unknown future version
    # ------------------------------------------------------------------
    def test_missing_migration_refuses_and_keeps_version(self):
        seed_v1(self.db_path)
        before = self.dump()
        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path, target_version=3, migrations={2: migration_two})
        self.assertEqual(caught.exception.details["missing_versions"], [3])
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.dump(), before)

    def test_unknown_future_version_is_refused(self):
        seed_v1(self.db_path)
        self.set_version("99")
        before = self.dump()
        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path)
        self.assertEqual(caught.exception.details["database_version"], 99)
        self.assertEqual(self.version(), 99)
        self.assertEqual(self.dump(), before)

    def test_non_numeric_version_is_refused(self):
        seed_v1(self.db_path)
        self.set_version("not-a-number")
        with self.assertRaises(SchemaVersionError):
            init_db(self.db_path)

    def test_incomplete_schema_is_refused(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES('schema_version', '1')")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(SchemaVersionError) as caught:
            init_db(self.db_path)
        self.assertIn("missing_tables", caught.exception.details)

    # ------------------------------------------------------------------
    # 6. Repeated startup at target version
    # ------------------------------------------------------------------
    def test_repeated_start_at_target_does_not_rerun_migrations(self):
        seed_v1(self.db_path)
        calls: list[int] = []

        def step_two(conn):
            calls.append(2)
            MIGRATIONS[2](conn)

        init_db(self.db_path, target_version=2, migrations={2: step_two})
        init_db(self.db_path, target_version=2, migrations={2: step_two})
        self.assertEqual(calls, [2])
        self.assertEqual(self.version(), 2)

    # ------------------------------------------------------------------
    # 7. Data invariants
    # ------------------------------------------------------------------
    def test_invariants_are_preserved_across_migration(self):
        seed_v1(self.db_path)
        before = self.dump()
        init_db(
            self.db_path,
            target_version=2,
            migrations={2: migration_two},
        )
        after = self.dump()
        self.assertEqual(
            [row["id"] for row in after["content"]],
            [row["id"] for row in before["content"]],
        )
        self.assertEqual(
            [(row["id"], row["from_id"], row["to_id"], row["type"]) for row in after["relations"]],
            [(row["id"], row["from_id"], row["to_id"], row["type"]) for row in before["relations"]],
        )
        self.assertEqual(
            [(row["id"], row["provenance"], bool(row["confirmed"])) for row in after["attributes"]],
            [(row["id"], row["provenance"], bool(row["confirmed"])) for row in before["attributes"]],
        )
        deleted = [row for row in after["content"] if row["state"] == "deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertIsNotNone(deleted[0]["deleted_at"])
        self.assertEqual(
            [(row["id"], row["content_id"]) for row in after["attachments"]],
            [(row["id"], row["content_id"]) for row in before["attachments"]],
        )
        conn = connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 8. Application startup guard
    # ------------------------------------------------------------------
    def test_app_startup_refuses_incompatible_database(self):
        seed_v1(self.db_path)
        self.set_version("99")
        with self.assertRaises(SchemaVersionError):
            create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        self.assertFalse((self.data_dir / "owner_secret").exists())

    def test_app_startup_accepts_v1_database(self):
        seed_v1(self.db_path)
        app = create_app(self.data_dir, bootstrap_token=TOKEN, testing=True)
        client = app.test_client()
        client.get(f"/auth?token={TOKEN}")
        response = client.get("/api/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["data"]["items"]), 2)

    def test_cli_refuses_incompatible_database(self):
        seed_v1(self.db_path)
        self.set_version("99")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_app.py"),
                "--data-dir",
                str(self.data_dir),
                "--no-open",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("schema 不兼容", completed.stderr)
        self.assertEqual(self.version(), 99)

    # ------------------------------------------------------------------
    # 9. Production registry only exposes real migrations
    # ------------------------------------------------------------------
    def test_production_registry_only_exposes_real_migrations(self):
        self.assertEqual(sorted(MIGRATIONS), [2])
        self.assertEqual(MIGRATIONS[2].__name__, "_migrate_002_content_model")
        self.assertEqual(SCHEMA_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
