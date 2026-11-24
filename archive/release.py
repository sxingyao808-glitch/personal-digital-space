"""Immutable release-bundle construction for the controlled publishing lifecycle.

P1 scope: this module only *prepares* a release bundle from an explicit
selection of public content and public READY attachments. It never activates a
release, never changes public routes, and never writes to the private source
database or attachment objects.

Layout under ``<data_dir>/releases``::

    index.json          registry pointer + prepared release records
    events.jsonl        append-only audit events (actor=owner)
    .lock               short-lived build lock
    rel_<uuid>/         immutable release bundle
        release.json    manifest (format, ids, files, SHA-256, boundaries)
        content/<id>.html
        attachments/<attachment_id>/<filename>
        assets/style.css

The registry is deliberately separate from the F-03 archive backup: it is not
part of ``ArchiveBackup``. ``ReleaseRegistry.backup()`` provides its own
integrity-checked ZIP outside the F-03 scope.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .db import SCHEMA_VERSION, connect, utcnow
from .fsutil import fsync_directory
from .store import ArchiveError

RELEASE_FORMAT = "personal-archive-release"
RELEASE_FORMAT_VERSION = 3
# Bundle-relative paths that may be served publicly. ``index.html`` is the
# site home; ``spaces/`` holds one listing page per published space.
PUBLIC_BUNDLE_PREFIXES = ("content/", "attachments/", "assets/", "spaces/")
REGISTRY_FORMAT = "personal-archive-release-registry"
REGISTRY_FORMAT_VERSION = 3
HISTORY_FORMAT = "personal-archive-release-history"
HISTORY_FORMAT_VERSION = 3
REGISTRY_BACKUP_FORMAT = "personal-archive-release-registry-backup"
REGISTRY_BACKUP_FORMAT_VERSION = 4
LOCK_STALE_SECONDS = 300
RELEASE_STATUSES = ("prepared", "active", "superseded", "withdrawn")
HISTORY_ACTIONS = (
    "build",
    "activate",
    "supersede",
    "withdraw",
    "restore_request",
    "reauthorize",
)
RESTORE_SCOPES = ("full", "content_subset")
PUBLISH_OPERATIONS = (
    "prepared",
    "publish",
    "withdraw_all",
    "withdraw_subset",
    "restore_full",
    "restore_subset",
)
PUBLIC_PAYLOAD_VERSION = 2
PUBLIC_PAYLOAD_FIELDS = {
    "payload_version",
    "id",
    "title",
    "summary",
    "body",
    "type",
    "primary_space",
    "updated_at",
    "attachments",
}
PUBLIC_ATTACHMENT_FIELDS = {"id", "filename", "byte_size", "mime", "url"}
WITHDRAWAL_MARKER = "WITHDRAWAL_REQUIRED"
PUBLICATION_DRIFT_CATEGORIES = (
    "PUBLISHED_AND_CURRENT",
    "PUBLISHED_SNAPSHOT_STALE",
    "PUBLISHED_BUT_NOW_PRIVATE",
    "PUBLISHED_BUT_DELETED",
    "PUBLISHED_BUT_DESELECTED",
    "PUBLISHED_ATTACHMENT_DRIFT",
    "SELECTED_BUT_NOT_PUBLISHED",
    "NOT_IN_ACTIVE_RELEASE",
)
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

# Public page stylesheet. This is the public-facing excerpt of
# ``archive/web/static/styles.css`` (brand mark, public shell, typography and
# attachment list) so that the bundle-rendered page keeps the existing public
# visual design (AC-19) without depending on owner UI assets.
STYLE_CSS = """\
:root {
  --ink: #100e0c;
  --line: rgba(240, 231, 214, 0.09);
  --text: #e9e2d5;
  --muted: #8b8072;
  --brass: #c9a96e;
  --serif: "Noto Serif SC", "Songti SC", "STSong", Georgia, "Times New Roman", serif;
  --sans: "Inter", "PingFang SC", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  min-height: 100vh;
  background:
    radial-gradient(1100px 620px at 78% -12%, rgba(201, 169, 110, 0.07), transparent 62%),
    radial-gradient(760px 520px at 6% 100%, rgba(120, 96, 66, 0.06), transparent 68%),
    var(--ink);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--brass); text-decoration: none; }
a:hover { text-decoration: underline; }
.brand-mark {
  width: 14px;
  height: 14px;
  border: 1px solid var(--brass);
  border-radius: 50%;
  position: relative;
  top: 1px;
  flex: none;
}
.brand-mark::after {
  content: "";
  position: absolute;
  inset: 4px;
  background: var(--brass);
  border-radius: 50%;
  opacity: 0.7;
}
.public-shell {
  max-width: 700px;
  margin: 0 auto;
  padding: 72px 24px 100px;
}
.public-brand {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 52px;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
}
.public-brand .brand-mark { width: 11px; height: 11px; }
.public-brand .brand-mark::after { inset: 3px; }
.public-title {
  font-family: var(--serif);
  font-weight: 400;
  font-size: clamp(28px, 4vw, 38px);
  line-height: 1.38;
  margin: 0 0 10px;
}
.public-meta {
  font-size: 11.5px;
  color: var(--muted);
  margin-bottom: 34px;
}
.public-summary {
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.8;
  color: #b9b0a2;
  margin: 0 0 18px;
}
.public-tags {
  font-size: 10.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--brass);
  margin: 0 0 14px;
}
.public-body {
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.95;
  color: #ded6c8;
  white-space: pre-wrap;
  word-break: break-word;
  border-top: 1px solid var(--line);
  padding-top: 30px;
}
.public-attachments {
  margin-top: 44px;
  padding-top: 20px;
  border-top: 1px solid var(--line);
}
.public-attachments h2 {
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 500;
  margin: 0 0 12px;
}
.attachment-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
  color: var(--text);
}
.attachment-link:hover { text-decoration: none; color: var(--brass); }
.public-foot {
  margin-top: 60px;
  font-size: 10.5px;
  color: var(--muted);
  letter-spacing: 0.05em;
}
.public-brand a { color: inherit; text-decoration: none; }
.public-brand a:hover { color: var(--brass); text-decoration: none; }
.public-intro {
  margin: 0 0 36px;
  max-width: 46ch;
  font-size: 13px;
  line-height: 1.8;
  color: var(--muted);
}
.public-list { border-top: 1px solid var(--line); }
.public-list-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
  color: var(--text);
}
.public-list-item:hover { color: var(--brass); text-decoration: none; }
.public-list-title { font-family: var(--serif); font-size: 17px; line-height: 1.5; }
.public-list-summary {
  display: block;
  font-family: var(--sans);
  font-size: 11.5px;
  color: var(--muted);
  margin-top: 3px;
}
.public-list-date { flex: none; font-size: 11px; color: var(--muted); white-space: nowrap; }
.public-empty {
  padding: 22px 0;
  border-top: 1px solid var(--line);
  font-size: 13px;
  color: var(--muted);
}
.public-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin: 0 0 30px;
}
.public-space-link {
  font-size: 10.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 12px;
}
.public-space-link:hover,
.public-space-link[aria-current="page"] {
  color: var(--brass);
  border-color: rgba(201, 169, 110, 0.45);
  text-decoration: none;
}
.public-tags a { color: var(--brass); }
.public-related {
  margin-top: 52px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
}
.public-related h2 {
  font-size: 10.5px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 500;
  margin: 0 0 4px;
}
.public-preview-banner {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 50;
  padding: 8px 16px;
  text-align: center;
  font-size: 11px;
  letter-spacing: 0.12em;
  color: #100e0c;
  background: var(--brass);
}
body.has-preview-banner { padding-bottom: 38px; }
@media (max-width: 620px) {
  .public-shell { padding: 48px 20px 72px; }
  .public-list-item { flex-direction: column; gap: 4px; }
}
"""


class ReleaseError(ArchiveError):
    status = 500
    code = "release_failed"


class ReleaseValidationError(ArchiveError):
    status = 422
    code = "release_validation_error"


class ReleaseNotFoundError(ArchiveError):
    status = 404
    code = "release_not_found"


class ReleaseLockError(ArchiveError):
    status = 409
    code = "release_locked"


class ReleaseConfirmationError(ArchiveError):
    status = 409
    code = "restore_requires_confirmation"


class ReleaseConflictError(ArchiveError):
    status = 409
    code = "release_conflict"


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_plain_regular(info: os.stat_result) -> bool:
    """True only for a non-reparse regular file (never a link or junction)."""
    if not stat.S_ISREG(info.st_mode):
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return not (attributes & reparse_flag)


def _lstat_regular(path: Path) -> os.stat_result | None:
    """Return ``lstat()`` info only for a plain regular file.

    ``lstat()`` never follows the final path component, so a symlink, junction,
    mount point or any other reparse point is reported instead of resolved.
    Only ``S_ISREG`` is accepted; everything else (including a path that cannot
    be inspected) returns ``None`` so callers fail closed.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    return info if _is_plain_regular(info) else None


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _fsync_directory(path: Path) -> bool:
    """Flush one directory entry through the shared platform helper.

    Windows cannot ``os.open`` a directory, so the old best-effort version was a
    silent no-op there. The shared helper uses CreateFileW +
    FlushFileBuffers and reports whether the flush was actually confirmed.
    """
    return fsync_directory(path)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows can raise a transient sharing violation from antivirus/indexers
        # holding the destination briefly; retry with a small bounded backoff.
        delay = 0.02
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(delay)
                delay *= 2
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)


def _validate_id(value: Any, field: str, issues: list[dict[str, Any]]) -> str | None:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        issues.append(
            {
                "code": "invalid_identifier",
                "severity": "error",
                "target": field,
                "detail": "标识符必须为 1-120 位字母、数字、下划线或连字符。",
            }
        )
        return None
    return value


def _safe_filename(value: str | None) -> str:
    base = Path(str(value or "")).name.strip().replace("\\", "_").replace("/", "_")
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._- ") else "_" for ch in base
    ).strip()
    cleaned = cleaned.replace(" ", "_")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "attachment"
    stem = Path(cleaned).stem or "attachment"
    if stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > 120:
        suffix = Path(cleaned).suffix[:20]
        cleaned = cleaned[: 118 - len(suffix)] + suffix
    return cleaned


def public_content_id(content_id: str) -> str:
    """Stable, non-reversible public identifier for a Content."""
    digest = hashlib.sha256(
        f"archive-public-content:{content_id}".encode("utf-8")
    ).hexdigest()
    return f"p_{digest[:20]}"


def public_attachment_id(attachment_id: str) -> str:
    """Stable, non-reversible public identifier for an attachment."""
    digest = hashlib.sha256(
        f"archive-public-attachment:{attachment_id}".encode("utf-8")
    ).hexdigest()
    return f"a_{digest[:20]}"


def is_public_bundle_path(relative: str) -> bool:
    """True for the fixed set of bundle paths the public site may serve."""
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        return False
    if "\\" in relative or ".." in Path(relative).parts:
        return False
    if relative == "index.html":
        return True
    return relative.startswith(PUBLIC_BUNDLE_PREFIXES)


def _space_slug(value: Any) -> str:
    """Directory-safe space name used for ``spaces/<SLUG>/`` pages."""
    raw = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_-]{0,23}", raw):
        return raw
    return "OTHER"


_RELATION_LABELS = {
    ("PART_OF", "from"): "属于",
    ("PART_OF", "to"): "包含",
    ("DERIVED_FROM", "from"): "源自",
    ("DERIVED_FROM", "to"): "衍生出",
    ("LED_TO", "from"): "走向",
    ("LED_TO", "to"): "来自",
    ("EVOLVED_INTO", "from"): "演化为",
    ("EVOLVED_INTO", "to"): "源自",
    ("REFERENCES", "from"): "引用",
    ("REFERENCES", "to"): "被引用",
}


def _attachment_relative(attachment: dict[str, Any]) -> str:
    if attachment.get("public_path"):
        return attachment["public_path"]
    public_id = attachment.get("public_id") or public_attachment_id(
        attachment["attachment_id"]
    )
    filename = attachment.get("source_filename") or attachment.get("filename") or ""
    return f"attachments/{public_id}/{filename}"


