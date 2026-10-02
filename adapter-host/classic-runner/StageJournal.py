"""Append one complete UTF-8 stage record without Windows CRT seek races."""
import ctypes
from ctypes import wintypes
import json
import os


def append_record(path, record):
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    if os.name != "nt":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Stage journal write was incomplete")
        finally:
            os.close(fd)
        return
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.WriteFile.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    # FILE_APPEND_DATA without FILE_WRITE_DATA: the kernel chooses EOF for
    # this single WriteFile. CRT O_APPEND instead seeks on each independent
    # handle, which lets concurrent driver/observer writes overwrite records.
    handle = api.CreateFileW(str(path), 0x0004, 0x0003, None, 4, 0x0080, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(data)
        written = wintypes.DWORD()
        if not api.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(data):
            raise OSError("Stage journal write was incomplete")
    finally:
        api.CloseHandle(handle)
