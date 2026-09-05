"""A small single-instance guard for the tray host."""

from __future__ import annotations

import os
from pathlib import Path


class SingleInstance:
    def __init__(self, name: str, fallback_lock: Path) -> None:
        self.name = name
        self.fallback_lock = fallback_lock
        self._handle: int | None = None
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_bool,
                ctypes.c_wchar_p,
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.SetLastError(0)
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return False
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self._handle = int(handle)
            return True

        self.fallback_lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._descriptor = os.open(
                self.fallback_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
            self.fallback_lock.unlink(missing_ok=True)

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("another YeYu Gamer tray instance is already running")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
