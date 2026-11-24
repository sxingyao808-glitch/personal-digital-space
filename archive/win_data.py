"""Create private archive roots and inspect existing Windows permissions."""

from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import re
import subprocess

logger = logging.getLogger(__name__)


def _system_tool(name: str) -> str:
    return str(Path(os.environ["SystemRoot"]) / "System32" / name)


def _current_sid() -> str:
    result = subprocess.check_output(
        [_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = next(csv.reader([result.decode("mbcs", errors="replace").strip()]))[-1]
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise OSError("无法识别当前 Windows 用户 SID。")
    return sid


def make_data_dir(path: Path) -> None:
    if path.is_dir():
        return
    if os.name != "nt":
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sid = _current_sid()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    convert.restype = wintypes.BOOL
    create = kernel.CreateDirectoryW
    create.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    create.restype = wintypes.BOOL
    release = kernel.LocalFree
    release.argtypes = (ctypes.c_void_p,)
    release.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not create(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183 or not path.is_dir():
                raise ctypes.WinError(error)
    finally:
        release(descriptor)


def diagnose_existing_permissions(path: Path) -> None:
    """Read-only inventory. Additional trustees require an effective-access check."""
    if os.name != "nt":
        return
    try:
        current = _current_sid()
        files = [path, *(path / name for name in ("archive.db", "owner_secret", "objects", "backups", "exports"))]
        inherited_or_extra = []
        from .win_permissions import has_other_access
        for file in files:
            if not file.exists():
                continue
            if has_other_access(file, current):
                inherited_or_extra.append(file.name)
        if inherited_or_extra:
            logger.warning(
                "归档权限需检查（%s）。请核对数据库、会话密钥、对象与备份对其他账户的有效权限。",
                "、".join(inherited_or_extra),
            )
    except (OSError, subprocess.SubprocessError, StopIteration):
        logger.warning("无法完成归档权限诊断。请检查数据库、会话密钥、对象与备份的有效权限。")
