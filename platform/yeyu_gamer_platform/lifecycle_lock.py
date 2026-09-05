"""Cross-process gate shared by installation and local lifecycle starts."""

from __future__ import annotations

import os
from pathlib import Path


INSTALL_LIFECYCLE_MUTEX_NAME = r"Local\YeYuGamer.InstallLifecycle.v1"


class InstallLifecycleLock:
    """Own the installer/start transition gate until explicitly released."""

    def __init__(self, fallback_lock: Path) -> None:
        self.fallback_lock = fallback_lock
        self._handle: int | None = None
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None or self._descriptor is not None:
            raise RuntimeError("install lifecycle lock is already acquired")
        if os.name == "nt":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        handle = kernel32.CreateMutexW(None, False, INSTALL_LIFECYCLE_MUTEX_NAME)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        wait_result = int(kernel32.WaitForSingleObject(handle, 0))
        if wait_result == 0:  # WAIT_OBJECT_0
            self._handle = int(handle)
            return True
        if wait_result == 0x00000102:  # WAIT_TIMEOUT
            kernel32.CloseHandle(handle)
            return False
        if wait_result == 0x00000080:  # WAIT_ABANDONED
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
            raise RuntimeError(
                "a previous installation abandoned the lifecycle lock; repair first"
            )
        kernel32.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")

    def _acquire_posix(self) -> bool:
        import fcntl

        self.fallback_lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.fallback_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return True

    def acquire_or_raise(self) -> None:
        if not self.acquire():
            raise RuntimeError(
                "YeYu Gamer installation is in progress; lifecycle start is blocked"
            )

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            handle = ctypes.c_void_p(self._handle)
            try:
                if not ctypes.windll.kernel32.ReleaseMutex(handle):
                    raise OSError(ctypes.get_last_error(), "ReleaseMutex failed")
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
                self._handle = None
        if self._descriptor is not None:
            import fcntl

            descriptor = self._descriptor
            self._descriptor = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> "InstallLifecycleLock":
        self.acquire_or_raise()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
