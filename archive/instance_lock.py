"""Single-instance guard for one Archive data directory.

The guard uses an OS-held file lock, not PID probing: the lock is authoritative
while the owning process is alive, and Windows releases it automatically when
the process exits, including after a hard kill. The ``.app.lock`` file content
is diagnostic only (pid, nonce, start time); stale content is rewritten by the
next successful owner.

This deliberately never deletes another instance's lock file: on Windows an
open lock file cannot be unlinked while held, and on POSIX unlinking a held
lock file would let a second process create a fresh file and acquire a second
lock. The file therefore stays as a stable lock anchor.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .store import ArchiveError

LOCK_CONTENT_LIMIT = 4096
LOCK_OFFSET = 4096


class DataDirLockedError(ArchiveError):
    """Another live process already owns this data directory."""

    status = 409
    code = "data_dir_locked"


class DataDirLock:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / ".app.lock"
        self.nonce = uuid.uuid4().hex
        self.stale_detected = False
        self.previous_holder: dict[str, Any] | None = None
        self._fd: int | None = None

    def acquire(self) -> "DataDirLock":
        if self._fd is not None:
            raise RuntimeError("DataDirLock 只能获取一次。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.previous_holder = self._read_holder()
        existed = self.path.exists()
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_fd(fd, blocking=False)
        except OSError as exc:
            os.close(fd)
            holder = self.previous_holder or {}
            raise DataDirLockedError(
                "数据目录已被另一个运行中的 Archive 实例占用。",
                data_dir=str(self.data_dir),
                lock=str(self.path),
                holder_pid=holder.get("pid"),
                holder_started_at=holder.get("started_at"),
                holder_nonce=holder.get("nonce"),
                reason=str(exc),
            ) from exc
        self._fd = fd
        self.stale_detected = existed
        self._write_diagnostic()
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock_fd(self._fd)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "DataDirLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def diagnostic(self) -> dict[str, Any]:
        return {
            "lock_file": str(self.path),
            "data_dir": str(self.data_dir),
            "pid": os.getpid(),
            "nonce": self.nonce,
            "stale_file_reused": self.stale_detected,
            "previous_holder": self.previous_holder,
        }

    def _read_holder(self) -> dict[str, Any] | None:
        # ``Path.read_bytes``/``open()`` can be denied while our own byte-range
        # lock is held on Windows, so read the diagnostic range with a plain
        # os-level handle (the lock byte sits just past this range).
        try:
            fd = os.open(str(self.path), os.O_RDONLY)
        except OSError:
            return None
        try:
            raw = os.read(fd, LOCK_CONTENT_LIMIT)
        except OSError:
            return None
        finally:
            os.close(fd)
        try:
            payload = json.loads(raw.decode("utf-8").strip() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_diagnostic(self) -> None:
        assert self._fd is not None
        payload = {
            "pid": os.getpid(),
            "nonce": self.nonce,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "host": os.environ.get("COMPUTERNAME", ""),
            "python": sys.version.split()[0],
        }
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data + b" " * (LOCK_CONTENT_LIMIT - len(data)))
        os.fsync(self._fd)


def _lock_fd(fd: int, *, blocking: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        mode = msvcrt.LK_NBLCK if not blocking else msvcrt.LK_LOCK
        msvcrt.locking(fd, mode, 1)
        return
    import fcntl

    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fcntl.lockf(fd, flags, 1, LOCK_OFFSET)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.lockf(fd, fcntl.LOCK_UN, 1, LOCK_OFFSET)
