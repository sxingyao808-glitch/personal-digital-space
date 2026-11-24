"""SQLite persistence layer for the local Archive.

The schema is intentionally explicit: every Content, Relation, Attribute and
attachment has its own stable identity independent of titles, paths or views.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 2

# Schema version 1 is the existing baseline created by SCHEMA below. Real
# schema changes start at version 2 and register exactly one callable per
# target version. Callables must be safe to run only once, inside a transaction.
Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: dict[int, Migration] = {}

REQUIRED_TABLES = (
    "meta",
    "content",
    "relations",
    "attributes",
    "attachments",
    "operations",
    "integrity_issues",
)


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk schema cannot be safely used or upgraded."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    body               TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    type               TEXT NOT NULL DEFAULT 'THOUGHT',
    primary_space      TEXT NOT NULL DEFAULT 'ME',
    lifecycle          TEXT NOT NULL DEFAULT 'DRAFT',
    visibility         TEXT NOT NULL DEFAULT 'private',
    visibility_selected INTEGER NOT NULL DEFAULT 0,
    revision           INTEGER NOT NULL DEFAULT 1,
    state              TEXT NOT NULL DEFAULT 'active',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    deleted_at         TEXT,
    CHECK (type IN ('PROJECT', 'THOUGHT', 'EXPERIENCE')),
    CHECK (primary_space IN ('ME', 'BUILD', 'THINK', 'JOURNEY', 'COMPETE')),
    CHECK (lifecycle IN ('DRAFT', 'PUBLISHED', 'COMPLETED', 'ARCHIVED')),
    CHECK (visibility IN ('private', 'public')),
    CHECK (visibility_selected IN (0, 1)),
    CHECK (state IN ('active', 'deleted'))
);

CREATE INDEX IF NOT EXISTS idx_content_state_updated
    ON content(state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_model
    ON content(primary_space, type, lifecycle);

CREATE TABLE IF NOT EXISTS content_spaces (
    content_id TEXT NOT NULL REFERENCES content(id),
    space      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_id, space),
    CHECK (space IN ('ME', 'BUILD', 'THINK', 'JOURNEY', 'COMPETE'))
);

CREATE INDEX IF NOT EXISTS idx_content_spaces_space ON content_spaces(space);

CREATE TABLE IF NOT EXISTS relations (
    id         TEXT PRIMARY KEY,
    from_id    TEXT NOT NULL REFERENCES content(id),
    to_id      TEXT NOT NULL REFERENCES content(id),
    type       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (type IN ('PART_OF', 'DERIVED_FROM', 'LED_TO', 'EVOLVED_INTO', 'REFERENCES')),
    CHECK (from_id <> to_id),
    UNIQUE (from_id, to_id, type)
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id);

CREATE TABLE IF NOT EXISTS attributes (
    id          TEXT PRIMARY KEY,
    content_id  TEXT NOT NULL REFERENCES content(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    provenance  TEXT NOT NULL,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    source_ref  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (provenance IN ('user', 'ai_inferred', 'ai_suggested', 'user_confirmed')),
    CHECK (confirmed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_attributes_content ON attributes(content_id);

CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    content_id   TEXT NOT NULL REFERENCES content(id),
    filename     TEXT NOT NULL,
    byte_size    INTEGER,
    sha256       TEXT,
    mime         TEXT NOT NULL DEFAULT 'application/octet-stream',
    visibility   TEXT NOT NULL DEFAULT 'private',
    state        TEXT NOT NULL DEFAULT 'PENDING',
    operation_id TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (visibility IN ('private', 'public')),
    CHECK (state IN ('PENDING', 'READY', 'BROKEN', 'QUARANTINED'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_content ON attachments(content_id);
CREATE INDEX IF NOT EXISTS idx_attachments_state ON attachments(state);

CREATE TABLE IF NOT EXISTS operations (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    resource_id      TEXT,
    owner_content_id TEXT,
    payload_sha256   TEXT,
    payload_size     INTEGER,
    state            TEXT NOT NULL DEFAULT 'PENDING',
    result           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK (state IN ('PENDING', 'COMPLETED', 'FAILED', 'UNCERTAIN'))
);

CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state);

CREATE TABLE IF NOT EXISTS integrity_issues (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    target      TEXT NOT NULL,
    detail      TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (state IN ('open', 'resolved'))
);
"""

SUMMARY_HEADING = "summary"
BODY_HEADING = "正文"


