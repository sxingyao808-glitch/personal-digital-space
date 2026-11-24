"""Journaled attachment storage with crash recovery.

The protocol is deliberately conservative:

1. reserve an operation + attachment row in PENDING state;
2. write bytes to a unique staging file while hashing them;
3. promote the verified staging file to its resource-ID path atomically;
4. mark the attachment READY only after the committed metadata can be confirmed.

A retry is idempotent only for the same attachment, owner and payload. A
different owner or different payload is rejected without mutating stored bytes.
Startup reconciliation scans staging, final objects, metadata and quarantine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .db import connect, get_meta, set_meta, transaction, utcnow
from .fsutil import fsync_directory
from .store import (
    ConflictError,
    NotFoundError,
    UncertainOutcomeError,
    ValidationError,
)

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

# Device names Windows refuses to use as file names in any directory, with or
# without an extension. An id that reaches a filesystem path must not be one.
_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{index}" for index in range(1, 10)]
    + [f"LPT{index}" for index in range(1, 10)]
)

logger = logging.getLogger(__name__)


class _StaleReconciliation(Exception):
    """Internal signal: a reconcile snapshot no longer matches current state."""


class AttachmentUnverifiedError(ConflictError):
    """The stored bytes no longer match the READY claim.

    Download is refused instead of serving content whose integrity cannot be
    confirmed. The HTTP layer maps this to 409 ``attachment_unverified``.
    """

    code = "attachment_unverified"


class _QuarantinedDuringWrite(ConflictError):
    """A quarantine decision landed between reservation and commit.

    It is re-raised as a definite conflict rather than being folded into an
    "uncertain outcome": quarantining must never be undone by an upload.
    """


# In-process registry of uploads that are actively using an Archive's staging
# area. reconcile() must never move a file that an active upload still owns.
# Registration happens before the staging file is created and is released in a
# finally block, so an exception cannot leave a permanent lock behind. The
# registry is intentionally process-local: it guarantees same-process
# serialization only and makes no multi-process claim. If the process dies, the
# registry disappears with it and its staging files become ordinary crash
# leftovers that a later reconcile can quarantine.
_ACTIVE_UPLOADS: dict[str, dict[str, dict[str, Any]]] = {}
_ACTIVE_UPLOAD_LOCK = threading.Lock()

# Reconcile is serialized per resolved Archive path in this process. This keeps
# a later reconcile from mutating files or the issue ledger while an earlier
# scan is still using its snapshot. Different archives get different locks.
# The lock is intentionally process-local; multi-process coordination is not
# claimed here.
_RECONCILE_LOCKS: dict[str, threading.Lock] = {}
_RECONCILE_LOCKS_GUARD = threading.Lock()

_FILE_ISSUE_KINDS = {
    "orphan_object_quarantined",
    "orphan_object_quarantine_failed",
    "stale_staging_quarantined",
    "stale_staging_quarantine_failed",
    # Staged bytes deliberately retained without a journal entry, or retained
    # after a failed promotion. Both resolve only when the file is gone.
    "staging_unjournaled_retained",
    "staging_retained_after_promotion_failure",
}

# A successful quarantine move always gets a durable inventory row. The row is
# keyed by the quarantine file name (unique because of the random suffix) and
# records the original directory, name, size, SHA-256 and move time. It is a
# record of disposition, not a new active-area finding, so it is reported in
# quarantine_inventory rather than in the per-scan issue list.
_QUARANTINE_LEDGER_KIND = "quarantine_file_isolated"
_QUARANTINE_UNTRACKED_KIND = "quarantine_file_untracked"
_QUARANTINED_OBJECT_MOVED_KIND = "quarantined_attachment_object_moved"
_QUARANTINED_OBJECT_MOVE_FAILED_KIND = "quarantined_attachment_object_move_failed"
_QUARANTINED_PENDING_OPERATION_KIND = "quarantined_attachment_pending_operation"

# Durable commit marker for one reconcile attempt. A stored report may only be
# treated as current when the same transaction that wrote it also marked this
# attempt completed. Any other phase (scanning, committing_report, failed)
# means the last report is stale and must not be read as a clean scan.
_RECONCILE_ATTEMPT_KEY = "reconcile_attempt"

# Findings that describe the quarantine area itself rather than the active
# attachment area. They retain audit value, so they stay in `open_issues`, but
# they must not drive the "active problems" badge forever.
_QUARANTINE_AREA_KINDS = frozenset(
    {_QUARANTINE_LEDGER_KIND, _QUARANTINE_UNTRACKED_KIND}
)

# A scan must never abort because one object cannot be inspected. These kinds
# make the failure first-class: the item is reported and the scan continues.
_OBJECT_ANOMALY_KINDS = {
    "not_regular": "attachment_object_not_regular",
    "unreadable": "attachment_object_unreadable",
    "hash_failed": "attachment_object_hash_failed",
}

_OBJECT_ANOMALY_DETAILS = {
    "not_regular": "最终对象不是普通文件（目录、符号链接或 junction），未读取、未移动、未改变附件状态。",
    "unreadable": "最终对象无法读取（权限或 I/O 错误），未读取、未移动、未改变附件状态。",
    "hash_failed": "最终对象存在但哈希读取失败，未移动、未改变附件状态。",
}

# A staging file the scan could not settle. Its target is the operation id, not
# the transient staging file name: the name changes between attempts, while the
# operation identity is stable and can be resolved by a later scan. Resolution
# requires a *positive* observation of that operation's staging area
# (staging_settled); a file merely becoming readable is not enough on its own.
_STAGING_ANOMALY_KIND = "staging_file_uninspectable"

# Issue kinds grouped by the condition that produced them. A finding may only
# be closed when *that* condition was positively observed to be gone, so
# closing one kind never closes another kind's finding for the same target:
#   object_absent       - the final object was missing
#   hash_mismatch       - the final object did not match the recorded payload
#   metadata_incomplete - the recorded payload hash/size was missing
#   unverified_write    - the write was never confirmed as a verified READY
_ISSUE_CAUSES = {
    "ready_file_missing": "object_absent",
    "broken_resource_missing": "object_absent",
    "ready_hash_mismatch": "hash_mismatch",
    "attachment_metadata_incomplete": "metadata_incomplete",
    "broken_state_unrecovered": "unverified_write",
    "pending_resource_missing": "unverified_write",
    "final_without_journaled_payload": "unverified_write",
    "final_hash_mismatch": "unverified_write",
    "staging_without_journaled_payload": "unverified_write",
    "staging_hash_mismatch": "unverified_write",
    "attachment_object_not_regular": "object_readable",
    "attachment_object_unreadable": "object_readable",
    "attachment_object_hash_failed": "object_readable",
    _STAGING_ANOMALY_KIND: "staging_probe",
    # A repaired object is only healed once a scan confirms a verified READY.
    "broken_object_quarantined_for_repair": "unverified_write",
}

# The positive observation each cause requires before its finding may close.
# "object_present" only proves the file came back; "bytes_match" only proves
# the bytes agree with the recorded payload; "healthy" is the only evidence
# that the system itself acknowledged a complete, verified READY write.
_CAUSE_EVIDENCE = {
    "object_absent": "object_present",
    "hash_mismatch": "bytes_match",
    "metadata_incomplete": "metadata_complete",
    "unverified_write": "healthy",
    "object_readable": "readable",
    "staging_probe": "staging_settled",
}


def _reconcile_lock_for(key: str) -> threading.Lock:
    with _RECONCILE_LOCKS_GUARD:
        return _RECONCILE_LOCKS.setdefault(key, threading.Lock())


def _active_upload_begin(key: str, operation_id: str, attachment_id: str) -> None:
    with _ACTIVE_UPLOAD_LOCK:
        bucket = _ACTIVE_UPLOADS.setdefault(key, {})
        entry = bucket.setdefault(
            operation_id, {"count": 0, "attachments": {}}
        )
        entry["count"] += 1
        attachments = entry["attachments"]
        attachments[attachment_id] = attachments.get(attachment_id, 0) + 1


def _active_upload_end(key: str, operation_id: str, attachment_id: str) -> None:
    with _ACTIVE_UPLOAD_LOCK:
        bucket = _ACTIVE_UPLOADS.get(key)
        if not bucket:
            return
        entry = bucket.get(operation_id)
        if entry is None:
            return
        entry["count"] -= 1
        attachments = entry["attachments"]
        remaining = attachments.get(attachment_id, 0) - 1
        if remaining > 0:
            attachments[attachment_id] = remaining
        else:
            attachments.pop(attachment_id, None)
        if entry["count"] <= 0:
            bucket.pop(operation_id, None)
        if not bucket:
            _ACTIVE_UPLOADS.pop(key, None)


def _issue_fingerprint(issue: dict[str, str]) -> str:
    return hashlib.sha256(
        f"{issue['kind']}|{issue['target']}".encode("utf-8")
    ).hexdigest()


def _upsert_issue(
    conn: sqlite3.Connection, issue: dict[str, str], checked_at: str
) -> None:
    conn.execute(
        "INSERT INTO integrity_issues(fingerprint, kind, target, detail, "
        "state, first_seen, last_seen) "
        "VALUES(?, ?, ?, ?, 'open', ?, ?) "
        "ON CONFLICT(fingerprint) DO UPDATE SET detail = excluded.detail, "
        "state = 'open', last_seen = excluded.last_seen, resolved_at = NULL",
        (
            _issue_fingerprint(issue),
            issue["kind"],
            issue["target"],
            issue["detail"],
            checked_at,
            checked_at,
        ),
    )


def _validate_id(value: str, field: str) -> str:
    # fullmatch, not match: with `$`, match would accept a trailing newline,
    # which Windows would then strip or reject on a different code path.
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{field} 格式无效。", field=field)
    if value.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValidationError(
            f"{field} 使用了 Windows 保留设备名，无法映射为文件路径。",
            field=field,
        )
    return value


def _fault(name: str) -> None:
    """Test-only fault injection. Never active unless explicitly enabled."""
    if os.environ.get("ARCHIVE_TEST_MODE") != "1":
        return
    if os.environ.get("ARCHIVE_FAULT_INJECT") != name:
        return
    if name in ("during_staging", "after_staging", "after_promotion"):
        os._exit(73)
    if name == "raise_after_promotion":
        raise UncertainOutcomeError(
            "附件写入已越过提交边界，但元数据尚未确认，结果不确定。"
        )
    if name == "fail_ready_commit":
        raise sqlite3.OperationalError("injected READY metadata failure")


def _discard_staging(path: Path) -> None:
    """Best-effort staging cleanup that never masks the caller's error.

    A failure to remove a temporary file is not a data-safety failure: the file
    is still in the staging area and reconcile() will quarantine it.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _hash_file(path: Path, handle: BinaryIO | None = None) -> tuple[str, int]:
    """Hash a file, optionally through an already-opened handle.

    The handle form is used by ``_probe_object`` so the bytes that are hashed
    come from the same file object whose identity was just verified.
    """
    digest = hashlib.sha256()
    size = 0
    if handle is not None:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    with path.open("rb") as opened:
        while True:
            chunk = opened.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _probe_object(
    path: Path,
) -> tuple[str, str | None, int | None, OSError | None]:
    """Classify one object path without ever raising.

    Returns ``(status, digest, size, error)``. A scan uses the classification
    to report a single unreadable item and continue, instead of losing the whole
    audit (and the ``last_reconcile_report``) to one directory or locked file.

    The hash is read from the *opened* file and its identity is compared with
    the preceding ``lstat()``: a path swapped between the check and the open is
    reported as ``not_regular`` instead of being followed.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent", None, None, None
    except OSError as exc:
        return "unreadable", None, None, exc
    if not stat.S_ISREG(info.st_mode):
        return "not_regular", None, None, None
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return "absent", None, None, None
    except OSError as exc:
        return "hash_failed", None, None, exc
    try:
        with handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (info.st_dev, info.st_ino):
                return "not_regular", None, None, None
            digest_hex, size = _hash_file(path, handle)
            after = os.fstat(handle.fileno())
            if (
                after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                return "hash_failed", None, None, OSError(0, "file changed during read")
    except OSError as exc:
        return "hash_failed", None, None, exc
    return "regular", digest_hex, size, None


def _safe_iterdir(folder: Path) -> tuple[list[Path], OSError | None]:
    """List a directory without aborting a scan when it cannot be read."""
    try:
        return sorted(folder.iterdir()), None
    except OSError as exc:
        return [], exc


def _classify_failure(exc: BaseException) -> str:
    """Split failures into permanent, transient and genuinely uncertain.

    Permanent failures prove the request itself is invalid and the attempt is
    over. Transient failures say nothing about the payload, so retained bytes
    and an UNCERTAIN operation remain the accurate record. Anything else is
    treated as uncertain: guessing "failed" would discard recoverable evidence.
    """
    if isinstance(exc, (ConflictError, NotFoundError, ValidationError)):
        return "permanent"
    if isinstance(exc, (sqlite3.Error, OSError)):
        return "transient"
    return "uncertain"


def _safe_error_text(exc: BaseException) -> str:
    """A short, path-free description of an internal failure.

    OS error messages routinely embed the absolute path that failed, which may
    include the user's home directory. Notes and reports are returned by the API,
    so only the exception class, the errno and the bare file name are kept.
    """
    parts = [type(exc).__name__]
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        parts.append(f"errno={errno}")
    filename = getattr(exc, "filename", None)
    if filename:
        try:
            parts.append(f"file={Path(os.fspath(filename)).name}")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def _fsync_directory(path: Path) -> bool:
    """Flush one directory entry through the shared platform helper.

    Returns the platform's confirmation. A failure is logged by the helper and
    reported as ``False``; it is never silently treated as a successful fsync.
    """
    return fsync_directory(path)


class AttachmentStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._archive_key = str(self.data_dir.resolve())
        self.db_path = self.data_dir / "archive.db"
        self.objects = self.data_dir / "objects"
        self.staging = self.data_dir / "staging"
        self.quarantine = self.data_dir / "quarantine"
        for folder in (self.objects, self.staging, self.quarantine):
            folder.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    def upload(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        content_id: str,
        operation_id: str,
        attachment_id: str | None = None,
        declared_mime: str | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        visibility: str = "private",
    ) -> dict[str, Any]:
        # Register before _prepare_pending so reconcile() cannot observe a
        # PENDING operation and treat its not-yet-created staging as missing.
        # The id is normalized here exactly like _upload() would, so the active
        # registry always carries the attachment id that will be created.
        if not attachment_id and isinstance(operation_id, str):
            # Reuse the attachment a previous attempt of this operation already
            # reserved. Generating a fresh id would orphan the operation's
            # resource link and let a retry create a second attachment row.
            existing_operation = self._get_operation(operation_id)
            if existing_operation is not None:
                attachment_id = existing_operation["resource_id"]
            else:
                attachment_id = f"f_{uuid.uuid4().hex}"
        active = isinstance(operation_id, str) and isinstance(attachment_id, str)
        if active:
            _active_upload_begin(self._archive_key, operation_id, attachment_id)
        try:
            return self._upload(
                stream,
                filename=filename,
                content_id=content_id,
                operation_id=operation_id,
                attachment_id=attachment_id,
                declared_mime=declared_mime,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                visibility=visibility,
            )
        finally:
            if active:
                _active_upload_end(self._archive_key, operation_id, attachment_id)

    def _upload(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        content_id: str,
        operation_id: str,
        attachment_id: str | None = None,
        declared_mime: str | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        visibility: str = "private",
    ) -> dict[str, Any]:
        content_id = _validate_id(content_id, "content_id")
        operation_id = _validate_id(operation_id, "operation_id")
        attachment_id = _validate_id(attachment_id, "attachment_id") if attachment_id else f"f_{uuid.uuid4().hex}"
        if visibility not in ("private", "public"):
            raise ValidationError("visibility 只能是 private 或 public。", field="visibility")
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ValidationError("expected_sha256 必须是 64 位十六进制。", field="expected_sha256")
        if expected_size is not None and (not isinstance(expected_size, int) or expected_size < 0):
            raise ValidationError("expected_size 必须是非负整数。", field="expected_size")
        if expected_size is not None and expected_size > MAX_ATTACHMENT_BYTES:
            raise ValidationError("附件超过大小上限。", field="expected_size")

        safe_name = Path(filename or "").name.strip()
        if not safe_name:
            raise ValidationError("附件缺少文件名。", field="filename")
        if len(safe_name) > 255:
            safe_name = safe_name[-255:]
        mime = (declared_mime or "application/octet-stream")[:120]

        prior = self._prepare_pending(
            content_id=content_id,
            operation_id=operation_id,
            attachment_id=attachment_id,
            filename=safe_name,
            mime=mime,
            visibility=visibility,
        )
        prior_uncertain = bool(
            prior is not None and prior["operation"]["state"] == "UNCERTAIN"
        )

        staging_path = self.staging / f"{operation_id}.{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        first_chunk = True
        try:
            with staging_path.open("wb") as handle:
                while True:
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise ValidationError("附件超过 50 MB 上限。", field="file")
                    digest.update(chunk)
                    handle.write(chunk)
                    if first_chunk:
                        first_chunk = False
                        if (
                            os.environ.get("ARCHIVE_TEST_MODE") == "1"
                            and os.environ.get("ARCHIVE_FAULT_INJECT") == "during_staging"
                        ):
                            # Flush only for the mid-write crash test so the
                            # durable state contains a partial, unjournaled file.
                            handle.flush()
                            _fault("during_staging")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as exc:
            _discard_staging(staging_path)
            self._mark_failed(
                attachment_id,
                operation_id,
                f"写入暂存文件失败：{_safe_error_text(exc)}",
                prior_uncertain=prior_uncertain,
            )
            raise

        digest_hex = digest.hexdigest()

        if expected_size is not None and expected_size != size:
            _discard_staging(staging_path)
            note = f"声明大小 {expected_size} 与实际 {size} 不一致"
            self._reject_attempt(attachment_id, operation_id, prior, note)
            raise ValidationError("附件大小与声明不一致。", field="file")
        if expected_sha256 is not None and expected_sha256 != digest_hex:
            _discard_staging(staging_path)
            note = "声明的 SHA-256 与实际内容不一致"
            self._reject_attempt(attachment_id, operation_id, prior, note)
            raise ValidationError("附件哈希与声明不一致。", field="file")

        # A retry of an already committed write must not disturb READY state.
        # The bytes were hashed only to prove the payload is identical.
        fast_path = False
        committed = None
        committed_operation = None
        committed_file_ok = False
        committed_path = self.objects / attachment_id
        try:
            committed = self._get_attachment(attachment_id)
            committed_operation = self._get_operation(operation_id)
            committed_status, committed_digest, committed_size, _committed_error = (
                _probe_object(committed_path)
            )
            if committed_status == "regular":
                committed_file_ok = (
                    committed_digest == digest_hex and committed_size == size
                )
        except sqlite3.Error as exc:
            _discard_staging(staging_path)
            raise UncertainOutcomeError(
                "无法确认附件提交状态，结果不确定。",
                attachment_id=attachment_id,
                operation_id=operation_id,
            ) from exc
        except OSError as exc:
            _discard_staging(staging_path)
            raise UncertainOutcomeError(
                "无法读取已提交附件对象，结果不确定。",
                attachment_id=attachment_id,
                operation_id=operation_id,
            ) from exc
        if (
            committed is not None
            and committed["state"] == "READY"
            and committed["sha256"] == digest_hex
            and committed["byte_size"] == size
            and committed_file_ok
            and committed_operation is not None
            and committed_operation["state"] == "COMPLETED"
            and committed_operation["owner_content_id"] == content_id
            and committed_operation["resource_id"] == attachment_id
            and committed_operation["payload_sha256"] == digest_hex
            and committed_operation["payload_size"] == size
        ):
            fast_path = True
            _discard_staging(staging_path)
            return {
                "attachment": committed,
                "operation": committed_operation,
                "idempotent": True,
            }

        journal_snapshot: dict[str, Any] | None = None
        try:
            with transaction(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("附件记录不存在。", attachment_id=attachment_id)
                if row["state"] == "QUARANTINED":
                    # Quarantine is a terminal, operator-driven decision. An
                    # upload must never turn it back into PENDING or READY.
                    raise _QuarantinedDuringWrite(
                        "附件已被隔离，拒绝以新写入解除隔离状态。",
                        attachment_id=attachment_id,
                    )
                if row["content_id"] != content_id:
                    raise ConflictError(
                        "附件属于其他内容，拒绝重新关联。",
                        attachment_id=attachment_id,
                    )
                if row["sha256"] and (
                    row["sha256"] != digest_hex or row["byte_size"] != size
                ):
                    raise ConflictError(
                        "同一附件标识已存在不同内容，拒绝替换。",
                        attachment_id=attachment_id,
                    )
                op = conn.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                if op is None:
                    raise NotFoundError("操作记录不存在。", operation_id=operation_id)
                if op["owner_content_id"] != content_id or op["resource_id"] != attachment_id:
                    raise ConflictError("操作标识与附件/内容身份不一致。", operation_id=operation_id)
                if op["kind"] != "attachment_write":
                    raise ConflictError(
                        "操作标识属于其他业务类型，不能用于附件写入。",
                        operation_id=operation_id,
                    )
                if op["payload_sha256"] and op["payload_sha256"] != digest_hex:
                    raise ConflictError("同一操作标识对应不同内容，拒绝替换。", operation_id=operation_id)

                # Captured before the journal write so a later integrity-conflict
                # decision can be conditioned on this exact row version.
                journal_snapshot = {
                    "state": row["state"],
                    "sha256": row["sha256"],
                    "byte_size": row["byte_size"],
                    "operation_id": row["operation_id"],
                    "updated_at": row["updated_at"],
                }

                # A concurrent duplicate of this operation may have already
                # committed READY/COMPLETED. Never journal over that result:
                # otherwise a later failure handler could downgrade a success.
                conn.execute(
                    "UPDATE operations SET payload_sha256 = ?, payload_size = ?, "
                    "state = 'PENDING', updated_at = ? "
                    "WHERE id = ? AND state != 'COMPLETED'",
                    (digest_hex, size, utcnow(), operation_id),
                )
                # Metadata is fixed when the payload is first journaled. Retries
                # of an already journaled payload must not change MIME, whether
                # they take the committed fast path or the repair path.
                conn.execute(
                    "UPDATE attachments SET byte_size = ?, sha256 = ?, "
                    "mime = CASE WHEN sha256 IS NULL THEN ? ELSE mime END, "
                    "state = 'PENDING', note = NULL, updated_at = ? "
                    "WHERE id = ? AND state != 'READY' AND state != 'QUARANTINED'",
                    (size, digest_hex, mime, utcnow(), attachment_id),
                )
        except BaseException as exc:
            if _classify_failure(exc) == "permanent":
                # The request itself is invalid (other owner, different
                # payload, ...): these bytes can never become this object.
                _discard_staging(staging_path)
                self._reject_attempt(
                    attachment_id,
                    operation_id,
                    prior,
                    f"日志写入被拒绝：{type(exc).__name__}",
                )
            else:
                # The bytes are complete and fsynced but not journaled. A
                # later scan cannot promote them, yet deleting them destroys
                # the only copy of the attempt; retain and report instead.
                self._retain_staging(
                    staging_path,
                    kind="staging_unjournaled_retained",
                    exc=exc,
                    digest=digest_hex,
                    size=size,
                    attachment_id=attachment_id,
                    operation_id=operation_id,
                )
            raise

        # The journal must already contain the payload hash before a hard crash,
        # otherwise restart reconciliation could accept arbitrary staged bytes.
        _fault("after_staging")

        final_path = self.objects / attachment_id
        existing_digest: str | None = None
        existing_size: int | None = None
        repaired_object: dict[str, Any] | None = None
        conflict_issue: dict[str, str] | None = None
        try:
            probe_status, existing_digest, existing_size, _probe_error = (
                _probe_object(final_path)
            )
            if probe_status == "not_regular":
                # A directory, symlink, junction or other reparse point at the
                # object path is never promoted over and never accepted as the
                # committed object. The claim is marked BROKEN for the operator.
                conflict_issue = {
                    "kind": "attachment_object_not_regular",
                    "target": attachment_id,
                    "detail": "目标资源不是普通文件（目录、符号链接或 junction），拒绝覆盖。",
                }
                raise ConflictError(
                    "目标资源不是普通文件，拒绝覆盖。",
                    attachment_id=attachment_id,
                )
            if probe_status in {"unreadable", "hash_failed"}:
                # The target cannot be classified, so it is unsafe to overwrite
                # or delete. Keep the verified staged bytes and report the
                # outcome as genuinely uncertain instead of guessing.
                raise UncertainOutcomeError(
                    "无法确认目标资源状态，结果不确定。",
                    attachment_id=attachment_id,
                    operation_id=operation_id,
                )
            if probe_status == "regular":
                if existing_digest != digest_hex or existing_size != size:
                    if journal_snapshot is None or journal_snapshot["state"] != "BROKEN":
                        # A committed object is never replaced by an upload. The
                        # conflict marks the claim BROKEN first; only a later
                        # retry, when the row is already BROKEN, may repair it.
                        raise ConflictError(
                            "目标资源已存在不同字节，拒绝覆盖。",
                            attachment_id=attachment_id,
                        )
                    # The row was already BROKEN and the bytes on disk provably
                    # differ from the recorded payload. Move the corrupt object
                    # aside through the normal quarantine path, then promote the
                    # verified staged bytes. Nothing is overwritten or deleted.
                    _quarantine_path, moved, repair_meta = self._quarantine_file(
                        final_path,
                        origin="object",
                        reason="corrupt_object_replaced",
                        checked_at=utcnow(),
                    )
                    if not moved or final_path.exists():
                        raise ConflictError(
                            "损坏对象无法安全隔离，拒绝覆盖。",
                            attachment_id=attachment_id,
                        )
                    repaired_object = repair_meta
                    os.replace(staging_path, final_path)
                    _fsync_directory(self.objects)
                else:
                    _discard_staging(staging_path)
            else:
                os.replace(staging_path, final_path)
                _fsync_directory(self.objects)
        except BaseException as exc:
            if isinstance(exc, ConflictError):
                # A conflict is decided by the bytes already at the target; the
                # staged copy could never replace them without overwriting a
                # committed object, so it is discarded.
                _discard_staging(staging_path)
                # A conflicting object proves the committed READY claim wrong.
                # The transition is conditioned on the row version observed
                # before journaling, so a concurrently completed upload wins.
                issue = conflict_issue or {
                    "kind": "ready_hash_mismatch",
                    "target": attachment_id,
                    "detail": (
                        f"最终资源与已登记元数据不一致：期望 {digest_hex} "
                        f"({size} 字节)，实际 {existing_digest} ({existing_size} 字节)。"
                    ),
                }
                if journal_snapshot is not None and journal_snapshot["state"] == "READY":
                    self._mark_conflict_broken(
                        attachment_id,
                        operation_id,
                        journal_snapshot,
                        _safe_error_text(exc),
                        issue=issue,
                        checked_at=utcnow(),
                    )
                else:
                    self._mark_failed(
                        attachment_id,
                        operation_id,
                        _safe_error_text(exc),
                        issue=issue,
                        checked_at=utcnow(),
                        prior_uncertain=prior_uncertain,
                    )
            else:
                # The payload is already journaled and its bytes were verified.
                # Deleting them would make a retry impossible, so retain the
                # staging file and record a genuinely uncertain outcome.
                self._retain_staging(
                    staging_path,
                    kind="staging_retained_after_promotion_failure",
                    exc=exc,
                    digest=digest_hex,
                    size=size,
                    attachment_id=attachment_id,
                    operation_id=operation_id,
                )
            raise

        if repaired_object is not None:
            issue = {
                "kind": "broken_object_quarantined_for_repair",
                "target": attachment_id,
                "detail": (
                    f"损坏对象已隔离为 {repaired_object.get('quarantine_name')}，"
                    f"并以已登记载荷重建：期望 {digest_hex} ({size} 字节)。"
                ),
            }
            if not self._persist_issue_now(issue, utcnow()):
                logger.warning(
                    "could not persist repair issue (attachment=%s)", attachment_id
                )

        # This fault deliberately leaves bytes promoted but metadata PENDING.
        # It must surface as an uncertain outcome, never as success or failure.
        _fault("after_promotion")

        try:
            with transaction(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("附件记录不存在。", attachment_id=attachment_id)
                if row["state"] == "QUARANTINED":
                    # A quarantine that landed while the bytes were being
                    # promoted wins. The object is not served and the claim is
                    # not restored to READY.
                    raise _QuarantinedDuringWrite(
                        "附件在写入期间被隔离，拒绝提交 READY。",
                        attachment_id=attachment_id,
                    )
                if row["content_id"] != content_id:
                    raise ConflictError("附件归属在写入期间发生变化。", attachment_id=attachment_id)
                if row["sha256"] != digest_hex or row["byte_size"] != size:
                    raise ConflictError("附件元数据在写入期间发生变化。", attachment_id=attachment_id)
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'READY', note = NULL, updated_at = ? "
                    "WHERE id = ? AND state != 'QUARANTINED'",
                    (utcnow(), attachment_id),
                )
                operation_update = conn.execute(
                    "UPDATE operations SET state = 'COMPLETED', result = 'ready', "
                    "updated_at = ? WHERE id = ?",
                    (utcnow(), operation_id),
                )
                if attachment_update.rowcount != 1 or operation_update.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "READY 元数据更新未命中预期记录。"
                    )
                _fault("fail_ready_commit")
        except _QuarantinedDuringWrite:
            # Definite rejection: do not rewrite it as an uncertain outcome.
            raise
        except BaseException:
            # Bytes are promoted but committed metadata is not confirmed. This is
            # an uncertain outcome, not a success and not a definite failure.
            try:
                self._mark_uncertain_after_ready_failure(attachment_id, operation_id)
            except Exception as exc:
                logger.warning(
                    "could not record uncertain READY outcome "
                    "(attachment=%s operation=%s): %s",
                    attachment_id,
                    operation_id,
                    type(exc).__name__,
                    exc_info=True,
                )
            raise UncertainOutcomeError(
                "附件写入结果不确定，请重试或运行完整性检查。",
                attachment_id=attachment_id,
                operation_id=operation_id,
            )

        try:
            confirmed = self._get_attachment(attachment_id)
            confirmed_operation = self._get_operation(operation_id)
        except sqlite3.Error as exc:
            raise UncertainOutcomeError(
                "附件元数据已提交，但无法确认 READY 状态，请重试或运行完整性检查。",
                attachment_id=attachment_id,
                operation_id=operation_id,
            ) from exc
        if (
            confirmed is None
            or confirmed["state"] != "READY"
            or confirmed["sha256"] != digest_hex
            or confirmed["byte_size"] != size
            or confirmed_operation is None
            or confirmed_operation["state"] != "COMPLETED"
            or confirmed_operation["payload_sha256"] != digest_hex
        ):
            raise UncertainOutcomeError(
                "附件元数据提交后未能确认 READY 状态，请重试或运行完整性检查。",
                attachment_id=attachment_id,
                operation_id=operation_id,
            )

        # idempotent is true only when no state change or file promotion occurred.
        return {
            "attachment": confirmed,
            "operation": confirmed_operation,
            "idempotent": fast_path,
        }

    def _prepare_pending(
        self,
        *,
        content_id: str,
        operation_id: str,
        attachment_id: str,
        filename: str,
        mime: str,
        visibility: str,
    ) -> dict[str, Any] | None:
        """Reserve the attachment/operation rows and return the prior snapshot.

        The snapshot lets a rejected retry put the previous operation state and
        result back instead of leaving the row reset to PENDING with its history
        erased. It is only captured for an existing, unfinished operation.
        """
        prior: dict[str, Any] | None = None
        with transaction(self.db_path) as conn:
            content = conn.execute(
                "SELECT id, state FROM content WHERE id = ?", (content_id,)
            ).fetchone()
            if content is None:
                raise NotFoundError("内容不存在。", content_id=content_id)
            if content["state"] == "deleted":
                raise ConflictError("内容已删除，不能添加附件。", content_id=content_id)

            attachment = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if attachment is not None:
                if attachment["content_id"] != content_id:
                    raise ConflictError(
                        "附件标识已属于其他内容，拒绝重新关联。",
                        attachment_id=attachment_id,
                    )
                if attachment["state"] == "QUARANTINED":
                    raise ConflictError(
                        "该附件已被隔离，需要先处理完整性审计结果。",
                        attachment_id=attachment_id,
                    )
                if (
                    attachment["state"] == "PENDING"
                    and attachment["operation_id"] != operation_id
                ):
                    raise ConflictError(
                        "该附件正处于另一个未完成操作中。",
                        attachment_id=attachment_id,
                    )
                if attachment["sha256"]:
                    # A known payload can only be retried with the same operation.
                    if attachment["operation_id"] != operation_id:
                        raise ConflictError(
                            "该附件已存在，需使用原操作标识重试。",
                            attachment_id=attachment_id,
                        )

            operation = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            now = utcnow()
            if operation is not None:
                if operation["kind"] != "attachment_write":
                    # Operations of other business kinds must never be reset or
                    # rebound by an attachment write path.
                    raise ConflictError(
                        "操作标识属于其他业务类型，不能用于附件写入。",
                        operation_id=operation_id,
                    )
                if operation["state"] != "COMPLETED":
                    prior = {
                        "operation": {
                            "state": operation["state"],
                            "result": operation["result"],
                        },
                        "attachment": (
                            {"state": attachment["state"], "note": attachment["note"]}
                            if attachment is not None
                            else None
                        ),
                    }
            if operation is None:
                conn.execute(
                    "INSERT INTO operations(id, kind, resource_id, owner_content_id, "
                    "state, created_at, updated_at) "
                    "VALUES(?, 'attachment_write', ?, ?, 'PENDING', ?, ?)",
                    (operation_id, attachment_id, content_id, now, now),
                )
            else:
                if (
                    operation["resource_id"] != attachment_id
                    or operation["owner_content_id"] != content_id
                ):
                    raise ConflictError(
                        "操作标识已绑定其他附件或内容。",
                        operation_id=operation_id,
                    )
                if operation["state"] == "COMPLETED":
                    # Completed operations are handled by the idempotent path.
                    if attachment is None:
                        raise ConflictError("操作已完成但附件记录缺失。", operation_id=operation_id)
                    if attachment["sha256"] is None:
                        raise ConflictError(
                            "操作已完成但附件缺少已登记载荷哈希，拒绝重新绑定。",
                            attachment_id=attachment_id,
                        )
                else:
                    # Reset only the state. The previous result is history and is
                    # replaced later only by a newly observed outcome.
                    conn.execute(
                        "UPDATE operations SET state = 'PENDING', "
                        "updated_at = ? WHERE id = ?",
                        (now, operation_id),
                    )

            if (
                attachment is not None
                and attachment["sha256"] is None
                and attachment["operation_id"] != operation_id
            ):
                # No payload has been journaled for this attachment. A terminal
                # previous attempt may be superseded by the new operation. The
                # binding moves inside this transaction so a crash cannot leave
                # the new operation without an attachment.
                previous = conn.execute(
                    "SELECT * FROM operations WHERE id = ?",
                    (attachment["operation_id"],),
                ).fetchone()
                if previous is not None:
                    if previous["state"] == "PENDING":
                        raise ConflictError(
                            "该附件正处于另一个未完成操作中。",
                            attachment_id=attachment_id,
                        )
                    if previous["state"] == "COMPLETED":
                        raise ConflictError(
                            "该附件的原操作已完成但载荷哈希缺失，拒绝重新绑定。",
                            attachment_id=attachment_id,
                        )
                    # Keep the previous terminal state (FAILED or UNCERTAIN) and
                    # append an auditable supersede note; never rewrite an
                    # uncertain outcome as a definite failure just to clear it.
                    note = f"superseded by {operation_id}"
                    previous_result = (previous["result"] or "").strip()
                    room = max(0, 500 - len(note) - 2)
                    combined = (
                        f"{previous_result[:room]}; {note}"
                        if previous_result
                        else note
                    )
                    conn.execute(
                        "UPDATE operations SET result = ?, updated_at = ? WHERE id = ?",
                        (combined[:500], now, previous["id"]),
                    )
                conn.execute(
                    "UPDATE attachments SET operation_id = ?, updated_at = ? WHERE id = ?",
                    (operation_id, now, attachment_id),
                )

            if attachment is None:
                # Windows resolves object paths case-insensitively. Two rows
                # whose ids differ only by case would map to one file, so the
                # second reservation is refused instead of silently sharing it.
                case_conflict = conn.execute(
                    "SELECT id FROM attachments "
                    "WHERE id = ? COLLATE NOCASE AND id != ?",
                    (attachment_id, attachment_id),
                ).fetchone()
                if case_conflict is not None:
                    raise ConflictError(
                        "附件标识与已有标识仅大小写不同，"
                        "在大小写不敏感的文件系统上会映射到同一对象路径。",
                        attachment_id=attachment_id,
                    )
                conn.execute(
                    "INSERT INTO attachments(id, content_id, filename, byte_size, sha256, "
                    "mime, visibility, state, operation_id, created_at, updated_at) "
                    "VALUES(?, ?, ?, NULL, NULL, ?, ?, 'PENDING', ?, ?, ?)",
                    (
                        attachment_id,
                        content_id,
                        filename,
                        mime,
                        visibility,
                        operation_id,
                        now,
                        now,
                    ),
                )
        return prior

    def _reject_attempt(
        self,
        attachment_id: str,
        operation_id: str,
        prior: dict[str, Any] | None,
        note: str,
    ) -> None:
        """Record a definite rejection without destroying a retry's history.

        A first attempt has no history to protect and is marked FAILED. A retry
        puts the previous state and result back with a compare-and-set, so a
        later reader sees the last real outcome rather than a reset row.
        """
        if prior is None:
            self._mark_failed(attachment_id, operation_id, note)
        else:
            self._restore_operation(attachment_id, operation_id, prior)

    def _restore_operation(
        self,
        attachment_id: str,
        operation_id: str,
        prior: dict[str, Any],
    ) -> None:
        try:
            with transaction(self.db_path) as conn:
                conn.execute(
                    "UPDATE operations SET state = ?, result = ?, updated_at = ? "
                    "WHERE id = ? AND state = 'PENDING'",
                    (
                        prior["operation"]["state"],
                        prior["operation"]["result"],
                        utcnow(),
                        operation_id,
                    ),
                )
                previous_attachment = prior.get("attachment")
                if previous_attachment is not None:
                    conn.execute(
                        "UPDATE attachments SET state = ?, note = ?, "
                        "updated_at = ? WHERE id = ? AND operation_id = ? "
                        "AND state = 'PENDING'",
                        (
                            previous_attachment["state"],
                            previous_attachment["note"],
                            utcnow(),
                            attachment_id,
                            operation_id,
                        ),
                    )
        except sqlite3.Error as exc:
            logger.warning(
                "could not restore prior operation state "
                "(attachment=%s operation=%s): %s",
                attachment_id,
                operation_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _mark_failed(
        self,
        attachment_id: str,
        operation_id: str,
        note: str,
        *,
        issue: dict[str, str] | None = None,
        checked_at: str | None = None,
        prior_uncertain: bool = False,
    ) -> None:
        try:
            with transaction(self.db_path) as conn:
                # Only downgrade rows this attempt still owns. A concurrent
                # duplicate that already committed READY/COMPLETED must win.
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'BROKEN', note = ?, updated_at = ? "
                    "WHERE id = ? AND operation_id = ? "
                    "AND state NOT IN ('READY', 'QUARANTINED')",
                    (note[:500], utcnow(), attachment_id, operation_id),
                )
                if (
                    issue is not None
                    and checked_at is not None
                    and attachment_update.rowcount == 1
                ):
                    # The integrity issue is written in the same transaction as
                    # the state it describes; if a concurrent success won, the
                    # stale issue is not written at all.
                    _upsert_issue(conn, issue, checked_at)
                operation = conn.execute(
                    "SELECT state, payload_sha256 FROM operations WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                journaled = bool(
                    operation is not None and operation["payload_sha256"]
                )
                already_uncertain = bool(
                    prior_uncertain
                    or (operation is not None and operation["state"] == "UNCERTAIN")
                )
                if journaled or already_uncertain:
                    # Bytes may still exist for this attempt, or a previous
                    # attempt already reported an uncertain outcome. Neither can
                    # be honestly reduced to a definite failure.
                    conn.execute(
                        "UPDATE operations SET state = 'UNCERTAIN', result = ?, "
                        "updated_at = ? WHERE id = ? "
                        "AND state IN ('PENDING', 'UNCERTAIN')",
                        (note[:500], utcnow(), operation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE operations SET state = 'FAILED', result = ?, updated_at = ? "
                        "WHERE id = ? AND state IN ('PENDING', 'UNCERTAIN')",
                        (note[:500], utcnow(), operation_id),
                    )
        except Exception as exc:
            # Never mask the original error with a secondary bookkeeping failure,
            # but never hide it either: a swallowed bookkeeping error is exactly
            # how a caller ends up with a state that nothing can explain.
            logger.warning(
                "attachment bookkeeping failed (attachment=%s operation=%s): %s",
                attachment_id,
                operation_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _mark_conflict_broken(
        self,
        attachment_id: str,
        operation_id: str,
        snapshot: dict[str, Any],
        note: str,
        *,
        issue: dict[str, str] | None = None,
        checked_at: str | None = None,
    ) -> None:
        """Mark a READY attachment BROKEN after a proven object conflict.

        The update is a CAS on the exact row version observed before journaling,
        so a duplicate upload that committed a newer version is never downgraded.
        The operation row is deliberately left untouched: if it was already
        COMPLETED, this attempt must not rewrite that history.
        """
        try:
            with transaction(self.db_path) as conn:
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'BROKEN', note = ?, updated_at = ? "
                    "WHERE id = ? AND operation_id = ? AND state = ? "
                    "AND sha256 IS ? AND byte_size IS ? AND updated_at = ?",
                    (
                        note[:500],
                        utcnow(),
                        attachment_id,
                        operation_id,
                        snapshot["state"],
                        snapshot["sha256"],
                        snapshot["byte_size"],
                        snapshot["updated_at"],
                    ),
                )
                if (
                    issue is not None
                    and checked_at is not None
                    and attachment_update.rowcount == 1
                ):
                    _upsert_issue(conn, issue, checked_at)
        except Exception as exc:
            logger.warning(
                "attachment conflict bookkeeping failed (attachment=%s operation=%s): %s",
                attachment_id,
                operation_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _mark_uncertain_after_ready_failure(
        self, attachment_id: str, operation_id: str
    ) -> None:
        """Mark an uncertain READY commit without overwriting a concurrent success.

        The conditional updates are deliberately not reported as errors when
        they match no row: a concurrent duplicate may have committed
        READY/COMPLETED while this attempt was failing, and that result must
        stand. The caller still raises UncertainOutcomeError for this attempt.
        """
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE operations SET state = 'UNCERTAIN', "
                "result = 'metadata commit failed', updated_at = ? "
                "WHERE id = ? AND state = 'PENDING'",
                (utcnow(), operation_id),
            )
            conn.execute(
                "UPDATE attachments SET state = 'PENDING', note = ?, "
                "updated_at = ? WHERE id = ? AND operation_id = ? AND state = 'PENDING'",
                ("资源已就位但元数据未确认", utcnow(), attachment_id, operation_id),
            )

    def _mark_uncertain_retained(
        self, attachment_id: str, operation_id: str, note: str
    ) -> None:
        """Keep an attempt UNCERTAIN while its staged bytes remain recoverable."""
        try:
            with transaction(self.db_path) as conn:
                conn.execute(
                    "UPDATE operations SET state = 'UNCERTAIN', result = ?, "
                    "updated_at = ? WHERE id = ? "
                    "AND state IN ('PENDING', 'UNCERTAIN')",
                    (note[:500], utcnow(), operation_id),
                )
                conn.execute(
                    "UPDATE attachments SET state = 'PENDING', note = ?, "
                    "updated_at = ? WHERE id = ? AND operation_id = ? "
                    "AND state = 'PENDING'",
                    (note[:500], utcnow(), attachment_id, operation_id),
                )
        except sqlite3.Error as exc:
            logger.warning(
                "could not record retained-staging outcome "
                "(attachment=%s operation=%s): %s",
                attachment_id,
                operation_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _retain_staging(
        self,
        staging_path: Path,
        *,
        kind: str,
        exc: BaseException,
        digest: str,
        size: int,
        attachment_id: str,
        operation_id: str,
    ) -> None:
        """Retain verified staging bytes and record exactly what was kept.

        The record carries the payload identity and error class only. It never
        carries the absolute staging path, which is host-specific.
        """
        note = (
            "暂存字节未写入日志，已保留。"
            if kind == "staging_unjournaled_retained"
            else "资源提升失败，暂存字节已保留。"
        )
        self._mark_uncertain_retained(attachment_id, operation_id, note)
        issue = {
            "kind": kind,
            "target": staging_path.name,
            "detail": json.dumps(
                {
                    "sha256": digest,
                    "bytes": size,
                    "error_class": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                },
                ensure_ascii=False,
            ),
        }
        if not self._persist_issue_now(issue, utcnow()):
            logger.warning(
                "could not persist retained-staging issue (%s, %s)",
                kind,
                staging_path.name,
            )

    # ------------------------------------------------------------------
    # Reconciliation and audit
    # ------------------------------------------------------------------
    def reconcile(self) -> dict[str, Any]:
        """Run one reconciliation while serializing with other reconciles.

        The lock is keyed by the resolved Archive data directory, so two
        attachments stores pointing at the same Archive cannot use different
        snapshots at the same time. Different archives do not block each other.
        """
        with _reconcile_lock_for(self._archive_key):
            return self._reconcile()

    def _reconcile(self) -> dict[str, Any]:
        checked_at = utcnow()
        # Recoverable commit protocol. The marker is written before the scan and
        # moved to "committing_report" before the final transaction; the same
        # transaction that stores the report marks it "completed". A crash or a
        # failed report therefore leaves a non-completed marker, and readers can
        # tell that any earlier report is stale instead of trusting "ok".
        attempt_id = f"rec_{uuid.uuid4().hex}"
        attempt: dict[str, Any] = {
            "attempt_id": attempt_id,
            "started_at": checked_at,
            "phase": "scanning",
            "state": "in_progress",
        }
        self._write_reconcile_attempt(attempt)
        actions: list[dict[str, Any]] = []
        issues: list[dict[str, str]] = []
        # Per-item failures that did not stop the scan. They are reported next
        # to the issues so "ok=true" can never hide a skipped audit item.
        errors: list[dict[str, Any]] = []
        quarantined: list[str] = []
        # Fingerprints already persisted inside the same transaction as their
        # state transition. The final _record_issues() must not touch them
        # again, otherwise an upload that healed during the scan could still be
        # stamped with a later last_seen for a conclusion that is no longer true.
        persisted: set[str] = set()
        # Per-cause evidence collected by this scan. An issue is closed only
        # when the specific condition it recorded is confirmed gone, so one
        # kind never closes another kind's finding for the same target.
        object_present_ids: set[str] = set()
        metadata_complete_ids: set[str] = set()
        bytes_match_ids: set[str] = set()
        healthy_ids: set[str] = set()
        # Targets positively observed as a readable regular file in this scan.
        readable_ids: set[str] = set()
        # Operation ids whose staging area was positively settled this scan:
        # nothing left to inspect, a definite disposition was applied, or the
        # staged bytes were successfully probed. Keyed by operation id, the same
        # identity used as the finding target, so no other operation's finding
        # can be closed by this evidence.
        staging_settled: set[str] = set()
        # File targets observed as referenced/known by the directory sweep.
        # Kept separate from attachment IDs because an object filename and an
        # attachment ID share the same namespace.
        checked_files: set[str] = set()
        # Targets deliberately skipped because an upload owns them. They must
        # never be treated as healed merely because no issue was emitted.
        blocked: set[str] = set()
        # Staging files already judged (and possibly failed to move) in the
        # pending pass must not be reported again by the final sweep of the
        # same scan. The next reconcile retries them.
        staging_attempted: set[str] = set()
        # Quarantine inventory records produced by this scan. A successful move
        # is recorded immediately; if that write fails, the record is retried
        # once inside the final _record_issues() transaction.
        quarantine_records: list[dict[str, Any]] = []
        quarantine_inventory: list[dict[str, Any]] = []
        quarantine_moves: list[dict[str, Any]] = []
        quarantined_attachments: list[dict[str, Any]] = []

        def quarantine_path(
            path: Path, origin: str, reason: str
        ) -> tuple[Path, bool, dict[str, Any]]:
            target, moved, meta = self._quarantine_file(
                path, origin=origin, reason=reason, checked_at=checked_at
            )
            if moved:
                # Reports are API payloads, so only the quarantine file name
                # (already unique) is published; the absolute directory stays
                # internal. Origin/reason travel beside it for the operator.
                quarantined.append(target.name)
                quarantine_moves.append(
                    {"name": target.name, "origin": origin, "reason": reason}
                )
                if meta.get("recorded"):
                    persisted.add(_issue_fingerprint(self._quarantine_issue(meta)))
                else:
                    quarantine_records.append(meta)
            return target, moved, meta

        def record_object_anomaly(
            target: str, status: str, exc: OSError | None
        ) -> None:
            """Report one uninspectable item and let the scan continue."""
            kind = _OBJECT_ANOMALY_KINDS[status]
            issues.append(
                {
                    "kind": kind,
                    "target": target,
                    "detail": _OBJECT_ANOMALY_DETAILS[status],
                }
            )
            errors.append(
                {
                    "target": target,
                    "kind": kind,
                    "status": status,
                    "error_class": type(exc).__name__ if exc is not None else None,
                    "errno": exc.errno if exc is not None else None,
                }
            )

        def record_staging_anomaly(
            operation_id: str, stage_name: str | None, status: str, exc: OSError | None
        ) -> None:
            """Report an unsettled staging area against a stable identity.

            The target is the operation id, never the staging file name, so the
            finding can be closed by a later scan that settles the same
            operation even after the file was renamed or replaced.
            """
            detail = _OBJECT_ANOMALY_DETAILS[status]
            if stage_name:
                detail = f"{detail}（暂存文件 {stage_name}）"
            issues.append(
                {
                    "kind": _STAGING_ANOMALY_KIND,
                    "target": operation_id,
                    "detail": detail,
                }
            )
            errors.append(
                {
                    "target": operation_id,
                    "kind": _STAGING_ANOMALY_KIND,
                    "status": status,
                    "stage": stage_name,
                    "error_class": type(exc).__name__ if exc is not None else None,
                    "errno": exc.errno if exc is not None else None,
                }
            )

        conn = connect(self.db_path)
        try:
            pending = conn.execute(
                "SELECT o.id AS operation_id, o.state AS operation_state, "
                "o.resource_id, o.payload_sha256, o.payload_size, "
                "o.result AS operation_result, "
                "o.updated_at AS operation_updated_at, "
                "a.id AS attachment_id, a.content_id, a.sha256, a.byte_size, "
                "a.filename, a.state AS attachment_state, "
                "a.updated_at AS attachment_updated_at "
                "FROM operations o LEFT JOIN attachments a ON a.operation_id = o.id "
                "WHERE o.state IN ('PENDING', 'UNCERTAIN')"
            ).fetchall()
        finally:
            conn.close()

        for row in pending:
            operation_id = row["operation_id"]
            attachment_id = row["attachment_id"]
            if self._upload_is_active(operation_id):
                # The operation still belongs to an in-flight upload. Its files
                # and state are not crash evidence; leave both untouched.
                blocked.add(operation_id)
                if attachment_id is not None:
                    blocked.add(attachment_id)
                actions.append(
                    {
                        "action": "skipped_active_upload",
                        "attachment_id": attachment_id,
                        "operation_id": operation_id,
                        "reason": "upload is still in progress",
                    }
                )
                continue
            if attachment_id is None:
                if "superseded by " in (row["operation_result"] or ""):
                    # A terminal attempt deliberately superseded by a newer
                    # operation. Its original state and result stay auditable;
                    # it is not a new orphan or integrity problem.
                    continue
                issue = {
                    "kind": "operation_without_attachment",
                    "target": operation_id,
                    "detail": "操作存在，但没有对应的附件记录。",
                }
                if not self._set_operation_state(
                    operation_id,
                    "UNCERTAIN",
                    "operation has no attachment",
                    expected_state=row["operation_state"],
                    expected_updated_at=row["operation_updated_at"],
                    issue=issue,
                    checked_at=checked_at,
                ):
                    actions.append(
                        {
                            "action": "skipped_stale_reconciliation",
                            "attachment_id": None,
                            "operation_id": operation_id,
                            "reason": "operation state changed during scan",
                        }
                    )
                    continue
                persisted.add(_issue_fingerprint(issue))
                issues.append(issue)
                continue

            if row["attachment_state"] == "QUARANTINED":
                # Quarantine is a deliberate, durable state. A leftover pending
                # operation must never be used to promote the attachment back to
                # READY or to rewrite its metadata. The audit pass below moves
                # any object that still sits in the active objects directory.
                issue = {
                    "kind": _QUARANTINED_PENDING_OPERATION_KIND,
                    "target": attachment_id,
                    "detail": (
                        "附件状态为 QUARANTINED，但仍有关联操作未终止；"
                        "对账不会提升、恢复或改写该附件。"
                    ),
                }
                issues.append(issue)
                actions.append(
                    {
                        "action": "skipped_quarantined_attachment",
                        "attachment_id": attachment_id,
                        "operation_id": operation_id,
                        "reason": "attachment is quarantined",
                    }
                )
                continue

            snapshot = {
                "attachment_state": row["attachment_state"],
                "attachment_sha256": row["sha256"],
                "attachment_size": row["byte_size"],
                "attachment_updated_at": row["attachment_updated_at"],
                "operation_state": row["operation_state"],
                "payload_sha256": row["payload_sha256"],
                "payload_size": row["payload_size"],
                "operation_updated_at": row["operation_updated_at"],
            }

            expected_hash = row["sha256"] or row["payload_sha256"]
            expected_size = row["byte_size"] if row["byte_size"] is not None else row["payload_size"]
            journal_missing = expected_hash is None or expected_size is None
            final_path = self.objects / attachment_id
            probe_status, probe_digest, probe_size, probe_error = _probe_object(
                final_path
            )
            if probe_status in _OBJECT_ANOMALY_KINDS:
                # Leave state, bytes and the operation untouched: the scan did
                # not learn anything about them, and the next scan retries.
                record_object_anomaly(attachment_id, probe_status, probe_error)
                continue
            if probe_status == "regular":
                readable_ids.add(attachment_id)

            if probe_status == "regular":
                if journal_missing:
                    # Without a journaled payload hash/size there is no durable
                    # proof that these bytes are the committed payload. Keep the
                    # object untouched, but never confirm it as READY.
                    issue = {
                        "kind": "final_without_journaled_payload",
                        "target": attachment_id,
                        "detail": "存在最终对象，但记录中缺少已登记的载荷哈希或大小；对象保持原样，未确认 READY。",
                    }
                    if not self._mark_broken_reconciliation(
                        attachment_id,
                        operation_id,
                        "最终资源缺少已登记的载荷哈希或大小，无法确认。",
                        snapshot=snapshot,
                        issue=issue,
                        checked_at=checked_at,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_reconciliation",
                                "attachment_id": attachment_id,
                                "operation_id": operation_id,
                                "reason": "state changed during scan",
                            }
                        )
                        continue
                    persisted.add(_issue_fingerprint(issue))
                    issues.append(issue)
                    continue
                digest, size = probe_digest, probe_size
                if digest == expected_hash and size == expected_size:
                    if not self._complete_reconciliation(
                        attachment_id,
                        operation_id,
                        digest,
                        size,
                        "final_resource_confirmed",
                        snapshot=snapshot,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_reconciliation",
                                "attachment_id": attachment_id,
                                "operation_id": operation_id,
                                "reason": "state changed during scan",
                            }
                        )
                        continue
                    actions.append(
                        {
                            "action": "confirmed_final_resource",
                            "attachment_id": attachment_id,
                            "operation_id": operation_id,
                        }
                    )
                else:
                    issue = {
                        "kind": "final_hash_mismatch",
                        "target": attachment_id,
                        "detail": "最终资源与预期哈希不一致，文件保持原样。",
                    }
                    if not self._mark_broken_reconciliation(
                        attachment_id,
                        operation_id,
                        "最终资源哈希与操作记录不一致，未覆盖文件。",
                        snapshot=snapshot,
                        issue=issue,
                        checked_at=checked_at,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_reconciliation",
                                "attachment_id": attachment_id,
                                "operation_id": operation_id,
                                "reason": "state changed during scan",
                            }
                        )
                        continue
                    persisted.add(_issue_fingerprint(issue))
                    issues.append(issue)
                continue

            staging_files = self._staging_snapshot(operation_id)
            if staging_files is None:
                blocked.add(operation_id)
                if attachment_id is not None:
                    blocked.add(attachment_id)
                actions.append(
                    {
                        "action": "skipped_active_upload",
                        "attachment_id": attachment_id,
                        "operation_id": operation_id,
                        "reason": "upload registered while the scan was running",
                    }
                )
                continue
            if staging_files:
                try:
                    stage = max(staging_files, key=lambda p: p.stat().st_mtime)
                except OSError as exc:
                    # The journaled bytes may or may not still exist. Neither
                    # promote nor quarantine: report against the operation and
                    # let the next scan retry.
                    record_staging_anomaly(operation_id, None, "unreadable", exc)
                    continue
                if journal_missing:
                    # A crash can happen mid-write, before the payload hash is
                    # journaled. Such a file must never be promoted: it may be
                    # truncated or empty. Move it first so the persisted issue
                    # describes the actual filesystem result; a non-active
                    # staging file at this point is a crash leftover.
                    target, moved, _meta = quarantine_path(
                        stage, "staging", "staging_without_journaled_payload"
                    )
                    staging_attempted.add(stage.name)
                    staging_settled.add(operation_id)
                    issue = {
                        "kind": "staging_without_journaled_payload",
                        "target": attachment_id,
                        "detail": (
                            "暂存文件未被任何已登记的载荷哈希覆盖，已非破坏性隔离，未提升为 READY。"
                            if moved
                            else "暂存文件未被任何已登记的载荷哈希覆盖，隔离失败，文件保留在暂存区，未提升为 READY。"
                        ),
                    }
                    # Persist the file disposition before the attachment CAS. A
                    # concurrent upload may win the row; the quarantined file
                    # must still have a durable finding even when the state
                    # transition is correctly skipped as stale.
                    if self._persist_issue_now(issue, checked_at):
                        persisted.add(_issue_fingerprint(issue))
                    issues.append(issue)
                    if not self._mark_broken_reconciliation(
                        attachment_id,
                        operation_id,
                        (
                            "暂存文件缺少已登记的载荷哈希或大小，已隔离。"
                            if moved
                            else "暂存文件缺少已登记的载荷哈希或大小，隔离失败，文件保留在暂存区。"
                        ),
                        snapshot=snapshot,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_reconciliation",
                                "attachment_id": attachment_id,
                                "operation_id": operation_id,
                                "reason": "state changed during scan",
                            }
                        )
                        continue
                else:
                    stage_status, stage_digest, stage_size, stage_error = (
                        _probe_object(stage)
                    )
                    if stage_status != "regular":
                        # A journaled staging payload that cannot be read is
                        # never promoted and never deleted. Report it against
                        # the operation and let a later scan retry once the
                        # staging area can be settled.
                        if stage_status == "absent":
                            record_staging_anomaly(
                                operation_id, stage.name, "unreadable", None
                            )
                        else:
                            record_staging_anomaly(
                                operation_id, stage.name, stage_status, stage_error
                            )
                        continue
                    readable_ids.add(stage.name)
                    # The staged bytes were positively probed: the staging area
                    # for this operation is settled even if its hash does not
                    # match, because any remaining problem gets its own finding.
                    staging_settled.add(operation_id)
                    digest, size = stage_digest, stage_size
                    if digest == expected_hash and size == expected_size:
                        os.replace(stage, final_path)
                        _fsync_directory(self.objects)
                        if not self._complete_reconciliation(
                            attachment_id,
                            operation_id,
                            digest,
                            size,
                            "staging_promoted_after_restart",
                            snapshot=snapshot,
                        ):
                            actions.append(
                                {
                                    "action": "skipped_stale_reconciliation",
                                    "attachment_id": attachment_id,
                                    "operation_id": operation_id,
                                    "reason": "state changed during scan",
                                }
                            )
                            continue
                        actions.append(
                            {
                                "action": "resumed_staging_promotion",
                                "attachment_id": attachment_id,
                                "operation_id": operation_id,
                            }
                        )
                    else:
                        target, moved, _meta = quarantine_path(
                            stage, "staging", "staging_hash_mismatch"
                        )
                        staging_attempted.add(stage.name)
                        issue = {
                            "kind": "staging_hash_mismatch",
                            "target": attachment_id,
                            "detail": (
                                "暂存文件与预期哈希不一致，已非破坏性隔离。"
                                if moved
                                else "暂存文件与预期哈希不一致，隔离失败，文件保留在暂存区。"
                            ),
                        }
                        if self._persist_issue_now(issue, checked_at):
                            persisted.add(_issue_fingerprint(issue))
                        issues.append(issue)
                        if not self._mark_broken_reconciliation(
                            attachment_id,
                            operation_id,
                            (
                                "暂存文件哈希与操作记录不一致，已隔离。"
                                if moved
                                else "暂存文件哈希与操作记录不一致，隔离失败，文件保留在暂存区。"
                            ),
                            snapshot=snapshot,
                        ):
                            actions.append(
                                {
                                    "action": "skipped_stale_reconciliation",
                                    "attachment_id": attachment_id,
                                    "operation_id": operation_id,
                                    "reason": "state changed during scan",
                                }
                            )
                            continue
            else:
                # Nothing left to inspect: the staging area is settled, and any
                # remaining problem is represented by pending_resource_missing.
                staging_settled.add(operation_id)
                issue = {
                    "kind": "pending_resource_missing",
                    "target": attachment_id,
                    "detail": "未完成操作缺少任何可用资源。",
                }
                if not self._mark_broken_reconciliation(
                    attachment_id,
                    operation_id,
                    "没有可用的暂存或最终资源。",
                    snapshot=snapshot,
                    issue=issue,
                    checked_at=checked_at,
                ):
                    actions.append(
                        {
                            "action": "skipped_stale_reconciliation",
                            "attachment_id": attachment_id,
                            "operation_id": operation_id,
                            "reason": "state changed during scan",
                        }
                    )
                    continue
                persisted.add(_issue_fingerprint(issue))
                issues.append(issue)

        # A terminal operation proves only that the write attempt ended; it does
        # not prove the attachment resource exists. Re-examine every terminal
        # operation whose stable resource identity still resolves to no
        # attachment row, and re-raise the finding so a missing resource can
        # never be hidden behind a closed issue or a clean report.
        conn = connect(self.db_path)
        try:
            terminal_orphans = conn.execute(
                "SELECT o.id AS operation_id, o.resource_id "
                "FROM operations o "
                "WHERE o.state NOT IN ('PENDING', 'UNCERTAIN') "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM attachments a WHERE a.operation_id = o.id"
                ") "
                "AND (o.resource_id IS NULL OR NOT EXISTS ("
                "    SELECT 1 FROM attachments a2 WHERE a2.id = o.resource_id"
                "))"
            ).fetchall()
        finally:
            conn.close()
        for row in terminal_orphans:
            operation_id = row["operation_id"]
            if self._upload_is_active(operation_id):
                blocked.add(operation_id)
                if row["resource_id"] is not None:
                    blocked.add(row["resource_id"])
                actions.append(
                    {
                        "action": "skipped_active_upload",
                        "attachment_id": row["resource_id"],
                        "operation_id": operation_id,
                        "reason": "upload is still in progress",
                    }
                )
                continue
            issue = {
                "kind": "operation_without_attachment",
                "target": operation_id,
                "detail": (
                    "操作已结束，但按稳定资源身份仍找不到对应的附件记录；"
                    "缺失资源不会因操作终止而视为已解决。"
                ),
            }
            issues.append(issue)

        # READY metadata must point at a present, matching file. BROKEN rows are
        # audited even when the payload hash/size is absent: missing metadata is
        # itself an integrity finding and cannot be treated as a clean scan.
        # QUARANTINED rows are audited separately so a quarantined object can
        # never remain in the active objects directory or be revived as READY.
        conn = connect(self.db_path)
        try:
            audited = conn.execute(
                "SELECT id, content_id, state, sha256, byte_size, operation_id, updated_at "
                "FROM attachments "
                "WHERE state IN ('READY', 'BROKEN', 'QUARANTINED')"
            ).fetchall()
        finally:
            conn.close()
        for row in audited:
            if self._upload_is_active_for_attachment(row["id"]):
                # A READY row being repaired by an upload is not stable audit
                # evidence; its files must not be moved or judged mid-write.
                blocked.add(row["id"])
                if row["operation_id"] is not None:
                    blocked.add(row["operation_id"])
                actions.append(
                    {
                        "action": "skipped_active_upload",
                        "attachment_id": row["id"],
                        "operation_id": row["operation_id"],
                        "reason": "attachment upload is in progress",
                    }
                )
                continue
            if row["state"] == "QUARANTINED":
                path = self.objects / row["id"]
                object_present = path.is_file()
                disposition = "no_active_object"
                if object_present:
                    target, moved, _meta = quarantine_path(
                        path, "objects", "quarantined_attachment_object"
                    )
                    if moved:
                        disposition = "moved_to_quarantine"
                        issues.append(
                            {
                                "kind": _QUARANTINED_OBJECT_MOVED_KIND,
                                "target": row["id"],
                                "detail": (
                                    "附件状态为 QUARANTINED，但最终对象仍位于活动对象目录；"
                                    "已非破坏性移入隔离区，附件状态未改变。"
                                ),
                            }
                        )
                    else:
                        disposition = "quarantine_move_failed"
                        issues.append(
                            {
                                "kind": _QUARANTINED_OBJECT_MOVE_FAILED_KIND,
                                "target": row["id"],
                                "detail": (
                                    "附件状态为 QUARANTINED，但最终对象仍位于活动对象目录；"
                                    "隔离失败，文件保持原样，下次对账重试。"
                                ),
                            }
                        )
                quarantined_attachments.append(
                    {
                        "id": row["id"],
                        "content_id": row["content_id"],
                        "operation_id": row["operation_id"],
                        "state": "QUARANTINED",
                        "active_object_present": object_present,
                        "disposition": disposition,
                    }
                )
                continue
            audit_snapshot = {
                "state": row["state"],
                "sha256": row["sha256"],
                "size": row["byte_size"],
                "operation_id": row["operation_id"],
                "updated_at": row["updated_at"],
            }
            path = self.objects / row["id"]
            metadata_missing = row["sha256"] is None or row["byte_size"] is None
            probe_status, probe_digest, probe_size, probe_error = _probe_object(path)
            if probe_status in _OBJECT_ANOMALY_KINDS:
                # One uninspectable object must not stop the audit of the rest.
                # State is left untouched: this scan cannot prove corruption.
                record_object_anomaly(row["id"], probe_status, probe_error)
                continue
            object_present = probe_status == "regular"
            bytes_match = False
            if object_present:
                readable_ids.add(row["id"])
            if object_present and not metadata_missing:
                bytes_match = (
                    probe_digest == row["sha256"] and probe_size == row["byte_size"]
                )
            # Per-cause evidence for this scan. A cause counts as gone only
            # when the scan positively observed it gone; the absence of a
            # finding in this scan is never treated as proof.
            if object_present:
                object_present_ids.add(row["id"])
            if not metadata_missing:
                metadata_complete_ids.add(row["id"])
            if bytes_match:
                bytes_match_ids.add(row["id"])
            if row["state"] == "READY" and bytes_match:
                # A READY row whose object and metadata agree is the only
                # verified-healthy state; it may close the findings that
                # described the same attachment.
                healthy_ids.add(row["id"])
            if not object_present:
                audit_issues: list[dict[str, str]] = []
                if metadata_missing:
                    audit_issues.append(
                        {
                            "kind": "attachment_metadata_incomplete",
                            "target": row["id"],
                            "detail": (
                                "附件缺少已登记的 SHA-256 或大小，且最终对象不存在，"
                                "无法验证。"
                            ),
                        }
                    )
                if row["state"] == "READY":
                    audit_issues.append(
                        {
                            "kind": "ready_file_missing",
                            "target": row["id"],
                            "detail": "元数据为 READY，但最终文件不存在。",
                        }
                    )
                    if not self._mark_attachment_broken(
                        row["id"],
                        "已就绪附件缺少最终文件。",
                        snapshot=audit_snapshot,
                        issues=audit_issues,
                        checked_at=checked_at,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_audit",
                                "attachment_id": row["id"],
                                "reason": "attachment changed during scan",
                            }
                        )
                        continue
                else:
                    audit_issues.append(
                        {
                            "kind": "broken_resource_missing",
                            "target": row["id"],
                            "detail": "附件已损坏且缺少最终文件。",
                        }
                    )
                    if not self._record_issues_if_current(
                        audit_issues, audit_snapshot, checked_at
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_audit",
                                "attachment_id": row["id"],
                                "reason": "attachment changed during scan",
                            }
                        )
                        continue
                for audit_issue in audit_issues:
                    persisted.add(_issue_fingerprint(audit_issue))
                    issues.append(audit_issue)
                continue
            if metadata_missing:
                audit_issue = {
                    "kind": "attachment_metadata_incomplete",
                    "target": row["id"],
                    "detail": (
                        "最终对象存在，但记录缺少 SHA-256 或大小，无法验证；"
                        "未标记 READY。"
                    ),
                }
                if row["state"] == "READY":
                    if not self._mark_attachment_broken(
                        row["id"],
                        "已就绪附件缺少可验证的载荷元数据。",
                        snapshot=audit_snapshot,
                        issue=audit_issue,
                        checked_at=checked_at,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_audit",
                                "attachment_id": row["id"],
                                "reason": "attachment changed during scan",
                            }
                        )
                        continue
                elif not self._record_issue_if_current(
                    audit_issue, audit_snapshot, checked_at
                ):
                    actions.append(
                        {
                            "action": "skipped_stale_audit",
                            "attachment_id": row["id"],
                            "reason": "attachment changed during scan",
                        }
                    )
                    continue
                persisted.add(_issue_fingerprint(audit_issue))
                issues.append(audit_issue)
                continue
            if not bytes_match:
                issue = {
                    "kind": "ready_hash_mismatch",
                    "target": row["id"],
                    "detail": "最终文件哈希与已记录元数据不一致。",
                }
                if row["state"] == "READY":
                    if not self._mark_attachment_broken(
                        row["id"],
                        "最终文件哈希与元数据不一致。",
                        snapshot=audit_snapshot,
                        issue=issue,
                        checked_at=checked_at,
                    ):
                        actions.append(
                            {
                                "action": "skipped_stale_audit",
                                "attachment_id": row["id"],
                                "reason": "attachment changed during scan",
                            }
                        )
                        continue
                elif not self._record_issue_if_current(
                    issue, audit_snapshot, checked_at
                ):
                    actions.append(
                        {
                            "action": "skipped_stale_audit",
                            "attachment_id": row["id"],
                            "reason": "attachment changed during scan",
                        }
                    )
                    continue
                persisted.add(_issue_fingerprint(issue))
                issues.append(issue)
                continue
            if row["state"] == "BROKEN":
                # Byte equality alone does not prove a legitimate recovery: no
                # committed metadata ever acknowledged these bytes as a verified
                # READY write. Reconcile therefore keeps BROKEN and reports the
                # outstanding recovery action instead of returning a clean scan.
                issue = {
                    "kind": "broken_state_unrecovered",
                    "target": row["id"],
                    "detail": (
                        "附件状态为 BROKEN，最终对象虽与已登记元数据字节一致，"
                        "但对账不会自动将其视为已恢复；需通过同字节重新上传、"
                        "备份恢复或人工处置完成合法恢复。"
                    ),
                }
                if not self._record_issue_if_current(
                    issue, audit_snapshot, checked_at
                ):
                    actions.append(
                        {
                            "action": "skipped_stale_audit",
                            "attachment_id": row["id"],
                            "reason": "attachment changed during scan",
                        }
                    )
                    continue
                persisted.add(_issue_fingerprint(issue))
                issues.append(issue)
            # READY rows that reach this point are verified healthy; the audit
            # preamble already recorded them as positive evidence.

        # Owner rows are part of the identity boundary.
        conn = connect(self.db_path)
        try:
            orphan_owner = conn.execute(
                "SELECT a.id FROM attachments a LEFT JOIN content c ON c.id = a.content_id "
                "WHERE c.id IS NULL"
            ).fetchall()
        finally:
            conn.close()
        for row in orphan_owner:
            issues.append(
                {
                    "kind": "attachment_owner_missing",
                    "target": row["id"],
                    "detail": "附件记录指向不存在的内容身份。",
                }
            )

        # Unreferenced final objects and leftover staging files are quarantined,
        # never deleted. Listing and moving happen under the upload registry lock
        # so an upload cannot register a new file between the snapshot and the
        # move. Referenced ids are re-read under the same lock for the same
        # reason: a row committed by a just-finished upload must be visible.
        with _ACTIVE_UPLOAD_LOCK:
            active_attachments = self._active_attachments_locked()
            conn = connect(self.db_path)
            try:
                referenced_ids = {
                    r["id"] for r in conn.execute("SELECT id FROM attachments").fetchall()
                }
            finally:
                conn.close()
            listing_ok = True
            object_paths, object_listing_error = _safe_iterdir(self.objects)
            if object_listing_error is not None:
                listing_ok = False
                logger.warning(
                    "reconcile: cannot list objects directory: %s",
                    type(object_listing_error).__name__,
                )
                errors.append(
                    {
                        "target": "objects",
                        "kind": "directory_unreadable",
                        "status": "unreadable",
                        "error_class": type(object_listing_error).__name__,
                        "errno": object_listing_error.errno,
                    }
                )
            for path in object_paths:
                if not path.is_file():
                    continue
                if path.name in active_attachments:
                    blocked.add(path.name)
                    actions.append(
                        {
                            "action": "skipped_active_upload",
                            "attachment_id": path.name,
                            "operation_id": None,
                            "object_file": path.name,
                            "reason": "object belongs to an upload in progress",
                        }
                    )
                    continue
                if path.name in referenced_ids:
                    checked_files.add(path.name)
                    continue
                _target, moved, _meta = quarantine_path(
                    path, "objects", "orphan_object"
                )
                if moved:
                    issues.append(
                        {
                            "kind": "orphan_object_quarantined",
                            "target": path.name,
                            "detail": "最终资源没有对应附件元数据，已隔离。",
                        }
                    )
                else:
                    issues.append(
                        {
                            "kind": "orphan_object_quarantine_failed",
                            "target": path.name,
                            "detail": "最终资源没有对应附件元数据，隔离失败，文件保留在最终资源目录。",
                        }
                    )

            active_operations = set(_ACTIVE_UPLOADS.get(self._archive_key, {}))
            staging_paths, staging_listing_error = _safe_iterdir(self.staging)
            if staging_listing_error is not None:
                listing_ok = False
                logger.warning(
                    "reconcile: cannot list staging directory: %s",
                    type(staging_listing_error).__name__,
                )
                errors.append(
                    {
                        "target": "staging",
                        "kind": "directory_unreadable",
                        "status": "unreadable",
                        "error_class": type(staging_listing_error).__name__,
                        "errno": staging_listing_error.errno,
                    }
                )
            for path in staging_paths:
                if not path.is_file():
                    continue
                if path.name in staging_attempted:
                    # Already judged in this scan; retry on the next one.
                    continue
                operation_id = path.name.split(".", 1)[0]
                if operation_id in active_operations:
                    blocked.add(operation_id)
                    blocked.add(path.name)
                    actions.append(
                        {
                            "action": "skipped_active_upload",
                            "attachment_id": None,
                            "operation_id": operation_id,
                            "staging_file": path.name,
                            "reason": "staging file belongs to an upload in progress",
                        }
                    )
                    continue
                _target, moved, _meta = quarantine_path(
                    path, "staging", "stale_staging"
                )
                if moved:
                    issues.append(
                        {
                            "kind": "stale_staging_quarantined",
                            "target": path.name,
                            "detail": "暂存文件未对应未完成操作，已隔离。",
                        }
                    )
                else:
                    issues.append(
                        {
                            "kind": "stale_staging_quarantine_failed",
                            "target": path.name,
                            "detail": "暂存文件未对应未完成操作，隔离失败，文件保留在暂存区。",
                        }
                    )

        # A successful move must stay traceable even if the process died before
        # the disposition issue was written. Files moved by this scan either
        # already have a ledger row (written immediately) or are queued for the
        # final transaction. Any other quarantine file has no disposition record
        # and is registered now without moving or deleting it.
        moved_meta = {
            meta["quarantine_name"]: meta
            for meta in quarantine_records
            if meta.get("quarantine_name")
        }
        conn = connect(self.db_path)
        try:
            ledger_rows = {
                row["target"]: dict(row)
                for row in conn.execute(
                    "SELECT target, detail, state FROM integrity_issues "
                    "WHERE kind = ?",
                    (_QUARANTINE_LEDGER_KIND,),
                ).fetchall()
            }
        finally:
            conn.close()
        quarantine_paths, quarantine_listing_error = _safe_iterdir(self.quarantine)
        if quarantine_listing_error is not None:
            logger.warning(
                "reconcile: cannot list quarantine directory: %s",
                type(quarantine_listing_error).__name__,
            )
            errors.append(
                {
                    "target": "quarantine",
                    "kind": "directory_unreadable",
                    "status": "unreadable",
                    "error_class": type(quarantine_listing_error).__name__,
                    "errno": quarantine_listing_error.errno,
                }
            )
        for path in quarantine_paths:
            if not path.is_file():
                continue
            ledger = ledger_rows.get(path.name)
            if ledger is not None:
                recorded: dict[str, Any] = {}
                try:
                    parsed = json.loads(ledger["detail"])
                    if isinstance(parsed, dict):
                        recorded = parsed
                except (TypeError, ValueError):
                    recorded = {}
                entry = {
                    "name": path.name,
                    "bytes": recorded.get("bytes"),
                    "sha256": recorded.get("sha256"),
                    "origin": recorded.get("origin"),
                    "reason": recorded.get("reason"),
                    "original_name": recorded.get("original_name"),
                    "moved_at": recorded.get("moved_at"),
                    "tracked_before_scan": True,
                    "registered_this_run": False,
                }
            else:
                meta = moved_meta.get(path.name)
                if meta is None:
                    meta = {
                        "origin": "unknown",
                        "reason": "untracked",
                        "original_name": None,
                        "quarantine_name": path.name,
                        "bytes": None,
                        "sha256": None,
                        "moved_at": checked_at,
                    }
                    try:
                        meta["bytes"] = path.stat().st_size
                        meta["sha256"], meta["bytes"] = _hash_file(path)
                    except OSError:
                        pass
                    quarantine_records.append(meta)
                    issues.append(
                        {
                            "kind": _QUARANTINE_UNTRACKED_KIND,
                            "target": path.name,
                            "detail": (
                                "隔离区文件没有对应的隔离记录；未移动、未删除，"
                                "已登记为可追踪的隔离清单项。"
                            ),
                        }
                    )
                entry = {
                    "name": path.name,
                    "bytes": meta.get("bytes"),
                    "sha256": meta.get("sha256"),
                    "origin": meta.get("origin"),
                    "reason": meta.get("reason"),
                    "original_name": meta.get("original_name"),
                    "moved_at": meta.get("moved_at"),
                    "tracked_before_scan": False,
                    "registered_this_run": True,
                }
            quarantine_inventory.append(entry)

        report = {
            "schema_version": 2,
            "checked_at": checked_at,
            "scope": [
                "operations",
                "staging",
                "objects",
                "metadata",
                "quarantine_inventory",
                "quarantine_ledger",
            ],
            "actions": actions,
            "issues": issues,
            "errors": errors,
            "partial": bool(errors),
            "quarantined": quarantined,
            "quarantine_moves": quarantine_moves,
            "quarantine_inventory": quarantine_inventory,
            "quarantine_inventory_count": len(quarantine_inventory),
            "quarantined_attachments": sorted(
                quarantined_attachments, key=lambda item: item["id"]
            ),
            "counts": {
                "pending_operations": len(pending),
                "actions": len(actions),
                "issues": len(issues),
                "errors": len(errors),
                "quarantined_this_run": len(quarantined),
                "quarantine_inventory": len(quarantine_inventory),
                "quarantined_attachments": len(quarantined_attachments),
            },
            "ok": len(issues) == 0 and not errors,
            "commit": {
                "attempt_id": attempt_id,
                "protocol": "staged-marker",
                "phase": "committed",
                # Findings already persisted together with their state change
                # before this report became visible. They are intentionally kept
                # even if the report transaction fails: they describe real,
                # durable state.
                "early_commits": len(persisted),
                "report_atomic": True,
            },
            "note": (
                "ok=true 仅表示本次扫描未发现新的活动区异常；"
                "已隔离文件与 QUARANTINED 附件见 quarantine_inventory / "
                "quarantined_attachments，不替代人工处置或备份恢复验证。"
                "partial=true 表示有单项无法检查，其余条目仍已完成审计。"
            ),
        }
        # The report and the issue/state conclusions are committed together:
        # a caller can never observe "report written but state not applied" or
        # the reverse. A failure here rolls the whole conclusion set back and
        # propagates, instead of leaving a stale report that looks current.
        attempt["phase"] = "committing_report"
        attempt["early_commits"] = len(persisted)
        self._write_reconcile_attempt(attempt)
        try:
            self._record_issues(
                issues,
                checked_at,
                persisted=persisted,
                evidence={
                    "object_present": object_present_ids,
                    "metadata_complete": metadata_complete_ids,
                    "bytes_match": bytes_match_ids,
                    "healthy": healthy_ids,
                    "readable": readable_ids,
                    "staging_settled": staging_settled,
                },
                checked_files=checked_files,
                blocked=blocked,
                file_swept=listing_ok,
                quarantine_records=quarantine_records,
                report=report,
                attempt=attempt,
            )
        except BaseException as exc:
            # The report never became visible, but earlier state/quarantine
            # commits did. Persist that fact (best effort) and re-raise: nothing
            # is swallowed and the marker cannot claim a completed scan.
            self._record_reconcile_failure(attempt, "report_commit", exc)
            raise
        return report

    def get_integrity(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            open_issues = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM integrity_issues WHERE state = 'open' "
                    "ORDER BY last_seen DESC"
                ).fetchall()
            ]
            resolved = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM integrity_issues WHERE state = 'resolved' "
                    "ORDER BY resolved_at DESC LIMIT 20"
                ).fetchall()
            ]
            non_ready_attachments = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, content_id, filename, state, note, operation_id, updated_at "
                    "FROM attachments WHERE state != 'READY' "
                    "ORDER BY updated_at, id"
                ).fetchall()
            ]
            quarantine_history_count = conn.execute(
                "SELECT COUNT(*) FROM integrity_issues WHERE kind = ?",
                (_QUARANTINE_LEDGER_KIND,),
            ).fetchone()[0]
        finally:
            conn.close()
        last_raw = get_meta(self.db_path, "last_reconcile_report")
        try:
            last_report = json.loads(last_raw) if last_raw else None
        except json.JSONDecodeError:
            last_report = None
        attempt_raw = get_meta(self.db_path, _RECONCILE_ATTEMPT_KEY)
        try:
            reconcile_attempt = json.loads(attempt_raw) if attempt_raw else None
        except json.JSONDecodeError:
            reconcile_attempt = None
        # A stored report is current only when the same transaction recorded its
        # attempt as completed. In-progress, failed or mismatched attempts mean
        # the report predates unfinished work and must not be read as "ok".
        if reconcile_attempt is None:
            last_report_stale = last_report is not None
        else:
            report_attempt_id = (last_report or {}).get("commit", {}).get("attempt_id")
            last_report_stale = (
                reconcile_attempt.get("state") != "completed"
                or reconcile_attempt.get("report_attempt_id") != report_attempt_id
            )
        # Quarantine inventory rows are records of disposition, not new active
        # findings. Keeping them in `open_issues` preserves the audit trail;
        # the active list is what an operator is expected to act on today.
        active_open_issues = [
            issue
            for issue in open_issues
            if issue["kind"] not in _QUARANTINE_AREA_KINDS
        ]
        return {
            "open_issues": open_issues,
            "active_open_issues": active_open_issues,
            "active_open_issue_count": len(active_open_issues),
            "quarantine_history_count": quarantine_history_count,
            "recently_resolved": resolved,
            "last_report": last_report,
            "last_report_stale": last_report_stale,
            "reconcile_attempt": reconcile_attempt,
            "non_ready_attachments": non_ready_attachments,
        }

    # ------------------------------------------------------------------
    # Read / mutate existing attachments
    # ------------------------------------------------------------------
    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        _validate_id(attachment_id, "attachment_id")
        row = self._get_attachment(attachment_id)
        if row is None:
            raise NotFoundError("附件不存在。", attachment_id=attachment_id)
        return row

    def set_visibility(self, attachment_id: str, visibility: str) -> dict[str, Any]:
        _validate_id(attachment_id, "attachment_id")
        if visibility not in ("private", "public"):
            raise ValidationError("visibility 只能是 private 或 public。", field="visibility")
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("附件不存在。", attachment_id=attachment_id)
            conn.execute(
                "UPDATE attachments SET visibility = ?, updated_at = ? WHERE id = ?",
                (visibility, utcnow(), attachment_id),
            )
        return self.get_attachment(attachment_id)

    def download_target(self, attachment_id: str, *, owner: bool) -> tuple[Path, str, str]:
        _validate_id(attachment_id, "attachment_id")
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT a.*, c.id AS content_exists, c.state AS content_state, "
                "c.visibility AS content_visibility, "
                "c.visibility_selected AS content_visibility_selected "
                "FROM attachments a LEFT JOIN content c ON c.id = a.content_id "
                "WHERE a.id = ?",
                (attachment_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            raise NotFoundError("附件不存在。", attachment_id=attachment_id)
        if row["state"] != "READY":
            raise NotFoundError(
                "附件当前不可用。",
                attachment_id=attachment_id,
                state=row["state"],
            )
        if not owner:
            public_content = (
                row["content_exists"] is not None
                and row["content_state"] == "active"
                and row["content_visibility"] == "public"
                and row["content_visibility_selected"] == 1
            )
            if not public_content or row["visibility"] != "public":
                # Deliberately indistinguishable from a missing resource.
                raise NotFoundError("附件不存在。", attachment_id=attachment_id)
        path = self.objects / attachment_id
        # The same classifier is used for uploads, scans and downloads, so a
        # symlink/junction or a swapped path is judged identically everywhere.
        status, digest, size, error = _probe_object(path)
        if status == "absent":
            raise NotFoundError("附件文件缺失。", attachment_id=attachment_id)
        if status == "not_regular":
            # A symlink or Windows junction at the object path could serve
            # bytes from outside the store. It is never followed here.
            raise AttachmentUnverifiedError(
                "附件路径不是普通文件，已拒绝下载。",
                attachment_id=attachment_id,
                reason="not_regular",
            )
        if status != "regular":
            raise AttachmentUnverifiedError(
                "附件文件无法校验，已拒绝下载。",
                attachment_id=attachment_id,
                reason=type(error).__name__ if error is not None else status,
            )
        if (
            row["sha256"] is None
            or row["byte_size"] is None
            or size != row["byte_size"]
            or digest != row["sha256"]
        ):
            raise AttachmentUnverifiedError(
                "附件内容与登记元数据不一致，已拒绝下载。",
                attachment_id=attachment_id,
            )
        return path, row["filename"], row["mime"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return self._attachment_dict(row) if row else None
        finally:
            conn.close()

    def _get_operation(self, operation_id: str) -> dict[str, Any] | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

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

    def _upload_is_active(self, operation_id: str) -> bool:
        with _ACTIVE_UPLOAD_LOCK:
            return operation_id in _ACTIVE_UPLOADS.get(self._archive_key, {})

    def _upload_is_active_for_attachment(self, attachment_id: str) -> bool:
        with _ACTIVE_UPLOAD_LOCK:
            return attachment_id in self._active_attachments_locked()

    def _active_attachments_locked(self) -> set[str]:
        # Caller must hold _ACTIVE_UPLOAD_LOCK.
        return {
            attachment_id
            for entry in _ACTIVE_UPLOADS.get(self._archive_key, {}).values()
            for attachment_id in entry["attachments"]
        }

    def _staging_snapshot(self, operation_id: str) -> list[Path] | None:
        """List staging files for an operation, or None if an upload is active.

        The active check and the directory listing share the registry lock with
        upload registration, and upload() registers before it creates its file.
        Therefore a file cannot be created between the check and the listing:
        every listed file belongs to a crashed/finished attempt and is safe for
        the caller to inspect or quarantine.
        """
        prefix = f"{operation_id}."
        with _ACTIVE_UPLOAD_LOCK:
            if operation_id in _ACTIVE_UPLOADS.get(self._archive_key, {}):
                return None
            candidates: list[Path] = []
            for path in self.staging.iterdir():
                if not path.name.startswith(prefix):
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    # Not even stat() works. Return it so the caller reports an
                    # unsettled staging area instead of silently ignoring it.
                    candidates.append(path)
                    continue
                if stat.S_ISDIR(info.st_mode):
                    attributes = getattr(info, "st_file_attributes", 0)
                    reparse_flag = getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                    )
                    if not (attributes & reparse_flag):
                        # A real directory is not a staging payload. A directory
                        # reparse point (junction/mount point) is not a directory
                        # to trust: keep it so the probe rejects it explicitly.
                        continue
                # Symlinks and junctions are included: they are not directories
                # here, and the probe must reject them rather than skip them.
                candidates.append(path)
            return candidates

    def _complete_reconciliation(
        self,
        attachment_id: str,
        operation_id: str,
        digest: str,
        size: int,
        result: str,
        *,
        snapshot: dict[str, Any],
    ) -> bool:
        try:
            with transaction(self.db_path) as conn:
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'READY', sha256 = ?, byte_size = ?, "
                    "note = NULL, updated_at = ? "
                    "WHERE id = ? AND operation_id = ? AND state = ? "
                    "AND sha256 IS ? AND byte_size IS ? AND updated_at = ?",
                    (
                        digest,
                        size,
                        utcnow(),
                        attachment_id,
                        operation_id,
                        snapshot["attachment_state"],
                        snapshot["attachment_sha256"],
                        snapshot["attachment_size"],
                        snapshot["attachment_updated_at"],
                    ),
                )
                if attachment_update.rowcount != 1:
                    raise _StaleReconciliation
                operation_update = conn.execute(
                    "UPDATE operations SET state = 'COMPLETED', result = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND payload_sha256 IS ? "
                    "AND payload_size IS ? AND updated_at = ?",
                    (
                        result,
                        utcnow(),
                        operation_id,
                        snapshot["operation_state"],
                        snapshot["payload_sha256"],
                        snapshot["payload_size"],
                        snapshot["operation_updated_at"],
                    ),
                )
                if operation_update.rowcount != 1:
                    raise _StaleReconciliation
        except _StaleReconciliation:
            return False
        return True

    def _mark_broken_reconciliation(
        self,
        attachment_id: str,
        operation_id: str,
        note: str,
        *,
        snapshot: dict[str, Any],
        issue: dict[str, str] | None = None,
        checked_at: str | None = None,
    ) -> bool:
        try:
            with transaction(self.db_path) as conn:
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'BROKEN', note = ?, updated_at = ? "
                    "WHERE id = ? AND operation_id = ? AND state = ? "
                    "AND sha256 IS ? AND byte_size IS ? AND updated_at = ?",
                    (
                        note,
                        utcnow(),
                        attachment_id,
                        operation_id,
                        snapshot["attachment_state"],
                        snapshot["attachment_sha256"],
                        snapshot["attachment_size"],
                        snapshot["attachment_updated_at"],
                    ),
                )
                if attachment_update.rowcount != 1:
                    raise _StaleReconciliation
                operation_update = conn.execute(
                    "UPDATE operations SET state = 'UNCERTAIN', result = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND payload_sha256 IS ? "
                    "AND payload_size IS ? AND updated_at = ?",
                    (
                        note,
                        utcnow(),
                        operation_id,
                        snapshot["operation_state"],
                        snapshot["payload_sha256"],
                        snapshot["payload_size"],
                        snapshot["operation_updated_at"],
                    ),
                )
                if operation_update.rowcount != 1:
                    raise _StaleReconciliation
                if issue is not None and checked_at is not None:
                    _upsert_issue(conn, issue, checked_at)
        except _StaleReconciliation:
            return False
        return True

    def _mark_attachment_broken(
        self,
        attachment_id: str,
        note: str,
        *,
        snapshot: dict[str, Any],
        issue: dict[str, str] | None = None,
        issues: list[dict[str, str]] | None = None,
        checked_at: str | None = None,
    ) -> bool:
        try:
            with transaction(self.db_path) as conn:
                attachment_update = conn.execute(
                    "UPDATE attachments SET state = 'BROKEN', note = ?, updated_at = ? "
                    "WHERE id = ? AND operation_id = ? AND state = ? "
                    "AND sha256 IS ? AND byte_size IS ? AND updated_at = ?",
                    (
                        note,
                        utcnow(),
                        attachment_id,
                        snapshot["operation_id"],
                        snapshot["state"],
                        snapshot["sha256"],
                        snapshot["size"],
                        snapshot["updated_at"],
                    ),
                )
                if attachment_update.rowcount != 1:
                    raise _StaleReconciliation
                if checked_at is not None:
                    for item in ([issue] if issue is not None else []) + (
                        issues or []
                    ):
                        _upsert_issue(conn, item, checked_at)
        except _StaleReconciliation:
            return False
        return True

    def _set_operation_state(
        self,
        operation_id: str,
        state: str,
        result: str,
        *,
        expected_state: str,
        expected_updated_at: str,
        issue: dict[str, str] | None = None,
        checked_at: str | None = None,
    ) -> bool:
        try:
            with transaction(self.db_path) as conn:
                operation_update = conn.execute(
                    "UPDATE operations SET state = ?, result = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND updated_at = ?",
                    (
                        state,
                        result,
                        utcnow(),
                        operation_id,
                        expected_state,
                        expected_updated_at,
                    ),
                )
                if operation_update.rowcount != 1:
                    raise _StaleReconciliation
                if issue is not None and checked_at is not None:
                    _upsert_issue(conn, issue, checked_at)
        except _StaleReconciliation:
            return False
        return True

    def _record_issues_if_current(
        self,
        issues: list[dict[str, str]],
        snapshot: dict[str, Any],
        checked_at: str,
    ) -> bool:
        """Persist issues only if the audited attachment row is unchanged.

        Used for BROKEN rows where no state transition happens: the read and all
        issue upserts share one BEGIN IMMEDIATE transaction, so an upload that
        healed the row cannot race a stale issue into the ledger.
        """
        if not issues:
            return True
        target = issues[0]["target"]
        try:
            with transaction(self.db_path) as conn:
                row = conn.execute(
                    "SELECT state, sha256, byte_size, operation_id, updated_at "
                    "FROM attachments WHERE id = ?",
                    (target,),
                ).fetchone()
                if row is None:
                    raise _StaleReconciliation
                if (
                    row["state"] != snapshot["state"]
                    or row["sha256"] != snapshot["sha256"]
                    or row["byte_size"] != snapshot["size"]
                    or row["operation_id"] != snapshot["operation_id"]
                    or row["updated_at"] != snapshot["updated_at"]
                ):
                    raise _StaleReconciliation
                for issue in issues:
                    _upsert_issue(conn, issue, checked_at)
        except _StaleReconciliation:
            return False
        return True

    def _record_issue_if_current(
        self,
        issue: dict[str, str],
        snapshot: dict[str, Any],
        checked_at: str,
    ) -> bool:
        return self._record_issues_if_current([issue], snapshot, checked_at)

    def _quarantine_issue(self, meta: dict[str, Any]) -> dict[str, str]:
        """Build the durable inventory record for one quarantined file."""
        detail = json.dumps(
            {
                "origin": meta.get("origin"),
                "reason": meta.get("reason"),
                "original_name": meta.get("original_name"),
                "bytes": meta.get("bytes"),
                "sha256": meta.get("sha256"),
                "moved_at": meta.get("moved_at"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return {
            "kind": _QUARANTINE_LEDGER_KIND,
            "target": meta["quarantine_name"],
            "detail": detail,
        }

    def _write_reconcile_attempt(self, attempt: dict[str, Any]) -> None:
        """Durably record the phase of the current reconcile attempt.

        A failure here propagates: without the marker the caller cannot tell a
        fresh report from a stale one, so continuing would risk an inaccurate
        "ok" being observed.
        """
        set_meta(
            self.db_path,
            _RECONCILE_ATTEMPT_KEY,
            json.dumps(attempt, ensure_ascii=False, sort_keys=True),
        )

    def _record_reconcile_failure(
        self, attempt: dict[str, Any], phase: str, exc: BaseException
    ) -> None:
        """Mark an attempt failed without masking the original exception.

        The failed marker is written in its own short transaction. If even that
        fails, the previous marker (scanning/committing_report) still reports a
        non-completed phase, so readers keep treating the last report as stale.
        """
        failed = dict(attempt)
        failed.update(
            {
                "phase": "failed",
                "state": "failed",
                "failure_phase": phase,
                "error_class": type(exc).__name__,
                "failed_at": utcnow(),
                "report_committed": False,
            }
        )
        try:
            self._write_reconcile_attempt(failed)
        except sqlite3.Error as marker_exc:
            logger.warning(
                "reconcile: failed to record attempt failure (%s): %s",
                type(marker_exc).__name__,
                marker_exc,
            )

    def _persist_issue_now(self, issue: dict[str, str], checked_at: str) -> bool:
        """Persist a finding before its state CAS, tolerating a locked DB.

        A failure is retried by _record_issues() in the same scan; the caller
        must keep the issue in its local list either way.
        """
        try:
            with transaction(self.db_path) as conn:
                _upsert_issue(conn, issue, checked_at)
        except sqlite3.Error:
            return False
        return True

    def _quarantine_target(self, origin: str, original_name: str) -> Path | None:
        """Return a quarantine path that does not already exist.

        os.replace() overwrites its destination, so a colliding candidate must
        be rejected before the move. Full UUID suffixes plus a bounded retry
        make a deterministic collision fall through to a different name. The
        app is single-process and reconcile is serialized per Archive, so no
        other application writer can create the target between this check and
        the move; multi-process coordination is not claimed.
        """
        for _ in range(16):
            candidate = self.quarantine / (
                f"{origin}-{original_name}.{uuid.uuid4().hex}"
            )
            if not candidate.exists():
                return candidate
        return None

    def _quarantine_file(
        self, path: Path, *, origin: str, reason: str, checked_at: str
    ) -> tuple[Path, bool, dict[str, Any]]:
        """Move a file to quarantine, record its identity, report the move.

        The inventory row is written before the move result is returned so a
        caller cannot treat a successful move as a pure filesystem action. The
        row is keyed by the unique quarantine name. If that immediate write
        fails, the caller queues the metadata for the final reconciliation
        transaction; the file itself is never deleted.
        """
        moved_at = utcnow()
        size: int | None = None
        digest: str | None = None
        try:
            size = path.stat().st_size
        except OSError:
            pass
        try:
            digest, size = _hash_file(path)
        except OSError:
            pass
        target = self._quarantine_target(origin, path.name)
        if target is not None:
            try:
                os.replace(path, target)
            except OSError:
                target = None
        if target is None:
            return path, False, {
                "origin": origin,
                "reason": reason,
                "original_name": path.name,
                "quarantine_name": None,
                "bytes": size,
                "sha256": digest,
                "moved_at": moved_at,
                "recorded": False,
            }
        meta = {
            "origin": origin,
            "reason": reason,
            "original_name": path.name,
            "quarantine_name": target.name,
            "bytes": size,
            "sha256": digest,
            "moved_at": moved_at,
            "recorded": False,
        }
        try:
            with transaction(self.db_path) as conn:
                _upsert_issue(conn, self._quarantine_issue(meta), checked_at)
        except sqlite3.Error as exc:
            # The file has already moved. The caller queues this record for the
            # final transaction; if that also fails, the move must stay visible.
            logger.warning(
                "quarantine ledger write failed (file=%s): %s",
                target.name,
                type(exc).__name__,
                exc_info=True,
            )
        else:
            meta["recorded"] = True
        return target, True, meta

    def _record_issues(
        self,
        issues: list[dict[str, str]],
        checked_at: str,
        *,
        resolve_missing: bool = True,
        persisted: set[str] | None = None,
        evidence: dict[str, set[str]] | None = None,
        checked_files: set[str] | None = None,
        blocked: set[str] | None = None,
        file_swept: bool = False,
        quarantine_records: list[dict[str, Any]] | None = None,
        report: dict[str, Any] | None = None,
        attempt: dict[str, Any] | None = None,
    ) -> None:
        """Persist current issues and resolve only demonstrably-gone problems.

        ``evidence`` maps an observed condition to the attachment ids this scan
        positively observed satisfying it. An issue is closed only when the
        evidence required by *its own* cause is present, so returning bytes
        never closes an unrelated finding for the same target. A target absent
        from every evidence set is not inferred healthy merely because it is
        absent from ``seen``. ``blocked`` contains targets owned by an active
        upload or otherwise skipped. ``last_seen`` must predate the scan, so an
        issue written by a concurrent writer after the scan began is never
        resolved by this scan's older snapshot.
        """
        # Issues already written in the same transaction as their state
        # transition keep that transaction's last_seen. Writing them again here
        # would let a healed upload be stamped with a stale, later observation.
        seen: set[str] = set(persisted or ())
        evidence_sets = evidence or {}
        checked_file_targets = checked_files or set()
        blocked_targets = set(blocked or ())
        # Re-read active upload ownership at resolution time as well: an upload
        # may have started after the scan snapshot. Its target is not healed just
        # because the older scan did not see it as active.
        with _ACTIVE_UPLOAD_LOCK:
            for operation_id, entry in _ACTIVE_UPLOADS.get(
                self._archive_key, {}
            ).items():
                blocked_targets.add(operation_id)
                blocked_targets.update(entry["attachments"].keys())
        with transaction(self.db_path) as conn:
            for issue in issues:
                fingerprint = _issue_fingerprint(issue)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                _upsert_issue(conn, issue, checked_at)
            # Retry inventory rows whose immediate write failed. They are written
            # in the same transaction as the scan's findings, and their
            # fingerprints enter `seen` so the resolution pass below cannot close
            # the record it just created.
            for meta in quarantine_records or ():
                if not meta.get("quarantine_name"):
                    continue
                record = self._quarantine_issue(meta)
                fingerprint = _issue_fingerprint(record)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                _upsert_issue(conn, record, checked_at)
            if not resolve_missing:
                # A single discovery is not a full scan: it must not resolve
                # unrelated open issues that this caller did not examine.
                return
            open_rows = conn.execute(
                "SELECT fingerprint, kind, target, last_seen "
                "FROM integrity_issues WHERE state = 'open'"
            ).fetchall()
            for row in open_rows:
                fingerprint = row["fingerprint"]
                target = row["target"]
                if fingerprint in seen or target in blocked_targets:
                    continue
                # An issue created or re-stamped after this scan began is not
                # part of this scan's observation, even if its target was later
                # checked by another writer.
                if row["last_seen"] > checked_at:
                    continue
                cause = _ISSUE_CAUSES.get(row["kind"])
                if cause is not None:
                    # Per-cause resolution: only the observation that produced
                    # this kind may close it.
                    resolvable = target in evidence_sets.get(
                        _CAUSE_EVIDENCE[cause], set()
                    )
                    if (
                        not resolvable
                        and row["kind"] in _OBJECT_ANOMALY_KINDS.values()
                        and target in evidence_sets.get("staging_settled", set())
                    ):
                        # Findings written before staging probes had their own
                        # kind were keyed by operation id. A settled staging
                        # area is the same positive observation, so those rows
                        # close on the same evidence without deleting history.
                        resolvable = True
                elif row["kind"] in _FILE_ISSUE_KINDS:
                    resolvable = target in checked_file_targets
                    if not resolvable and file_swept:
                        # The complete directory sweep observed this domain; an
                        # absent target means the file problem is gone.
                        resolvable = (
                            not (self.staging / target).is_file()
                            and not (self.objects / target).is_file()
                        )
                elif row["kind"] == "operation_without_attachment":
                    # A terminal operation state is not evidence that the
                    # resource exists. Resolve only when the stable identity
                    # links to a real attachment row, or the operation row
                    # itself is gone.
                    operation = conn.execute(
                        "SELECT resource_id FROM operations WHERE id = ?", (target,)
                    ).fetchone()
                    if operation is None:
                        resolvable = True
                    else:
                        resolvable = (
                            conn.execute(
                                "SELECT 1 FROM attachments "
                                "WHERE operation_id = ? OR id = ? LIMIT 1",
                                (target, operation["resource_id"]),
                            ).fetchone()
                            is not None
                        )
                elif row["kind"] == "attachment_owner_missing":
                    owner = conn.execute(
                        "SELECT 1 FROM attachments a "
                        "JOIN content c ON c.id = a.content_id WHERE a.id = ?",
                        (target,),
                    ).fetchone()
                    resolvable = owner is not None
                elif row["kind"] == _QUARANTINE_LEDGER_KIND:
                    # Inventory evidence is closed only when the quarantined
                    # file itself is gone. Reconcile never deletes it.
                    resolvable = not (self.quarantine / target).is_file()
                elif row["kind"] == _QUARANTINE_UNTRACKED_KIND:
                    file_present = (self.quarantine / target).is_file()
                    if not file_present:
                        resolvable = True
                    else:
                        resolvable = (
                            conn.execute(
                                "SELECT 1 FROM integrity_issues "
                                "WHERE kind = ? AND target = ?",
                                (_QUARANTINE_LEDGER_KIND, target),
                            ).fetchone()
                            is not None
                        )
                elif row["kind"] in (
                    _QUARANTINED_OBJECT_MOVED_KIND,
                    _QUARANTINED_OBJECT_MOVE_FAILED_KIND,
                ):
                    attachment = conn.execute(
                        "SELECT state FROM attachments WHERE id = ?", (target,)
                    ).fetchone()
                    if attachment is None or attachment["state"] != "QUARANTINED":
                        resolvable = True
                    else:
                        resolvable = not (self.objects / target).is_file()
                elif row["kind"] == _QUARANTINED_PENDING_OPERATION_KIND:
                    link = conn.execute(
                        "SELECT a.state AS attachment_state, "
                        "o.state AS operation_state "
                        "FROM attachments a LEFT JOIN operations o "
                        "ON o.id = a.operation_id WHERE a.id = ?",
                        (target,),
                    ).fetchone()
                    resolvable = (
                        link is None
                        or link["attachment_state"] != "QUARANTINED"
                        or link["operation_state"] not in ("PENDING", "UNCERTAIN")
                    )
                else:
                    resolvable = False
                if not resolvable:
                    continue
                conn.execute(
                    "UPDATE integrity_issues SET state = 'resolved', resolved_at = ? "
                    "WHERE fingerprint = ? AND state = 'open' AND last_seen <= ?",
                    (checked_at, fingerprint, checked_at),
                )
            if report is not None:
                # A report is a conclusion about the same state transaction, so
                # it must commit or roll back with the findings it describes.
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_reconcile_report', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(report, ensure_ascii=False),),
                )
            if attempt is not None:
                # Mark the attempt completed in the *same* transaction as the
                # report. Either both become visible or neither does, so readers
                # never see a completed attempt with a missing/stale report.
                completed = dict(attempt)
                completed.update(
                    {
                        "phase": "completed",
                        "state": "completed",
                        "completed_at": utcnow(),
                        "report_committed": report is not None,
                        "report_attempt_id": attempt.get("attempt_id"),
                    }
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (
                        _RECONCILE_ATTEMPT_KEY,
                        json.dumps(completed, ensure_ascii=False, sort_keys=True),
                    ),
                )