def build_content_payload(release_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Public JSON payload for one published content item.

    Every field is copied explicitly; database rows are never spread into the
    payload. Internal identifiers, revisions, relations, attributes, private
    attachments and owner metadata are never included.
    """
    return {
        "payload_version": PUBLIC_PAYLOAD_VERSION,
        "id": item["public_id"],
        "title": item["title"],
        "summary": item.get("summary") or "",
        "body": item["body"],
        "type": item["content_type"],
        "primary_space": item["primary_space"],
        "updated_at": item["updated_at"],
        "attachments": [
            {
                "id": attachment["public_id"],
                "filename": attachment["source_filename"],
                "byte_size": attachment["bytes"],
                "mime": attachment["mime"] or "application/octet-stream",
                "url": _attachment_relative(attachment),
            }
            for attachment in item["attachments"]
        ],
    }



def _render_list_block(items: list[dict[str, Any]], prefix: str) -> str:
    """Rendered list rows linking to bundle-relative content pages."""
    rows = []
    for item in items:
        title = html.escape(item["title"] or "未命名")
        summary = html.escape(item.get("summary") or "")
        updated = html.escape((item.get("updated_at") or "")[:10])
        href = html.escape(f"{prefix}{item['page_path']}")
        summary_html = (
            f'\n          <span class="public-list-summary">{summary}</span>'
            if summary
            else ""
        )
        rows.append(
            f'      <a class="public-list-item" href="{href}">\n'
            f'        <span class="public-list-title">{title}{summary_html}</span>\n'
            f'        <span class="public-list-date">{updated}</span>\n'
            "      </a>"
        )
    return "\n".join(rows)


def _render_space_nav(spaces: list[str], current: str | None, prefix: str) -> str:
    links = []
    for space in spaces:
        current_attr = ' aria-current="page"' if space == current else ""
        links.append(
            f'<a class="public-space-link" href="{prefix}spaces/{space}/"{current_attr}>'
            f"{html.escape(space)}</a>"
        )
    return (
        '    <nav class="public-nav" aria-label="公开空间">\n'
        f'      {"".join(links)}\n'
        "    </nav>\n"
    )


def _render_home_page(items: list[dict[str, Any]], spaces: list[str]) -> bytes:
    """Root ``index.html``: the anonymously browsable public site home."""
    ordered = sorted(
        items,
        key=lambda entry: ((entry.get("updated_at") or ""), entry.get("public_id") or ""),
        reverse=True,
    )
    listing = _render_list_block(ordered, "")
    nav = _render_space_nav(spaces, None, "")
    document = (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="referrer" content="no-referrer">\n'
        "  <title>公开内容 · Archive</title>\n"
        '  <link rel="stylesheet" href="assets/style.css">\n'
        "</head>\n"
        "<body>\n"
        '  <main class="public-shell">\n'
        '    <div class="public-brand">\n'
        '      <span class="brand-mark" aria-hidden="true"></span>\n'
        '      <a href="./">公开内容 · Archive</a>\n'
        "    </div>\n"
        '    <h1 class="public-title">公开内容</h1>\n'
        '    <p class="public-intro">这里只展示所有者明确选择公开的内容。'
        "管理、编辑、私密条目与发布操作都不会出现在这个站点。</p>\n"
        f"{nav}"
        f'    <div class="public-list">\n{listing}\n    </div>\n'
        f'    <p class="public-foot">共 {len(ordered)} 条公开内容。</p>\n'
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


def _render_space_page(
    space: str, items: list[dict[str, Any]], spaces: list[str]
) -> bytes:
    """``spaces/<SLUG>/index.html``: one space's published listing."""
    ordered = sorted(
        items,
        key=lambda entry: ((entry.get("updated_at") or ""), entry.get("public_id") or ""),
        reverse=True,
    )
    listing = _render_list_block(ordered, "../../")
    nav = _render_space_nav(spaces, space, "../../")
    document = (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="referrer" content="no-referrer">\n'
        f"  <title>{html.escape(space)} · 公开内容 · Archive</title>\n"
        '  <link rel="stylesheet" href="../../assets/style.css">\n'
        "</head>\n"
        "<body>\n"
        '  <main class="public-shell">\n'
        '    <div class="public-brand">\n'
        '      <span class="brand-mark" aria-hidden="true"></span>\n'
        '      <a href="../../">公开内容 · Archive</a>\n'
        "    </div>\n"
        f'    <h1 class="public-title">{html.escape(space)}</h1>\n'
        f'    <p class="public-meta">{len(ordered)} 条公开内容</p>\n'
        f"{nav}"
        f'    <div class="public-list">\n{listing}\n    </div>\n'
        '    <p class="public-foot"><a href="../../">查看全部公开内容</a></p>\n'
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


def _render_content_page(item: dict[str, Any], release_id: str) -> bytes:
    """Render the public HTML page inside the bundle.

    Only whitelisted public fields are rendered. Internal identifiers are not
    written into the page; styles and attachments resolve from bundle-relative
    public paths.
    """
    title = html.escape(item["title"] or "未命名")
    body = html.escape(item["body"] or "")
    summary = html.escape(item.get("summary") or "")
    updated = html.escape((item["updated_at"] or "")[:10])
    content_type = html.escape(item.get("content_type") or "")
    space_label = html.escape(item.get("primary_space") or "")
    space_slug = _space_slug(item.get("primary_space"))
    attachments = item["attachments"]
    if attachments:
        rows = "\n".join(
            '        <a class="attachment-link" href="../{}" rel="noopener">\n'
            "          <span>{}</span>\n"
            "          <span>{} B</span>\n"
            "        </a>".format(
                html.escape(_attachment_relative(att)),
                html.escape(att["source_filename"]),
                att["bytes"],
            )
            for att in attachments
        )
        attachment_block = (
            '      <section class="public-attachments">\n'
            "        <h2>附件</h2>\n"
            f"{rows}\n"
            "      </section>\n"
        )
        meta_extra = f" · {len(attachments)} 个公开附件"
    else:
        attachment_block = ""
        meta_extra = ""
    summary_block = (
        f'      <p class="public-summary">{summary}</p>\n' if summary else ""
    )
    tag_parts = []
    if content_type:
        tag_parts.append(f"<span>{content_type}</span>")
    if space_label:
        if space_slug != "OTHER":
            tag_parts.append(
                f'<a href="../spaces/{space_slug}/">{space_label}</a>'
            )
        else:
            tag_parts.append(f"<span>{space_label}</span>")
    tags = " · ".join(tag_parts)
    tags_block = f'      <p class="public-tags">{tags}</p>\n' if tags else ""
    related = item.get("related") or []
    if related:
        related_rows = "\n".join(
            '        <a class="public-list-item" href="{}.html">\n'
            '          <span class="public-list-title">{}</span>\n'
            '          <span class="public-list-date">{}</span>\n'
            "        </a>".format(
                html.escape(entry["public_id"]),
                html.escape(entry["title"] or "未命名"),
                html.escape(entry["label"]),
            )
            for entry in related
        )
        related_block = (
            '    <section class="public-related">\n'
            "      <h2>相关内容</h2>\n"
            f"{related_rows}\n"
            "    </section>\n"
        )
    else:
        related_block = ""
    document = (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="referrer" content="no-referrer">\n'
        f'  <meta name="release-id" content="{html.escape(release_id)}">\n'
        f"  <title>{title} · Archive</title>\n"
        '  <link rel="stylesheet" href="../assets/style.css">\n'
        "</head>\n"
        "<body>\n"
        '  <main class="public-shell">\n'
        '    <div class="public-brand">\n'
        '      <span class="brand-mark" aria-hidden="true"></span>\n'
        '      <a href="../">公开内容 · Archive</a>\n'
        "    </div>\n"
        "    <article>\n"
        f'      <h1 class="public-title">{title}</h1>\n'
        f"{tags_block}"
        f"{summary_block}"
        '      <p class="public-meta">\n'
        f"        更新于 {updated}{meta_extra}\n"
        "      </p>\n"
        f'      <div class="public-body">{body}</div>\n'
        f"{attachment_block}"
        "    </article>\n"
        f"{related_block}"
        '    <p class="public-foot">这条内容由所有者明确选择公开。属性、私有附件与私有关系不会出现在公开页面。</p>\n'
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8")


def _pid_alive(pid: int) -> bool:
    """Best-effort PID liveness check used for stale lock recovery."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            synchronize = 0x00100000
            wait_timeout = 0x00000102
            handle = kernel32.OpenProcess(synchronize, False, int(pid))
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class _RegistryLock:
    """Exclusive registry lock with owner-aware stale recovery.

    The lock file carries ``pid``/``nonce``/``host``. An existing lock is only
    reclaimed when it is older than ``LOCK_STALE_SECONDS`` *and* its owner PID
    is not alive (or the payload is unreadable). A live owner is never stolen
    from. Release removes the file only when this holder's nonce still matches.
    """

    def __init__(self, releases_dir: Path) -> None:
        self.releases_dir = releases_dir
        self.path = releases_dir / ".lock"
        self.held = False
        self.nonce = uuid.uuid4().hex

    def __enter__(self) -> "_RegistryLock":
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                payload = _lock_payload(self.path)
                owner_alive = bool(payload and _pid_alive(int(payload.get("pid", 0) or 0)))
                if age > LOCK_STALE_SECONDS and not owner_alive:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise ReleaseLockError(
                            "无法清理过期的发布注册表锁。",
                            lock=str(self.path),
                            reason=str(exc),
                        ) from exc
                    continue
                raise ReleaseLockError(
                    "发布注册表正被其他操作占用；请稍后重试。",
                    lock=str(self.path),
                    lock_age_seconds=round(age, 1),
                    lock_owner_pid=payload.get("pid") if payload else None,
                )
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "at": utcnow(),
                    "nonce": self.nonce,
                    "host": os.environ.get("COMPUTERNAME", ""),
                }
            ).encode("utf-8")
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            self.held = True
            return self
        raise ReleaseLockError("发布注册表锁竞争失败。", lock=str(self.path))

    def touch(self) -> None:
        """Heartbeat for long operations; only touches our own lock."""
        payload = _lock_payload(self.path)
        if payload and payload.get("nonce") == self.nonce:
            try:
                os.utime(self.path, None)
            except OSError:
                pass

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.held:
            payload = _lock_payload(self.path)
            if payload is None or payload.get("nonce") == self.nonce:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            self.held = False


def _empty_index() -> dict[str, Any]:
    return {
        "format": REGISTRY_FORMAT,
        "format_version": REGISTRY_FORMAT_VERSION,
        "history_format": {"format": HISTORY_FORMAT, "version": HISTORY_FORMAT_VERSION},
        "sequence": 0,
        "registry_revision": 0,
        "updated_at": None,
        "active_since": None,
        "active_release_id": None,
        "releases": [],
        "withdrawals": {"content": {}, "attachments": {}},
    }


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Fill v3 record fields for v1/v2 read compatibility without losing data."""
    record = dict(record)
    record.setdefault("status", "prepared")
    record.setdefault("activated_at", None)
    record.setdefault("superseded_at", None)
    record.setdefault("withdrawn_at", None)
    record.setdefault("activated_from_release_id", None)
    record.setdefault("superseded_by_release_id", None)
    record.setdefault("withdrawn_content_ids", [])
    record.setdefault("withdrawn_attachment_ids", [])
    record.setdefault("withdrawal_scope", None)
    record.setdefault("reason", None)
    record.setdefault("bundle_format_version", 1)
    record.setdefault("actor", "owner")
    record.setdefault("restored_from_release_id", None)
    record.setdefault("restore_scope", None)
    record.setdefault("sequence", None)
    record.setdefault("history", [])
    return record


def _derived_history_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesize a read-only history ledger for a v1/v2 record.

    The entries mirror facts already recorded in the v2 registry. They are
    never written back unless a later mutating operation upgrades the file,
    and ``origin`` stays ``derived`` so consumers can tell them apart.
    """
    release_id = record["release_id"]
    created_at = record.get("created_at")
    action = record.get("action") or "prepare"
    if action == "restore":
        build_action = "restore_request"
        build_scope = record.get("restore_scope")
        build_content = record.get("content_ids", [])
        build_attachments = record.get("attachment_ids", [])
    elif action == "withdraw":
        build_action = "build"
        build_scope = record.get("withdrawal_scope")
        build_content = record.get("withdrawn_content_ids", [])
        build_attachments = record.get("withdrawn_attachment_ids", [])
    else:
        build_action = "build"
        build_scope = None
        build_content = record.get("content_ids", [])
        build_attachments = record.get("attachment_ids", [])
    entries = [
        {
            "seq": None,
            "registry_revision": None,
            "at": created_at,
            "actor": record.get("actor") or "owner",
            "action": build_action,
            "origin": "derived",
            "from_release_id": None,
            "to_release_id": release_id,
            "scope": build_scope,
            "content_ids": list(build_content or []),
            "attachment_ids": list(build_attachments or []),
            "reason": record.get("reason"),
        }
    ]
    if record.get("activated_at"):
        entries.append(
            {
                "seq": None,
                "registry_revision": None,
                "at": record["activated_at"],
                "actor": record.get("actor") or "owner",
                "action": "activate",
                "origin": "derived",
                "from_release_id": record.get("activated_from_release_id"),
                "to_release_id": release_id,
                "scope": None,
                "content_ids": list(record.get("content_ids", [])),
                "attachment_ids": list(record.get("attachment_ids", [])),
                "reason": record.get("reason"),
            }
        )
    if record.get("superseded_at"):
        entries.append(
            {
                "seq": None,
                "registry_revision": None,
                "at": record["superseded_at"],
                "actor": record.get("actor") or "owner",
                "action": "supersede",
                "origin": "derived",
                "from_release_id": release_id,
                "to_release_id": record.get("superseded_by_release_id"),
                "scope": None,
                "content_ids": list(record.get("content_ids", [])),
                "attachment_ids": list(record.get("attachment_ids", [])),
                "reason": record.get("reason"),
            }
        )
    if record.get("withdrawn_at"):
        entries.append(
            {
                "seq": None,
                "registry_revision": None,
                "at": record["withdrawn_at"],
                "actor": record.get("actor") or "owner",
                "action": "withdraw",
                "origin": "derived",
                "from_release_id": release_id,
                "to_release_id": None,
                "scope": record.get("withdrawal_scope"),
                "content_ids": list(record.get("withdrawn_content_ids", [])),
                "attachment_ids": list(record.get("withdrawn_attachment_ids", [])),
                "reason": record.get("reason"),
            }
        )
    return entries


class ReleaseRegistry:
    """File-based, owner-driven release registry and publishing lifecycle.

    P1 built immutable prepared bundles. P2 adds verified activation, whole
    withdrawal, content-subset withdrawal (successor bundle) and read-only
    orphan auditing. The registry ``index.json`` atomic replace is the only
    commit point for public state changes.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.db_path = self.data_dir / "archive.db"
        self.objects_dir = self.data_dir / "objects"
        self.secret_path = self.data_dir / "owner_secret"
        self.releases_dir = self.data_dir / "releases"
        self.index_path = self.releases_dir / "index.json"
        self.events_path = self.releases_dir / "events.jsonl"
        self.withdrawal_marker_path = self.releases_dir / WITHDRAWAL_MARKER
        self.backups_dir = self.data_dir / "release-backups"

    # ------------------------------------------------------------------
    # Withdrawal safety helpers
    # ------------------------------------------------------------------
    def withdrawal_pending(self) -> bool:
        try:
            self.withdrawal_marker_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True  # An unreadable publication state cannot authorize public access.
        return True

    def _arm_withdrawal(self, reason: str) -> None:
        """Fail closed before any withdrawal mutation.

        The marker makes the public reader return 503 until a withdrawal
        completes successfully. It survives crashes and partial failures.
        """
        payload = {
            "armed_at": utcnow(),
            "reason": reason,
        }
        _atomic_write(self.withdrawal_marker_path, _canonical_bytes(payload))

    def _disarm_withdrawal(self) -> None:
        try:
            self.withdrawal_marker_path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self.withdrawal_marker_path.parent)

    @staticmethod
    def _withdrawal_ledger_hits(
        index: dict[str, Any], content_ids: list[str], attachment_ids: list[str]
    ) -> list[dict[str, Any]]:
        withdrawals = index.setdefault(
            "withdrawals", {"content": {}, "attachments": {}}
        )
        content_bucket = withdrawals.setdefault("content", {})
        attachment_bucket = withdrawals.setdefault("attachments", {})
        hits: list[dict[str, Any]] = []
        for content_id in content_ids:
            entry = content_bucket.get(content_id)
            if entry and not entry.get("reauthorized_at"):
                hits.append({"kind": "content", "id": content_id, **entry})
        for attachment_id in attachment_ids:
            entry = attachment_bucket.get(attachment_id)
            if entry and not entry.get("reauthorized_at"):
                hits.append({"kind": "attachment", "id": attachment_id, **entry})
        return hits

    @staticmethod
    def _mark_withdrawn(
        index: dict[str, Any],
        content_ids: list[str],
        attachment_ids: list[str],
        release_id: str,
        reason: str,
    ) -> None:
        withdrawals = index.setdefault(
            "withdrawals", {"content": {}, "attachments": {}}
        )
        now = utcnow()
        for bucket, ids in (
            (withdrawals.setdefault("content", {}), content_ids),
            (withdrawals.setdefault("attachments", {}), attachment_ids),
        ):
            for identifier in ids:
                bucket[identifier] = {
                    "withdrawn_at": now,
                    "release_id": release_id,
                    "reason": reason,
                    "reauthorized_at": None,
                }

    @staticmethod
    def _mark_republished(index: dict[str, Any], content_ids: list[str], attachment_ids: list[str]) -> None:
        withdrawals = index.setdefault(
            "withdrawals", {"content": {}, "attachments": {}}
        )
        now = utcnow()
        for bucket, ids in (
            (withdrawals.setdefault("content", {}), content_ids),
            (withdrawals.setdefault("attachments", {}), attachment_ids),
        ):
            for identifier in ids:
                entry = bucket.get(identifier)
                if entry and not entry.get("reauthorized_at"):
                    entry["reauthorized_at"] = now

    # ------------------------------------------------------------------
    # Public API (P1)
    # ------------------------------------------------------------------
    def build(
        self,
        content_ids: list[str] | None,
        attachment_ids: list[str] | None,
    ) -> dict[str, Any]:
        """Build one prepared release bundle from an explicit selection."""
        if not isinstance(content_ids, list) or not content_ids:
            raise ReleaseValidationError(
                "发布必须显式选择一个或多个 Content；空选择不产生 Release。",
                field="content_ids",
            )
        if attachment_ids is None:
            raise ReleaseValidationError(
                "发布必须显式选择附件；如无附件请传空列表。",
                field="attachment_ids",
            )
        if not isinstance(attachment_ids, list):
            raise ReleaseValidationError("attachment_ids 必须为数组。", field="attachment_ids")

        issues: list[dict[str, Any]] = []
        selected_content: list[str] = []
        for value in content_ids:
            validated = _validate_id(value, "content_ids", issues)
            if validated is not None and validated not in selected_content:
                selected_content.append(validated)
        selected_attachments: list[str] = []
        for value in attachment_ids:
            validated = _validate_id(value, "attachment_ids", issues)
            if validated is not None and validated not in selected_attachments:
                selected_attachments.append(validated)
        if issues:
            raise ReleaseValidationError("发布选择包含无效标识符，未生成 Release。", issues=issues)

        with _RegistryLock(self.releases_dir):
            index = self._load_index()
            self._reconcile_events_locked(index)
            content_rows, attachment_rows = self._read_selection(
                selected_content, selected_attachments
            )
            self._validate_selection(
                selected_content, selected_attachments, content_rows, attachment_rows
            )
            release_id = f"rel_{uuid.uuid4().hex}"
            staging = self.releases_dir / f".{release_id}.{uuid.uuid4().hex}.tmp"
            final = self.releases_dir / release_id
            index_committed = False
            try:
                manifest = self._write_bundle(
                    staging,
                    release_id,
                    selected_content,
                    selected_attachments,
                    content_rows,
                    attachment_rows,
                )
                inspection = self._inspect_bundle(staging, release_id)
                if not inspection["ok"]:
                    raise ReleaseValidationError(
                        "发布产物边界检查未通过，未发布 Release。",
                        issues=inspection["issues"],
                    )
                if final.exists():
                    raise ReleaseError("Release 目录已存在，拒绝覆盖。", release_id=release_id)
                os.replace(staging, final)
                _fsync_directory(self.releases_dir)

                manifest_sha256, manifest_bytes = _hash_file(final / "release.json")
                record = {
                    "release_id": release_id,
                    "created_at": manifest["created_at"],
                    "parent_release_id": manifest["parent_release_id"],
                    "action": manifest["action"],
                    "status": "prepared",
                    "content_ids": manifest["content_ids"],
                    "attachment_ids": manifest["attachment_ids"],
                    "bundle_sha256": manifest["bundle_sha256"],
                    "manifest_sha256": manifest_sha256,
                    "manifest_bytes": manifest_bytes,
                    "file_count": len(manifest["files"]),
                    "total_bytes": sum(item["bytes"] for item in manifest["files"]),
                    "activated_at": None,
                    "superseded_at": None,
                    "withdrawn_at": None,
                    "activated_from_release_id": None,
                    "superseded_by_release_id": None,
                    "withdrawn_content_ids": [],
                    "withdrawn_attachment_ids": [],
                    "withdrawal_scope": None,
                    "reason": None,
                    "bundle_format_version": manifest["format_version"],
                    "actor": "owner",
                    "restored_from_release_id": None,
                    "restore_scope": None,
                    "sequence": None,
                    "history": [],
                }
                index["registry_revision"] = int(index.get("registry_revision", 0)) + 1
                entry = self._history_entry(
                    index,
                    record,
                    action="build",
                    at=manifest["created_at"],
                    registry_revision=index["registry_revision"],
                    to_release_id=release_id,
                    content_ids=manifest["content_ids"],
                    attachment_ids=manifest["attachment_ids"],
                )
                record["sequence"] = entry["seq"]
                index["releases"].append(record)
                index["updated_at"] = manifest["created_at"]
                self._mark_v3(index)
                _atomic_write(self.index_path, _canonical_bytes(index))
                index_committed = True
                event_recorded = True
                try:
                    self._append_event(
                        {
                            "event_id": f"ev_prepare_{release_id}",
                            "at": manifest["created_at"],
                            "actor": "owner",
                            "action": "prepare",
                            "release_id": release_id,
                            "previous_active_release_id": index["active_release_id"],
                            "result": "prepared",
                            "detail": "显式选择构建；P1 未激活公开发布。",
                            "content_ids": manifest["content_ids"],
                            "attachment_ids": manifest["attachment_ids"],
                            "bundle_sha256": manifest["bundle_sha256"],
                        }
                    )
                except OSError:
                    # index.json is authoritative; a missing prepare event is
                    # repaired idempotently by reconcile_events() / next build.
                    event_recorded = False
            except Exception:
                _remove_tree(staging)
                if final.exists() and not index_committed:
                    _remove_tree(final)
                raise

        return {
            "release_id": release_id,
            "path": str(final),
            "status": "prepared",
            "active_release_id": index["active_release_id"],
            "created_at": manifest["created_at"],
            "content_ids": manifest["content_ids"],
            "attachment_ids": manifest["attachment_ids"],
            "file_count": len(manifest["files"]),
            "total_bytes": record["total_bytes"],
            "bundle_sha256": manifest["bundle_sha256"],
            "boundaries": manifest["boundaries"],
            "inspection": inspection,
            "audit_event_recorded": event_recorded,
            "audit_event_pending": not event_recorded,
        }

    def render_site_preview(self, destination: Path, plan: dict[str, Any]) -> dict[str, Any]:
        """Render an owner-only draft outside the release registry."""
        content_ids, attachment_ids = plan["content_ids"], plan["attachment_ids"]
        contents, attachments = self._read_selection(content_ids, attachment_ids)
        self._validate_selection(content_ids, attachment_ids, contents, attachments)
        return self._write_bundle(destination, "draft-preview", content_ids, attachment_ids, contents, attachments)

    def preview_build(
        self,
        content_ids: list[str] | None,
        attachment_ids: list[str] | None,
    ) -> dict[str, Any]:
        """Read-only projection of a prospective release selection.

        Reuses the same public-field whitelist, attachment verification and
        boundary checks as :meth:`build`, but never creates a bundle, never
        touches the registry, never appends an event, and never writes to the
        private database or attachment objects.
        """
        if not isinstance(content_ids, list) or not content_ids:
            raise ReleaseValidationError(
                "预览必须显式选择一个或多个 Content；空选择不产生 Release。",
                field="content_ids",
            )
        if attachment_ids is None:
            raise ReleaseValidationError(
                "预览必须显式选择附件；如无附件请传空列表。",
                field="attachment_ids",
            )
        if not isinstance(attachment_ids, list):
            raise ReleaseValidationError("attachment_ids 必须为数组。", field="attachment_ids")

        issues: list[dict[str, Any]] = []
        selected_content: list[str] = []
        for value in content_ids:
            validated = _validate_id(value, "content_ids", issues)
            if validated is not None and validated not in selected_content:
                selected_content.append(validated)
        selected_attachments: list[str] = []
        for value in attachment_ids:
            validated = _validate_id(value, "attachment_ids", issues)
            if validated is not None and validated not in selected_attachments:
                selected_attachments.append(validated)
        if issues:
            raise ReleaseValidationError(
                "预览选择包含无效标识符，未生成预览。", issues=issues
            )

        index = self._load_index()
        content_rows, attachment_rows = self._read_selection(
            selected_content, selected_attachments
        )
        selection_issues: list[dict[str, Any]] = []
        try:
            self._validate_selection(
                selected_content, selected_attachments, content_rows, attachment_rows
            )
        except ReleaseValidationError as exc:
            reported = exc.details.get("issues") if isinstance(exc.details, dict) else None
            selection_issues = list(reported or [])

        attachments_by_content: dict[str, list[dict[str, Any]]] = {}
        attachment_facts: list[dict[str, Any]] = []
        for attachment_id in selected_attachments:
            row = attachment_rows.get(attachment_id)
            if row is None:
                continue
            fact = {
                "attachment_id": attachment_id,
                "content_id": row["content_id"],
                "filename": row["filename"],
                "mime": row["mime"],
                "bytes": int(row["byte_size"]) if row["byte_size"] is not None else 0,
                "state": row["state"],
                "visibility": row["visibility"],
            }
            attachment_facts.append(fact)
            attachments_by_content.setdefault(row["content_id"], []).append(fact)

        contents: list[dict[str, Any]] = []
        payload_bytes_total = 0
        for content_id in selected_content:
            row = content_rows.get(content_id)
            if row is None:
                continue
            body = row["body"] or ""
            body_bytes = len(body.encode("utf-8"))
            payload_bytes_total += body_bytes
            payload = build_content_payload(
                "preview",
                {
                    "content_id": content_id,
                    "public_id": public_content_id(content_id),
                    "title": row["title"],
                    "summary": row["summary"] or "",
                    "body": body,
                    "content_type": row["content_type"],
                    "primary_space": row["primary_space"],
                    "updated_at": row["updated_at"],
                    "attachments": [
                        {
                            "attachment_id": item["attachment_id"],
                            "public_id": public_attachment_id(item["attachment_id"]),
                            "source_filename": item["filename"],
                            "bytes": item["bytes"],
                            "mime": item["mime"],
                        }
                        for item in attachments_by_content.get(content_id, [])
                    ],
                },
            )
            # The projected public payload must stay inside the frozen
            # whitelist; anything else would be a boundary regression.
            if set(payload) != PUBLIC_PAYLOAD_FIELDS:
                raise ReleaseError("预览生成的公开字段超出白名单。")
            contents.append(
                {
                    "content_id": content_id,
                    "title": row["title"],
                    "revision": row["revision"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "body_excerpt": body[:120] + ("…" if len(body) > 120 else ""),
                    "body_bytes": body_bytes,
                    "payload_fields": sorted(payload),
                    "attachments": list(attachments_by_content.get(content_id, [])),
                }
            )

        active_id = index.get("active_release_id")
        active_record = (
            self._get_record(index, active_id) if active_id else None
        )
        active_content = list(active_record.get("content_ids", [])) if active_record else []
        active_attachments = (
            list(active_record.get("attachment_ids", [])) if active_record else []
        )

        def _difference(selected: list[str], current: list[str]) -> list[str]:
            return [value for value in selected if value not in current]

        delta = {
            "active_release_id": active_id,
            "added_content_ids": _difference(selected_content, active_content),
            "removed_content_ids": _difference(active_content, selected_content),
            "added_attachment_ids": _difference(selected_attachments, active_attachments),
            "removed_attachment_ids": _difference(
                active_attachments, selected_attachments
            ),
            "same_as_active": bool(
                active_id
                and sorted(selected_content) == sorted(active_content)
                and sorted(selected_attachments) == sorted(active_attachments)
            ),
        }
        boundaries = {
            "scope": "owner-preview-selection",
            "publishes_content": False,
            "activated": False,
            "read_only": True,
            "includes_public_payload": True,
            "includes_private_content": False,
            "includes_private_attachments": False,
            "includes_relations": False,
            "includes_attributes": False,
            "includes_database": False,
            "includes_owner_secret": False,
            "includes_local_paths": False,
            "includes_archive_backup": False,
            "uses_attachment_operations_table": False,
        }
        return {
            "read_only": True,
            "valid": not selection_issues,
            "issues": selection_issues,
            "generated_at": utcnow(),
            "active_release_id": active_id,
            "content_ids": selected_content,
            "attachment_ids": selected_attachments,
            "contents": contents,
            "attachments": attachment_facts,
            "totals": {
                "content_count": len(contents),
                "attachment_count": len(attachment_facts),
                "attachment_bytes": sum(item["bytes"] for item in attachment_facts),
                "payload_bytes": payload_bytes_total,
            },
            "delta": delta,
            "boundaries": boundaries,
        }

    def reconcile_events(self) -> dict[str, Any]:
        """Repair missing prepare events from the authoritative index.

        Safe to call repeatedly: event ids are deterministic per release and
        existing ids are never duplicated.
        """
        with _RegistryLock(self.releases_dir):
            index = self._load_index()
            appended = self._reconcile_events_locked(index)
        return {"appended": len(appended), "event_ids": appended}

    def list(self) -> dict[str, Any]:
        index = self._load_index()
        return {
            "format": index["format"],
            "format_version": index["format_version"],
            "registry_revision": index.get("registry_revision", 0),
            "updated_at": index.get("updated_at"),
            "active_since": index.get("active_since"),
            "active_release_id": index["active_release_id"],
            "releases": index["releases"],
            "release_count": len(index["releases"]),
        }

    def get(self, release_id: str) -> dict[str, Any]:
        index = self._load_index()
        record = next(
            (item for item in index["releases"] if item["release_id"] == release_id), None
        )
        if record is None:
            raise ReleaseNotFoundError("Release 不存在。", release_id=release_id)
        manifest_path = self.releases_dir / release_id / "release.json"
        if not manifest_path.is_file():
            raise ReleaseError("Release 清单缺失。", release_id=release_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {"record": record, "manifest": manifest, "path": str(manifest_path.parent)}

    def verify(self, release_id: str) -> dict[str, Any]:
        record = self.get(release_id)["record"]
        inspection = self._inspect_bundle(
            self.releases_dir / release_id,
            release_id,
            expected_manifest_sha256=record.get("manifest_sha256"),
        )
        return {
            "release_id": release_id,
            "ok": inspection["ok"],
            "issues": inspection["issues"],
            "checked_files": inspection["checked_files"],
            "bundle_sha256": inspection["bundle_sha256"],
            "manifest_sha256": inspection["manifest_sha256"],
        }

    # ------------------------------------------------------------------
    # Publishing history (P3-B)
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_operation(record: dict[str, Any]) -> str:
        """Derive the publishing operation a release record represents."""
        action = record.get("action")
        if action == "restore" or record.get("restored_from_release_id"):
            return (
                "restore_full"
                if record.get("restore_scope") == "full"
                else "restore_subset"
            )
        if action == "withdraw" and record.get("withdrawal_scope") == "content_subset":
            return "withdraw_subset"
        if record.get("withdrawn_at") and record.get("withdrawal_scope") == "all":
            return "withdraw_all"
        if (
            record.get("status") == "prepared"
            and not record.get("activated_at")
            and not record.get("withdrawn_at")
            and action != "withdraw"
        ):
            return "prepared"
        return "publish"

    def _history_item(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "release_id": record["release_id"],
            "sequence": record.get("sequence"),
            "status": record.get("status"),
            "operation": self._derive_operation(record),
            "action": record.get("action"),
            "actor": record.get("actor") or "owner",
            "created_at": record.get("created_at"),
            "activated_at": record.get("activated_at"),
            "superseded_at": record.get("superseded_at"),
            "withdrawn_at": record.get("withdrawn_at"),
            "parent_release_id": record.get("parent_release_id"),
            "superseded_by_release_id": record.get("superseded_by_release_id"),
            "activated_from_release_id": record.get("activated_from_release_id"),
            "restored_from_release_id": record.get("restored_from_release_id"),
            "restore_scope": record.get("restore_scope"),
            "content_ids": list(record.get("content_ids", [])),
            "attachment_ids": list(record.get("attachment_ids", [])),
            "withdrawal_scope": record.get("withdrawal_scope"),
            "withdrawn_content_ids": list(record.get("withdrawn_content_ids", [])),
            "withdrawn_attachment_ids": list(
                record.get("withdrawn_attachment_ids", [])
            ),
            "reason": record.get("reason"),
            "bundle_sha256": record.get("bundle_sha256"),
            "bundle_format_version": record.get("bundle_format_version"),
            "history": list(record.get("history", [])),
        }

    @staticmethod
    def _parse_time(value: str | None, field: str) -> datetime | None:
        if value is None or value == "":
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ReleaseValidationError(
                f"{field} 必须为 ISO 8601 时间。", field=field
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _record_time(record: dict[str, Any]) -> datetime | None:
        created = record.get("created_at")
        if not isinstance(created, str):
            return None
        try:
            parsed = datetime.fromisoformat(
                created[:-1] + "+00:00" if created.endswith("Z") else created
            )
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _mentions_content(item: dict[str, Any], content_id: str) -> bool:
        if content_id in item.get("content_ids", []):
            return True
        if content_id in item.get("withdrawn_content_ids", []):
            return True
        for entry in item.get("history", []):
            if content_id in entry.get("content_ids", []):
                return True
        return False

    def history(
        self,
        *,
        statuses: list[str] | None = None,
        actions: list[str] | None = None,
        content_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        order: str = "desc",
        limit: int = 50,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Query publishing history; ``index.json`` is the sole authority."""
        if order not in ("asc", "desc"):
            raise ReleaseValidationError("order 必须是 asc 或 desc。", field="order")
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 200):
            raise ReleaseValidationError("limit 必须是 1-200 的整数。", field="limit")
        since_dt = self._parse_time(since, "since")
        until_dt = self._parse_time(until, "until")

        index = self._load_index()
        items = [self._history_item(record) for record in index["releases"]]
        if statuses:
            wanted = {value for value in statuses if value}
            items = [item for item in items if item["status"] in wanted]
        if actions:
            wanted = {value for value in actions if value}
            items = [item for item in items if item["operation"] in wanted]
        if content_id:
            items = [item for item in items if self._mentions_content(item, content_id)]
        if since_dt is not None or until_dt is not None:
            filtered = []
            for item in items:
                created = self._record_time(item)
                if created is None:
                    continue
                if since_dt is not None and created < since_dt:
                    continue
                if until_dt is not None and created > until_dt:
                    continue
                filtered.append(item)
            items = filtered

        reverse = order == "desc"
        items.sort(
            key=lambda item: (
                item["sequence"] if isinstance(item["sequence"], int) else -1
            ),
            reverse=reverse,
        )
        total = len(items)
        if cursor is not None:
            if reverse:
                items = [
                    item
                    for item in items
                    if isinstance(item["sequence"], int) and item["sequence"] < cursor
                ]
            else:
                items = [
                    item
                    for item in items
                    if isinstance(item["sequence"], int) and item["sequence"] > cursor
                ]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and isinstance(page[-1]["sequence"], int):
            next_cursor = str(page[-1]["sequence"])
        return {
            "format_version": index.get("format_version"),
            "history_format": index.get("history_format"),
            "registry_revision": index.get("registry_revision", 0),
            "sequence": index.get("sequence", 0),
            "active_release_id": index.get("active_release_id"),
            "items": page,
            "total": total,
            "returned": len(page),
            "order": order,
            "next_cursor": next_cursor,
            "filters": {
                "status": sorted(statuses) if statuses else [],
                "action": sorted(actions) if actions else [],
                "content_id": content_id,
                "since": since,
                "until": until,
            },
        }

    def release_history(
        self, release_id: str, *, verify: bool = False
    ) -> dict[str, Any]:
        index = self._load_index()
        record = self._get_record(index, release_id)
        if record is None:
            raise ReleaseNotFoundError("Release 不存在。", release_id=release_id)
        result: dict[str, Any] = {
            "release_id": release_id,
            "operation": self._derive_operation(record),
            "active": index.get("active_release_id") == release_id,
            "sequence": record.get("sequence"),
            "registry_revision": index.get("registry_revision", 0),
            "record": record,
            "history": list(record.get("history", [])),
        }
        if verify:
            result["verification"] = self.verify(release_id)
        return result

    # ------------------------------------------------------------------
    # Rollback / restore (P3-C)
    # ------------------------------------------------------------------
    def restore(
        self,
        source_release_id: str,
        *,
        content_ids: list[str] | None = None,
        attachment_ids: list[str] | None = None,
        reason: str | None = None,
        expected_active_release_id: str | None = None,
        confirm_republish_withdrawn: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Restore historical bundle content as a brand-new successor release.

        The historical bundle is only read: content, pages, styles and
        attachments are byte-copied into a new bundle. Activation then goes
        through the P2 single-commit path. ``dry_run`` performs every check
        without building or writing anything.
        """
        if not isinstance(source_release_id, str) or not _ID_PATTERN.match(
            source_release_id
        ):
            raise ReleaseValidationError(
                "source_release_id 无效。", field="source_release_id"
            )
        if content_ids is not None and not isinstance(content_ids, list):
            raise ReleaseValidationError(
                "content_ids 必须为数组。", field="content_ids"
            )
        if attachment_ids is not None and not isinstance(attachment_ids, list):
            raise ReleaseValidationError(
                "attachment_ids 必须为数组。", field="attachment_ids"
            )
        if content_ids is None and attachment_ids is not None:
            raise ReleaseValidationError(
                "部分附件选择必须同时指定 content_ids。", field="attachment_ids"
            )
        if reason is not None and not isinstance(reason, str):
            raise ReleaseValidationError("reason 必须是文本。", field="reason")
        if expected_active_release_id is not None and not isinstance(
            expected_active_release_id, str
        ):
            raise ReleaseValidationError(
                "expected_active_release_id 必须是文本。",
                field="expected_active_release_id",
            )

        if dry_run:
            index = self._load_index()
            return self._restore_locked(
                index,
                source_release_id,
                content_ids=content_ids,
                attachment_ids=attachment_ids,
                reason=reason,
                expected_active_release_id=expected_active_release_id,
                confirm_republish_withdrawn=confirm_republish_withdrawn,
                dry_run=True,
                lock=None,
            )
        with _RegistryLock(self.releases_dir) as lock:
            index = self._load_index()
            self._reconcile_events_locked(index)
            return self._restore_locked(
                index,
                source_release_id,
                content_ids=content_ids,
                attachment_ids=attachment_ids,
                reason=reason,
                expected_active_release_id=expected_active_release_id,
                confirm_republish_withdrawn=confirm_republish_withdrawn,
                dry_run=False,
                lock=lock,
            )

    def _restore_locked(
        self,
        index: dict[str, Any],
        source_release_id: str,
        *,
        content_ids: list[str] | None,
        attachment_ids: list[str] | None,
        reason: str | None,
        expected_active_release_id: str | None,
        confirm_republish_withdrawn: bool,
        dry_run: bool,
        lock: "_RegistryLock" | None,
    ) -> dict[str, Any]:
        source = self._get_record(index, source_release_id)
        if source is None:
            raise ReleaseNotFoundError(
                "来源 Release 不存在。", release_id=source_release_id
            )
        active_id = index["active_release_id"]
        if (
            expected_active_release_id is not None
            and expected_active_release_id != active_id
        ):
            raise ReleaseConflictError(
                "active Release 已变化，请刷新后重试。",
                expected_active_release_id=expected_active_release_id,
                active_release_id=active_id,
            )
        if source.get("status") == "active" and active_id == source_release_id:
            return {
                "ok": True,
                "changed": False,
                "already_active": True,
                "already_restored": False,
                "resumed": False,
                "dry_run": dry_run,
                "source_release_id": source_release_id,
                "active_release_id": active_id,
                "registry_revision": index.get("registry_revision", 0),
            }
        if source.get("status") == "prepared" and not source.get("activated_at"):
            raise ReleaseValidationError(
                "来源 Release 从未发布，不能作为恢复来源。",
                issues=[
                    {
                        "code": "source_never_published",
                        "severity": "error",
                        "target": source_release_id,
                        "detail": "prepared Release 没有公开内容可恢复。",
                    }
                ],
            )
        if source.get("status") not in ("superseded", "withdrawn"):
            raise ReleaseValidationError(
                "只有已发布且当前离线的历史 Release 可作为恢复来源。",
                issues=[
                    {
                        "code": "source_release_not_restorable",
                        "severity": "error",
                        "target": source_release_id,
                        "detail": f"当前状态为 {source.get('status')}。",
                    }
                ],
            )

        source_dir = self.releases_dir / source_release_id
        manifest_path = source_dir / "release.json"
        if not manifest_path.is_file():
            raise ReleaseValidationError(
                "来源 Bundle 缺失，拒绝恢复。",
                issues=[
                    {
                        "code": "source_bundle_missing",
                        "severity": "error",
                        "target": source_release_id,
                        "detail": "来源 release.json 缺失。",
                    }
                ],
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError(
                "来源 Bundle 清单不可解析，拒绝恢复。",
                issues=[
                    {
                        "code": "source_bundle_invalid",
                        "severity": "error",
                        "target": source_release_id,
                        "detail": str(exc),
                    }
                ],
            ) from exc
        issues: list[dict[str, Any]] = []
        if manifest.get("format") != RELEASE_FORMAT:
            issues.append(
                {
                    "code": "source_format_mismatch",
                    "severity": "error",
                    "target": source_release_id,
                    "detail": "来源清单格式不匹配。",
                }
            )
        if manifest.get("release_id") != source_release_id:
            issues.append(
                {
                    "code": "source_manifest_id_mismatch",
                    "severity": "error",
                    "target": source_release_id,
                    "detail": "来源清单 ID 与目录不一致。",
                }
            )
        if manifest.get("format_version") != RELEASE_FORMAT_VERSION:
            issues.append(
                {
                    "code": "source_format_unsupported",
                    "severity": "error",
                    "target": source_release_id,
                    "detail": "仅 v2 Bundle 可作为恢复来源。",
                }
            )
        if issues:
            raise ReleaseValidationError("来源 Release 不可作为恢复来源。", issues=issues)

        inspection = self._inspect_bundle(
            source_dir,
            source_release_id,
            expected_manifest_sha256=source.get("manifest_sha256"),
        )
        if not inspection["ok"]:
            raise ReleaseValidationError(
                "来源 Bundle 完整性校验未通过，拒绝恢复。",
                issues=inspection["issues"],
            )
        for path in source_dir.rglob("*"):
            if path.is_symlink():
                raise ReleaseValidationError(
                    "来源 Bundle 包含符号链接，拒绝恢复。",
                    issues=[
                        {
                            "code": "source_symlink_not_allowed",
                            "severity": "error",
                            "target": source_release_id,
                            "detail": str(path.relative_to(source_dir)),
                        }
                    ],
                )

        item_map = {
            item["content_id"]: item for item in manifest.get("items", [])
        }
        for content_id, item in item_map.items():
            payload = self._read_public_payload(source_dir, item, source_release_id)
            self._validate_public_payload(payload, item, source_release_id)

        source_content = list(manifest.get("content_ids", []))
        source_attachments = list(manifest.get("attachment_ids", []))
        attachment_owner = {
            attachment["attachment_id"]: item["content_id"]
            for item in manifest.get("items", [])
            for attachment in item.get("attachments", [])
        }

        selection_issues: list[dict[str, Any]] = []
        if content_ids is None:
            selected_content = list(source_content)
            selected_attachments = list(source_attachments)
            scope = "full"
        else:
            selected_content = []
            for value in content_ids:
                validated = _validate_id(value, "content_ids", selection_issues)
                if validated is not None and validated not in selected_content:
                    selected_content.append(validated)
            if not selected_content:
                selection_issues.append(
                    {
                        "code": "empty_content_selection",
                        "severity": "error",
                        "detail": "部分恢复必须至少选择一个 Content。",
                    }
                )
            for content_id in selected_content:
                if content_id not in source_content:
                    selection_issues.append(
                        {
                            "code": "content_not_in_source_release",
                            "severity": "error",
                            "target": content_id,
                            "detail": "该 Content 不在来源 Release 中。",
                        }
                    )
            if attachment_ids is None:
                selected_attachments = [
                    attachment_id
                    for attachment_id in source_attachments
                    if attachment_owner.get(attachment_id) in selected_content
                ]
            else:
                selected_attachments = []
                for value in attachment_ids:
                    validated = _validate_id(
                        value, "attachment_ids", selection_issues
                    )
                    if validated is None or validated in selected_attachments:
                        continue
                    selected_attachments.append(validated)
                    if validated not in source_attachments:
                        selection_issues.append(
                            {
                                "code": "attachment_not_in_source_release",
                                "severity": "error",
                                "target": validated,
                                "detail": "该附件不在来源 Release 中。",
                            }
                        )
                    elif attachment_owner.get(validated) not in selected_content:
                        selection_issues.append(
                            {
                                "code": "attachment_content_not_selected",
                                "severity": "error",
                                "target": validated,
                                "detail": "附件所属 Content 未在选择范围内。",
                            }
                        )
            if selection_issues:
                raise ReleaseValidationError(
                    "恢复选择无效，未生成恢复 Release。",
                    issues=selection_issues,
                )
            scope = "content_subset"

        gate_issues, warnings = self._restore_public_gate(
            manifest, selected_content, selected_attachments
        )
        if gate_issues:
            raise ReleaseValidationError(
                "恢复目标包含当前不可公开的内容，拒绝恢复。",
                issues=gate_issues,
            )

        active_record = self._get_record(index, active_id) if active_id else None
        if (
            active_record is not None
            and active_record.get("restored_from_release_id") == source_release_id
            and active_record.get("restore_scope") == scope
            and sorted(active_record.get("content_ids", [])) == sorted(selected_content)
            and sorted(active_record.get("attachment_ids", []))
            == sorted(selected_attachments)
        ):
            return {
                "ok": True,
                "changed": False,
                "already_active": False,
                "already_restored": True,
                "resumed": False,
                "dry_run": dry_run,
                "source_release_id": source_release_id,
                "release_id": active_id,
                "active_release_id": active_id,
                "registry_revision": index.get("registry_revision", 0),
            }

        withdrawn_content, withdrawn_attachments = self._withdrawn_targets(
            index, source_release_id
        )
        previously_withdrawn = {
            "content_ids": sorted(set(selected_content) & withdrawn_content),
            "attachment_ids": sorted(
                set(selected_attachments) & withdrawn_attachments
            ),
        }
        requires_confirmation = bool(
            previously_withdrawn["content_ids"]
            or previously_withdrawn["attachment_ids"]
        )
        if requires_confirmation and not confirm_republish_withdrawn and not dry_run:
            raise ReleaseConfirmationError(
                "恢复目标曾在后续版本中被撤回，需要显式确认。",
                previously_withdrawn=previously_withdrawn,
            )

        pending_restore = next(
            (
                record
                for record in index["releases"]
                if record.get("status") == "prepared"
                and record.get("action") == "restore"
                and record.get("restored_from_release_id") == source_release_id
                and record.get("restore_scope") == scope
                and sorted(record.get("content_ids", []))
                == sorted(selected_content)
                and sorted(record.get("attachment_ids", []))
                == sorted(selected_attachments)
            ),
            None,
        )

        preview: dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "source_release_id": source_release_id,
            "scope": scope,
            "content_ids": selected_content,
            "attachment_ids": selected_attachments,
            "previous_active_release_id": active_id,
            "expected_active_release_id": expected_active_release_id,
            "warnings": warnings,
            "previously_withdrawn": previously_withdrawn,
            "requires_confirmation": requires_confirmation,
            "plan": {
                "action": "restore",
                "parent_release_id": source_release_id,
                "restored_from_release_id": source_release_id,
                "restore_scope": scope,
                "will_supersede_release_id": active_id,
            },
        }
        if dry_run:
            if pending_restore is not None:
                preview["resume_release_id"] = pending_restore["release_id"]
            return preview

        if pending_restore is not None:
            activation = self._activate_locked(
                index,
                pending_restore["release_id"],
                reason=reason,
                verify_private=False,
                lock=lock,
                confirm_republish_withdrawn=confirm_republish_withdrawn,
            )
            preview.update(
                {
                    "changed": True,
                    "already_active": False,
                    "already_restored": False,
                    "resumed": True,
                    "release_id": pending_restore["release_id"],
                    "status": activation["status"],
                    "active_release_id": activation["active_release_id"],
                    "active_since": activation["active_since"],
                    "registry_revision": activation["registry_revision"],
                    "audit_event_recorded": activation["audit_event_recorded"],
                    "audit_event_pending": activation["audit_event_pending"],
                }
            )
            return preview

        new_release_id = f"rel_{uuid.uuid4().hex}"
        result = self._build_restore_locked(
            index,
            new_release_id,
            source_release_id,
            source_dir,
            manifest,
            selected_content,
            selected_attachments,
            scope,
            reason,
            lock,
        )
        try:
            activation = self._activate_locked(
                index,
                new_release_id,
                reason=reason,
                verify_private=False,
                lock=lock,
                confirm_republish_withdrawn=confirm_republish_withdrawn,
            )
        except Exception as exc:
            if hasattr(exc, "details") and isinstance(exc.details, dict):
                exc.details.setdefault("restore_release_id", new_release_id)
            raise
        result.update(
            {
                "changed": True,
                "already_active": False,
                "already_restored": False,
                "resumed": False,
                "source_release_id": source_release_id,
                "scope": scope,
                "status": activation["status"],
                "previous_active_release_id": activation[
                    "previous_active_release_id"
                ],
                "active_release_id": activation["active_release_id"],
                "active_since": activation["active_since"],
                "registry_revision": activation["registry_revision"],
                "audit_event_recorded": activation["audit_event_recorded"],
                "audit_event_pending": activation["audit_event_pending"],
                "warnings": warnings,
                "previously_withdrawn": previously_withdrawn,
            }
        )
        return result

    def _build_restore_locked(
        self,
        index: dict[str, Any],
        new_release_id: str,
        source_release_id: str,
        source_dir: Path,
        source_manifest: dict[str, Any],
        content_ids: list[str],
        attachment_ids: list[str],
        scope: str,
        reason: str | None,
        lock: "_RegistryLock",
    ) -> dict[str, Any]:
        staging = (
            self.releases_dir
            / f".{new_release_id}.{uuid.uuid4().hex}.tmp"
        )
        final = self.releases_dir / new_release_id
        index_committed = False
        files: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        source_items = {
            item["content_id"]: item for item in source_manifest.get("items", [])
        }
        source_attachments = {
            attachment["attachment_id"]: attachment
            for item in source_items.values()
            for attachment in item.get("attachments", [])
        }
        selected_attachments = set(attachment_ids)
        try:
            staging.mkdir(parents=True, exist_ok=False)
            style_source = source_dir / "assets" / "style.css"
            style_bytes = style_source.read_bytes()
            _write_bytes(staging / "assets" / "style.css", style_bytes)
            files.append(
                {
                    "path": "assets/style.css",
                    "kind": "asset",
                    "bytes": len(style_bytes),
                    "sha256": _sha256_bytes(style_bytes),
                }
            )

            for content_id in content_ids:
                source_item = source_items[content_id]
                payload_data = self._read_public_payload(
                    source_dir, source_item, source_release_id
                )
                attachment_items = [
                    dict(attachment)
                    for attachment in source_item.get("attachments", [])
                    if attachment["attachment_id"] in selected_attachments
                ]
                item = {
                    "content_id": content_id,
                    "public_id": source_item.get("public_id")
                    or payload_data["id"],
                    "title": payload_data["title"],
                    "summary": payload_data.get("summary") or "",
                    "body": payload_data["body"],
                    "content_type": payload_data["type"],
                    "primary_space": payload_data["primary_space"],
                    "revision": source_item["revision"],
                    "created_at": source_item["created_at"],
                    "updated_at": payload_data["updated_at"],
                    "related": [
                        dict(entry)
                        for entry in source_item.get("related", [])
                        if entry.get("content_id") in content_ids
                    ],
                    "attachments": attachment_items,
                }
                payload_bytes = _canonical_bytes(
                    build_content_payload(new_release_id, item)
                )
                payload_path = source_item["payload_path"]
                _write_bytes(staging / payload_path, payload_bytes)
                files.append(
                    {
                        "path": payload_path,
                        "kind": "payload",
                        "bytes": len(payload_bytes),
                        "sha256": _sha256_bytes(payload_bytes),
                    }
                )
                page_bytes = _render_content_page(item, new_release_id)
                page_path = source_item["page_path"]
                _write_bytes(staging / page_path, page_bytes)
                files.append(
                    {
                        "path": page_path,
                        "kind": "content",
                        "bytes": len(page_bytes),
                        "sha256": _sha256_bytes(page_bytes),
                    }
                )
                items.append(
                    {
                        "content_id": content_id,
                        "public_id": item["public_id"],
                        "title": item["title"],
                        "summary": item["summary"],
                        "content_type": item["content_type"],
                        "primary_space": item["primary_space"],
                        "revision": item["revision"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                        "body_sha256": _sha256_bytes(
                            (item["body"] or "").encode("utf-8")
                        ),
                        "body_bytes": len((item["body"] or "").encode("utf-8")),
                        "payload_path": payload_path,
                        "payload_sha256": files[-2]["sha256"],
                        "payload_bytes": files[-2]["bytes"],
                        "page_path": page_path,
                        "page_sha256": files[-1]["sha256"],
                        "space_path": f"spaces/{_space_slug(item['primary_space'])}/index.html",
                        "related": item.get("related", []),
                        "attachments": attachment_items,
                    }
                )

            for attachment_id in attachment_ids:
                attachment_entry = source_attachments.get(attachment_id)
                source_entry = next(
                    (
                        entry
                        for entry in source_manifest["files"]
                        if attachment_entry
                        and entry["path"] == attachment_entry.get("path")
                    ),
                    None,
                )
                if source_entry is None:
                    raise ReleaseValidationError(
                        "来源 Bundle 缺少附件文件声明，拒绝恢复。",
                        issues=[
                            {
                                "code": "source_attachment_missing",
                                "severity": "error",
                                "target": attachment_id,
                                "detail": "附件未在来源清单中声明。",
                            }
                        ],
                    )
                relative = source_entry["path"]
                source = source_dir / Path(relative)
                destination = staging / Path(relative)
                digest, size = self._copy_verified(
                    source,
                    destination,
                    source_entry.get("sha256"),
                    source_entry.get("bytes"),
                )
                files.append(
                    {
                        "path": relative,
                        "kind": "attachment",
                        "bytes": size,
                        "sha256": digest,
                    }
                )

            self._write_site_pages(staging, items, files)
            files.sort(key=lambda entry: entry["path"])
            created_at = utcnow()
            manifest = {
                "format": RELEASE_FORMAT,
                "format_version": RELEASE_FORMAT_VERSION,
                "release_id": new_release_id,
                "created_at": created_at,
                "parent_release_id": source_release_id,
                "action": "restore",
                "status": "prepared",
                "schema_version": SCHEMA_VERSION,
                "app_version": __version__,
                "content_ids": content_ids,
                "attachment_ids": attachment_ids,
                "withdrawn_content_ids": [],
                "withdrawn_attachment_ids": [],
                "withdrawal_scope": None,
                "restored_from_release_id": source_release_id,
                "restore_scope": scope,
                "activated_from_release_id": None,
                "reason": reason,
                "items": items,
                "files": files,
                "bundle_sha256": _sha256_bytes(_canonical_bytes({"files": files})),
                "bundle_sha256_scope": "files[] excluding release.json; manifest hash is anchored in index.json",
                "boundaries": {
                    "scope": "owner-selected-public-content",
                    "publishes_content": True,
                    "activated": False,
                    "includes_public_payload": True,
                    "includes_private_content": False,
                    "includes_private_attachments": False,
                    "includes_relations": False,
                    "includes_relation_links": True,
                    "includes_site_index": True,
                    "includes_attributes": False,
                    "includes_database": False,
                    "includes_owner_secret": False,
                    "includes_local_paths": False,
                    "includes_archive_backup": False,
                    "uses_attachment_operations_table": False,
                    "restored_from_historical_bundle": True,
                },
            }
            _write_bytes(staging / "release.json", _canonical_bytes(manifest))
            inspection = self._inspect_bundle(staging, new_release_id)
            if not inspection["ok"]:
                raise ReleaseValidationError(
                    "恢复 Bundle 边界检查未通过。", issues=inspection["issues"]
                )
            if final.exists():
                raise ReleaseError(
                    "Release 目录已存在，拒绝覆盖。", release_id=new_release_id
                )
            os.replace(staging, final)
            _fsync_directory(self.releases_dir)

            manifest_sha256, manifest_bytes = _hash_file(final / "release.json")
            record = {
                "release_id": new_release_id,
                "created_at": created_at,
                "parent_release_id": source_release_id,
                "action": "restore",
                "status": "prepared",
                "content_ids": content_ids,
                "attachment_ids": attachment_ids,
                "bundle_sha256": manifest["bundle_sha256"],
                "manifest_sha256": manifest_sha256,
                "manifest_bytes": manifest_bytes,
                "file_count": len(files),
                "total_bytes": sum(entry["bytes"] for entry in files),
                "activated_at": None,
                "superseded_at": None,
                "withdrawn_at": None,
                "activated_from_release_id": None,
                "superseded_by_release_id": None,
                "withdrawn_content_ids": [],
                "withdrawn_attachment_ids": [],
                "withdrawal_scope": None,
                "reason": reason,
                "bundle_format_version": RELEASE_FORMAT_VERSION,
                "actor": "owner",
                "restored_from_release_id": source_release_id,
                "restore_scope": scope,
                "sequence": None,
                "history": [],
            }
            index["registry_revision"] = int(index.get("registry_revision", 0)) + 1
            entry = self._history_entry(
                index,
                record,
                action="restore_request",
                at=created_at,
                registry_revision=index["registry_revision"],
                to_release_id=new_release_id,
                scope=scope,
                content_ids=content_ids,
                attachment_ids=attachment_ids,
                reason=reason,
            )
            record["sequence"] = entry["seq"]
            index["releases"].append(record)
            index["updated_at"] = created_at
            lock.touch()
            self._mark_v3(index)
            _atomic_write(self.index_path, _canonical_bytes(index))
            index_committed = True
        except Exception:
            _remove_tree(staging)
            if final.exists() and not index_committed:
                _remove_tree(final)
            raise

        event_recorded = True
        try:
            self._append_event(
                {
                    "event_id": f"ev_prepare_{new_release_id}",
                    "at": created_at,
                    "actor": "owner",
                    "action": "prepare",
                    "release_id": new_release_id,
                    "previous_active_release_id": index["active_release_id"],
                    "result": "prepared",
                    "detail": reason or "所有者从历史 Bundle 创建恢复 Release。",
                    "content_ids": content_ids,
                    "attachment_ids": attachment_ids,
                    "restored_from_release_id": source_release_id,
                    "restore_scope": scope,
                    "bundle_sha256": manifest["bundle_sha256"],
                }
            )
        except OSError:
            event_recorded = False

        return {
            "release_id": new_release_id,
            "status": "prepared",
            "parent_release_id": source_release_id,
            "action": "restore",
            "restored_from_release_id": source_release_id,
            "restore_scope": scope,
            "content_ids": content_ids,
            "attachment_ids": attachment_ids,
            "file_count": len(files),
            "total_bytes": record["total_bytes"],
            "bundle_sha256": manifest["bundle_sha256"],
            "sequence": record["sequence"],
            "audit_event_recorded": event_recorded,
            "audit_event_pending": not event_recorded,
        }

    def _read_public_payload(
        self,
        source_dir: Path,
        item: dict[str, Any],
        source_release_id: str,
    ) -> dict[str, Any]:
        payload_path = item.get("payload_path")
        if not isinstance(payload_path, str):
            raise ReleaseValidationError(
                "来源条目缺少公开 payload 路径。",
                issues=[
                    {
                        "code": "source_payload_missing",
                        "severity": "error",
                        "target": source_release_id,
                        "detail": item.get("content_id"),
                    }
                ],
            )
        try:
            payload = json.loads(
                (source_dir / Path(payload_path)).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError(
                "来源公开 payload 不可解析，拒绝恢复。",
                issues=[
                    {
                        "code": "source_payload_invalid",
                        "severity": "error",
                        "target": item.get("content_id"),
                        "detail": str(exc),
                    }
                ],
            ) from exc
        return payload

    @staticmethod
    def _validate_public_payload(
        payload: Any, item: dict[str, Any], source_release_id: str
    ) -> None:
        issues: list[dict[str, Any]] = []
        if not isinstance(payload, dict):
            issues.append(
                {
                    "code": "source_payload_invalid",
                    "severity": "error",
                    "target": source_release_id,
                    "detail": "payload 必须是对象。",
                }
            )
        else:
            unknown = set(payload) - PUBLIC_PAYLOAD_FIELDS
            if unknown:
                issues.append(
                    {
                        "code": "source_payload_private_field",
                        "severity": "error",
                        "target": item.get("content_id"),
                        "detail": f"payload 包含非公开字段: {sorted(unknown)}",
                    }
                )
            for field in (
                "payload_version",
                "id",
                "title",
                "summary",
                "body",
                "type",
                "primary_space",
                "updated_at",
                "attachments",
            ):
                if field not in payload:
                    issues.append(
                        {
                            "code": "source_payload_field_missing",
                            "severity": "error",
                            "target": item.get("content_id"),
                            "detail": f"缺少公开字段 {field}。",
                        }
                    )
            if payload.get("payload_version") != PUBLIC_PAYLOAD_VERSION:
                issues.append(
                    {
                        "code": "source_payload_version_unsupported",
                        "severity": "error",
                        "target": item.get("content_id"),
                        "detail": "payload_version 不是 2。",
                    }
                )
            expected_public_id = item.get("public_id")
            if not expected_public_id or payload.get("id") != expected_public_id:
                issues.append(
                    {
                        "code": "source_payload_id_mismatch",
                        "severity": "error",
                        "target": item.get("content_id"),
                        "detail": "payload 公开 ID 与条目 public_id 不一致。",
                    }
                )
            attachments = payload.get("attachments")
            if not isinstance(attachments, list):
                issues.append(
                    {
                        "code": "source_payload_attachments_invalid",
                        "severity": "error",
                        "target": item.get("content_id"),
                        "detail": "payload attachments 必须是数组。",
                    }
                )
            else:
                declared_ids = {
                    attachment.get("public_id")
                    for attachment in item.get("attachments", [])
                }
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        issues.append(
                            {
                                "code": "source_payload_attachment_invalid",
                                "severity": "error",
                                "target": item.get("content_id"),
                                "detail": "payload 附件条目必须是对象。",
                            }
                        )
                        continue
                    unknown_attachment = set(attachment) - PUBLIC_ATTACHMENT_FIELDS
                    if unknown_attachment:
                        issues.append(
                            {
                                "code": "source_payload_attachment_private_field",
                                "severity": "error",
                                "target": item.get("content_id"),
                                "detail": f"附件公开字段越界: {sorted(unknown_attachment)}",
                            }
                        )
                    if attachment.get("id") not in declared_ids:
                        issues.append(
                            {
                                "code": "source_payload_attachment_mismatch",
                                "severity": "error",
                                "target": item.get("content_id"),
                                "detail": "payload 附件 ID 未在条目中声明。",
                            }
                        )
        if issues:
            raise ReleaseValidationError(
                "来源公开 payload 不符合公开字段白名单，拒绝恢复。",
                issues=issues,
            )

    def _restore_public_gate(
        self,
        manifest: dict[str, Any],
        content_ids: list[str],
        attachment_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Check current private-database publication state for restore targets.

        Revision/hash drift is a warning because the restored bytes come from
        the historical bundle, but the content must still be currently public
        and active so a restore can never expose private or deleted data.
        """
        issues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if not self.db_path.is_file():
            return (
                [
                    {
                        "code": "database_missing",
                        "severity": "error",
                        "detail": "私有数据库不存在，无法校验公开边界。",
                    }
                ],
                warnings,
            )
        item_map = {
            item["content_id"]: item for item in manifest.get("items", [])
        }
        attachment_map = {
            attachment["attachment_id"]: attachment
            for item in manifest.get("items", [])
            for attachment in item.get("attachments", [])
        }
        conn = connect(self.db_path)
        try:
            content_rows = (
                {
                    row["id"]: dict(row)
                    for row in conn.execute(
                        "SELECT id, state, visibility, visibility_selected, revision FROM content "
                        "WHERE id IN ({})".format(",".join("?" for _ in content_ids)),
                        tuple(content_ids),
                    ).fetchall()
                }
                if content_ids
                else {}
            )
            attachment_rows = (
                {
                    row["id"]: dict(row)
                    for row in conn.execute(
                        "SELECT id, content_id, state, visibility, byte_size, sha256 FROM attachments "
                        "WHERE id IN ({})".format(
                            ",".join("?" for _ in attachment_ids)
                        ),
                        tuple(attachment_ids),
                    ).fetchall()
                }
                if attachment_ids
                else {}
            )
        finally:
            conn.close()

        for content_id in content_ids:
            row = content_rows.get(content_id)
            if row is None:
                issues.append(
                    {
                        "code": "content_not_found",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 已不存在，恢复会创建无效公开内容。",
                    }
                )
                continue
            if row["state"] != "active":
                issues.append(
                    {
                        "code": "content_not_active",
                        "severity": "error",
                        "target": content_id,
                        "detail": f"Content 状态为 {row['state']}。",
                    }
                )
                continue
            if row["visibility"] != "public" or row["visibility_selected"] != 1:
                issues.append(
                    {
                        "code": "content_not_public",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 当前不是所有者选择的公开状态。",
                    }
                )
                continue
            source_item = item_map.get(content_id, {})
            if int(row["revision"]) != int(source_item.get("revision", 0)):
                warnings.append(
                    {
                        "code": "content_revision_changed",
                        "severity": "warning",
                        "target": content_id,
                        "detail": "当前修订号与历史版本不同；恢复使用历史 Bundle 内容。",
                    }
                )

        for attachment_id in attachment_ids:
            row = attachment_rows.get(attachment_id)
            if row is None:
                issues.append(
                    {
                        "code": "attachment_not_found",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件已不存在。",
                    }
                )
                continue
            if row["content_id"] not in content_ids:
                issues.append(
                    {
                        "code": "attachment_content_not_selected",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件所属 Content 不在恢复范围。",
                    }
                )
                continue
            if row["state"] != "READY":
                issues.append(
                    {
                        "code": "attachment_not_ready",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": f"附件状态为 {row['state']}。",
                    }
                )
                continue
            if row["visibility"] != "public":
                issues.append(
                    {
                        "code": "attachment_private",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件当前不是公开状态。",
                    }
                )
                continue
            source_attachment = attachment_map.get(attachment_id, {})
            if row["sha256"] != source_attachment.get("sha256") or int(
                row["byte_size"] or -1
            ) != int(source_attachment.get("bytes", -1)):
                warnings.append(
                    {
                        "code": "attachment_content_changed",
                        "severity": "warning",
                        "target": attachment_id,
                        "detail": "当前附件对象与历史版本不同；恢复使用历史 Bundle 副本。",
                    }
                )
        return issues, warnings

    @staticmethod
    def _withdrawn_targets(
        index: dict[str, Any], source_release_id: str
    ) -> tuple[set[str], set[str]]:
        content: set[str] = set()
        attachments: set[str] = set()
        for record in index["releases"]:
            if record["release_id"] == source_release_id:
                continue
            content.update(record.get("withdrawn_content_ids") or [])
            attachments.update(record.get("withdrawn_attachment_ids") or [])
        return content, attachments

    # ------------------------------------------------------------------
    # Activation and withdrawal (P2)
    # ------------------------------------------------------------------
    @staticmethod
    def _get_record(index: dict[str, Any], release_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in index["releases"] if item["release_id"] == release_id),
            None,
        )

    def activate(
        self,
        release_id: str,
        *,
        reason: str | None = None,
        confirm_republish_withdrawn: bool = False,
    ) -> dict[str, Any]:
        """Activate one verified v2 prepared release.

        The registry ``index.json`` atomic replace is the single commit point.
        Anything that fails before it leaves the previous active release intact.
        """
        with _RegistryLock(self.releases_dir) as lock:
            index = self._load_index()
            self._reconcile_events_locked(index)
            return self._activate_locked(
                index,
                release_id,
                reason=reason,
                lock=lock,
                confirm_republish_withdrawn=confirm_republish_withdrawn,
            )

    def _activate_locked(
        self,
        index: dict[str, Any],
        release_id: str,
        *,
        reason: str | None,
        verify_private: bool = True,
        lock: "_RegistryLock" | None = None,
        confirm_republish_withdrawn: bool = False,
    ) -> dict[str, Any]:
        record = self._get_record(index, release_id)
        if record is None:
            raise ReleaseNotFoundError("Release 不存在。", release_id=release_id)
        if record["status"] == "active" and index["active_release_id"] == release_id:
            return {
                "release_id": release_id,
                "status": "active",
                "already_active": True,
                "active_release_id": release_id,
                "registry_revision": index.get("registry_revision", 0),
                "active_since": index.get("active_since"),
                "audit_event_recorded": True,
                "audit_event_pending": False,
            }
        if record["status"] != "prepared":
            raise ReleaseValidationError(
                "只有 prepared Release 可以激活。",
                issues=[
                    {
                        "code": "release_not_prepared",
                        "severity": "error",
                        "target": release_id,
                        "detail": f"当前状态为 {record['status']}。",
                    }
                ],
            )

        release_dir = self.releases_dir / release_id
        manifest_path = release_dir / "release.json"
        if not manifest_path.is_file():
            raise ReleaseValidationError(
                "Release 清单缺失，无法激活。",
                issues=[
                    {
                        "code": "manifest_missing",
                        "severity": "error",
                        "target": release_id,
                        "detail": "release.json 缺失。",
                    }
                ],
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReleaseValidationError(
                "Release 清单无法解析，无法激活。", release_id=release_id, reason=str(exc)
            ) from exc
        if manifest.get("format") != RELEASE_FORMAT:
            raise ReleaseValidationError(
                "Release 清单格式不匹配，无法激活。", release_id=release_id
            )
        if manifest.get("format_version") != RELEASE_FORMAT_VERSION:
            raise ReleaseValidationError(
                "v1 prepared Bundle 不可直接激活；请重新 prepare。",
                issues=[
                    {
                        "code": "release_format_unsupported",
                        "severity": "error",
                        "target": release_id,
                        "detail": "仅 v2 Bundle 可激活。",
                    }
                ],
            )
        if manifest.get("release_id") != release_id:
            raise ReleaseValidationError(
                "Release 清单 ID 与注册表不一致，无法激活。", release_id=release_id
            )

        inspection = self._inspect_bundle(
            release_dir,
            release_id,
            expected_manifest_sha256=record.get("manifest_sha256"),
        )
        if not inspection["ok"]:
            raise ReleaseValidationError(
                "Release Bundle 完整性校验未通过，无法激活。",
                issues=inspection["issues"],
            )

        self._audit_locked(index, write_report=False)

        if verify_private:
            self._validate_release_against_db(manifest)

        target_content_ids = list(manifest.get("content_ids", []))
        target_attachment_ids = list(manifest.get("attachment_ids", []))
        withdrawn_hits = self._withdrawal_ledger_hits(
            index, target_content_ids, target_attachment_ids
        )
        if withdrawn_hits and not confirm_republish_withdrawn:
            raise ReleaseConfirmationError(
                "该 Release 包含先前被撤回的内容；重新公开需要显式授权。",
                withdrawn=withdrawn_hits,
                hint="请构建新的 Release 并显式确认重新公开，或保持撤回状态。",
            )
        if withdrawn_hits:
            self._mark_republished(index, target_content_ids, target_attachment_ids)

        now = utcnow()
        previous_active = index["active_release_id"]
        previous_record: dict[str, Any] | None = None
        for item in index["releases"]:
            if item["release_id"] == previous_active and item["status"] == "active":
                item["status"] = "superseded"
                item["superseded_at"] = now
                item["superseded_by_release_id"] = release_id
                previous_record = item
        record["status"] = "active"
        record["activated_at"] = now
        record["activated_from_release_id"] = previous_active
        record["reason"] = reason
        record["bundle_format_version"] = RELEASE_FORMAT_VERSION
        record.setdefault("actor", "owner")
        index["active_release_id"] = release_id
        index["active_since"] = now
        index["updated_at"] = now
        index["registry_revision"] = int(index.get("registry_revision", 0)) + 1
        revision = index["registry_revision"]
        if withdrawn_hits:
            self._history_entry(
                index,
                record,
                action="reauthorize",
                at=now,
                registry_revision=revision,
                from_release_id=previous_active,
                to_release_id=release_id,
                content_ids=[
                    hit["id"] for hit in withdrawn_hits if hit["kind"] == "content"
                ],
                attachment_ids=[
                    hit["id"] for hit in withdrawn_hits if hit["kind"] == "attachment"
                ],
                reason=reason or "所有者显式确认重新公开先前撤回的内容。",
            )
        if previous_record is not None:
            self._history_entry(
                index,
                previous_record,
                action="supersede",
                at=now,
                registry_revision=revision,
                from_release_id=previous_active,
                to_release_id=release_id,
                content_ids=previous_record.get("content_ids", []),
                attachment_ids=previous_record.get("attachment_ids", []),
            )
        self._history_entry(
            index,
            record,
            action="activate",
            at=now,
            registry_revision=revision,
            from_release_id=previous_active,
            to_release_id=release_id,
            scope=record.get("restore_scope"),
            content_ids=record.get("content_ids", []),
            attachment_ids=record.get("attachment_ids", []),
            reason=reason,
        )

        if lock is not None:
            lock.touch()
        self._mark_v3(index)
        # ---- single commit point ----
        _atomic_write(self.index_path, _canonical_bytes(index))

        event_recorded = True
        try:
            self._append_event(
                {
                    "event_id": f"ev_activate_{release_id}",
                    "at": now,
                    "actor": "owner",
                    "action": "activate",
                    "release_id": release_id,
                    "previous_active_release_id": previous_active,
                    "result": "active",
                    "detail": reason or "所有者激活已校验的 prepared Release。",
                    "content_ids": manifest.get("content_ids", []),
                    "attachment_ids": manifest.get("attachment_ids", []),
                    "withdrawn_content_ids": record.get("withdrawn_content_ids", []),
                    "withdrawn_attachment_ids": record.get(
                        "withdrawn_attachment_ids", []
                    ),
                    "restored_from_release_id": record.get(
                        "restored_from_release_id"
                    ),
                    "restore_scope": record.get("restore_scope"),
                    "sequence": record.get("sequence"),
                    "bundle_sha256": record.get("bundle_sha256"),
                }
            )
        except OSError:
            # index.json is authoritative; the missing event is repaired by
            # reconcile_events(). Never roll back the committed activation.
            event_recorded = False

        return {
            "release_id": release_id,
            "status": "active",
            "already_active": False,
            "previous_active_release_id": previous_active,
            "active_release_id": release_id,
            "active_since": now,
            "registry_revision": index["registry_revision"],
            "content_ids": manifest.get("content_ids", []),
            "attachment_ids": manifest.get("attachment_ids", []),
            "bundle_sha256": record.get("bundle_sha256"),
            "audit_event_recorded": event_recorded,
            "audit_event_pending": not event_recorded,
        }

    def _validate_release_against_db(self, manifest: dict[str, Any]) -> None:
        """Re-check a prepared bundle against the current private database."""
        issues: list[dict[str, Any]] = []
        if not self.db_path.is_file():
            issues.append(
                {
                    "code": "database_missing",
                    "severity": "error",
                    "detail": "私有数据库不存在，无法激活。",
                }
            )
            raise ReleaseValidationError(
                "激活前置校验未通过。", issues=issues
            )
        content_items = {
            item["content_id"]: item for item in manifest.get("items", [])
        }
        attachment_items: dict[str, dict[str, Any]] = {}
        for item in manifest.get("items", []):
            for attachment in item.get("attachments", []):
                attachment_items[attachment["attachment_id"]] = attachment
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, state, visibility, visibility_selected, revision FROM content "
                "WHERE id IN ({})".format(
                    ",".join("?" for _ in content_items)
                ),
                tuple(content_items),
            ).fetchall() if content_items else []
            content_rows = {row["id"]: dict(row) for row in rows}
            rows = conn.execute(
                "SELECT id, content_id, state, visibility, byte_size, sha256 FROM attachments "
                "WHERE id IN ({})".format(
                    ",".join("?" for _ in attachment_items)
                ),
                tuple(attachment_items),
            ).fetchall() if attachment_items else []
            attachment_rows = {row["id"]: dict(row) for row in rows}
        finally:
            conn.close()

        for content_id, item in content_items.items():
            row = content_rows.get(content_id)
            if row is None:
                issues.append(
                    {
                        "code": "content_not_found",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 不存在，Release 已过期。",
                    }
                )
                continue
            if row["state"] != "active":
                issues.append(
                    {
                        "code": "content_not_active",
                        "severity": "error",
                        "target": content_id,
                        "detail": f"Content 状态为 {row['state']}。",
                    }
                )
            if row["visibility"] != "public" or row["visibility_selected"] != 1:
                issues.append(
                    {
                        "code": "content_not_public",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 已不再处于所有者选择的公开状态。",
                    }
                )
            if int(row["revision"]) != int(item.get("revision", 0)):
                issues.append(
                    {
                        "code": "content_revision_changed",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 修订号已变化，请重新 prepare。",
                    }
                )

        for attachment_id, item in attachment_items.items():
            row = attachment_rows.get(attachment_id)
            if row is None:
                issues.append(
                    {
                        "code": "attachment_not_found",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件不存在，Release 已过期。",
                    }
                )
                continue
            if row["state"] != "READY":
                issues.append(
                    {
                        "code": "attachment_not_ready",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": f"附件状态为 {row['state']}。",
                    }
                )
            if row["visibility"] != "public":
                issues.append(
                    {
                        "code": "attachment_private",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件已不再公开。",
                    }
                )
            object_path = self.objects_dir / attachment_id
            try:
                object_info = object_path.lstat()
            except FileNotFoundError:
                issues.append(
                    {
                        "code": "attachment_file_missing",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象文件缺失。",
                    }
                )
                continue
            except OSError:
                issues.append(
                    {
                        "code": "attachment_file_unreadable",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象无法检查，拒绝激活。",
                    }
                )
                continue
            if not _is_plain_regular(object_info):
                issues.append(
                    {
                        "code": "attachment_file_not_regular",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象不是普通文件（目录、符号链接或 junction），拒绝激活。",
                    }
                )
                continue
            try:
                digest, size = _hash_file(object_path)
            except OSError:
                issues.append(
                    {
                        "code": "attachment_file_unreadable",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象无法读取，拒绝激活。",
                    }
                )
                continue
            if row["sha256"] != digest:
                issues.append(
                    {
                        "code": "attachment_hash_mismatch",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象 SHA-256 与元数据不一致。",
                    }
                )
            if row["byte_size"] is None or int(row["byte_size"]) != size:
                issues.append(
                    {
                        "code": "attachment_size_mismatch",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象大小与元数据不一致。",
                    }
                )
            if digest != item.get("sha256") or size != int(item.get("bytes", -1)):
                issues.append(
                    {
                        "code": "attachment_bundle_mismatch",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象与 Release 中的副本不一致。",
                    }
                )

        if issues:
            raise ReleaseValidationError(
                "激活前置校验未通过，旧 active Release 保持不变。", issues=issues
            )

    def withdraw(
        self,
        content_ids: list[str] | None = None,
        attachment_ids: list[str] | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw the whole active release or a content subset."""
        if content_ids is None and not attachment_ids:
            return self.withdraw_all(reason=reason)
        if not content_ids and attachment_ids:
            raise ReleaseValidationError(
                "不支持仅撤回附件；请同时撤回其所属 Content。",
                issues=[
                    {
                        "code": "attachment_only_withdrawal_not_supported",
                        "severity": "error",
                        "detail": "附件级独立撤回不在 P2 范围内。",
                    }
                ],
            )
        if not isinstance(content_ids, list):
            raise ReleaseValidationError("content_ids 必须为数组。", field="content_ids")
        if attachment_ids is None:
            attachment_ids = []
        if not isinstance(attachment_ids, list):
            raise ReleaseValidationError(
                "attachment_ids 必须为数组。", field="attachment_ids"
            )

        issues: list[dict[str, Any]] = []
        selected_content: list[str] = []
        for value in content_ids:
            validated = _validate_id(value, "content_ids", issues)
            if validated is not None and validated not in selected_content:
                selected_content.append(validated)
        selected_attachments: list[str] = []
        for value in attachment_ids:
            validated = _validate_id(value, "attachment_ids", issues)
            if validated is not None and validated not in selected_attachments:
                selected_attachments.append(validated)
        if issues:
            raise ReleaseValidationError("撤回选择包含无效标识符。", issues=issues)
        if not selected_content:
            raise ReleaseValidationError(
                "子集撤回必须至少选择一个 Content。",
                issues=[
                    {
                        "code": "empty_content_withdrawal",
                        "severity": "error",
                        "detail": "空选择不产生 successor Bundle。",
                    }
                ],
            )

        with _RegistryLock(self.releases_dir) as lock:
            index = self._load_index()
            self._reconcile_events_locked(index)
            active_id = index["active_release_id"]
            if not active_id:
                raise ReleaseNotFoundError("当前没有 active Release 可撤回。")
            active_record = self._get_record(index, active_id)
            if active_record is None:
                raise ReleaseError("active 指针引用的 Release 记录缺失。", release_id=active_id)
            active_dir = self.releases_dir / active_id
            manifest_path = active_dir / "release.json"
            if not manifest_path.is_file():
                raise ReleaseValidationError(
                    "active Bundle 缺失，无法构建 successor；请使用整体撤回。",
                    issues=[
                        {
                            "code": "active_release_unusable",
                            "severity": "error",
                            "target": active_id,
                            "detail": "active release.json 缺失。",
                        }
                    ],
                )
            inspection = self._inspect_bundle(
                active_dir,
                active_id,
                expected_manifest_sha256=active_record.get("manifest_sha256"),
            )
            if not inspection["ok"]:
                raise ReleaseValidationError(
                    "active Bundle 完整性校验未通过，无法构建 successor；请使用整体撤回。",
                    issues=inspection["issues"],
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            active_content = list(manifest.get("content_ids", []))
            active_attachments = list(manifest.get("attachment_ids", []))

            subset_issues: list[dict[str, Any]] = []
            for content_id in selected_content:
                if content_id not in active_content:
                    subset_issues.append(
                        {
                            "code": "content_not_in_active_release",
                            "severity": "error",
                            "target": content_id,
                            "detail": "该 Content 不在当前 active Release 中。",
                        }
                    )
            attachment_content = {
                attachment["attachment_id"]: item["content_id"]
                for item in manifest.get("items", [])
                for attachment in item.get("attachments", [])
            }
            for attachment_id in selected_attachments:
                if attachment_id not in active_attachments:
                    subset_issues.append(
                        {
                            "code": "attachment_not_in_active_release",
                            "severity": "error",
                            "target": attachment_id,
                            "detail": "该附件不在当前 active Release 中。",
                        }
                    )
                elif attachment_content.get(attachment_id) not in selected_content:
                    subset_issues.append(
                        {
                            "code": "attachment_only_withdrawal_not_supported",
                            "severity": "error",
                            "target": attachment_id,
                            "detail": "附件级独立撤回不在 P2 范围内。",
                        }
                    )
            remaining_content = [
                item for item in active_content if item not in selected_content
            ]
            if not remaining_content:
                subset_issues.append(
                    {
                        "code": "withdrawal_removes_all_content",
                        "severity": "error",
                        "detail": "该子集撤回会清空全部 Content；请使用整体撤回。",
                    }
                )
            if subset_issues:
                raise ReleaseValidationError(
                    "子集撤回选择无效，未生成 successor Bundle。",
                    issues=subset_issues,
                )

            withdrawn_content_set = set(selected_content)
            withdrawn_attachments = sorted(
                set(
                    [
                        attachment_id
                        for attachment_id in active_attachments
                        if attachment_content.get(attachment_id) in withdrawn_content_set
                    ]
                    + selected_attachments
                )
            )
            remaining_attachments = [
                item for item in active_attachments if item not in withdrawn_attachments
            ]

            self._arm_withdrawal(reason or "withdraw_subset")
            successor_id = f"rel_{uuid.uuid4().hex}"
            try:
                result = self._build_successor_locked(
                    index,
                    successor_id,
                    active_id,
                    active_dir,
                    manifest,
                    remaining_content,
                    remaining_attachments,
                    selected_content,
                    withdrawn_attachments,
                    reason,
                    lock=lock,
                )
                activation = self._activate_locked(
                    index,
                    successor_id,
                    reason=reason,
                    verify_private=False,
                    lock=lock,
                )
            except Exception as exc:
                if hasattr(exc, "details") and isinstance(exc.details, dict):
                    exc.details.setdefault("successor_release_id", successor_id)
                closed = self._fail_closed_after_withdrawal(
                    reason=reason, lock=lock
                )
                if hasattr(exc, "details") and isinstance(exc.details, dict):
                    exc.details.setdefault("fail_closed", closed)
                # Keep the marker on any withdrawal failure: public access must
                # remain closed until the owner explicitly resolves the state.
                raise
            self._disarm_withdrawal()
            result.update(
                {
                    "status": activation["status"],
                    "already_active": activation["already_active"],
                    "previous_active_release_id": active_id,
                    "active_release_id": successor_id,
                    "active_since": activation["active_since"],
                    "registry_revision": activation["registry_revision"],
                    "audit_event_recorded": activation["audit_event_recorded"],
                    "audit_event_pending": activation["audit_event_pending"],
                }
            )
            return result

    def _build_successor_locked(
        self,
        index: dict[str, Any],
        successor_id: str,
        parent_id: str,
        parent_dir: Path,
        parent_manifest: dict[str, Any],
        remaining_content: list[str],
        remaining_attachments: list[str],
        withdrawn_content: list[str],
        withdrawn_attachments: list[str],
        reason: str | None,
        *,
        lock: "_RegistryLock",
    ) -> dict[str, Any]:
        parent_items = {
            item["content_id"]: item for item in parent_manifest.get("items", [])
        }
        parent_attachments = {
            attachment["attachment_id"]: attachment
            for item in parent_items.values()
            for attachment in item.get("attachments", [])
        }
        staging = self.releases_dir / f".{successor_id}.{uuid.uuid4().hex}.tmp"
        final = parent_dir.parent / successor_id
        index_committed = False
        files: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        try:
            staging.mkdir(parents=True, exist_ok=False)
            style_bytes = (parent_dir / "assets" / "style.css").read_bytes()
            _write_bytes(staging / "assets" / "style.css", style_bytes)
            files.append(
                {
                    "path": "assets/style.css",
                    "kind": "asset",
                    "bytes": len(style_bytes),
                    "sha256": _sha256_bytes(style_bytes),
                }
            )

            remaining_set = set(remaining_attachments)
            for content_id in remaining_content:
                parent_item = parent_items[content_id]
                payload_data = json.loads(
                    (parent_dir / parent_item["payload_path"]).read_text(encoding="utf-8")
                )
                attachment_items = [
                    dict(attachment)
                    for attachment in parent_item.get("attachments", [])
                    if attachment["attachment_id"] in remaining_set
                ]
                item = {
                    "content_id": content_id,
                    "public_id": parent_item.get("public_id")
                    or public_content_id(content_id),
                    "title": payload_data["title"],
                    "summary": payload_data.get("summary") or "",
                    "body": payload_data["body"],
                    "content_type": payload_data["type"],
                    "primary_space": payload_data["primary_space"],
                    "revision": parent_item["revision"],
                    "created_at": parent_item["created_at"],
                    "updated_at": payload_data["updated_at"],
                    "related": [
                        dict(entry)
                        for entry in parent_item.get("related", [])
                        if entry.get("content_id") in remaining_content
                    ],
                    "attachments": attachment_items,
                }
                payload_bytes = _canonical_bytes(
                    build_content_payload(successor_id, item)
                )
                payload_path = parent_item["payload_path"]
                _write_bytes(staging / payload_path, payload_bytes)
                files.append(
                    {
                        "path": payload_path,
                        "kind": "payload",
                        "bytes": len(payload_bytes),
                        "sha256": _sha256_bytes(payload_bytes),
                    }
                )
                page_bytes = _render_content_page(item, successor_id)
                page_path = parent_item["page_path"]
                _write_bytes(staging / page_path, page_bytes)
                files.append(
                    {
                        "path": page_path,
                        "kind": "content",
                        "bytes": len(page_bytes),
                        "sha256": _sha256_bytes(page_bytes),
                    }
                )
                items.append(
                    {
                        "content_id": content_id,
                        "public_id": item["public_id"],
                        "title": item["title"],
                        "summary": item["summary"],
                        "content_type": item["content_type"],
                        "primary_space": item["primary_space"],
                        "revision": item["revision"],
                        "created_at": item["created_at"],
                        "updated_at": item["updated_at"],
                        "body_sha256": _sha256_bytes(
                            (item["body"] or "").encode("utf-8")
                        ),
                        "body_bytes": len((item["body"] or "").encode("utf-8")),
                        "payload_path": payload_path,
                        "payload_sha256": files[-2]["sha256"],
                        "payload_bytes": files[-2]["bytes"],
                        "page_path": page_path,
                        "page_sha256": files[-1]["sha256"],
                        "space_path": f"spaces/{_space_slug(item['primary_space'])}/index.html",
                        "related": item.get("related", []),
                        "attachments": attachment_items,
                    }
                )

            for attachment_id in remaining_attachments:
                source_entry = next(
                    entry
                    for entry in parent_manifest["files"]
                    if entry["path"] == parent_attachments[attachment_id]["path"]
                )
                relative = source_entry["path"]
                source = parent_dir / Path(relative)
                destination = staging / Path(relative)
                digest, size = self._copy_verified(
                    source,
                    destination,
                    source_entry.get("sha256"),
                    source_entry.get("bytes"),
                )
                files.append(
                    {
                        "path": relative,
                        "kind": "attachment",
                        "bytes": size,
                        "sha256": digest,
                    }
                )

            self._write_site_pages(staging, items, files)
            files.sort(key=lambda entry: entry["path"])
            created_at = utcnow()
            manifest = {
                "format": RELEASE_FORMAT,
                "format_version": RELEASE_FORMAT_VERSION,
                "release_id": successor_id,
                "created_at": created_at,
                "parent_release_id": parent_id,
                "action": "withdraw",
                "status": "prepared",
                "schema_version": SCHEMA_VERSION,
                "app_version": __version__,
                "content_ids": remaining_content,
                "attachment_ids": remaining_attachments,
                "withdrawn_content_ids": withdrawn_content,
                "withdrawn_attachment_ids": withdrawn_attachments,
                "withdrawal_scope": "content_subset",
                "activated_from_release_id": None,
                "reason": reason,
                "items": items,
                "files": files,
                "bundle_sha256": _sha256_bytes(_canonical_bytes({"files": files})),
                "bundle_sha256_scope": "files[] excluding release.json; manifest hash is anchored in index.json",
                "boundaries": {
                    "scope": "owner-selected-public-content",
                    "publishes_content": True,
                    "activated": False,
                    "includes_public_payload": True,
                    "includes_private_content": False,
                    "includes_private_attachments": False,
                    "includes_relations": False,
                    "includes_relation_links": True,
                    "includes_site_index": True,
                    "includes_attributes": False,
                    "includes_database": False,
                    "includes_owner_secret": False,
                    "includes_local_paths": False,
                    "includes_archive_backup": False,
                    "uses_attachment_operations_table": False,
                },
            }
            _write_bytes(staging / "release.json", _canonical_bytes(manifest))
            inspection = self._inspect_bundle(staging, successor_id)
            if not inspection["ok"]:
                raise ReleaseValidationError(
                    "successor Bundle 边界检查未通过。", issues=inspection["issues"]
                )
            if final.exists():
                raise ReleaseError(
                    "Release 目录已存在，拒绝覆盖。", release_id=successor_id
                )
            os.replace(staging, final)
            _fsync_directory(self.releases_dir)

            manifest_sha256, manifest_bytes = _hash_file(final / "release.json")
            record = {
                "release_id": successor_id,
                "created_at": created_at,
                "parent_release_id": parent_id,
                "action": "withdraw",
                "status": "prepared",
                "content_ids": remaining_content,
                "attachment_ids": remaining_attachments,
                "bundle_sha256": manifest["bundle_sha256"],
                "manifest_sha256": manifest_sha256,
                "manifest_bytes": manifest_bytes,
                "file_count": len(files),
                "total_bytes": sum(entry["bytes"] for entry in files),
                "activated_at": None,
                "superseded_at": None,
                "withdrawn_at": None,
                "activated_from_release_id": None,
                "superseded_by_release_id": None,
                "withdrawn_content_ids": withdrawn_content,
                "withdrawn_attachment_ids": withdrawn_attachments,
                "withdrawal_scope": "content_subset",
                "reason": reason,
                "bundle_format_version": RELEASE_FORMAT_VERSION,
                "actor": "owner",
                "restored_from_release_id": None,
                "restore_scope": None,
                "sequence": None,
                "history": [],
            }
            index["registry_revision"] = int(index.get("registry_revision", 0)) + 1
            entry = self._history_entry(
                index,
                record,
                action="build",
                at=created_at,
                registry_revision=index["registry_revision"],
                to_release_id=successor_id,
                scope="content_subset",
                content_ids=withdrawn_content,
                attachment_ids=withdrawn_attachments,
                reason=reason,
            )
            record["sequence"] = entry["seq"]
            index["releases"].append(record)
            index["updated_at"] = created_at
            self._mark_withdrawn(
                index,
                withdrawn_content,
                withdrawn_attachments,
                successor_id,
                reason or "withdraw_subset",
            )
            lock.touch()
            self._mark_v3(index)
            _atomic_write(self.index_path, _canonical_bytes(index))
            index_committed = True
        except Exception:
            _remove_tree(staging)
            if final.exists() and not index_committed:
                _remove_tree(final)
            raise
        return {
            "release_id": successor_id,
            "status": "prepared",
            "parent_release_id": parent_id,
            "action": "withdraw",
            "content_ids": remaining_content,
            "attachment_ids": remaining_attachments,
            "withdrawn_content_ids": withdrawn_content,
            "withdrawn_attachment_ids": withdrawn_attachments,
            "file_count": len(files),
            "total_bytes": record["total_bytes"],
            "bundle_sha256": manifest["bundle_sha256"],
        }

    def _fail_closed_after_withdrawal(
        self, *, reason: str | None, lock: "_RegistryLock"
    ) -> bool:
        """Best-effort emergency close after a failed subset withdrawal.

        Returns True only when the active pointer is confirmed cleared. The
        caller keeps the fail-closed marker when this returns False.
        """
        try:
            fresh = self._load_index()
            if not fresh.get("active_release_id"):
                return True
            self._withdraw_all_locked(
                fresh,
                reason=f"withdrawal_failed: {reason or 'unspecified'}",
                lock=lock,
            )
            return True
        except Exception:
            return False

    def withdraw_all(self, *, reason: str | None = None) -> dict[str, Any]:
        """Clear the active pointer without building a new bundle."""
        self._arm_withdrawal(reason or "withdraw_all")
        try:
            with _RegistryLock(self.releases_dir) as lock:
                index = self._load_index()
                self._reconcile_events_locked(index)
                result = self._withdraw_all_locked(index, reason=reason, lock=lock)
        except Exception:
            # Marker stays: public access remains closed even if the registry
            # could not be updated.
            raise
        self._disarm_withdrawal()
        return result

    def _withdraw_all_locked(
        self, index: dict[str, Any], *, reason: str | None, lock: "_RegistryLock"
    ) -> dict[str, Any]:
        active_id = index["active_release_id"]
        if not active_id:
            return {
                "status": "withdrawn",
                "changed": False,
                "already_withdrawn": True,
                "active_release_id": None,
                "registry_revision": index.get("registry_revision", 0),
                "audit_event_recorded": True,
                "audit_event_pending": False,
            }
        record = self._get_record(index, active_id)
        if record is None:
            raise ReleaseError(
                "active 指针引用的 Release 记录缺失。", release_id=active_id
            )
        now = utcnow()
        record["status"] = "withdrawn"
        record["withdrawn_at"] = now
        record["withdrawal_scope"] = "all"
        record["withdrawn_content_ids"] = list(record.get("content_ids", []))
        record["withdrawn_attachment_ids"] = list(record.get("attachment_ids", []))
        record["reason"] = reason
        index["active_release_id"] = None
        index["active_since"] = None
        index["updated_at"] = now
        index["registry_revision"] = int(index.get("registry_revision", 0)) + 1
        self._history_entry(
            index,
            record,
            action="withdraw",
            at=now,
            registry_revision=index["registry_revision"],
            from_release_id=active_id,
            to_release_id=None,
            scope="all",
            content_ids=record.get("withdrawn_content_ids", []),
            attachment_ids=record.get("withdrawn_attachment_ids", []),
            reason=reason,
        )
        self._mark_withdrawn(
            index,
            record.get("withdrawn_content_ids", []),
            record.get("withdrawn_attachment_ids", []),
            active_id,
            reason or "withdraw_all",
        )
        lock.touch()
        self._mark_v3(index)
        _atomic_write(self.index_path, _canonical_bytes(index))
        event_recorded = True
        try:
            self._append_event(
                {
                    "event_id": f"ev_withdraw_{active_id}",
                    "at": now,
                    "actor": "owner",
                    "action": "withdraw",
                    "release_id": active_id,
                    "previous_active_release_id": active_id,
                    "result": "withdrawn",
                    "detail": reason or "所有者整体撤回当前公开版本。",
                    "withdrawal_scope": "all",
                    "withdrawn_content_ids": record["withdrawn_content_ids"],
                    "withdrawn_attachment_ids": record["withdrawn_attachment_ids"],
                    "bundle_sha256": record.get("bundle_sha256"),
                }
            )
        except OSError:
            event_recorded = False
        return {
            "status": "withdrawn",
            "changed": True,
            "already_withdrawn": False,
            "release_id": active_id,
            "active_release_id": None,
            "registry_revision": index["registry_revision"],
            "withdrawn_content_ids": record["withdrawn_content_ids"],
            "withdrawn_attachment_ids": record["withdrawn_attachment_ids"],
            "audit_event_recorded": event_recorded,
            "audit_event_pending": not event_recorded,
        }

    # ------------------------------------------------------------------
    # Startup / orphan audit
    # ------------------------------------------------------------------
    def audit(self, *, write_report: bool = True) -> dict[str, Any]:
        """Read-only audit of bundles, the registry and leftover entries."""
        index: dict[str, Any] | None = None
        index_error: str | None = None
        try:
            index = self._load_index()
        except ReleaseError as exc:
            index_error = exc.message
        report = self._audit_locked(index, index_error=index_error, write_report=write_report)
        return report

    def startup_audit(self) -> dict[str, Any]:
        """Best-effort startup audit; never blocks application start."""
        try:
            return self.audit(write_report=True)
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "ok": False,
                "error": str(exc),
                "action_taken": "none",
                "blocking_public": True,
            }

    def _audit_locked(
        self,
        index: dict[str, Any] | None,
        *,
        index_error: str | None = None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        root = self.releases_dir
        referenced: set[str] = set()
        if index is not None:
            for record in index["releases"]:
                referenced.add(record["release_id"])

        dirs = (
            [path for path in root.iterdir() if path.is_dir()]
            if root.is_dir()
            else []
        )
        known_entries = {"index.json", "events.jsonl", ".lock", "audit"}
        for path in sorted(dirs, key=lambda item: item.name):
            if path.name == "audit":
                continue
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                findings.append(
                    {
                        "code": "STAGING_LEFTOVER",
                        "target": path.name,
                        "detail": "发现未完成的 staging 目录。",
                    }
                )
                continue
            if not path.name.startswith("rel_"):
                findings.append(
                    {
                        "code": "UNKNOWN_ENTRY",
                        "target": path.name,
                        "detail": "无法识别的 releases/ 条目。",
                    }
                )
                continue
            manifest_path = path / "release.json"
            manifest_ok = False
            if manifest_path.is_file():
                try:
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest_ok = True
                except (json.JSONDecodeError, OSError):
                    manifest_ok = False
            if path.name not in referenced:
                findings.append(
                    {
                        "code": (
                            "UNREFERENCED_BUNDLE"
                            if manifest_ok
                            else "UNREFERENCED_MANIFEST_INVALID"
                        ),
                        "target": path.name,
                        "detail": "未被注册表引用的 Bundle；仅审计，不删除。",
                    }
                )
            else:
                inspection = self._inspect_bundle(
                    path,
                    path.name,
                    expected_manifest_sha256=next(
                        (
                            record.get("manifest_sha256")
                            for record in index["releases"]
                            if record["release_id"] == path.name
                        ),
                        None,
                    ),
                )
                if not inspection["ok"]:
                    findings.append(
                        {
                            "code": "REFERENCED_BUNDLE_INVALID",
                            "target": path.name,
                            "detail": "注册表引用的 Bundle 完整性校验失败。",
                            "issues": inspection["issues"],
                        }
                    )

        active_id = index.get("active_release_id") if index else None
        active_invalid = False
        if index is not None:
            for record in index["releases"]:
                if record.get("status") not in RELEASE_STATUSES:
                    findings.append(
                        {
                            "code": "INDEX_INVARIANT_VIOLATION",
                            "target": record.get("release_id"),
                            "detail": f"未知状态 {record.get('status')}。",
                        }
                    )
            active_records = [
                record for record in index["releases"] if record.get("status") == "active"
            ]
            if len(active_records) > 1:
                findings.append(
                    {
                        "code": "INDEX_INVARIANT_VIOLATION",
                        "target": "active_release_id",
                        "detail": "注册表存在多个 active 记录。",
                    }
                )
                active_invalid = True
            if active_id:
                active_record = next(
                    (
                        record
                        for record in index["releases"]
                        if record["release_id"] == active_id
                    ),
                    None,
                )
                active_dir = root / active_id
                if active_record is None:
                    findings.append(
                        {
                            "code": "INDEX_INVARIANT_VIOLATION",
                            "target": active_id,
                            "detail": "active 指针指向不存在的记录。",
                        }
                    )
                    active_invalid = True
                elif active_record.get("status") != "active":
                    findings.append(
                        {
                            "code": "INDEX_INVARIANT_VIOLATION",
                            "target": active_id,
                            "detail": "active 指针与记录状态不一致。",
                        }
                    )
                    active_invalid = True
                if not active_dir.is_dir():
                    findings.append(
                        {
                            "code": "REFERENCED_BUNDLE_MISSING",
                            "target": active_id,
                            "detail": "active Bundle 目录缺失。",
                        }
                    )
                    active_invalid = True
                else:
                    inspection = self._inspect_bundle(
                        active_dir,
                        active_id,
                        expected_manifest_sha256=(
                            active_record or {}
                        ).get("manifest_sha256"),
                    )
                    if not inspection["ok"]:
                        findings.append(
                            {
                                "code": "ACTIVE_BUNDLE_INVALID",
                                "target": active_id,
                                "detail": "active Bundle 完整性校验失败。",
                                "issues": inspection["issues"],
                            }
                        )
                        active_invalid = True
            else:
                for record in index["releases"]:
                    if record.get("status") == "active":
                        findings.append(
                            {
                                "code": "INDEX_INVARIANT_VIOLATION",
                                "target": record["release_id"],
                                "detail": "记录为 active 但 active 指针为空。",
                            }
                        )
                        active_invalid = True
            for record in index["releases"]:
                if not (root / record["release_id"]).is_dir():
                    findings.append(
                        {
                            "code": "REFERENCED_BUNDLE_MISSING",
                            "target": record["release_id"],
                            "detail": "注册表引用的 Bundle 目录缺失。",
                        }
                    )
            # ---- P3-D lineage and history consistency (read-only) ----
            release_ids = {record["release_id"] for record in index["releases"]}
            children_by_previous: dict[str, list[str]] = {}
            history_owners: dict[int, str] = {}
            history_seqs: list[int] = []
            for record in index["releases"]:
                rid = record["release_id"]
                parent = record.get("parent_release_id")
                if parent and parent not in release_ids:
                    findings.append(
                        {
                            "code": "DANGLING_PARENT_REFERENCE",
                            "target": rid,
                            "detail": f"parent_release_id {parent} 不存在。",
                        }
                    )
                restored = record.get("restored_from_release_id")
                if restored and restored not in release_ids:
                    findings.append(
                        {
                            "code": "DANGLING_RESTORE_SOURCE",
                            "target": rid,
                            "detail": f"restored_from_release_id {restored} 不存在。",
                        }
                    )
                successor = record.get("superseded_by_release_id")
                if successor and successor not in release_ids:
                    findings.append(
                        {
                            "code": "DANGLING_SUCCESSOR_REFERENCE",
                            "target": rid,
                            "detail": f"superseded_by_release_id {successor} 不存在。",
                        }
                    )
                activated_from = record.get("activated_from_release_id")
                if activated_from and activated_from not in release_ids:
                    findings.append(
                        {
                            "code": "DANGLING_ACTIVATION_REFERENCE",
                            "target": rid,
                            "detail": f"activated_from_release_id {activated_from} 不存在。",
                        }
                    )
                if activated_from:
                    children_by_previous.setdefault(activated_from, []).append(rid)

                if restored:
                    source_record = next(
                        (
                            item
                            for item in index["releases"]
                            if item["release_id"] == restored
                        ),
                        None,
                    )
                    if source_record is not None:
                        if record.get("action") != "restore":
                            findings.append(
                                {
                                    "code": "RESTORE_LINEAGE_MISMATCH",
                                    "target": rid,
                                    "detail": "存在恢复来源但 action 不是 restore。",
                                }
                            )
                        if record.get("restore_scope") not in RESTORE_SCOPES:
                            findings.append(
                                {
                                    "code": "RESTORE_LINEAGE_MISMATCH",
                                    "target": rid,
                                    "detail": f"restore_scope 无效: {record.get('restore_scope')}。",
                                }
                            )
                        if not set(record.get("content_ids", [])) <= set(
                            source_record.get("content_ids", [])
                        ):
                            findings.append(
                                {
                                    "code": "RESTORE_LINEAGE_MISMATCH",
                                    "target": rid,
                                    "detail": "恢复内容超出来源 Release 范围。",
                                }
                            )
                        if not set(record.get("attachment_ids", [])) <= set(
                            source_record.get("attachment_ids", [])
                        ):
                            findings.append(
                                {
                                    "code": "RESTORE_LINEAGE_MISMATCH",
                                    "target": rid,
                                    "detail": "恢复附件超出来源 Release 范围。",
                                }
                            )
                        if source_record.get("status") == "prepared" and not source_record.get(
                            "activated_at"
                        ):
                            findings.append(
                                {
                                    "code": "RESTORE_LINEAGE_MISMATCH",
                                    "target": rid,
                                    "detail": "恢复来源从未发布。",
                                }
                            )

                history = record.get("history") or []
                for entry in history:
                    if not isinstance(entry, dict):
                        continue
                    seq = entry.get("seq")
                    if not isinstance(seq, int):
                        continue
                    if seq in history_owners and history_owners[seq] != rid:
                        findings.append(
                            {
                                "code": "SEQUENCE_DUPLICATE",
                                "target": rid,
                                "detail": f"sequence {seq} 与 {history_owners[seq]} 重复。",
                            }
                        )
                    else:
                        history_owners[seq] = rid
                    history_seqs.append(seq)

                cursor = rid
                path_seen: set[str] = set()
                while cursor:
                    if cursor in path_seen:
                        findings.append(
                            {
                                "code": "LINEAGE_CYCLE",
                                "target": rid,
                                "detail": f"parent 链存在环：{cursor}。",
                            }
                        )
                        break
                    path_seen.add(cursor)
                    current = next(
                        (
                            item
                            for item in index["releases"]
                            if item["release_id"] == cursor
                        ),
                        None,
                    )
                    if current is None:
                        break
                    cursor = current.get("parent_release_id")

            for parent_id, children in children_by_previous.items():
                if len(children) > 1:
                    findings.append(
                        {
                            "code": "MULTIPLE_SUCCESSORS",
                            "target": parent_id,
                            "detail": f"同一前序 Release 存在多个后继：{sorted(children)}",
                        }
                    )
            if history_seqs:
                missing = sorted(
                    set(range(1, max(history_seqs) + 1)) - set(history_seqs)
                )
                if missing:
                    findings.append(
                        {
                            "code": "SEQUENCE_GAP",
                            "target": "history",
                            "detail": f"历史序号不连续，缺失：{missing[:10]}",
                        }
                    )
            if self.events_path.is_file():
                try:
                    event_ids = self._read_event_ids()
                except ReleaseError:
                    event_ids = set()
                for event_id in sorted(event_ids):
                    release_reference = None
                    for prefix in ("ev_prepare_", "ev_activate_", "ev_withdraw_"):
                        if event_id.startswith(prefix):
                            release_reference = event_id[len(prefix) :]
                            break
                    if release_reference is not None and release_reference not in referenced:
                        findings.append(
                            {
                                "code": "EVENT_ONLY_RELEASE",
                                "target": event_id,
                                "detail": "事件引用的 Release 不在注册表中；仅审计。",
                            }
                        )
        if root.is_dir():
            for path in root.iterdir():
                if path.is_file() and path.name not in known_entries:
                    findings.append(
                        {
                            "code": "UNKNOWN_ENTRY",
                            "target": path.name,
                            "detail": "releases/ 下存在未知文件。",
                        }
                    )

        pending_events: list[str] = []
        if index is not None:
            try:
                expected = [event["event_id"] for event in self._expected_events(index)]
                existing = self._read_event_ids()
                pending_events = [
                    event_id for event_id in expected if event_id not in existing
                ]
            except ReleaseError:
                pending_events = []

        blocking_public = bool(index_error) or active_invalid
        report: dict[str, Any] = {
            "ok": not findings,
            "generated_at": utcnow(),
            "index_error": index_error,
            "active_release_id": active_id,
            "registry_revision": index.get("registry_revision", 0) if index else None,
            "referenced_release_count": len(referenced),
            "bundles_on_disk": len(
                [path for path in dirs if path.name.startswith("rel_")]
            ),
            "findings": findings,
            "finding_counts": {
                code: sum(1 for item in findings if item["code"] == code)
                for code in sorted({item["code"] for item in findings})
            },
            "audit_event_pending": pending_events,
            "blocking_public": blocking_public,
            "action_taken": "none",
            "report_path": None,
        }
        if write_report:
            try:
                root.mkdir(parents=True, exist_ok=True)
                audit_dir = root / "audit"
                audit_dir.mkdir(parents=True, exist_ok=True)
                stamp = utcnow().replace("-", "").replace(":", "").replace(".", "")
                target = audit_dir / f"audit-{stamp}-{uuid.uuid4().hex[:8]}.json"
                _write_bytes(target, _canonical_bytes(report))
                report["report_path"] = str(target)
            except OSError:
                report["report_path"] = None
        return report

    def backup(self, output_path: Path | str | None = None) -> dict[str, Any]:
        """Create an integrity-checked ZIP of the release registry only.

        The F-03 archive backup does not include ``releases/``. This ZIP is the
        explicit registry backup and excludes ``.lock`` as well as the private
        database, attachment objects, and owner secret.
        """
        with _RegistryLock(self.releases_dir):
            index = self._load_index()
            self._reconcile_events_locked(index)
            if output_path is None:
                self.backups_dir.mkdir(parents=True, exist_ok=True)
                stamp = utcnow().replace("-", "").replace(":", "").replace(".", "")
                target = self.backups_dir / f"release-registry-{stamp}-{uuid.uuid4().hex[:8]}.zip"
            else:
                target = Path(output_path).resolve()
                if target.exists():
                    raise ReleaseError("备份目标已存在，拒绝覆盖。", path=str(target))
                if target.parent == self.releases_dir or self.releases_dir in target.parents:
                    raise ReleaseValidationError(
                        "注册表备份不得写入 releases/ 目录。", path=str(target)
                    )
                target.parent.mkdir(parents=True, exist_ok=True)

            members: list[tuple[str, Path]] = []
            if self.index_path.is_file():
                members.append(("registry/index.json", self.index_path))
            if self.events_path.is_file():
                members.append(("registry/events.jsonl", self.events_path))
            pending = self.withdrawal_pending()
            if pending:
                if not _is_plain_regular(self.withdrawal_marker_path.lstat()):
                    raise ReleaseValidationError("待撤回标记无法安全保存。")
                members.append((f"registry/{WITHDRAWAL_MARKER}", self.withdrawal_marker_path))
            for record in index["releases"]:
                release_dir = self.releases_dir / record["release_id"]
                if not release_dir.is_dir():
                    raise ReleaseError(
                        "注册表引用的 Release 目录缺失。", release_id=record["release_id"]
                    )
                inspection = self._inspect_bundle(
                    release_dir,
                    record["release_id"],
                    expected_manifest_sha256=record.get("manifest_sha256"),
                )
                if not inspection["ok"]:
                    raise ReleaseError(
                        "注册表引用的 Release 完整性校验失败，拒绝备份。",
                        release_id=record["release_id"],
                        issues=inspection["issues"],
                    )
                for path in sorted(p for p in release_dir.rglob("*") if p.is_file()):
                    relative = path.relative_to(release_dir).as_posix()
                    members.append((f"registry/{record['release_id']}/{relative}", path))

            manifest_files = []
            for archive_name, path in members:
                digest, size = _hash_file(path)
                manifest_files.append(
                    {"path": archive_name, "bytes": size, "sha256": digest}
                )
            index_digest = _hash_file(self.index_path)[0] if self.index_path.is_file() else None
            lineage = [
                {
                    "release_id": record["release_id"],
                    "sequence": record.get("sequence"),
                    "status": record.get("status"),
                    "action": record.get("action"),
                    "parent_release_id": record.get("parent_release_id"),
                    "superseded_by_release_id": record.get(
                        "superseded_by_release_id"
                    ),
                    "restored_from_release_id": record.get(
                        "restored_from_release_id"
                    ),
                    "restore_scope": record.get("restore_scope"),
                }
                for record in index["releases"]
            ]
            manifest = {
                "format": REGISTRY_BACKUP_FORMAT,
                "format_version": REGISTRY_BACKUP_FORMAT_VERSION,
                "generated_at": utcnow(),
                "registry_format_version": index.get(
                    "format_version", REGISTRY_FORMAT_VERSION
                ),
                "history_format": index.get("history_format")
                or {"format": HISTORY_FORMAT, "version": HISTORY_FORMAT_VERSION},
                "withdrawal_pending": pending,
                "registry_revision": index.get("registry_revision", 0),
                "registry_sequence": index.get("sequence", 0),
                "active_since": index.get("active_since"),
                "active_release_id": index["active_release_id"],
                "index_sha256": index_digest,
                "release_ids": [item["release_id"] for item in index["releases"]],
                "release_count": len(index["releases"]),
                "lineage": lineage,
                "files": manifest_files,
                "boundaries": {
                    "includes_release_registry": True,
                    "includes_private_database": False,
                    "includes_attachment_objects": False,
                    "includes_owner_secret": False,
                    "includes_archive_backup": False,
                    "includes_lock_file": False,
                },
            }
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with zipfile.ZipFile(
                    tmp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
                ) as archive:
                    archive.writestr("manifest.json", _canonical_bytes(manifest))
                    for archive_name, path in members:
                        archive.write(path, arcname=archive_name)
                _verify_registry_backup(tmp)
                os.link(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            digest, size = _hash_file(target)
            return {
                "ok": True,
                "path": str(target),
                "bytes": size,
                "sha256": digest,
                "file_count": len(manifest_files),
                "active_release_id": index["active_release_id"],
                "release_count": len(index["releases"]),
                "registry_format_version": manifest["registry_format_version"],
                "history_format_version": manifest["history_format"]["version"],
                "lineage_count": len(lineage),
                "boundaries": manifest["boundaries"],
            }

    def restore_backup(self, package: Path | str) -> dict[str, Any]:
        """Restore into a stopped core archive without overwriting history."""
        from .instance_lock import DataDirLock

        if not self.db_path.is_file():
            raise ReleaseValidationError("请先恢复核心归档数据库与附件。")
        package = Path(package)
        with DataDirLock(self.data_dir):
            if self.releases_dir.exists():
                raise ReleaseValidationError("发布目录已存在，拒绝覆盖。")
            temporary = self.data_dir / f".registry-restore-{uuid.uuid4().hex}"
            temporary.mkdir()
            staged = ReleaseRegistry(self.data_dir)
            staged.releases_dir = temporary
            staged.index_path = temporary / "index.json"
            staged.events_path = temporary / "events.jsonl"
            staged.withdrawal_marker_path = temporary / WITHDRAWAL_MARKER
            try:
                with zipfile.ZipFile(package) as archive:
                    manifest = _read_verified_registry_backup(archive)
                    version = manifest.get("format_version")
                    if type(version) is not int or version not in (1, 2, 3, REGISTRY_BACKUP_FORMAT_VERSION):
                        raise ReleaseValidationError("不支持的注册表备份版本。")
                    for entry in manifest["files"]:
                        rel = entry["path"].removeprefix("registry/")
                        if entry["path"] != f"registry/{rel}":
                            raise ReleaseValidationError("注册表备份路径无效。")
                        if rel not in ("index.json", "events.jsonl", WITHDRAWAL_MARKER):
                            release_id, _, bundle_file = rel.partition("/")
                            if not re.fullmatch(r"rel_[a-f0-9]{32}", release_id) or (
                                bundle_file != "release.json" and not is_public_bundle_path(bundle_file)
                            ):
                                raise ReleaseValidationError("注册表备份成员无效。")
                        destination = temporary / rel
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry["path"]) as source, destination.open("xb") as output:
                            shutil.copyfileobj(source, output)
                        if _hash_file(destination) != (entry["sha256"], entry["bytes"]):
                            raise ReleaseValidationError("还原后的成员摘要不一致。")
                index = staged._load_index()
                records = {record["release_id"]: record for record in index["releases"]}
                active = index["active_release_id"]
                for release_id, record in records.items():
                    if not re.fullmatch(r"rel_[a-f0-9]{32}", release_id):
                        raise ReleaseValidationError("注册表版本标识无效。")
                    inspected = staged._inspect_bundle(temporary / release_id, release_id, record.get("manifest_sha256"))
                    if not inspected["ok"]:
                        raise ReleaseValidationError("还原的 Release 未通过校验。", issues=inspected["issues"])
                closed = staged.withdrawal_pending()
                reason = None
                if version < REGISTRY_BACKUP_FORMAT_VERSION:
                    reason = "旧备份未记录待撤回状态"
                elif manifest.get("active_release_id") != active or set(manifest.get("release_ids", [])) != set(records):
                    reason = "备份与注册表状态不一致"
                elif active and not closed:
                    if active not in records or records[active]["status"] != "active":
                        reason = "活动 Release 不一致"
                    else:
                        bundle = staged.get(active)["manifest"]
                        try:
                            staged._validate_release_against_db(bundle)
                            if staged._withdrawal_ledger_hits(index, bundle["content_ids"], bundle["attachment_ids"]):
                                reason = "活动 Release 涉及未重新授权的撤回项"
                        except (ReleaseValidationError, sqlite3.Error):
                            reason = "活动 Release 与恢复后的归档不一致"
                if reason and not closed:
                    staged._arm_withdrawal(reason)
                    closed = True
                os.rename(temporary, self.releases_dir)
                return {"restored": True, "publication_closed": closed, "reason": reason,
                        "active_release_id": active, "release_count": len(records)}
            finally:
                if temporary.is_dir():
                    shutil.rmtree(temporary)

    # ------------------------------------------------------------------
    # Read-only automatic publishing plan (Batch B)
    # ------------------------------------------------------------------
    def publish_plan(self) -> dict[str, Any]:
        """Read-only automatic selection for one-click publishing.

        The plan inherits exactly what the owner has already marked public:
        active contents with ``visibility=public`` and ``visibility_selected=1``
        plus their READY, public attachments. Nothing is rebuilt, written or
        activated here.
        """
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, primary_space, updated_at, revision "
                "FROM content WHERE state = 'active' AND visibility = 'public' "
                "AND visibility_selected = 1 ORDER BY updated_at DESC, id"
            ).fetchall()
            content_ids = [row["id"] for row in rows]
            attachment_rows: list[Any] = []
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                attachment_rows = conn.execute(
                    "SELECT id, content_id, filename, byte_size, state, visibility "
                    f"FROM attachments WHERE content_id IN ({placeholders}) "
                    "ORDER BY created_at, id",
                    tuple(content_ids),
                ).fetchall()
        finally:
            conn.close()

        attachments: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in attachment_rows:
            entry = {
                "attachment_id": row["id"],
                "content_id": row["content_id"],
                "filename": row["filename"],
                "bytes": int(row["byte_size"] or 0),
            }
            if row["state"] != "READY":
                entry["reason"] = f"附件状态为 {row['state']}"
                skipped.append(entry)
            elif row["visibility"] != "public":
                entry["reason"] = "附件为私有"
                skipped.append(entry)
            else:
                attachments.append(entry)
        attachment_ids = [entry["attachment_id"] for entry in attachments]

        active_id: str | None = None
        stale: list[str] = []
        active_content_ids: list[str] = []
        active_attachment_ids: list[str] = []
        withdrawn: set[str] = set()
        try:
            index = self._load_index()
            active_id = index.get("active_release_id") or None
            withdrawn = set(index.get("withdrawals", {}).get("content", {}))
            if active_id:
                manifest = self.get(active_id)["manifest"]
                active_content_ids = manifest["content_ids"]
                active_attachment_ids = manifest["attachment_ids"]
                published = {
                    item["content_id"]: item
                    for item in manifest.get("items", [])
                    if isinstance(item, dict) and isinstance(item.get("content_id"), str)
                }
                for row in rows:
                    item = published.get(row["id"])
                    if item is not None and int(item.get("revision") or 0) != int(
                        row["revision"] or 0
                    ):
                        stale.append(row["id"])
        except ReleaseError:
            active_id = None
            stale = []

        contents = [
            {
                "content_id": row["id"],
                "title": row["title"],
                "primary_space": row["primary_space"],
                "updated_at": row["updated_at"],
                "revision": row["revision"],
            }
            for row in rows
        ]

        if not content_ids:
            return {
                "generated_at": utcnow(),
                "active_release_id": active_id,
                "empty": True,
                "content_ids": [],
                "attachment_ids": [],
                "contents": [],
                "attachments": [],
                "skipped_attachments": skipped,
                "valid": False,
                "issues": [
                    {
                        "code": "no_public_content",
                        "severity": "info",
                        "detail": "当前没有已选择公开的内容，无法发布。",
                    }
                ],
                "totals": {
                    "content_count": 0,
                    "attachment_count": 0,
                    "attachment_bytes": 0,
                },
                "delta": {
                    "added_content_ids": [],
                    "removed_content_ids": active_content_ids,
                    "added_attachment_ids": [],
                    "removed_attachment_ids": active_attachment_ids,
                    "changed_content_ids": [],
                    "same_as_active": False,
                    "has_changes": bool(active_content_ids),
                },
            }

        preview = self.preview_build(content_ids, attachment_ids)
        delta = dict(preview["delta"])
        delta["changed_content_ids"] = stale
        delta["has_changes"] = bool(
            active_id is None
            or delta.get("added_content_ids")
            or delta.get("removed_content_ids")
            or delta.get("added_attachment_ids")
            or delta.get("removed_attachment_ids")
            or stale
        )
        return {
            "generated_at": utcnow(),
            "active_release_id": active_id,
            "empty": False,
            "content_ids": content_ids,
            "attachment_ids": attachment_ids,
            "contents": contents,
            "attachments": attachments,
            "skipped_attachments": skipped,
            "previously_withdrawn": [item for item in content_ids if item in withdrawn],
            "valid": bool(preview["valid"]),
            "issues": preview["issues"],
            "totals": preview["totals"],
            "delta": delta,
        }

    # ------------------------------------------------------------------
    # Read-only publication drift (P6-C)
    # ------------------------------------------------------------------
    def drift_report(self) -> dict[str, Any]:
        """Compare the active Release Bundle with the current private archive.

        Strictly read-only: the registry, bundles, private database, attachment
        objects and audit log are only read. Anything that cannot be read or
        verified is reported as ``uncertain`` instead of being treated as safe.
        """
        summary = {name: 0 for name in PUBLICATION_DRIFT_CATEGORIES}
        report: dict[str, Any] = {
            "read_only": True,
            "generated_at": utcnow(),
            "state": "verified",
            "active_release_id": None,
            "active_since": None,
            "release_count": 0,
            "bundle_verified": None,
            "summary": summary,
            "contents": [],
            "attachments": [],
            "warnings": [],
            "privacy_risk_count": 0,
            "stale_count": 0,
        }
        try:
            index = self._load_index()
        except ArchiveError as exc:
            report["state"] = "uncertain"
            report["warnings"].append(
                {"code": "registry_unreadable", "detail": exc.message}
            )
            return report

        report["release_count"] = len(index["releases"])
        report["withdrawal_required"] = self.withdrawal_pending()
        report["withdrawn_content_ids"] = sorted(
            index.get("withdrawals", {}).get("content", {}).keys()
        )
        report["withdrawn_attachment_ids"] = sorted(
            index.get("withdrawals", {}).get("attachments", {}).keys()
        )
        if report["withdrawal_required"]:
            report["state"] = "uncertain"
            report["warnings"].append(
                {
                    "code": "withdrawal_incomplete",
                    "detail": "存在未完成的撤回标记；公开入口应处于失败关闭状态。",
                }
            )
        active_id = index.get("active_release_id")
        report["active_release_id"] = active_id
        report["active_since"] = index.get("active_since")

        published: dict[str, dict[str, Any]] = {}
        published_attachments: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if active_id:
            record = next(
                (
                    item
                    for item in index["releases"]
                    if item["release_id"] == active_id
                ),
                None,
            )
            if record is None:
                report["state"] = "uncertain"
                report["warnings"].append(
                    {
                        "code": "active_record_missing",
                        "detail": "active 指针指向的 Release 记录在注册表中不存在。",
                    }
                )
            else:
                try:
                    verification = self.verify(active_id)
                except ArchiveError as exc:
                    verification = {"ok": False, "issues": [{"detail": exc.message}]}
                report["bundle_verified"] = bool(verification.get("ok"))
                if not verification.get("ok"):
                    report["state"] = "uncertain"
                    report["warnings"].append(
                        {
                            "code": "bundle_integrity_failed",
                            "detail": "active Release Bundle 完整性校验未通过，差异结论不可信。",
                            "issues": verification.get("issues") or [],
                        }
                    )
                manifest = self._read_manifest_readonly(active_id)
                if manifest is None:
                    report["state"] = "uncertain"
                    report["warnings"].append(
                        {
                            "code": "bundle_manifest_unreadable",
                            "detail": "active Release Bundle 的清单无法读取。",
                        }
                    )
                else:
                    for item in manifest.get("items") or []:
                        if isinstance(item, dict) and item.get("content_id"):
                            published[item["content_id"]] = item
                            for attachment in item.get("attachments") or []:
                                if isinstance(attachment, dict):
                                    published_attachments.append((item, attachment))

        try:
            rows, attachment_rows = self._read_drift_state(
                list(published), [entry[1].get("attachment_id") for entry in published_attachments]
            )
        except ArchiveError as exc:
            report["state"] = "uncertain"
            report["warnings"].append(
                {"code": "database_unreadable", "detail": exc.message}
            )
            report["summary"] = summary
            return report

        def record(category: str, **fields: Any) -> None:
            summary[category] += 1
            report["contents"].append({"category": category, **fields})

        for content_id, item in published.items():
            row = rows.get(content_id)
            base = {
                "content_id": content_id,
                "published_title": item.get("title") or "",
                "published_revision": item.get("revision"),
                "published_updated_at": item.get("updated_at"),
            }
            if row is None:
                record(
                    "PUBLISHED_BUT_DELETED",
                    current_title=None,
                    current_revision=None,
                    detail="公开副本仍在线，但当前归档中已找不到该内容。",
                    **base,
                )
                continue
            base["current_title"] = row["title"]
            base["current_revision"] = row["revision"]
            if row["state"] != "active":
                record(
                    "PUBLISHED_BUT_DELETED",
                    detail="公开副本仍在线，但该内容在归档中已被删除。",
                    **base,
                )
                continue
            if not int(row["visibility_selected"] or 0):
                record(
                    "PUBLISHED_BUT_DESELECTED",
                    detail="公开副本仍在线，但该内容已取消『选择公开』。",
                    **base,
                )
                continue
            if row["visibility"] != "public":
                record(
                    "PUBLISHED_BUT_NOW_PRIVATE",
                    detail="公开副本仍在线，但该内容已改为私有。",
                    **base,
                )
                continue
            body_digest = _sha256_bytes((row["body"] or "").encode("utf-8"))
            changed = (
                row["title"] != (item.get("title") or "")
                or body_digest != item.get("body_sha256")
                or int(row["revision"] or 0) != int(item.get("revision") or 0)
            )
            if changed:
                record(
                    "PUBLISHED_SNAPSHOT_STALE",
                    detail="公开页面仍是上一次发布的快照，此后内容已被编辑。",
                    **base,
                )
            else:
                record("PUBLISHED_AND_CURRENT", detail="公开副本与当前内容一致。", **base)

        for parent_item, attachment in published_attachments:
            attachment_id = attachment.get("attachment_id")
            row = attachment_rows.get(attachment_id)
            entry: dict[str, Any] = {
                "attachment_id": attachment_id,
                "content_id": parent_item.get("content_id"),
                "published_filename": attachment.get("source_filename")
                or attachment.get("filename"),
                "published_bytes": attachment.get("bytes"),
                "current_filename": row["filename"] if row else None,
                "current_state": row["state"] if row else None,
                "current_visibility": row["visibility"] if row else None,
            }
            reasons: list[str] = []
            if row is None:
                reasons.append("附件在当前归档中已不存在")
            else:
                if row["content_id"] != parent_item.get("content_id"):
                    reasons.append("附件已归属到其它内容")
                if row["state"] != "READY":
                    reasons.append(f"附件当前状态为 {row['state']}")
                if row["visibility"] != "public":
                    reasons.append("附件当前为私有")
                try:
                    digest, size = _hash_file(self.objects_dir / str(attachment_id))
                except OSError:
                    reasons.append("附件对象文件缺失")
                else:
                    if size != int(attachment.get("bytes") or -1):
                        reasons.append("附件对象大小与已发布副本不一致")
                    elif attachment.get("sha256") and digest != attachment["sha256"]:
                        reasons.append("附件对象哈希与已发布副本不一致")
            if reasons:
                summary["PUBLISHED_ATTACHMENT_DRIFT"] += 1
                entry["reasons"] = reasons
                report["attachments"].append(entry)

        if active_id is None:
            category = "NOT_IN_ACTIVE_RELEASE"
            detail = "当前没有生效的发布版本，该内容尚未公开展示。"
        else:
            category = "SELECTED_BUT_NOT_PUBLISHED"
            detail = "该内容已选择公开，但未被当前生效的发布版本收录。"
        for content_id, row in rows.items():
            if content_id in published:
                continue
            if row["state"] != "active" or row["visibility"] != "public":
                continue
            if not int(row["visibility_selected"] or 0):
                continue
            record(
                category,
                content_id=content_id,
                current_title=row["title"],
                current_revision=row["revision"],
                published_title=None,
                published_revision=None,
                published_updated_at=None,
                detail=detail,
            )

        report["privacy_risk_count"] = (
            summary["PUBLISHED_BUT_NOW_PRIVATE"]
            + summary["PUBLISHED_BUT_DELETED"]
            + summary["PUBLISHED_BUT_DESELECTED"]
            + summary["PUBLISHED_ATTACHMENT_DRIFT"]
        )
        report["stale_count"] = summary["PUBLISHED_SNAPSHOT_STALE"]
        return report

    # ------------------------------------------------------------------
    # Unified publication state (Batch C)
    # ------------------------------------------------------------------
    PUBLICATION_STATE_LABELS = {
        "private": "私有",
        "public_not_selected": "公开但未选择发布",
        "selected_not_published": "已选择公开，尚未发布",
        "published": "已发布到本机",
        "published_stale": "内容已修改，等待更新",
        "withdrawn": "已从本机撤回",
        "pending_removal": "等待从公开网站移除",
        "deleted": "已移入回收站",
    }

    def publication_state(self) -> dict[str, Any]:
        """One authoritative per-content publication state for every UI.

        Derived from the drift report (active bundle vs private database) and
        the withdrawal ledger -- never inferred in the browser. Failure is
        reported at the operation level: a prepared release that never became
        active is the persisted evidence of an incomplete publish.
        """
        report = self.drift_report()
        categories: dict[str, str] = {}
        details: dict[str, str] = {}
        for entry in report.get("contents") or []:
            content_id = entry.get("content_id")
            if not content_id:
                continue
            categories[content_id] = entry.get("category") or ""
            details[content_id] = entry.get("detail") or ""

        withdrawn = set(report.get("withdrawn_content_ids") or [])
        published_revisions: dict[str, Any] = {}
        active_id = report.get("active_release_id")
        if active_id:
            manifest = self._read_manifest_readonly(active_id)
            for item in (manifest or {}).get("items") or []:
                if isinstance(item, dict) and item.get("content_id"):
                    published_revisions[item["content_id"]] = item.get("revision")

        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, primary_space, visibility, visibility_selected, "
                "revision, state, updated_at FROM content "
                "ORDER BY updated_at DESC, id"
            ).fetchall()
        finally:
            conn.close()

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        counts = {key: 0 for key in self.PUBLICATION_STATE_LABELS}
        for row in rows:
            content_id = row["id"]
            seen.add(content_id)
            category = categories.get(content_id, "")
            selected = bool(int(row["visibility_selected"] or 0))
            public = row["visibility"] == "public"
            if category in (
                "PUBLISHED_BUT_NOW_PRIVATE",
                "PUBLISHED_BUT_DESELECTED",
                "PUBLISHED_BUT_DELETED",
            ):
                state = "pending_removal"
            elif row["state"] != "active":
                state = "deleted"
            elif category == "PUBLISHED_SNAPSHOT_STALE":
                state = "published_stale"
            elif category == "PUBLISHED_AND_CURRENT":
                state = "published"
            elif content_id in withdrawn:
                state = "withdrawn"
            elif selected and public:
                state = "selected_not_published"
            elif public:
                state = "public_not_selected"
            else:
                state = "private"
            counts[state] += 1
            entries.append(
                {
                    "content_id": content_id,
                    "title": row["title"],
                    "primary_space": row["primary_space"],
                    "updated_at": row["updated_at"],
                    "current_revision": row["revision"],
                    "published_revision": published_revisions.get(content_id),
                    "withdrawn_before": content_id in withdrawn,
                    "selected_for_publication": row["state"] == "active" and selected and public,
                    "state": state,
                    "label": self.PUBLICATION_STATE_LABELS[state],
                    "detail": details.get(content_id) or "",
                    "public_url": (
                        f"/p/{content_id}"
                        if state in ("published", "published_stale")
                        else None
                    ),
                }
            )

        # Published copies whose source content no longer exists.
        for content_id, category in categories.items():
            if content_id in seen:
                continue
            state = "pending_removal" if category == "PUBLISHED_BUT_DELETED" else "deleted"
            counts[state] += 1
            entries.append(
                {
                    "content_id": content_id,
                    "title": "",
                    "primary_space": None,
                    "updated_at": None,
                    "current_revision": None,
                    "published_revision": published_revisions.get(content_id),
                    "withdrawn_before": False,
                    "state": state,
                    "label": self.PUBLICATION_STATE_LABELS[state],
                    "detail": details.get(content_id) or "",
                    "public_url": None,
                }
            )

        failed_attempt = None
        try:
            index = self._load_index()
        except ArchiveError:
            index = {"releases": []}
        prepared = [
            record
            for record in index.get("releases", [])
            if record.get("status") == "prepared"
            and (
                not report.get("active_since")
                or (record.get("created_at") or "") > (report.get("active_since") or "")
            )
        ]
        if prepared:
            latest = sorted(
                prepared, key=lambda record: record.get("created_at") or ""
            )[-1]
            failed_attempt = {
                "release_id": latest.get("release_id"),
                "created_at": latest.get("created_at"),
                "content_count": latest.get("content_count"),
                "action": latest.get("action") or "prepare",
                "message": "上次发布没有完成：静态版本已生成，但未激活为公开网站。",
            }

        return {
            "generated_at": utcnow(),
            "release": {
                "active_release_id": active_id,
                "active_since": report.get("active_since"),
                "release_count": report.get("release_count", 0),
                "state": report.get("state"),
                "withdrawal_required": bool(report.get("withdrawal_required")),
                "bundle_verified": report.get("bundle_verified"),
                "warnings": report.get("warnings") or [],
            },
            "deployment": {"status": "not_configured"},
            "last_failed_attempt": failed_attempt,
            "counts": counts,
            "contents": entries,
        }

    def _read_manifest_readonly(self, release_id: str) -> dict[str, Any] | None:
        path = self.releases_dir / release_id / "release.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def _read_drift_state(
        self, content_ids: list[str], attachment_ids: list[Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read content and attachment rows without ever writing to the DB."""
        if not self.db_path.is_file():
            raise ReleaseError(
                "私有数据库不存在，无法比对发布状态。", path=str(self.db_path)
            )
        try:
            uri = "file:" + quote(self.db_path.as_posix(), safe="/:") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise ReleaseError("私有数据库无法以只读方式打开。", reason=str(exc)) from exc
        try:
            rows = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT id, title, body, visibility, visibility_selected, "
                    "revision, state FROM content"
                )
            }
            wanted = {value for value in attachment_ids if value}
            attachments = {}
            if wanted:
                placeholders = ",".join("?" for _ in wanted)
                for row in conn.execute(
                    "SELECT id, content_id, filename, byte_size, sha256, mime, "
                    "visibility, state FROM attachments "
                    f"WHERE id IN ({placeholders})",
                    tuple(wanted),
                ):
                    attachments[row["id"]] = row
            return rows, attachments
        except sqlite3.Error as exc:
            raise ReleaseError("读取私有数据库失败。", reason=str(exc)) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Selection and validation
    # ------------------------------------------------------------------
    def _read_selection(
        self, content_ids: list[str], attachment_ids: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.db_path.is_file():
            raise ReleaseError("私有数据库不存在，无法构建 Release。", path=str(self.db_path))
        conn = connect(self.db_path)
        try:
            content_rows: dict[str, Any] = {}
            if content_ids:
                placeholders = ",".join("?" for _ in content_ids)
                rows = conn.execute(
                    "SELECT id, title, summary, body, type AS content_type, primary_space, "
                    "visibility, visibility_selected, state, "
                    "revision, created_at, updated_at FROM content "
                    f"WHERE id IN ({placeholders})",
                    tuple(content_ids),
                ).fetchall()
                content_rows = {row["id"]: dict(row) for row in rows}
            attachment_rows: dict[str, Any] = {}
            if attachment_ids:
                placeholders = ",".join("?" for _ in attachment_ids)
                rows = conn.execute(
                    "SELECT id, content_id, filename, mime, byte_size, sha256, state, "
                    "visibility, created_at FROM attachments "
                    f"WHERE id IN ({placeholders})",
                    tuple(attachment_ids),
                ).fetchall()
                attachment_rows = {row["id"]: dict(row) for row in rows}
            return content_rows, attachment_rows
        finally:
            conn.close()

    def _validate_selection(
        self,
        content_ids: list[str],
        attachment_ids: list[str],
        content_rows: dict[str, Any],
        attachment_rows: dict[str, Any],
    ) -> None:
        issues: list[dict[str, Any]] = []
        for content_id in content_ids:
            row = content_rows.get(content_id)
            if row is None:
                issues.append(
                    {
                        "code": "content_not_found",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 不存在。",
                    }
                )
                continue
            if row["state"] != "active":
                issues.append(
                    {
                        "code": "content_not_active",
                        "severity": "error",
                        "target": content_id,
                        "detail": f"Content 状态为 {row['state']}，不能发布。",
                    }
                )
            if row["visibility"] != "public" or row["visibility_selected"] != 1:
                issues.append(
                    {
                        "code": "content_not_public",
                        "severity": "error",
                        "target": content_id,
                        "detail": "Content 未处于所有者选择的公开状态。",
                    }
                )

        content_set = set(content_ids)
        for attachment_id in attachment_ids:
            row = attachment_rows.get(attachment_id)
            if row is None:
                issues.append(
                    {
                        "code": "attachment_not_found",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件不存在。",
                    }
                )
                continue
            if row["content_id"] not in content_set:
                issues.append(
                    {
                        "code": "attachment_content_not_selected",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件所属 Content 未被选择。",
                    }
                )
                continue
            if row["state"] != "READY":
                issues.append(
                    {
                        "code": "attachment_not_ready",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": f"附件状态为 {row['state']}，不能发布。",
                    }
                )
            if row["visibility"] != "public":
                issues.append(
                    {
                        "code": "attachment_private",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件未处于公开状态。",
                    }
                )
            # READY must carry complete, non-NULL integrity metadata. A missing
            # hash or size is a metadata defect, never a reason to skip the
            # verification that protects the published bytes.
            recorded_sha256 = row["sha256"]
            recorded_size = row["byte_size"]
            if (
                not isinstance(recorded_sha256, str)
                or _SHA256_PATTERN.fullmatch(recorded_sha256) is None
                or recorded_size is None
            ):
                issues.append(
                    {
                        "code": "attachment_metadata_incomplete",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件元数据缺少 SHA-256 或大小，无法校验发布字节。",
                    }
                )
                continue
            object_path = self.objects_dir / attachment_id
            try:
                object_info = object_path.lstat()
            except FileNotFoundError:
                issues.append(
                    {
                        "code": "attachment_file_missing",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象文件缺失。",
                    }
                )
                continue
            except OSError:
                issues.append(
                    {
                        "code": "attachment_file_unreadable",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象无法检查，拒绝发布。",
                    }
                )
                continue
            if not _is_plain_regular(object_info):
                issues.append(
                    {
                        "code": "attachment_file_not_regular",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象不是普通文件（目录、符号链接或 junction），拒绝发布。",
                    }
                )
                continue
            try:
                digest, size = _hash_file(object_path)
            except OSError:
                issues.append(
                    {
                        "code": "attachment_file_unreadable",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象无法读取，拒绝发布。",
                    }
                )
                continue
            if recorded_sha256 != digest:
                issues.append(
                    {
                        "code": "attachment_hash_mismatch",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象 SHA-256 与元数据不一致。",
                    }
                )
            if int(recorded_size) != size:
                issues.append(
                    {
                        "code": "attachment_size_mismatch",
                        "severity": "error",
                        "target": attachment_id,
                        "detail": "附件对象大小与元数据不一致。",
                    }
                )

        if issues:
            raise ReleaseValidationError(
                "发布选择未通过状态与边界校验，未生成 Release。", issues=issues
            )

    def _read_relations(self, content_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Relations whose *both* endpoints are in the published selection.

        Private relations are never projected: the query is restricted to the
        selected ids and the result only carries the peer's public identity.
        """
        if not content_ids:
            return {}
        conn = connect(self.db_path)
        try:
            placeholders = ",".join("?" for _ in content_ids)
            rows = conn.execute(
                "SELECT from_id, to_id, type FROM relations "
                f"WHERE from_id IN ({placeholders}) AND to_id IN ({placeholders}) "
                "ORDER BY type, from_id, to_id",
                tuple(content_ids) * 2,
            ).fetchall()
        finally:
            conn.close()
        selected = set(content_ids)
        relations: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            from_id, to_id, rel_type = row["from_id"], row["to_id"], row["type"]
            for owner, peer, direction in (
                (from_id, to_id, "from"),
                (to_id, from_id, "to"),
            ):
                if owner not in selected or peer not in selected:
                    continue
                relations.setdefault(owner, []).append(
                    {
                        "content_id": peer,
                        "type": rel_type,
                        "label": _RELATION_LABELS.get((rel_type, direction), rel_type),
                        "direction": direction,
                    }
                )
        return relations

    def _write_site_pages(
        self, staging: Path, items: list[dict[str, Any]], files: list[dict[str, Any]]
    ) -> None:
        """Write the bundle's public home and per-space index pages."""
        if not items:
            return
        spaces = sorted({_space_slug(item.get("primary_space")) for item in items})
        home_bytes = _render_home_page(items, spaces)
        _write_bytes(staging / "index.html", home_bytes)
        files.append(
            {
                "path": "index.html",
                "kind": "site",
                "bytes": len(home_bytes),
                "sha256": _sha256_bytes(home_bytes),
            }
        )
        for space in spaces:
            space_items = [
                item
                for item in items
                if _space_slug(item.get("primary_space")) == space
            ]
            page_bytes = _render_space_page(space, space_items, spaces)
            relative = f"spaces/{space}/index.html"
            _write_bytes(staging / relative, page_bytes)
            files.append(
                {
                    "path": relative,
                    "kind": "site",
                    "bytes": len(page_bytes),
                    "sha256": _sha256_bytes(page_bytes),
                }
            )

    def _write_bundle(
        self,
        staging: Path,
        release_id: str,
        content_ids: list[str],
        attachment_ids: list[str],
        content_rows: dict[str, Any],
        attachment_rows: dict[str, Any],
    ) -> dict[str, Any]:
        staging.mkdir(parents=True, exist_ok=False)
        files: list[dict[str, Any]] = []

        style_bytes = STYLE_CSS.encode("utf-8")
        _write_bytes(staging / "assets" / "style.css", style_bytes)
        files.append(
            {
                "path": "assets/style.css",
                "kind": "asset",
                "bytes": len(style_bytes),
                "sha256": _sha256_bytes(style_bytes),
            }
        )

        attachments_by_content: dict[str, list[dict[str, Any]]] = {}
        for attachment_id in attachment_ids:
            row = dict(attachment_rows[attachment_id])
            row["safe_filename"] = _safe_filename(row["filename"])
            attachments_by_content.setdefault(row["content_id"], []).append(row)

        relations = self._read_relations(content_ids)
        items: list[dict[str, Any]] = []
        for content_id in content_ids:
            row = content_rows[content_id]
            attachment_items: list[dict[str, Any]] = []
            for attachment in attachments_by_content.get(content_id, []):
                attachment_id = attachment["id"]
                attachment_public_id = public_attachment_id(attachment_id)
                relative = (
                    f"attachments/{attachment_public_id}/{attachment['safe_filename']}"
                )
                destination = staging / Path(relative)
                source = self.objects_dir / attachment_id
                copy_digest, copy_size = self._copy_verified(
                    source, destination, attachment["sha256"], attachment["byte_size"]
                )
                files.append(
                    {
                        "path": relative,
                        "kind": "attachment",
                        "bytes": copy_size,
                        "sha256": copy_digest,
                    }
                )
                attachment_items.append(
                    {
                        "attachment_id": attachment_id,
                        "public_id": attachment_public_id,
                        "filename": attachment["safe_filename"],
                        "source_filename": attachment["filename"],
                        "mime": attachment["mime"],
                        "bytes": copy_size,
                        "sha256": copy_digest,
                        "path": relative,
                        "public_path": relative,
                    }
                )
            item = {
                "content_id": content_id,
                "public_id": public_content_id(content_id),
                "title": row["title"],
                "summary": row["summary"] or "",
                "body": row["body"],
                "content_type": row["content_type"],
                "primary_space": row["primary_space"],
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "body_sha256": _sha256_bytes((row["body"] or "").encode("utf-8")),
                "body_bytes": len((row["body"] or "").encode("utf-8")),
                "payload_path": f"content/{public_content_id(content_id)}.json",
                "page_path": f"content/{public_content_id(content_id)}.html",
                "related": [
                    {
                        "content_id": entry["content_id"],
                        "public_id": public_content_id(entry["content_id"]),
                        "title": (content_rows.get(entry["content_id"]) or {}).get("title")
                        or "",
                        "type": entry["type"],
                        "label": entry["label"],
                    }
                    for entry in relations.get(content_id, [])
                    if entry["content_id"] in content_rows
                ],
                "attachments": attachment_items,
            }
            payload_bytes = _canonical_bytes(build_content_payload(release_id, item))
            _write_bytes(staging / item["payload_path"], payload_bytes)
            files.append(
                {
                    "path": item["payload_path"],
                    "kind": "payload",
                    "bytes": len(payload_bytes),
                    "sha256": _sha256_bytes(payload_bytes),
                }
            )
            page_bytes = _render_content_page(item, release_id)
            _write_bytes(staging / item["page_path"], page_bytes)
            files.append(
                {
                    "path": item["page_path"],
                    "kind": "content",
                    "bytes": len(page_bytes),
                    "sha256": _sha256_bytes(page_bytes),
                }
            )
            items.append(
                {
                    "content_id": content_id,
                    "public_id": item["public_id"],
                    "title": row["title"],
                    "summary": item["summary"],
                    "content_type": item["content_type"],
                    "primary_space": item["primary_space"],
                    "revision": row["revision"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "body_sha256": item["body_sha256"],
                    "body_bytes": item["body_bytes"],
                    "payload_path": item["payload_path"],
                    "payload_sha256": files[-2]["sha256"],
                    "payload_bytes": files[-2]["bytes"],
                    "page_path": item["page_path"],
                    "page_sha256": files[-1]["sha256"],
                    "space_path": f"spaces/{_space_slug(item['primary_space'])}/index.html",
                    "related": item["related"],
                    "attachments": attachment_items,
                }
            )

        self._write_site_pages(staging, items, files)
        files.sort(key=lambda entry: entry["path"])
        bundle_sha256 = _sha256_bytes(_canonical_bytes({"files": files}))
        manifest = {
            "format": RELEASE_FORMAT,
            "format_version": RELEASE_FORMAT_VERSION,
            "release_id": release_id,
            "created_at": utcnow(),
            "parent_release_id": None,
            "action": "prepare",
            "status": "prepared",
            "schema_version": SCHEMA_VERSION,
            "app_version": __version__,
            "content_ids": content_ids,
            "attachment_ids": attachment_ids,
            "withdrawn_content_ids": [],
            "withdrawn_attachment_ids": [],
            "withdrawal_scope": None,
            "activated_from_release_id": None,
            "items": items,
            "files": files,
            "bundle_sha256": bundle_sha256,
            "bundle_sha256_scope": "files[] excluding release.json; manifest hash is anchored in index.json",
            "boundaries": {
                "scope": "owner-selected-public-content",
                "publishes_content": True,
                "activated": False,
                "includes_public_payload": True,
                "includes_private_content": False,
                "includes_private_attachments": False,
                "includes_relations": False,
                "includes_attributes": False,
                "includes_database": False,
                "includes_owner_secret": False,
                "includes_local_paths": False,
                "includes_archive_backup": False,
                "uses_attachment_operations_table": False,
            },
        }
        _write_bytes(staging / "release.json", _canonical_bytes(manifest))
        return manifest

    def _copy_verified(
        self,
        source: Path,
        destination: Path,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> tuple[str, int]:
        # A published bundle may only carry bytes copied from a plain file. The
        # expected integrity metadata is mandatory: a NULL hash or size fails the
        # build instead of silently degrading to an unchecked copy.
        if (
            not isinstance(expected_sha256, str)
            or _SHA256_PATTERN.fullmatch(expected_sha256) is None
            or expected_size is None
        ):
            raise ReleaseValidationError(
                "附件元数据不完整，拒绝复制发布字节。", target=source.name
            )
        expected_size = int(expected_size)
        pre = _lstat_regular(source)
        if pre is None:
            raise ReleaseValidationError(
                "附件对象不是普通文件（目录、符号链接或 junction），拒绝复制。",
                target=source.name,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, destination.open("xb") as out:
                opened = os.fstat(src.fileno())
                # The path may have been swapped between lstat() and open().
                # fstat() describes the file actually opened; anything other than
                # the same plain file is refused rather than followed.
                if not _is_plain_regular(opened) or _file_identity(opened) != _file_identity(pre):
                    raise ReleaseValidationError(
                        "附件对象在检查与打开之间被替换，拒绝复制。", target=source.name
                    )
                for chunk in iter(lambda: src.read(1024 * 256), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
                after = os.fstat(src.fileno())
                if after.st_size != opened.st_size or after.st_mtime_ns != opened.st_mtime_ns:
                    raise ReleaseValidationError(
                        "附件在复制过程中被修改，拒绝复制。", target=source.name
                    )
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise ReleaseValidationError(
                "复制附件对象失败。", source=source.name, reason=str(exc)
            ) from exc
        # The destination directory entry must be durable before the bundle is
        # promoted as a whole.
        _fsync_directory(destination.parent)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise ReleaseValidationError(
                "附件在构建过程中发生变化，SHA-256 不一致。", target=source.name
            )
        if expected_size != size:
            raise ReleaseValidationError(
                "附件在构建过程中发生变化，大小不一致。", target=source.name
            )
        return actual, size

    # ------------------------------------------------------------------
    # Integrity and boundary inspection
    # ------------------------------------------------------------------
    def _inspect_bundle(
        self,
        release_dir: Path,
        release_id: str,
        expected_manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        manifest_path = release_dir / "release.json"
        if not manifest_path.is_file():
            return {
                "ok": False,
                "issues": [
                    {
                        "code": "manifest_missing",
                        "severity": "error",
                        "detail": "release.json 缺失。",
                    }
                ],
                "checked_files": 0,
                "bundle_sha256": None,
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReleaseError("release.json 无法解析。", reason=str(exc)) from exc

        if manifest.get("format") != RELEASE_FORMAT:
            issues.append(
                {
                    "code": "manifest_format_mismatch",
                    "severity": "error",
                    "detail": "Release 清单格式不匹配。",
                }
            )
        if manifest.get("release_id") != release_id:
            issues.append(
                {
                    "code": "manifest_release_id_mismatch",
                    "severity": "error",
                    "detail": "Release 清单 ID 与目录不一致。",
                }
            )
        manifest_sha256, _manifest_bytes = _hash_file(manifest_path)
        if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
            issues.append(
                {
                    "code": "manifest_sha256_mismatch",
                    "severity": "error",
                    "detail": "Release 清单与注册表锚定的哈希不一致。",
                }
            )

        declared = {
            entry["path"]: entry
            for entry in manifest.get("files", [])
            if isinstance(entry, dict) and "path" in entry
        }
        # Every bundled entry must be a plain regular file. A symlink, junction
        # or other reparse point inside the bundle would make the manifest
        # describe bytes that are not actually stored in the bundle.
        actual_files: list[str] = []
        for path in release_dir.rglob("*"):
            if path == manifest_path:
                continue
            try:
                info = path.lstat()
            except OSError:
                issues.append(
                    {
                        "code": "bundle_file_unreadable",
                        "severity": "error",
                        "path": path.relative_to(release_dir).as_posix(),
                        "detail": "产物条目无法检查。",
                    }
                )
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            if not _is_plain_regular(info):
                issues.append(
                    {
                        "code": "bundle_file_not_regular",
                        "severity": "error",
                        "path": path.relative_to(release_dir).as_posix(),
                        "detail": "产物包含非普通文件（符号链接或 junction）。",
                    }
                )
                continue
            actual_files.append(path.relative_to(release_dir).as_posix())
        actual_files.sort()
        expected_files = sorted(declared)
        for path in sorted(set(expected_files) - set(actual_files)):
            issues.append(
                {
                    "code": "declared_file_missing",
                    "severity": "error",
                    "path": path,
                    "detail": "清单声明的文件缺失。",
                }
            )
        for path in sorted(set(actual_files) - set(expected_files)):
            issues.append(
                {
                    "code": "undeclared_file",
                    "severity": "error",
                    "path": path,
                    "detail": "产物中存在未声明的文件。",
                }
            )

        for path, entry in declared.items():
            if not is_public_bundle_path(path):
                issues.append(
                    {
                        "code": "path_not_allowed",
                        "severity": "error",
                        "path": path,
                        "detail": "产物路径不在允许的公开范围内。",
                    }
                )
                continue
            target = release_dir / Path(path)
            if _lstat_regular(target) is None:
                # Missing and non-regular entries were reported above.
                continue
            try:
                digest, size = _hash_file(target)
            except OSError:
                issues.append(
                    {
                        "code": "bundle_file_unreadable",
                        "severity": "error",
                        "path": path,
                        "detail": "产物文件无法读取。",
                    }
                )
                continue
            if digest != entry.get("sha256") or size != entry.get("bytes"):
                issues.append(
                    {
                        "code": "bundle_file_mismatch",
                        "severity": "error",
                        "path": path,
                        "detail": "文件内容或大小与清单不一致。",
                    }
                )

        if manifest.get("format_version") in (2, 3):
            for item in manifest.get("items", []):
                public_id = item.get("public_id")
                if not isinstance(public_id, str) or not public_id.startswith("p_"):
                    issues.append(
                        {
                            "code": "public_id_missing",
                            "severity": "error",
                            "detail": "v2 Bundle 条目缺少公开内容标识。",
                        }
                    )
                elif item.get("payload_path") != f"content/{public_id}.json" or item.get(
                    "page_path"
                ) != f"content/{public_id}.html":
                    issues.append(
                        {
                            "code": "public_path_not_canonical",
                            "severity": "error",
                            "detail": "公开文件路径必须由 public_id 构成。",
                        }
                    )
                payload_path = item.get("payload_path")
                if payload_path not in declared:
                    issues.append(
                        {
                            "code": "payload_not_declared",
                            "severity": "error",
                            "path": payload_path,
                            "detail": "v2 Bundle 缺少公开 payload 声明。",
                        }
                    )
                elif declared[payload_path].get("sha256") != item.get("payload_sha256"):
                    issues.append(
                        {
                            "code": "payload_sha_mismatch",
                            "severity": "error",
                            "path": payload_path,
                            "detail": "公开 payload 哈希与条目不一致。",
                        }
                    )

        if manifest.get("format_version") == 3:
            if "index.html" not in declared:
                issues.append(
                    {
                        "code": "site_home_missing",
                        "severity": "error",
                        "detail": "v3 Bundle 缺少公开首页 index.html。",
                    }
                )
            for item in manifest.get("items", []):
                space_path = item.get("space_path")
                if (
                    not isinstance(space_path, str)
                    or not space_path.startswith("spaces/")
                    or space_path not in declared
                ):
                    issues.append(
                        {
                            "code": "space_page_missing",
                            "severity": "error",
                            "path": space_path,
                            "detail": "v3 Bundle 缺少已公开空间页面。",
                        }
                    )

        recomputed = _sha256_bytes(_canonical_bytes({"files": manifest.get("files", [])}))
        if recomputed != manifest.get("bundle_sha256"):
            issues.append(
                {
                    "code": "bundle_sha256_mismatch",
                    "severity": "error",
                    "detail": "Bundle SHA-256 与清单不一致。",
                }
            )

        forbidden_parts = {"archive.db", "owner_secret", "operations", "objects", "backups"}
        for path in actual_files:
            lowered = {part.lower() for part in Path(path).parts}
            if lowered & forbidden_parts or path.lower().endswith(".db"):
                issues.append(
                    {
                        "code": "private_path_leak",
                        "severity": "error",
                        "path": path,
                        "detail": "产物包含私有数据库、对象或备份路径。",
                    }
                )

        if self.secret_path.is_file():
            secret = self.secret_path.read_text(encoding="utf-8").strip()
            if secret:
                secret_bytes = secret.encode("utf-8")
                for path in actual_files:
                    target = release_dir / Path(path)
                    if target.is_file() and self._contains_secret(target, secret_bytes):
                        issues.append(
                            {
                                "code": "owner_secret_leak",
                                "severity": "error",
                                "path": path,
                                "detail": "产物包含所有者密钥内容。",
                            }
                        )

        local_path = str(self.data_dir)
        for system_file in ("release.json", "assets/style.css"):
            target = release_dir / Path(system_file)
            if target.is_file() and local_path.encode("utf-8") in target.read_bytes():
                issues.append(
                    {
                        "code": "local_path_leak",
                        "severity": "error",
                        "path": system_file,
                        "detail": "系统生成文件包含本地数据目录路径。",
                    }
                )

        return {
            "ok": not issues,
            "issues": issues,
            "checked_files": len(actual_files),
            "bundle_sha256": manifest.get("bundle_sha256"),
            "manifest_sha256": manifest_sha256,
        }

    @staticmethod
    def _contains_secret(path: Path, secret: bytes) -> bool:
        overlap = max(len(secret) - 1, 0)
        tail = b""
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                if secret in tail + chunk:
                    return True
                tail = (tail + chunk)[-overlap:] if overlap else b""
        return False

    # ------------------------------------------------------------------
    # Registry files
    # ------------------------------------------------------------------
    def _read_event_ids(self) -> set[str]:
        if not self.events_path.is_file():
            return set()
        ids: set[str] = set()
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReleaseError("events.jsonl 无法解析。", reason=str(exc)) from exc
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                ids.add(event_id)
        return ids

    def _expected_events(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        """Deterministically derive every audit event implied by the index."""
        expected: list[dict[str, Any]] = []
        for record in index["releases"]:
            release_id = record["release_id"]
            expected.append(
                {
                    "event_id": f"ev_prepare_{release_id}",
                    "at": record.get("created_at"),
                    "actor": "owner",
                    "action": "prepare",
                    "release_id": release_id,
                    "previous_active_release_id": record.get("activated_from_release_id"),
                    "result": "prepared",
                    "detail": "由注册表对账补记：index 已提交，原事件追加失败。",
                    "content_ids": record.get("content_ids", []),
                    "attachment_ids": record.get("attachment_ids", []),
                    "bundle_sha256": record.get("bundle_sha256"),
                    "reconciled": True,
                }
            )
            if record.get("activated_at"):
                expected.append(
                    {
                        "event_id": f"ev_activate_{release_id}",
                        "at": record["activated_at"],
                        "actor": "owner",
                        "action": "activate",
                        "release_id": release_id,
                        "previous_active_release_id": record.get(
                            "activated_from_release_id"
                        ),
                        "result": "active",
                        "detail": "由注册表对账补记：index 已提交，原事件追加失败。",
                        "content_ids": record.get("content_ids", []),
                        "attachment_ids": record.get("attachment_ids", []),
                        "withdrawn_content_ids": record.get(
                            "withdrawn_content_ids", []
                        ),
                        "withdrawn_attachment_ids": record.get(
                            "withdrawn_attachment_ids", []
                        ),
                        "bundle_sha256": record.get("bundle_sha256"),
                        "reconciled": True,
                    }
                )
            if record.get("status") == "withdrawn":
                expected.append(
                    {
                        "event_id": f"ev_withdraw_{release_id}",
                        "at": record.get("withdrawn_at") or record.get("created_at"),
                        "actor": "owner",
                        "action": "withdraw",
                        "release_id": release_id,
                        "previous_active_release_id": release_id,
                        "result": "withdrawn",
                        "detail": "由注册表对账补记：index 已提交，原事件追加失败。",
                        "withdrawal_scope": record.get("withdrawal_scope"),
                        "withdrawn_content_ids": record.get(
                            "withdrawn_content_ids", []
                        ),
                        "withdrawn_attachment_ids": record.get(
                            "withdrawn_attachment_ids", []
                        ),
                        "bundle_sha256": record.get("bundle_sha256"),
                        "reconciled": True,
                    }
                )
        return expected

    def _reconcile_events_locked(self, index: dict[str, Any]) -> list[str]:
        existing = self._read_event_ids()
        appended: list[str] = []
        for event in self._expected_events(index):
            if event["event_id"] in existing:
                continue
            self._append_event(event)
            existing.add(event["event_id"])
            appended.append(event["event_id"])
        return appended

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return _empty_index()
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReleaseError("发布注册表 index.json 无法解析。", reason=str(exc)) from exc
        except OSError as exc:
            raise ReleaseError("发布注册表 index.json 无法读取。", reason=str(exc)) from exc
        if index.get("format") != REGISTRY_FORMAT:
            raise ReleaseError("发布注册表格式不匹配。", format=index.get("format"))
        version = index.get("format_version")
        if version not in (1, 2, REGISTRY_FORMAT_VERSION):
            raise ReleaseError("发布注册表版本不受支持。", format_version=version)
        if not isinstance(index.get("releases"), list):
            raise ReleaseError("发布注册表 releases 字段无效。")
        if version == 1:
            # Read compatibility only: no rewrite happens here. The first
            # mutating operation upgrades the file while keeping every v1
            # record (and its prepared bundle) untouched.
            index["registry_revision"] = 0
            index["updated_at"] = None
            index["active_since"] = None
            index.setdefault("active_release_id", None)
        index["releases"] = [
            _normalize_record(record)
            for record in index["releases"]
            if isinstance(record, dict)
        ]
        index.setdefault("registry_revision", 0)
        index.setdefault("updated_at", None)
        index.setdefault("active_since", None)
        index.setdefault("active_release_id", None)
        withdrawals = index.get("withdrawals")
        if not isinstance(withdrawals, dict):
            withdrawals = {}
        for bucket in ("content", "attachments"):
            if not isinstance(withdrawals.get(bucket), dict):
                withdrawals[bucket] = {}
        index["withdrawals"] = withdrawals
        self._synthesize_lineage(index)
        return index

    def _synthesize_lineage(self, index: dict[str, Any]) -> dict[str, Any]:
        """Ensure every record has a sequence and a history ledger.

        Records that already carry a v3 ledger are preserved verbatim. All
        others get derived entries, which keeps read-only history available
        even when ``events.jsonl`` is missing.
        """
        seq = int(index.get("sequence") or 0)
        for record in index["releases"]:
            existing = record.get("history")
            valid = (
                isinstance(existing, list)
                and existing
                and all(
                    isinstance(entry, dict) and isinstance(entry.get("seq"), int)
                    for entry in existing
                )
            )
            if valid:
                for entry in existing:
                    entry.setdefault("origin", "recorded")
                    entry.setdefault("actor", record.get("actor") or "owner")
                    entry.setdefault("registry_revision", None)
                    entry.setdefault("from_release_id", None)
                    entry.setdefault("to_release_id", record["release_id"])
                    entry.setdefault("scope", None)
                    entry.setdefault("content_ids", [])
                    entry.setdefault("attachment_ids", [])
                    entry.setdefault("reason", None)
                seq = max(seq, max(int(entry["seq"]) for entry in existing))
                if not isinstance(record.get("sequence"), int):
                    record["sequence"] = int(existing[0]["seq"])
                continue
            derived = _derived_history_entries(record)
            for entry in derived:
                seq += 1
                entry["seq"] = seq
            record["history"] = derived
            record["sequence"] = derived[0]["seq"] if derived else None
        index["sequence"] = seq
        index.setdefault("history_format", {"format": HISTORY_FORMAT, "version": HISTORY_FORMAT_VERSION})
        return index

    def _history_entry(
        self,
        index: dict[str, Any],
        record: dict[str, Any],
        *,
        action: str,
        at: str,
        registry_revision: int,
        origin: str = "recorded",
        from_release_id: str | None = None,
        to_release_id: str | None = None,
        scope: str | None = None,
        content_ids: list[str] | None = None,
        attachment_ids: list[str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one ledger entry to a record and bump the global sequence."""
        index["sequence"] = int(index.get("sequence") or 0) + 1
        entry = {
            "seq": index["sequence"],
            "registry_revision": registry_revision,
            "at": at,
            "actor": record.get("actor") or "owner",
            "action": action,
            "origin": origin,
            "from_release_id": from_release_id,
            "to_release_id": to_release_id,
            "scope": scope,
            "content_ids": list(content_ids or []),
            "attachment_ids": list(attachment_ids or []),
            "reason": reason,
        }
        record.setdefault("history", []).append(entry)
        return entry

    def _mark_v3(self, index: dict[str, Any]) -> None:
        """Prepare the in-memory index for an atomic v3 write."""
        index["format_version"] = REGISTRY_FORMAT_VERSION
        index["history_format"] = {
            "format": HISTORY_FORMAT,
            "version": HISTORY_FORMAT_VERSION,
        }
        index.setdefault("sequence", 0)
        index.setdefault("withdrawals", {"content": {}, "attachments": {}})

    def _append_event(self, event: dict[str, Any]) -> None:
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        line = _canonical_bytes(event) + b"\n"
        with self.events_path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.events_path.parent)


def _verify_registry_backup(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        _read_verified_registry_backup(archive)


def _read_verified_registry_backup(archive: zipfile.ZipFile) -> dict[str, Any]:
    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    if manifest.get("format") != REGISTRY_BACKUP_FORMAT:
        raise ReleaseError("注册表备份清单格式不匹配。")
    listed = [entry["path"] for entry in manifest.get("files", [])]
    names = archive.namelist()
    if len(set(listed)) != len(listed) or len(set(names)) != len(names) or set(names) != set(listed) | {"manifest.json"}:
        raise ReleaseError("注册表成员与清单不一致。")
    if manifest.get("format_version") == REGISTRY_BACKUP_FORMAT_VERSION:
        pending = manifest.get("withdrawal_pending")
        if type(pending) is not bool or pending != (f"registry/{WITHDRAWAL_MARKER}" in listed):
            raise ReleaseError("待撤回标记与备份清单不一致。")
    for entry in manifest.get("files", []):
        with archive.open(entry["path"]) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["sha256"] or archive.getinfo(entry["path"]).file_size != entry["bytes"]:
            raise ReleaseError("注册表备份校验失败。", path=entry["path"])
    if manifest.get("format_version", 0) >= 3:
        history_format = manifest.get("history_format") or {}
        if history_format.get("format") != HISTORY_FORMAT:
            raise ReleaseError("注册表备份历史格式不匹配。")
        lineage = manifest.get("lineage")
        if not isinstance(lineage, list):
            raise ReleaseError("注册表备份缺少谱系摘要。")
        try:
            index_data = json.loads(
                archive.read("registry/index.json").decode("utf-8")
            )
        except KeyError:
            index_data = {"releases": []}
        expected_ids = [
            record.get("release_id")
            for record in index_data.get("releases", [])
            if isinstance(record, dict)
        ]
        lineage_ids = [
            entry.get("release_id")
            for entry in lineage
            if isinstance(entry, dict)
        ]
        if lineage_ids != expected_ids:
            raise ReleaseError(
                "注册表备份谱系摘要与 index.json 不一致。",
                expected=expected_ids,
                actual=lineage_ids,
            )
    return manifest