def extract_legacy_summary(body: str) -> str:
    """Return the paragraph an owner wrote under a literal ``Summary`` heading.

    This only reads an existing, explicitly labelled section; it never rewrites
    the body and returns ``""`` when the structure is absent. Callers must treat
    an empty result as "unknown", never as an invitation to generate text.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != SUMMARY_HEADING:
            continue
        collected: list[str] = []
        for raw in lines[index + 1 :]:
            stripped = raw.strip()
            if not stripped:
                if collected:
                    break
                continue
            if not collected and stripped == BODY_HEADING:
                break
            collected.append(stripped)
        return "\n".join(collected).strip()
    return ""


def _migrate_002_content_model(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE content ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "ALTER TABLE content ADD COLUMN type TEXT NOT NULL DEFAULT 'THOUGHT'"
        " CHECK (type IN ('PROJECT', 'THOUGHT', 'EXPERIENCE'))"
    )
    conn.execute(
        "ALTER TABLE content ADD COLUMN primary_space TEXT NOT NULL DEFAULT 'ME'"
        " CHECK (primary_space IN ('ME', 'BUILD', 'THINK', 'JOURNEY', 'COMPETE'))"
    )
    conn.execute(
        "ALTER TABLE content ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'DRAFT'"
        " CHECK (lifecycle IN ('DRAFT', 'PUBLISHED', 'COMPLETED', 'ARCHIVED'))"
    )
    # ``executescript`` would implicitly commit the surrounding migration
    # transaction, so the two statements are executed one by one.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS content_spaces ("
        " content_id TEXT NOT NULL REFERENCES content(id),"
        " space TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (content_id, space),"
        " CHECK (space IN ('ME', 'BUILD', 'THINK', 'JOURNEY', 'COMPETE'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_spaces_space"
        " ON content_spaces(space)"
    )
    for row in conn.execute("SELECT id, body FROM content").fetchall():
        summary = extract_legacy_summary(row["body"])
        if summary:
            conn.execute(
                "UPDATE content SET summary = ? WHERE id = ?", (summary, row["id"])
            )


MIGRATIONS[2] = _migrate_002_content_model


def utcnow() -> str:
    """Return a sortable UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    """Create an opaque stable identifier with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex}"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def missing_schema_tables(tables: set[str], version: int) -> list[str]:
    required = set(REQUIRED_TABLES)
    if version >= 2:
        required.add("content_spaces")
    return sorted(required - tables)


def _read_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise SchemaVersionError("数据库缺少 schema_version 元数据，无法确定兼容性。")
    try:
        version = int(str(row["value"]).strip())
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError(
            "schema_version 不是有效整数。", value=str(row["value"])
        ) from exc
    if version < 1:
        raise SchemaVersionError("schema_version 必须为正数。", value=version)
    return version


def init_db(
    db_path: str | Path,
    *,
    target_version: int | None = None,
    migrations: dict[int, Migration] | None = None,
) -> dict[str, Any]:
    """Create a new database or upgrade an existing one to the target version.

    Version 1 is the existing baseline and is not re-run. Every version above
    it must have exactly one registered migration; migrations run in ascending
    order and each one commits together with its version update. Any failure
    stops the upgrade, keeps the previous version, and raises
    SchemaVersionError so the caller can refuse to serve writes.

    This function only manages the database file. It never copies attachment
    objects or creates backups; before introducing a real migration, the
    operator must take a full Archive backup through run_app.py --backup.
    Migration callables must not commit or roll back the connection themselves.

    ``target_version`` and ``migrations`` are test-injection parameters only.
    Production callers (create_app / run_app) must omit them so the target is
    always SCHEMA_VERSION and the registry is always MIGRATIONS.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = SCHEMA_VERSION if target_version is None else target_version
    registry = MIGRATIONS if migrations is None else migrations
    if not isinstance(target, int) or target < 1:
        raise SchemaVersionError("目标 schema 版本无效。", target=target)

    conn: sqlite3.Connection | None = None
    created = False
    try:
        conn = connect(path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")

        tables = _table_names(conn)
        if not tables:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            created = True
        else:
            if "meta" not in tables:
                raise SchemaVersionError(
                    "数据库存在表但缺少 meta，无法确定 schema_version。",
                    tables=sorted(tables),
                )
            missing_tables = missing_schema_tables(tables, 1)
            if missing_tables:
                raise SchemaVersionError(
                    "数据库结构不完整，已拒绝启动。", missing_tables=missing_tables
                )

        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise SchemaVersionError(
                "数据库完整性检查未通过，已拒绝启动。", integrity=str(integrity)
            )

        stored = _read_schema_version(conn)
        if stored > target:
            raise SchemaVersionError(
                "数据库 schema 版本高于当前程序支持版本，已拒绝启动。",
                database_version=stored,
                supported_version=target,
            )

        missing_tables = missing_schema_tables(_table_names(conn), stored)
        if missing_tables:
            raise SchemaVersionError("数据库结构不完整，已拒绝启动。", missing_tables=missing_tables)
        pending = list(range(stored + 1, target + 1))
        missing_migrations = [version for version in pending if version not in registry]
        if missing_migrations:
            raise SchemaVersionError(
                "缺少必要的 schema 迁移，已拒绝启动。",
                database_version=stored,
                target_version=target,
                missing_versions=missing_migrations,
            )

        applied: list[int] = []
        current = stored
        for version in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                on_disk = _read_schema_version(conn)
                if on_disk != version - 1:
                    conn.rollback()
                    raise SchemaVersionError(
                        "迁移期间数据库版本发生变化，已停止。",
                        expected_version=version - 1,
                        database_version=on_disk,
                        failed_version=version,
                    )
                registry[version](conn)
                if migrations is None:
                    missing = missing_schema_tables(_table_names(conn), version)
                    if missing:
                        raise SchemaVersionError("迁移后的数据库结构不完整。", missing_tables=missing)
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise SchemaVersionError(
                        f"schema 迁移 {version} 后外键完整性检查未通过。",
                        failed_version=version,
                        violations=len(violations),
                    )
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(version),),
                )
                conn.commit()
            except SchemaVersionError:
                conn.rollback()
                raise
            except BaseException as exc:
                conn.rollback()
                raise SchemaVersionError(
                    f"schema 迁移 {version} 失败，版本保持在 {current}。",
                    database_version=current,
                    target_version=target,
                    failed_version=version,
                    reason=type(exc).__name__,
                ) from exc
            applied.append(version)
            current = version

        return {
            "created": created,
            "migrated": bool(applied),
            "from_version": stored,
            "to_version": target,
            "applied_versions": applied,
            "integrity": integrity,
        }
    except SchemaVersionError:
        raise
    except sqlite3.Error as exc:
        raise SchemaVersionError(
            "数据库无法读取或检查，已拒绝启动。",
            reason=type(exc).__name__,
            detail=str(exc),
        ) from exc
    finally:
        if conn is not None:
            conn.close()


@contextmanager
def transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_meta(db_path: str | Path, key: str, value: str) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(db_path: str | Path, key: str, default: str | None = None) -> str | None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()
