"""Content, Relation and Attribute services.

All user-facing behavior is expressed here so the HTTP layer stays thin and the
same operations can be verified directly in tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import connect, new_id, transaction, utcnow

RELATION_TYPES = ("PART_OF", "DERIVED_FROM", "LED_TO", "EVOLVED_INTO", "REFERENCES")
PROVENANCE_TYPES = ("user", "ai_inferred", "ai_suggested", "user_confirmed")
CONTENT_TYPES = ("PROJECT", "THOUGHT", "EXPERIENCE")
CONTENT_SPACES = ("ME", "BUILD", "THINK", "JOURNEY", "COMPETE")
LIFECYCLES = ("DRAFT", "PUBLISHED", "COMPLETED", "ARCHIVED")

UNSET = object()


class ArchiveError(Exception):
    status = 400
    code = "archive_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(ArchiveError):
    status = 404
    code = "not_found"


class ConflictError(ArchiveError):
    status = 409
    code = "conflict"


class ValidationError(ArchiveError):
    status = 422
    code = "validation_error"


class UncertainOutcomeError(ArchiveError):
    status = 202
    code = "outcome_uncertain"


def _require_str(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是文本。", field=field)
    if len(value) > max_length:
        raise ValidationError(f"{field} 超出允许长度。", field=field)
    return value


def _require_choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValidationError(
            f"{field} 只能是 {'、'.join(choices)}。", field=field, allowed=list(choices)
        )
    return value


def _related_spaces(value: Any, primary_space: str) -> list[str]:
    """Normalise the optional related-space collection.

    The primary space is authoritative and is never duplicated into the related
    set; order is preserved and duplicates collapse.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValidationError("related_spaces 必须是空间名称列表。", field="related_spaces")
    result: list[str] = []
    for raw in value:
        space = _require_choice(raw, "related_spaces", CONTENT_SPACES)
        if space == primary_space or space in result:
            continue
        result.append(space)
    return result


def _row_to_content(row: sqlite3.Row) -> dict[str, Any]:
    visibility_selected = bool(row["visibility_selected"])
    is_public = (
        row["state"] == "active" and visibility_selected and row["visibility"] == "public"
    )
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "summary": row["summary"],
        "type": row["type"],
        "primary_space": row["primary_space"],
        "lifecycle": row["lifecycle"],
        "visibility": row["visibility"],
        "visibility_selected": visibility_selected,
        "public": is_public,
        "revision": row["revision"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
    }


def _excerpt(body: str, limit: int = 120) -> str:
    text = " ".join(body.split())
    return text[:limit] + ("…" if len(text) > limit else "")


class ArchiveStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "archive.db"

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    def create_content(
        self,
        title: str = "",
        body: str = "",
        visibility: str | None = None,
        *,
        summary: str = "",
        content_type: str = "THOUGHT",
        primary_space: str = "ME",
        lifecycle: str = "DRAFT",
        related_spaces: list[str] | None = None,
    ) -> dict[str, Any]:
        title = _require_str(title, "title", max_length=300)
        body = _require_str(body, "body", max_length=2_000_000)
        summary = _require_str(summary, "summary", max_length=20_000)
        content_type = _require_choice(content_type, "type", CONTENT_TYPES)
        primary_space = _require_choice(primary_space, "primary_space", CONTENT_SPACES)
        lifecycle = _require_choice(lifecycle, "lifecycle", LIFECYCLES)
        spaces = _related_spaces(related_spaces, primary_space)
        if visibility not in (None, "private", "public"):
            raise ValidationError("visibility 只能是 private、public 或未选择。", field="visibility")

        selected = 0 if visibility is None else 1
        stored_visibility = "private" if visibility is None else visibility
        content_id = new_id("c")
        now = utcnow()

        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO content(id, title, body, summary, type, primary_space, "
                "lifecycle, visibility, visibility_selected, "
                "revision, state, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                (
                    content_id,
                    title,
                    body,
                    summary,
                    content_type,
                    primary_space,
                    lifecycle,
                    stored_visibility,
                    selected,
                    now,
                    now,
                ),
            )
            self._write_related_spaces(conn, content_id, spaces, now)
        return self.get_content(content_id, include_deleted=True)

    def update_content(
        self,
        content_id: str,
        revision: int,
        *,
        title: str | object = UNSET,
        body: str | object = UNSET,
        visibility: str | None | object = UNSET,
        summary: str | object = UNSET,
        content_type: str | object = UNSET,
        primary_space: str | object = UNSET,
        lifecycle: str | object = UNSET,
        related_spaces: list[str] | object = UNSET,
    ) -> dict[str, Any]:
        if not isinstance(revision, int):
            raise ValidationError("revision 必须是整数。", field="revision")
        if title is not UNSET:
            title = _require_str(title, "title", max_length=300)
        if body is not UNSET:
            body = _require_str(body, "body", max_length=2_000_000)
        if summary is not UNSET:
            summary = _require_str(summary, "summary", max_length=20_000)
        if content_type is not UNSET:
            content_type = _require_choice(content_type, "type", CONTENT_TYPES)
        if primary_space is not UNSET:
            primary_space = _require_choice(primary_space, "primary_space", CONTENT_SPACES)
        if lifecycle is not UNSET:
            lifecycle = _require_choice(lifecycle, "lifecycle", LIFECYCLES)
        if visibility is not UNSET and visibility not in (None, "private", "public"):
            raise ValidationError("visibility 只能是 private、public 或未选择。", field="visibility")

        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM content WHERE id = ?", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            if row["state"] == "deleted":
                raise ConflictError("内容已删除，请先恢复后再编辑。", content_id=content_id)
            if row["revision"] != revision:
                raise ConflictError(
                    "内容已被更新，当前修改未写入。",
                    current_revision=row["revision"],
                    current_updated_at=row["updated_at"],
                )

            resulting_space = (
                row["primary_space"] if primary_space is UNSET else primary_space
            )
            spaces = (
                UNSET
                if related_spaces is UNSET
                else _related_spaces(related_spaces, resulting_space)
            )

            sets: list[str] = []
            params: list[Any] = []
            if title is not UNSET:
                sets.append("title = ?")
                params.append(title)
            if body is not UNSET:
                sets.append("body = ?")
                params.append(body)
            if summary is not UNSET:
                sets.append("summary = ?")
                params.append(summary)
            if content_type is not UNSET:
                sets.append("type = ?")
                params.append(content_type)
            if primary_space is not UNSET:
                sets.append("primary_space = ?")
                params.append(primary_space)
            if lifecycle is not UNSET:
                sets.append("lifecycle = ?")
                params.append(lifecycle)
            if visibility is not UNSET:
                sets.append("visibility = ?")
                params.append("private" if visibility is None else visibility)
                sets.append("visibility_selected = ?")
                params.append(0 if visibility is None else 1)

            now = utcnow()
            sets.extend(["revision = revision + 1", "updated_at = ?"])
            params.append(now)
            params.append(content_id)
            conn.execute(f"UPDATE content SET {', '.join(sets)} WHERE id = ?", params)

            if spaces is not UNSET:
                self._write_related_spaces(conn, content_id, spaces, now)
            if primary_space is not UNSET:
                # A space can no longer be "related" once it becomes primary;
                # the content itself is never duplicated or moved.
                conn.execute(
                    "DELETE FROM content_spaces WHERE content_id = ? AND space = ?",
                    (content_id, primary_space),
                )
        return self.get_content(content_id, include_deleted=True)

    def delete_content(self, content_id: str, revision: int) -> dict[str, Any]:
        if not isinstance(revision, int):
            raise ValidationError("revision 必须是整数。", field="revision")
        impacts = self.get_deletion_impacts(content_id)

        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM content WHERE id = ?", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            if row["state"] == "deleted":
                raise ConflictError("内容已经处于删除状态。", content_id=content_id)
            if row["revision"] != revision:
                raise ConflictError(
                    "内容已被更新，删除操作未执行。",
                    current_revision=row["revision"],
                )
            now = utcnow()
            conn.execute(
                "UPDATE content SET state = 'deleted', deleted_at = ?, updated_at = ?, "
                "revision = revision + 1 WHERE id = ?",
                (now, now, content_id),
            )
        return {
            "content": self.get_content(content_id, include_deleted=True),
            "impacts": impacts,
        }

    def restore_content(self, content_id: str) -> dict[str, Any]:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM content WHERE id = ?", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            if row["state"] != "deleted":
                raise ConflictError("内容不在删除状态，无需恢复。", content_id=content_id)
            conn.execute(
                "UPDATE content SET state = 'active', deleted_at = NULL, updated_at = ?, "
                "revision = revision + 1 WHERE id = ?",
                (utcnow(), content_id),
            )

        content = self.get_content(content_id, include_deleted=True)
        attachments = content["attachments"]
        broken = [a["id"] for a in attachments if a["state"] != "READY"]
        report = {
            "content_id": content_id,
            "restored_identity": True,
            "relations": len(content["relations"]),
            "attributes": len(content["attributes"]),
            "attachments": len(attachments),
            "attachments_not_ready": broken,
            "complete": not broken,
        }
        return {"content": content, "report": report}

    def get_deletion_impacts(self, content_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT id FROM content WHERE id = ?", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            relations = conn.execute(
                "SELECT id, from_id, to_id, type FROM relations "
                "WHERE from_id = ? OR to_id = ? ORDER BY created_at",
                (content_id, content_id),
            ).fetchall()
            attributes = conn.execute(
                "SELECT COUNT(*) AS n FROM attributes WHERE content_id = ?", (content_id,)
            ).fetchone()["n"]
            attachments = conn.execute(
                "SELECT id, filename, state FROM attachments WHERE content_id = ? "
                "ORDER BY created_at",
                (content_id,),
            ).fetchall()
            return {
                "direct_relations": [dict(r) for r in relations],
                "direct_relation_count": len(relations),
                "attributes": attributes,
                "attachments": [dict(a) for a in attachments],
                "cascading_deletion": False,
                "note": "删除不会级联删除关系、属性或附件；关系端点会保留并可恢复。",
            }
        finally:
            conn.close()

    def get_content(self, content_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM content WHERE id = ?", (content_id,)).fetchone()
            if row is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            if row["state"] == "deleted" and not include_deleted:
                raise NotFoundError("内容不存在。", content_id=content_id)

            data = _row_to_content(row)
            data["related_spaces"] = [
                item["space"]
                for item in conn.execute(
                    "SELECT space FROM content_spaces WHERE content_id = ? ORDER BY space",
                    (content_id,),
                ).fetchall()
            ]
            data["relations"] = self._relations_for(conn, content_id)
            data["attributes"] = [
                self._attribute_dict(a)
                for a in conn.execute(
                    "SELECT * FROM attributes WHERE content_id = ? ORDER BY created_at",
                    (content_id,),
                ).fetchall()
            ]
            data["attachments"] = [
                self._attachment_dict(a)
                for a in conn.execute(
                    "SELECT * FROM attachments WHERE content_id = ? ORDER BY created_at",
                    (content_id,),
                ).fetchall()
            ]
            return data
        finally:
            conn.close()

    def list_content(
        self,
        *,
        query: str | None = None,
        include_deleted: bool = False,
        space: str | None = None,
        content_type: str | None = None,
        lifecycle: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_deleted:
            clauses.append("state = 'active'")
        if query:
            raw = query.strip()
            if raw:
                escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append(
                    "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
                    "OR summary LIKE ? ESCAPE '\\')"
                )
                params.extend([f"%{escaped}%"] * 3)
        if space is not None:
            space = _require_choice(space, "space", CONTENT_SPACES)
            clauses.append(
                "(primary_space = ? OR EXISTS (SELECT 1 FROM content_spaces s "
                "WHERE s.content_id = content.id AND s.space = ?))"
            )
            params.extend([space, space])
        if content_type is not None:
            content_type = _require_choice(content_type, "type", CONTENT_TYPES)
            clauses.append("type = ?")
            params.append(content_type)
        if lifecycle is not None:
            lifecycle = _require_choice(lifecycle, "lifecycle", LIFECYCLES)
            clauses.append("lifecycle = ?")
            params.append(lifecycle)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT * FROM content {where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
            space_map = self._related_spaces_map(conn, [row["id"] for row in rows])
            items: list[dict[str, Any]] = []
            for row in rows:
                item = _row_to_content(row)
                item["related_spaces"] = space_map.get(row["id"], [])
                item["excerpt"] = row["summary"] or _excerpt(row["body"])
                item["relation_count"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM relations WHERE from_id = ? OR to_id = ?",
                    (row["id"], row["id"]),
                ).fetchone()["n"]
                item["attachment_count"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM attachments WHERE content_id = ?",
                    (row["id"],),
                ).fetchone()["n"]
                items.append(item)
            return items
        finally:
            conn.close()

    def overview(self, *, recent_limit: int = 6) -> dict[str, Any]:
        """Read-only summary of the archive for the HOME and space panels.

        Space counts follow the same rule as ``list_content(space=...)``:
        a content counts once for its primary space and once for each related
        space. ARCHIVE is a lifecycle entry, not an eighth space.
        """
        limit = max(1, int(recent_limit))
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM content WHERE state = 'active'"
            ).fetchone()["n"]
            space_counts = {space: 0 for space in CONTENT_SPACES}
            rows = conn.execute(
                "SELECT space, COUNT(*) AS n FROM ("
                "  SELECT id, primary_space AS space FROM content WHERE state = 'active'"
                "  UNION"
                "  SELECT c.id AS id, s.space AS space FROM content c"
                "  JOIN content_spaces s ON s.content_id = c.id"
                "  WHERE c.state = 'active' AND s.space != c.primary_space"
                ") GROUP BY space"
            ).fetchall()
            for row in rows:
                if row["space"] in space_counts:
                    space_counts[row["space"]] = row["n"]
            archived = conn.execute(
                "SELECT COUNT(*) AS n FROM content "
                "WHERE state = 'active' AND lifecycle = 'ARCHIVED'"
            ).fetchone()["n"]
            recent_rows = conn.execute(
                "SELECT * FROM content WHERE state = 'active' "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            recent = []
            for row in recent_rows:
                recent.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "summary": row["summary"],
                        "primary_space": row["primary_space"],
                        "type": row["type"],
                        "lifecycle": row["lifecycle"],
                        "updated_at": row["updated_at"],
                    }
                )
            return {
                "total": total,
                "spaces": space_counts,
                "archived": archived,
                "recent": recent,
            }
        finally:
            conn.close()

    @staticmethod
    def _related_spaces_map(
        conn: sqlite3.Connection, content_ids: list[str]
    ) -> dict[str, list[str]]:
        if not content_ids:
            return {}
        placeholders = ", ".join("?" for _ in content_ids)
        rows = conn.execute(
            f"SELECT content_id, space FROM content_spaces "
            f"WHERE content_id IN ({placeholders}) ORDER BY space",
            content_ids,
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["content_id"], []).append(row["space"])
        return result

    @staticmethod
    def _write_related_spaces(
        conn: sqlite3.Connection, content_id: str, spaces: list[str], now: str
    ) -> None:
        conn.execute("DELETE FROM content_spaces WHERE content_id = ?", (content_id,))
        for space in spaces:
            conn.execute(
                "INSERT INTO content_spaces(content_id, space, created_at) VALUES(?, ?, ?)",
                (content_id, space, now),
            )

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------
    def add_relation(self, from_id: str, to_id: str, relation_type: str) -> dict[str, Any]:
        if relation_type not in RELATION_TYPES:
            raise ValidationError("不支持的关系类型。", field="type")
        if from_id == to_id:
            raise ValidationError("不能把内容关联到自身。", field="to_id")

        relation_id = new_id("r")
        now = utcnow()
        with transaction(self.db_path) as conn:
            for endpoint in (from_id, to_id):
                row = conn.execute(
                    "SELECT id, state FROM content WHERE id = ?", (endpoint,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("关系端点不存在。", content_id=endpoint)
                if row["state"] == "deleted":
                    raise ConflictError("关系端点已删除，请先恢复。", content_id=endpoint)
            try:
                conn.execute(
                    "INSERT INTO relations(id, from_id, to_id, type, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (relation_id, from_id, to_id, relation_type, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("相同的关系已经存在。") from exc
        return self.get_relation(relation_id)

    def get_relation(self, relation_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT r.*, cf.id AS from_exists, ct.id AS to_exists, "
                "cf.title AS from_title, ct.title AS to_title, "
                "cf.state AS from_state, ct.state AS to_state "
                "FROM relations r "
                "LEFT JOIN content cf ON cf.id = r.from_id "
                "LEFT JOIN content ct ON ct.id = r.to_id "
                "WHERE r.id = ?",
                (relation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("关系不存在。", relation_id=relation_id)
            return {
                "id": row["id"],
                "from_id": row["from_id"],
                "to_id": row["to_id"],
                "type": row["type"],
                "created_at": row["created_at"],
                "from_exists": row["from_exists"] is not None,
                "to_exists": row["to_exists"] is not None,
                "from_title": row["from_title"],
                "to_title": row["to_title"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "from_resolvable": row["from_exists"] is not None and row["from_state"] == "active",
                "to_resolvable": row["to_exists"] is not None and row["to_state"] == "active",
            }
        finally:
            conn.close()

    def delete_relation(self, relation_id: str) -> dict[str, Any]:
        with transaction(self.db_path) as tx:
            row = tx.execute(
                "SELECT * FROM relations WHERE id = ?", (relation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("关系不存在。", relation_id=relation_id)
            tx.execute("DELETE FROM relations WHERE id = ?", (relation_id,))
        return {"deleted_relation_id": relation_id}

    def _relations_for(self, conn: sqlite3.Connection, content_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT r.*, cf.id AS from_exists, ct.id AS to_exists, "
            "cf.title AS from_title, ct.title AS to_title, "
            "cf.state AS from_state, ct.state AS to_state "
            "FROM relations r "
            "LEFT JOIN content cf ON cf.id = r.from_id "
            "LEFT JOIN content ct ON ct.id = r.to_id "
            "WHERE r.from_id = ? OR r.to_id = ? "
            "ORDER BY r.created_at",
            (content_id, content_id),
        ).fetchall()
        result = []
        for row in rows:
            outgoing = row["from_id"] == content_id
            other_id = row["to_id"] if outgoing else row["from_id"]
            other_title = row["to_title"] if outgoing else row["from_title"]
            other_state = row["to_state"] if outgoing else row["from_state"]
            other_exists = (row["to_exists"] if outgoing else row["from_exists"]) is not None
            result.append(
                {
                    "id": row["id"],
                    "from_id": row["from_id"],
                    "to_id": row["to_id"],
                    "type": row["type"],
                    "direction": "outgoing" if outgoing else "incoming",
                    "other_id": other_id,
                    "other_title": other_title,
                    "other_state": other_state,
                    "other_exists": other_exists,
                    "other_resolvable": other_exists and other_state == "active",
                    "created_at": row["created_at"],
                }
            )
        return result

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------
    def add_attribute(
        self,
        content_id: str,
        key: str,
        value: str,
        provenance: str = "user",
        *,
        confirmed: bool = False,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        key = _require_str(key, "key", max_length=120).strip()
        if not key:
            raise ValidationError("属性名称不能为空。", field="key")
        value = _require_str(value, "value", max_length=100_000)
        if provenance not in PROVENANCE_TYPES:
            raise ValidationError("未知的来源类型。", field="provenance")
        if source_ref is not None:
            source_ref = _require_str(source_ref, "source_ref", max_length=500)

        if provenance == "user_confirmed":
            confirmed_value = 1
        elif provenance == "user":
            confirmed_value = 1 if confirmed else 0
        else:
            # AI suggestions and inferences never become confirmed automatically.
            confirmed_value = 0

        attribute_id = new_id("a")
        now = utcnow()
        with transaction(self.db_path) as conn:
            self._ensure_active_content(conn, content_id)
            conn.execute(
                "INSERT INTO attributes(id, content_id, key, value, provenance, confirmed, "
                "source_ref, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attribute_id,
                    content_id,
                    key,
                    value,
                    provenance,
                    confirmed_value,
                    source_ref,
                    now,
                    now,
                ),
            )
        return self.get_attribute(attribute_id)

    def update_attribute(
        self,
        attribute_id: str,
        *,
        value: str | object = UNSET,
        provenance: str | object = UNSET,
        confirmed: bool | object = UNSET,
        source_ref: str | None | object = UNSET,
    ) -> dict[str, Any]:
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM attributes WHERE id = ?", (attribute_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("属性不存在。", attribute_id=attribute_id)

            new_value = row["value"]
            new_provenance = row["provenance"]
            new_confirmed = bool(row["confirmed"])
            new_source = row["source_ref"]

            if value is not UNSET:
                new_value = _require_str(value, "value", max_length=100_000)
            if provenance is not UNSET:
                if provenance not in PROVENANCE_TYPES:
                    raise ValidationError("未知的来源类型。", field="provenance")
                new_provenance = provenance
            if source_ref is not UNSET:
                new_source = (
                    None
                    if source_ref is None
                    else _require_str(source_ref, "source_ref", max_length=500)
                )

            if confirmed is not UNSET:
                if confirmed and new_provenance in ("ai_inferred", "ai_suggested"):
                    # An explicit user confirmation changes provenance rather than
                    # letting an AI-origin attribute masquerade as confirmed AI output.
                    new_provenance = "user_confirmed"
                    new_confirmed = True
                else:
                    new_confirmed = bool(confirmed)

            if new_provenance == "user_confirmed":
                new_confirmed = True
            elif new_provenance in ("ai_inferred", "ai_suggested"):
                new_confirmed = False

            conn.execute(
                "UPDATE attributes SET value = ?, provenance = ?, confirmed = ?, "
                "source_ref = ?, updated_at = ? WHERE id = ?",
                (
                    new_value,
                    new_provenance,
                    1 if new_confirmed else 0,
                    new_source,
                    utcnow(),
                    attribute_id,
                ),
            )
            content_id = row["content_id"]
        return self._attribute_for(content_id, attribute_id)

    def get_attribute(self, attribute_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM attributes WHERE id = ?", (attribute_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("属性不存在。", attribute_id=attribute_id)
            return self._attribute_dict(row)
        finally:
            conn.close()

    def _attribute_for(self, content_id: str, attribute_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM attributes WHERE id = ? AND content_id = ?",
                (attribute_id, content_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("属性不存在。", attribute_id=attribute_id)
            return self._attribute_dict(row)
        finally:
            conn.close()

    @staticmethod
    def _attribute_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "content_id": row["content_id"],
            "key": row["key"],
            "value": row["value"],
            "provenance": row["provenance"],
            "confirmed": bool(row["confirmed"]),
            "source_ref": row["source_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _attachment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "content_id": row["content_id"],
            "filename": row["filename"],
            "byte_size": row["byte_size"],
            "sha256": row["sha256"],
            "mime": row["mime"],
            "visibility": row["visibility"],
            "state": row["state"],
            "operation_id": row["operation_id"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ensure_active_content(conn: sqlite3.Connection, content_id: str) -> None:
        row = conn.execute(
            "SELECT id, state FROM content WHERE id = ?", (content_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("内容不存在。", content_id=content_id)
        if row["state"] == "deleted":
            raise ConflictError("内容已删除，请先恢复。", content_id=content_id)
