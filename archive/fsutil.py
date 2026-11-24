"""Platform-specific durability helpers for directory entries.

Writing a file's bytes and fsyncing its own handle only makes the *content*
durable. Creating or moving that file additionally changes its directory entry,
which is durable only after the directory itself is flushed.

POSIX exposes ``fsync()`` on a directory descriptor. Windows needs
``CreateFileW`` with ``FILE_FLAG_BACKUP_SEMANTICS`` (required to open a
directory at all) followed by ``FlushFileBuffers``. Both helpers return ``True``
only when the platform confirmed the flush: an unsupported or failed call is
reported as ``False`` and logged, never presented as success.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


def fsync_directory(path: str | Path) -> bool:
    """Flush one directory entry, returning whether the platform confirmed it."""
    target = Path(path)
    if sys.platform == "win32":
        return _fsync_directory_windows(target)
    return _fsync_directory_posix(target)


def _fsync_directory_posix(path: Path) -> bool:
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        logger.warning(
            "directory flush open failed (%s): %s", path.name, type(exc).__name__
        )
        return False
    try:
        os.fsync(handle)
    except OSError as exc:
        logger.warning(
            "directory flush failed (%s): %s", path.name, type(exc).__name__
        )
        return False
    finally:
        os.close(handle)
    return True


def _fsync_directory_windows(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        # FlushFileBuffers requires a write-capable handle.
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if not handle or handle == invalid_handle:
        logger.warning(
            "directory flush open failed (%s): winerror=%s",
            path.name,
            ctypes.get_last_error(),
        )
        return False
    try:
        if not flush_file_buffers(handle):
            # Not every file system supports flushing a directory handle. The
            # caller must be able to tell that durability was not confirmed.
            logger.warning(
                "directory flush unsupported (%s): winerror=%s",
                path.name,
                ctypes.get_last_error(),
            )
            return False
        return True
    finally:
        close_handle(handle)
