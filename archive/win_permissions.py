"""Read DACLs without modifying archive files."""

import ctypes
from ctypes import wintypes
import re


def has_other_access(path, current_sid: str) -> bool:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    get.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    convert.restype = wintypes.BOOL
    release = kernel.LocalFree
    release.argtypes = (ctypes.c_void_p,)
    release.restype = ctypes.c_void_p
    descriptor, text = ctypes.c_void_p(), ctypes.c_void_p()
    status = get(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        if not convert(descriptor, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        dacl = ctypes.wstring_at(text)
        if "NO_ACCESS_CONTROL" in dacl:
            return True
        trusts = {"SY", "S-1-5-18", current_sid}
        return any(ace.split(";")[0].startswith("A") and ace.split(";")[5] not in trusts
                   for ace in re.findall(r"\(([^()]*)\)", dacl))
    finally:
        if text:
            release(text)
        release(descriptor)
