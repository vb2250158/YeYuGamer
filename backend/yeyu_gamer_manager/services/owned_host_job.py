from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
import threading
import time
import uuid

from ..logging_setup import get_logger


_log = get_logger("owned_host_job")
_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_BASIC_ACCOUNTING_INFORMATION = 1
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_POLL_SECONDS = 0.01


class OwnedHostJobError(RuntimeError):
    """Native ownership could not be established or verified; never a quiet receipt."""

    def __init__(self, operation: str, winerror: int = 0) -> None:
        super().__init__(f"{operation} failed (winerror={winerror})")
        self.operation = operation
        self.winerror = winerror


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("OwnedHostJob requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "DuplicateHandle": ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = arguments
            function.restype = result

    @staticmethod
    def check(success: object, operation: str) -> None:
        if not success:
            raise OwnedHostJobError(operation, ctypes.get_last_error())

    def close(self, handle: int) -> None:
        self.check(self.kernel.CloseHandle(handle), "CloseHandle")

    def query(self, handle: int, information_class: int, structure: ctypes.Structure) -> None:
        returned = wintypes.DWORD()
        self.check(self.kernel.QueryInformationJobObject(
            handle, information_class, ctypes.byref(structure), ctypes.sizeof(structure),
            ctypes.byref(returned),
        ), "QueryInformationJobObject")
        if returned.value != ctypes.sizeof(structure):
            raise OwnedHostJobError("incomplete_job_information")


@dataclass(frozen=True)
class JobObservation:
    active_processes: int
    root_signaled: bool


class OwnedHostJob:
    """Own one Windows Job and one successfully registered native root handle.

    Pass int(Popen._handle), never a PID, to wait_for_member before sending a
    plan. The trusted Host must self-join before reading stdin or spawning tools.
    False means a deadline expired; native errors raise OwnedHostJobError.
    Neither result proves reader drain, callback exclusion, or Store commit.
    Use a context manager or close explicitly; close is not a quiet receipt.
    Named Jobs use the creator token's default ACL, not a malicious-code sandbox.
    """

    def __init__(self, *, name: str | None = None) -> None:
        self._api = _WindowsApi()
        self._name = name if name is not None else "Local\\YeYuOwnedHost-" + uuid.uuid4().hex
        if not isinstance(self._name, str) or not self._name or "\x00" in self._name:
            raise ValueError("Job name must be a nonempty native name")
        self._lock = threading.Lock()
        self._root_handle: int | None = None
        self._startup_handle: int | None = None
        self._unverified_handles: list[int] = []
        self._closed = False
        self._stopping = False
        ctypes.set_last_error(0)
        handle = self._api.kernel.CreateJobObjectW(None, self._name)
        error = ctypes.get_last_error()
        self._api.check(handle, "CreateJobObjectW")
        self._job_handle = int(handle)
        try:
            if error == 183:
                raise OwnedHostJobError("job_name_already_exists", error)
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            self._api.check(self._api.kernel.SetInformationJobObject(
                handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits),
            ), "SetInformationJobObject")
            self._verify_limits()
        except BaseException:
            self._api.close(self._job_handle)
            self._closed = True
            raise
        _log.info("owned_job.created")

    @property
    def name(self) -> str:
        return self._name

    @staticmethod
    def _deadline(timeout_seconds: float) -> float:
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive and finite")
        return time.monotonic() + timeout_seconds

    def _acquire(self, deadline: float) -> None:
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise OwnedHostJobError("owner_lock_deadline")

    def _require_open(self) -> None:
        if self._closed:
            raise OwnedHostJobError("job_closed")

    def _verify_limits(self) -> None:
        limits = _ExtendedLimits()
        self._api.query(self._job_handle, _EXTENDED_LIMIT_INFORMATION, limits)
        if limits.BasicLimitInformation.LimitFlags != _KILL_ON_JOB_CLOSE:
            raise OwnedHostJobError("job_limits_changed")

    def wait_for_member(self, process_handle: int, timeout_seconds: float) -> bool:
        """Pin the exact process object only after native membership succeeds.

        A context binds once. Timeout does not register ownership or permit stop.
        The caller retains ownership of its original process handle.
        """
        deadline = self._deadline(timeout_seconds)
        return self._wait_for_member_until(process_handle, deadline)

    def _wait_for_member_until(self, process_handle: int, deadline: float) -> bool:
        if (isinstance(process_handle, bool) or not isinstance(process_handle, int)
                or process_handle <= 0 or process_handle >= ctypes.c_void_p(-1).value):
            raise ValueError("An exact real process handle is required")
        self._acquire(deadline)
        duplicate = wintypes.HANDLE()
        try:
            self._require_open()
            if self._root_handle is not None or self._stopping:
                raise OwnedHostJobError("root_already_registered_or_stopping")
            self._verify_limits()
            current = self._api.kernel.GetCurrentProcess()
            self._api.check(self._api.kernel.DuplicateHandle(
                current, process_handle, current, ctypes.byref(duplicate), 0, False, 2,
            ), "DuplicateHandle")
            while True:
                self._api.check(self._api.kernel.GetProcessId(duplicate), "GetProcessId")
                belongs = wintypes.BOOL()
                self._api.check(self._api.kernel.IsProcessInJob(
                    duplicate, self._job_handle, ctypes.byref(belongs),
                ), "IsProcessInJob")
                signaled = self._root_signaled(int(duplicate.value))
                if belongs.value and not signaled and time.monotonic() < deadline:
                    self._root_handle = int(duplicate.value)
                    duplicate = wintypes.HANDLE()
                    _log.info("owned_job.member_registered")
                    return True
                remaining = deadline - time.monotonic()
                if signaled or remaining <= 0:
                    _log.warning("owned_job.member_unconfirmed rootSignaled=%s", signaled)
                    return False
                time.sleep(min(_POLL_SECONDS, remaining))
        finally:
            try:
                if duplicate.value:
                    self._api.close(int(duplicate.value))
            finally:
                self._lock.release()

    def _root_signaled(self, handle: int) -> bool:
        result = self._api.kernel.WaitForSingleObject(handle, 0)
        if result not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
            raise OwnedHostJobError("WaitForSingleObject", ctypes.get_last_error())
        return result == _WAIT_OBJECT_0

    def _pin_startup_root_until(self, handle: int, deadline: float) -> None:
        if (isinstance(handle, bool) or not isinstance(handle, int)
                or handle <= 0 or handle >= ctypes.c_void_p(-1).value):
            raise ValueError("exact native handle required")
        self._acquire(deadline)
        try:
            self._require_open()
            if self._startup_handle is not None:
                return
            duplicate = wintypes.HANDLE()
            current = self._api.kernel.GetCurrentProcess()
            self._api.check(self._api.kernel.DuplicateHandle(
                current, handle, current, ctypes.byref(duplicate), 0, False, 2), "DuplicateHandle")
            duplicated_handle = int(duplicate.value)
            self._unverified_handles.append(duplicated_handle)
            try:
                self._api.check(self._api.kernel.GetProcessId(duplicated_handle), "GetProcessId")
            except OwnedHostJobError as failure:
                try:
                    self._api.close(duplicated_handle)
                    self._unverified_handles.remove(duplicated_handle)
                except OwnedHostJobError as close_error:
                    failure.add_note(str(close_error))
                raise
            self._startup_handle = duplicated_handle
            self._unverified_handles.remove(duplicated_handle)
        finally:
            self._lock.release()

    def _abort_startup_until(self, deadline: float) -> bool:
        self._acquire(deadline)
        try:
            self._require_open()
            if self._startup_handle is None:
                raise OwnedHostJobError("startup_root_missing")
            self._stopping = True
            if not self._root_signaled(self._startup_handle):
                self._api.check(self._api.kernel.TerminateProcess(self._startup_handle, 70), "TerminateProcess")
            self._api.check(self._api.kernel.TerminateJobObject(self._job_handle, 70), "TerminateJobObject")
            while time.monotonic() < deadline:
                accounting = _Accounting()
                self._api.query(self._job_handle, _BASIC_ACCOUNTING_INFORMATION, accounting)
                if self._root_signaled(self._startup_handle) and accounting.ActiveProcesses == 0:
                    return True
                time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
            return False
        finally:
            self._lock.release()

    def _observe(self) -> JobObservation:
        self._require_open()
        if self._root_handle is None:
            raise OwnedHostJobError("root_not_registered")
        self._api.check(self._api.kernel.GetProcessId(self._root_handle), "GetProcessId")
        accounting = _Accounting()
        self._api.query(self._job_handle, _BASIC_ACCOUNTING_INFORMATION, accounting)
        return JobObservation(accounting.ActiveProcesses, self._root_signaled(self._root_handle))

    def observe(self) -> JobObservation:
        """Read native facts, without stopping or granting a completion receipt."""
        with self._lock:
            return self._observe()

    def _observe_until(self, deadline: float) -> JobObservation:
        self._acquire(deadline)
        try:
            return self._observe()
        finally:
            self._lock.release()

    def stop_and_wait(self, timeout_seconds: float, *, exit_code: int = 70) -> bool:
        """Terminate only this registered Job; require zero members AND root exit.

        The monotonic deadline bounds lock/poll waits, not kernel syscall latency.
        Keep the context after False or an error for explicit recovery. No new
        dispatch/assignment may be issued against a context once stopping begins.
        """
        deadline = self._deadline(timeout_seconds)
        return self._stop_and_wait_until(deadline, exit_code=exit_code)

    def _stop_and_wait_until(self, deadline: float, *, exit_code: int = 70) -> bool:
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 0xFFFFFFFF:
            raise ValueError("exit_code must be a DWORD")
        self._acquire(deadline)
        try:
            self._observe()
            self._verify_limits()
            self._stopping = True
            self._api.check(self._api.kernel.TerminateJobObject(
                self._job_handle, exit_code,
            ), "TerminateJobObject")
            while True:
                observation = self._observe()
                remaining = deadline - time.monotonic()
                if observation.active_processes == 0 and observation.root_signaled and remaining > 0:
                    _log.info("owned_job.stopped activeProcesses=0 rootSignaled=True")
                    return True
                if remaining <= 0:
                    _log.warning("owned_job.stop_unconfirmed activeProcesses=%s rootSignaled=%s",
                                 observation.active_processes, observation.root_signaled)
                    return False
                time.sleep(min(_POLL_SECONDS, remaining))
        finally:
            self._lock.release()

    def terminate(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise OwnedHostJobError("owner_lock_busy")
        try:
            self._observe()
            self._stopping = True
            self._api.check(self._api.kernel.TerminateJobObject(self._job_handle, 70), "TerminateJobObject")
        finally:
            self._lock.release()

    def close(self) -> None:
        """Release owned handles; kill-on-last-close is not a verified stop result."""
        with self._lock:
            if self._closed:
                return
            for handle in list(self._unverified_handles):
                self._api.close(handle)
                self._unverified_handles.remove(handle)
            accounting = _Accounting()
            self._api.query(self._job_handle, _BASIC_ACCOUNTING_INFORMATION, accounting)
            if accounting.ActiveProcesses or (self._root_handle is not None
                    and not self._root_signaled(self._root_handle)):
                raise OwnedHostJobError("job_not_quiet")
            if self._startup_handle is not None:
                if not self._root_signaled(self._startup_handle):
                    raise OwnedHostJobError("startup_root_not_quiet")
                self._api.close(self._startup_handle)
                self._startup_handle = None
            if self._root_handle is not None:
                self._api.close(self._root_handle)
                self._root_handle = None
            self._api.close(self._job_handle)
            self._closed = True
            _log.info("owned_job.closed")

    def __enter__(self) -> OwnedHostJob:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
