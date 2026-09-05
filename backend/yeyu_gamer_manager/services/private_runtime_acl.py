"""Protected ACLs for Manager-owned runtime control material.

Cancellation authorities are bearer secrets.  POSIX mode bits are not a
security boundary on Windows, so this module applies and verifies a protected
DACL containing only the current user, LocalSystem, and Administrators.
"""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path


class PrivateRuntimeAclError(RuntimeError):
    """A runtime secret path is not protected by the product ACL."""


def _current_user_sid() -> str:
    if os.name != "nt":
        return f"uid:{os.getuid()}"

    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        if required.value <= 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            sid_pointer, ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def set_private_runtime_acl(path: Path, *, directory: bool) -> None:
    """Replace inheritance with the exact product ACL for a file or folder."""

    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return

    from ctypes import wintypes

    inheritance = "OICI" if directory else ""
    sid = _current_user_sid()
    sddl = (
        f"D:P(A;{inheritance};FA;;;SY)"
        f"(A;{inheritance};FA;;;BA)"
        f"(A;{inheritance};FA;;;{sid})"
    )
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi32.SetFileSecurityW(
            str(path),
            dacl_security_information | protected_dacl_security_information,
            descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _windows_dacl_sddl(path: Path) -> str:
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    required = wintypes.DWORD()
    advapi32.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.GetFileSecurityW(
        str(path), dacl_security_information, None, 0, ctypes.byref(required)
    )
    if required.value <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(required.value)
    if not advapi32.GetFileSecurityW(
        str(path),
        dacl_security_information,
        descriptor,
        required,
        ctypes.byref(required),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor,
        1,
        dacl_security_information,
        ctypes.byref(text),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(text)


def assert_private_runtime_acl(path: Path) -> None:
    """Fail closed unless the path still has the exact protected product ACL."""

    if os.name != "nt":
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise PrivateRuntimeAclError(
                "runtime control path grants group or other permissions"
            )
        return

    sddl = _windows_dacl_sddl(path)
    if not sddl.startswith("D:P") or sddl.count("(") != 3 or sddl.count("(A;") != 3:
        raise PrivateRuntimeAclError("runtime control DACL is not exact and protected")
    aliases = {
        "S-1-5-18": "SY",
        "S-1-5-32-544": "BA",
    }
    trustees: set[str] = set()
    for raw_ace in sddl.split("(")[1:]:
        ace = raw_ace.split(")", 1)[0]
        if ";FA;;;" not in ace or ";;;" not in ace:
            raise PrivateRuntimeAclError("runtime control DACL has a non-full ACE")
        trustee = ace.split(";;;", 1)[1]
        trustees.add(aliases.get(trustee, trustee))
    expected = {"SY", "BA", _current_user_sid()}
    if trustees != expected:
        raise PrivateRuntimeAclError(
            "runtime control DACL grants an unexpected principal"
        )
