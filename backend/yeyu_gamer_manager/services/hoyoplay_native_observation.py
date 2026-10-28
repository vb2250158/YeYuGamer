"""Bounded HoYoPlay evidence acquisition, with no game or UI action capability.

Native execution requires the Manager's current observation-task guard. This
module collects unqualified evidence; it never infers a selected game or grants
update, repair, login, consent, or game-entry permission.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import struct
import subprocess
import time
import uuid

from .hoyoplay_owner_family import OWNER_FAMILY_NATIVE, valid_node_owner

from .game_launcher import GameLaunchCancelled, GameLaunchError, GameLaunchService
from . import window_capture
from .hoyoplay_owner_diagnostics import owner_diagnostic


FIXED_GAME_EXECUTABLE = Path(r"D:\Game\Games\ZenlessZoneZero Game\ZenlessZoneZero.exe")
FIXED_LAUNCHER_ROOT = Path(r"D:\Game\Launchers\HoYoPlay")
FIXED_LAUNCHER_EXECUTABLE = FIXED_LAUNCHER_ROOT / "launcher.exe"
# Fixed Chromium/CEF accessibility option; no caller-supplied launcher arguments.
HYP_ACCESSIBILITY_ARGUMENTS = ("--force-renderer-accessibility",)
MAX_SECONDS = 15.0
MAX_HELPER_BYTES = 256 * 1024 * 1024
MAX_AX_BYTES = 256 * 1024
MAX_NODE_BYTES = 192 * 1024  # Reserve the remaining AX budget for bounded metadata.
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 8_000_000
ACQUISITION_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failure(reason: str, message: str, *, detail=None) -> GameLaunchError:
    return GameLaunchError(message, reason_code=reason, detail=detail)


def _known_game_names() -> list[str]:
    # Read the existing Manager-owned registry; do not fork its game mapping.
    names = set(window_capture._LDPLAYER_CAPTURE_PROCESS_NAMES)
    for rule in window_capture._RULES.values():
        names.update(rule.process_names)
    return sorted(name.casefold() for name in names)


def _trusted_image(image: str) -> bool:
    path = PureWindowsPath(image)
    root = PureWindowsPath(str(FIXED_LAUNCHER_ROOT))
    if path == root / "launcher.exe":
        return True
    return (path.name.casefold() == "hyp.exe" and path.parent.parent == root
            and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", path.parent.name) is not None)


class _PinnedLauncher:
    """Deny file writes/deletion across signature admission and process creation."""

    def __init__(self) -> None:
        self.handle = None

    def __enter__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "GetFinalPathNameByHandleW": ([wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD], wintypes.DWORD),
            "GetFileInformationByHandleEx": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        handle = self.kernel.CreateFileW(str(FIXED_LAUNCHER_EXECUTABLE), 0x80000000, 1,
                                         None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise _failure("zzz_observation_launcher_pin_failed", "无法锁定已安装启动器，未发出启动请求。")
        self.handle = handle
        try:
            self.verify()
        except BaseException:
            self.close()
            raise
        return self

    def verify(self) -> None:
        if not self.handle:
            raise _failure("zzz_observation_launcher_pin_missing", "启动器文件锁已失效。")
        name = ctypes.create_unicode_buffer(32768)
        size = self.kernel.GetFinalPathNameByHandleW(self.handle, name, len(name), 0)
        attributes = (wintypes.DWORD * 2)()
        valid = self.kernel.GetFileInformationByHandleEx(self.handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes))
        final_path = name.value.removeprefix("\\\\?\\")
        if (not valid or not 0 < size < len(name) or attributes[0] & 0x400
                or PureWindowsPath(final_path) != PureWindowsPath(str(FIXED_LAUNCHER_EXECUTABLE))):
            raise _failure("zzz_observation_launcher_pin_changed", "锁定的启动器路径或重解析属性不符。")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __exit__(self, *_exc) -> None:
        self.close()


class _HelperJob:
    """Lazy Windows Job; the launcher is never assigned to this helper tree."""

    def __init__(self) -> None:
        c, w = ctypes, wintypes
        class Basic(c.Structure):
            _fields_ = [("processTime", c.c_int64), ("jobTime", c.c_int64), ("flags", w.DWORD),
                        ("minWorking", c.c_size_t), ("maxWorking", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read", "write", "other", "readBytes", "writeBytes", "otherBytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("processMemory", c.c_size_t),
                        ("jobMemory", c.c_size_t), ("peakProcess", c.c_size_t), ("peakJob", c.c_size_t)]
        class Thread(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("threadId", w.DWORD),
                        ("owner", w.DWORD), ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]
        self.Thread = Thread
        class Process(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", c.c_size_t),
                        ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD),
                        ("priority", w.LONG), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
        self.Process = Process
        class Members(c.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("pids", c.c_size_t * 32)]
        self.Members = Members
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, c.POINTER(Thread)], w.BOOL),
            "Thread32Next": ([w.HANDLE, c.POINTER(Thread)], w.BOOL),
            "Process32FirstW": ([w.HANDLE, c.POINTER(Process)], w.BOOL),
            "Process32NextW": ([w.HANDLE, c.POINTER(Process)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2200  # KILL_ON_JOB_CLOSE | JOB_MEMORY; no breakaway
        limits.jobMemory = MAX_HELPER_BYTES
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.close()
            raise c.WinError(c.get_last_error())

    def assign(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process: subprocess.Popen) -> None:
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = self.Thread()
            value.size = ctypes.sizeof(value)
            threads = []
            more = self.kernel.Thread32First(snapshot, ctypes.byref(value))
            while more:
                if value.owner == process.pid:
                    threads.append(value.threadId)
                more = self.kernel.Thread32Next(snapshot, ctypes.byref(value))
            if len(threads) != 1:
                raise _failure("zzz_observation_suspended_identity_changed", "辅助进程初始线程身份不符。")
            thread = self.kernel.OpenThread(2, False, threads[0])
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.kernel.ResumeThread(thread) != 1:
                    raise _failure("zzz_observation_resume_failed", "辅助进程未从预期挂起状态恢复。")
            finally:
                self.kernel.CloseHandle(thread)
        finally:
            self.kernel.CloseHandle(snapshot)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def finish(self) -> tuple[bool, list[int]]:
        remaining = list(self.members())
        if remaining:
            if not self.kernel.TerminateJobObject(self.handle, 125):
                return False, remaining
            deadline = time.monotonic() + 2.0
            while remaining and time.monotonic() < deadline:
                time.sleep(0.05)
                remaining = list(self.members())
        return not remaining, remaining

    def members(self) -> tuple[int, ...]:
        value = self.Members()
        if not self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(value), ctypes.sizeof(value), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if value.assigned > 32 or value.count > 32:
            raise _failure("zzz_observation_helper_process_limit", "观察辅助进程树超出上限。")
        return tuple(value.pids[:value.count])

    def parent_pid(self, pid: int) -> int | None:
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = self.Process()
            value.size = ctypes.sizeof(value)
            more = self.kernel.Process32FirstW(snapshot, ctypes.byref(value))
            count = 0
            while more and count < 4096:
                if value.pid == pid:
                    return value.parent
                more = self.kernel.Process32NextW(snapshot, ctypes.byref(value))
                count += 1
            return None
        finally:
            self.kernel.CloseHandle(snapshot)


class NativeHoYoPlaySampler:
    def __init__(
        self, game_executable: Path, *, cancelled: Callable[[], bool],
        register_helper: Callable[[dict], None], task_id: str, manager_id: str,
        launch_guard: Callable[[], AbstractContextManager],
    ) -> None:
        self.game_executable = game_executable
        self.cancelled = cancelled
        self.register_helper = register_helper
        self.launch_guard = launch_guard
        self.task_id = str(uuid.UUID(task_id))
        if not manager_id or len(manager_id) > 128:
            raise ValueError("observation requires a bounded Manager identity")
        self.manager_id = manager_id
        self._helpers: list[dict] = []
        self._launches: list[dict] = []
        self._used = False
        self._helper_closed = True
        self._remaining_helpers: set[int] = set()

    def _guard(self) -> None:
        if self.cancelled():
            raise GameLaunchCancelled("观察任务已停止或失去当前代次。",
                                      reason_code="zzz_observation_cancelled")
        if time.monotonic() >= self._deadline:
            raise _failure("zzz_observation_timeout", "启动器观察达到时间上限。")

    def _validate_inputs(self, output_dir: Path) -> None:
        if os.name != "nt":
            raise _failure("zzz_observation_windows_required", "原生观察仅支持 Windows。")
        if PureWindowsPath(str(self.game_executable)) != PureWindowsPath(str(FIXED_GAME_EXECUTABLE)):
            raise _failure("zzz_observation_game_binding_mismatch", "绝区零路径与固定绑定不符。")
        for path in (output_dir, self.game_executable, FIXED_LAUNCHER_EXECUTABLE):
            GameLaunchService._require_queue_local_path(path)
        if not output_dir.is_dir() or not self.game_executable.is_file():
            raise _failure("zzz_observation_path_unavailable", "观察目录或游戏绑定不可用。")
        if any(output_dir.iterdir()):
            raise _failure("zzz_observation_output_not_empty", "观察输出目录必须是新建的空目录。")

    def sample(self, output_dir: Path, *, allow_start: bool) -> dict:
        result = {"status": "blocked", "reasonCode": None, "message": "",
                  "selectedGameVerified": False, "actions": [], "screenshots": [],
                  "qualification": False, "productionUpdateAllowed": False,
                  "processes": [], "helpers": self._helpers, "launches": self._launches,
                  "helperClosed": self._helper_closed, "remainingHelperPids": sorted(self._remaining_helpers)}
        if self._used:
            result.update(reasonCode="zzz_observation_already_sampled", message="采样实例不能重复运行。")
            return result
        self._used = True
        self._deadline = time.monotonic() + MAX_SECONDS
        try:
            self._guard()
            self._validate_inputs(output_dir)
            with _PinnedLauncher() as pinned:
                admission = self._run_worker("admit", output_dir)
                self._validate_processes(admission.get("processes"))
                if not admission["processes"]:
                    if not allow_start:
                        raise _failure("zzz_observation_launcher_not_running", "启动器尚未运行，未请求启动。")
                    self._start_launcher(output_dir, pinned)
            self._guard()
            observed = self._run_worker("observe", output_dir)
            self._guard()
            self._validate_result(observed, output_dir)
            started = {(item["pid"], item["createdAt"]) for item in self._launches}
            for item in observed["processes"]:
                item["source"] = "started" if (item["pid"], item["createdAt"]) in started else "existing"
            result.update(status="completed", reasonCode="zzz_observation_contract_unqualified",
                          message="仅取得启动器观察证据，游戏选中状态和动作合同尚未审定。",
                          axPath="ax.json", screenshots=observed["screenshots"],
                          axSha256=observed["axSha256"],
                          processes=observed["processes"], truncated=observed.get("truncated", True))
        except GameLaunchCancelled as error:
            result.update(status="cancelled", reasonCode=error.reason_code, message=str(error))
        except GameLaunchError as error:
            result.update(reasonCode=error.reason_code, message=str(error))
            if error.reason_code == 'zzz_observation_ax_owner_mismatch' and isinstance(error.detail, dict):
                value = error.detail.get('ownerDiagnostic')
                if isinstance(value, dict):
                    safe = owner_diagnostic(value, source='observation', task_id=self.task_id,
                        helper_pid=value.get('helperPid'), sequence=0)
                    if safe is not None:
                        result['ownerDiagnostic'] = safe
        except OSError:
            result.update(status="failed", reasonCode="zzz_observation_io_failed",
                          message="启动器观察的本机文件或进程操作失败。")
        except Exception:
            result.update(status="failed", reasonCode="zzz_observation_unexpected_failure",
                          message="观察失败；已发出的启动请求仍保留，不自动重试。")
        result.update(helperClosed=self._helper_closed, remainingHelperPids=sorted(self._remaining_helpers))
        if not self._helper_closed:
            result.update(status="failed", reasonCode="zzz_observation_helper_cleanup_unverified",
                          message="观察辅助进程树退出尚未证实，保留任务占用。")
        return result

    def _start_launcher(self, output_dir: Path, pinned: _PinnedLauncher) -> None:
        self._guard()
        pinned.verify()  # native path query stays outside the Store transaction
        with self.launch_guard():
            self._guard()  # Manager serializes this short section with Stop.
            requested_at = _utc_now()
            process = subprocess.Popen(
                [str(FIXED_LAUNCHER_EXECUTABLE), *HYP_ACCESSIBILITY_ARGUMENTS], cwd=str(FIXED_LAUNCHER_ROOT),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False,
            )
            record = {"pid": process.pid, "parentPid": os.getpid(), "source": "started",
                      "purpose": "hoyoplay-observation-launcher", "taskId": self.task_id,
                      "managerId": self.manager_id, "createdAt": None,
                      "imagePath": str(FIXED_LAUNCHER_EXECUTABLE), "launchRequestedAt": requested_at,
                      "logPath": "launcher-start.json", "noUiActions": True, "launcherFilePinned": True}
            self._launches.append(record)  # retained even if registration fails
            self.register_helper(dict(record))  # Manager must only register in memory here
        pinned.close()  # Popen returned; do not hold the file lock during later I/O
        self._guard()
        identity = GameLaunchService._queue_process_identity(process.pid)
        record["createdAt"] = identity.created_at if identity else None
        (output_dir / record["logPath"]).write_text(json.dumps(record), encoding="utf-8")
        self.register_helper(dict(record))
        # Never terminate an existing or newly started launcher, even on Stop.
        if identity is None or PureWindowsPath(str(identity.executable)) != PureWindowsPath(str(FIXED_LAUNCHER_EXECUTABLE)):
            raise _failure("zzz_observation_launch_identity_unavailable", "启动请求已发出，但进程身份未能确认。")
        self._guard()

    def _run_worker(self, mode: str, output_dir: Path) -> dict:
        self._guard()
        index = len(self._helpers)
        stdout_path = output_dir / f"helper-{index}.stdout.json"
        stderr_path = output_dir / f"helper-{index}.stderr.log"
        script_path = output_dir / f"helper-{index}.ps1"
        with script_path.open("x", encoding="utf-8") as script:
            script.write(_NATIVE_WORKER)
        environment = dict(os.environ)
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
        environment["PSModulePath"] = str(powershell.parent / "Modules")
        known_names = _known_game_names()
        known_names_json = json.dumps(known_names)
        environment.update(YEYU_HYP_MODE=mode, YEYU_HYP_OUTPUT=str(output_dir),
                           YEYU_HYP_SCRIPT=str(script_path),
                            YEYU_HYP_SAMPLE_ID=self.task_id,
                           YEYU_HYP_GAME=str(self.game_executable),
                           YEYU_HYP_GAME_NAMES=known_names_json,
                           YEYU_HYP_GAME_NAMES_COUNT=str(len(known_names)),
                           YEYU_HYP_GAME_NAMES_SHA256=hashlib.sha256(known_names_json.encode("utf-8")).hexdigest())
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                   "& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:YEYU_HYP_SCRIPT,[Text.Encoding]::UTF8)))"]
        record = None
        descendants = []
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            self._guard()
            job = _HelperJob()
            process = None
            assigned = False
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                           env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 4)
                record = {"pid": process.pid, "parentPid": os.getpid(), "source": str(powershell),
                          "purpose": "hoyoplay-readonly-" + mode, "taskId": self.task_id,
                          "managerId": self.manager_id, "createdAt": None,
                          "logPath": stderr_path.name, "stdoutPath": stdout_path.name,
                          "scriptPath": script_path.name, "startedAt": _utc_now(), "consoleHidden": True,
                          "killOnJobClose": True, "hardJobMemoryLimitBytes": MAX_HELPER_BYTES,
                          "breakawayAllowed": False, "createdSuspended": True}
                self._helpers.append(record)
                job.assign(process)
                assigned = True
                identity = GameLaunchService._queue_process_identity(process.pid)
                if identity is None or PureWindowsPath(str(identity.executable)) != PureWindowsPath(str(powershell)):
                    raise _failure("zzz_observation_helper_identity_unavailable", "观察辅助进程身份不可读。")
                record["createdAt"] = identity.created_at
                self.register_helper(dict(record))
                self._guard()
                job.resume(process)
                # Suspended creation prevents worker code before assignment/registration.
                seen_members = {process.pid}
                while process.poll() is None:
                    self._guard()
                    for child_pid in job.members():
                        if child_pid in seen_members:
                            continue
                        child_identity = GameLaunchService._queue_process_identity(child_pid)
                        if child_identity is None:
                            continue  # short-lived member may already have exited
                        parent_pid = job.parent_pid(child_pid)
                        if parent_pid is None:
                            continue
                        child_record = {"pid": child_pid, "parentPid": parent_pid,
                                        "source": str(child_identity.executable), "purpose": "hoyoplay-observation-helper-descendant",
                                        "taskId": self.task_id, "managerId": self.manager_id,
                                        "createdAt": child_identity.created_at, "logPath": stderr_path.name,
                                        "ownership": "inherited-helper-job", "parentPidVerified": True}
                        self._helpers.append(child_record)
                        descendants.append(child_record)
                        self.register_helper(dict(child_record))
                        seen_members.add(child_pid)
                    memory = GameLaunchService._process_working_set_bytes(process.pid)
                    if memory is None or memory > MAX_HELPER_BYTES:
                        raise _failure("zzz_observation_helper_memory_limit", "观察辅助进程内存超限或无法核验。")
                    if stdout_path.stat().st_size > MAX_AX_BYTES or stderr_path.stat().st_size > 65536:
                        raise _failure("zzz_observation_helper_output_limit", "观察辅助进程输出超过上限。")
                    time.sleep(0.05)
            finally:
                closed, remaining = False, [process.pid] if process else []
                try:
                    if process is not None and not assigned and process.poll() is None:
                        GameLaunchService._stop_launcher_probe(process)
                    closed, remaining = job.finish()  # includes compiler descendants
                    if process is not None and process.poll() is None:
                        closed = False
                        remaining = sorted(set(remaining) | {process.pid})
                except Exception:
                    closed = False
                finally:
                    job.close()  # kill-on-close remains the final fallback
                    self._helper_closed = self._helper_closed and closed
                    self._remaining_helpers.update(remaining)
                if record is not None:
                    record.update(exitCode=process.poll(), endedAt=_utc_now(), helperClosed=closed,
                                  remainingHelperPids=remaining, jobAssigned=assigned)
                    for child_record in descendants:
                        child_record.update(endedAt=record["endedAt"], exitCode=None,
                                            exitCodeKnown=False, endEvidence="owning-helper-job-empty" if closed else "exit-unverified")
                    (output_dir / f"helper-{index}.process.json").write_text(json.dumps(record), encoding="utf-8")
        if not self._helper_closed:
            raise _failure("zzz_observation_helper_cleanup_unverified", "辅助进程树退出尚未证实。")
        self._guard()
        if stdout_path.stat().st_size > MAX_AX_BYTES:
            raise _failure("zzz_observation_helper_output_limit", "观察辅助进程输出超过上限。")
        try:
            payload = json.loads(stdout_path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError):
            raise _failure("zzz_observation_invalid_helper_output", "观察辅助进程未返回有效结果。") from None
        if process.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
            reason = payload.get("reasonCode", "") if isinstance(payload, dict) else ""
            if not re.fullmatch(r"zzz_observation_[a-z_]{1,80}", reason):
                reason = "zzz_observation_native_failed"
            detail = None
            if reason == 'zzz_observation_ax_owner_mismatch':
                value = owner_diagnostic(payload.get('ownerDiagnostic'), source='observation',
                    task_id=self.task_id, helper_pid=process.pid, sequence=0)
                if value is not None:
                    detail = {'ownerDiagnostic':value}
            raise _failure(reason, "受限原生观察未通过，请查看任务诊断。", detail=detail)
        return payload

    @staticmethod
    def _validate_processes(processes: object) -> None:
        if not isinstance(processes, list) or len(processes) > 32:
            raise _failure("zzz_observation_invalid_processes", "启动器进程证据无效。")
        seen = set()
        for item in processes:
            if (not isinstance(item, dict) or type(item.get("pid")) is not int
                    or item["pid"] <= 0 or type(item.get("createdAt")) is not int or item["createdAt"] <= 0
                    or not _trusted_image(item.get("imagePath", "")) or item.get("signatureStatus") != "Valid"
                    or item.get("signer") != "miHoYo Co.,Ltd." or item.get("productName") != "米哈游启动器"
                    or item["pid"] in seen):
                raise _failure("zzz_observation_invalid_identity", "启动器身份或签名证据不符。")
            seen.add(item["pid"])

    def _validate_result(self, payload: dict, output_dir: Path) -> None:
        self._validate_processes(payload.get("processes"))
        if any(item.get("sampleId") != self.task_id for item in payload["processes"]):
            raise _failure("zzz_observation_invalid_acquisition", "进程身份不是同一次原生采集。")
        identities = {(p["pid"], p["createdAt"]) for p in payload["processes"]}
        screenshots = payload.get("screenshots")
        if payload.get("axPath") != "ax.json" or not isinstance(screenshots, list) or not 1 <= len(screenshots) <= 3:
            raise _failure("zzz_observation_invalid_artifacts", "观察产物清单无效。")
        ax = output_dir / "ax.json"
        GameLaunchService._require_queue_local_path(ax)
        if ax.stat().st_size > MAX_AX_BYTES:
            raise _failure("zzz_observation_ax_limit", "AX 证据超过大小上限。")
        ax_bytes = ax.read_bytes()
        ax_data = json.loads(ax_bytes.decode("utf-8-sig"))
        payload["axSha256"] = hashlib.sha256(ax_bytes).hexdigest()
        windows = ax_data.get("windows") if isinstance(ax_data, dict) else None
        if not isinstance(windows, list) or not 1 <= len(windows) <= 3:
            raise _failure("zzz_observation_invalid_ax", "AX 证据结构无效。")
        for window in windows:
            if (window.get("pid"), window.get("createdAt")) not in identities or len(window.get("nodes", [])) > 256:
                raise _failure("zzz_observation_invalid_ax", "AX 证据与进程身份不符。")
            owner = window.get("ownerProcess")
            if owner is not None and (owner not in payload["processes"]
                    or (owner.get("pid"), owner.get("createdAt")) != (window["pid"], window["createdAt"])):
                raise _failure("zzz_observation_invalid_ax", "窗口所有权证据与同次进程身份不符。")
        if ax_data.get("selectedGameVerified") is not False or ax_data.get("actions") != []:
            raise _failure("zzz_observation_invalid_ax", "观察证据不能宣称游戏已选中或授予动作。")
        self._validate_acquisition(ax_data)
        expected_windows = {(w["pid"], w["createdAt"], w.get("hwnd")) for w in windows}
        seen = set()
        for frame in screenshots:
            name = frame.get("path", "")
            if (not re.fullmatch(r"window-[0-2]\.png", name) or name in seen
                    or (frame.get("pid"), frame.get("createdAt")) not in identities
                    or (frame.get("pid"), frame.get("createdAt"), frame.get("hwnd")) not in expected_windows
                    or type(frame.get("hwnd")) is not int or frame["hwnd"] <= 0 or not frame.get("capturedAt")
                    or frame.get("sampleId") != self.task_id
                    or frame.get("method") != "PrintWindow" or frame.get("watermarked") is not False):
                raise _failure("zzz_observation_invalid_frame", "窗口原图身份或来源不符。")
            seen.add(name)
            path = output_dir / name
            GameLaunchService._require_queue_local_path(path)
            if path.stat().st_size > MAX_PNG_BYTES:
                raise _failure("zzz_observation_frame_limit", "窗口原图超过大小上限。")
            with path.open("rb") as stream:
                header = stream.read(24)
                if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
                    raise _failure("zzz_observation_invalid_png", "窗口原图不是 PNG。")
                width, height = struct.unpack(">II", header[16:24])
                if not 0 < width * height <= MAX_PIXELS or (width, height) != (frame.get("width"), frame.get("height")):
                    raise _failure("zzz_observation_frame_limit", "窗口原图尺寸不符。")
                stream.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            frame["sha256"] = digest.hexdigest()
            window = next(w for w in windows if (w["pid"], w["createdAt"], w["hwnd"])
                          == (frame["pid"], frame["createdAt"], frame["hwnd"]))
            capture = frame.get("acquisition", {})
            before = ax_data["inventoryChecks"][window["inventoryAfter"]]
            final = ax_data["inventoryChecks"][-1]
            if (not isinstance(capture, dict) or type(capture.get("startedTicks")) is not int
                    or type(capture.get("completedTicks")) is not int
                    or not before["completedTicks"] <= capture["startedTicks"] <= capture["completedTicks"] <= final["startedTicks"]
                    or capture.get("startedAt") != frame["capturedAt"]
                    or capture.get("completedAt") != frame.get("captureCompletedAt")):
                raise _failure("zzz_observation_invalid_acquisition", "原图采集时段与进程检查点不符。")
        if {(f["pid"], f["createdAt"], f["hwnd"]) for f in screenshots} != expected_windows:
            raise _failure("zzz_observation_missing_frame", "有窗口 AX 缺少对应原图。")

    def _validate_acquisition(self, ax: dict) -> None:
        """Reject detached acquisition metadata without assigning UI semantics."""
        def require(condition):
            if not condition:
                raise _failure("zzz_observation_invalid_acquisition", "原生采集关联或字段不完整。")

        def integer(value, lower=0):
            return type(value) is int and value >= lower

        def runtime_id(value):
            return (value == "unknown" or isinstance(value, list) and 1 <= len(value) <= 32
                    and all(type(part) is int and -(2**31) <= part < 2**31 for part in value))

        def interval(record, start, end):
            require(isinstance(record, dict))
            left, right = record.get("startedTicks"), record.get("completedTicks")
            require(integer(left) and integer(right) and start <= left <= right <= end)
            for field in ("startedAt", "completedAt"):
                try:
                    stamp = datetime.fromisoformat(record[field].replace("Z", "+00:00"))
                    require(stamp.utcoffset() is not None)
                except (KeyError, AttributeError, TypeError, ValueError):
                    require(False)

        require(type(ax.get("schemaVersion")) is int and ax["schemaVersion"] == ACQUISITION_SCHEMA_VERSION
                and ax.get("qualification") is False and ax.get("productionUpdateAllowed") is False)
        require(ax.get("nodeByteBudget") == MAX_NODE_BYTES and integer(ax.get("nodeByteCount"))
                and ax["nodeByteCount"] <= MAX_NODE_BYTES)
        capture = ax.get("acquisition", {})
        require(isinstance(capture, dict) and capture.get("sampleId") == self.task_id
                and integer(capture.get("workerPid"), 1)
                and integer(capture.get("clockFrequency"), 1)
                and capture.get("clock") == "System.Diagnostics.Stopwatch.GetTimestamp"
                and capture.get("atomicSnapshot") is False)
        start, end = capture.get("startedTicks"), capture.get("completedTicks")
        require(integer(start) and integer(end) and start <= end)
        interval(capture, start, end)
        association = ax.get("installationAssociation", {})
        require(isinstance(association, dict) and association.get("status") == "configuration-only"
                and association.get("primaryGame") == "nap_cn"
                and association.get("gameExecutable") == str(FIXED_GAME_EXECUTABLE)
                and re.fullmatch(r"[0-9a-f]{64}", association.get("configSha256", "")) is not None)
        checks = ax.get("inventoryChecks")
        require(isinstance(checks, list) and 1 <= len(checks) <= 8)
        rule_hash = hashlib.sha256(json.dumps(_known_game_names()).encode("utf-8")).hexdigest()
        for index, check in enumerate(checks):
            interval(check, start, end)
            require(check.get("sampleId") == self.task_id and type(check.get("index")) is int and check["index"] == index
                    and check.get("enumerationComplete") is True
                    and check.get("scope") == "all-Win32_Process"
                    and check.get("policy") == "known-games+game-root+same-session-readable"
                    and check.get("rulesSha256") == rule_hash
                    and integer(check.get("processCount"), 1) and check["processCount"] <= 4096
                    and integer(check.get("unreadableImagePathCount"))
                    and check["unreadableImagePathCount"] <= check["processCount"]
                    and type(check.get("sameSessionUnreadableCount")) is int and check["sameSessionUnreadableCount"] == 0
                    and type(check.get("knownGameMatches")) is int and check["knownGameMatches"] == 0
                    and type(check.get("gameRootMatches")) is int and check["gameRootMatches"] == 0
                    and check.get("unknownExecutableAbsence") == "unknown")
            if index:
                require(checks[index - 1]["completedTicks"] <= check["startedTicks"])
        require(checks[0].get("phase") == "admission" and checks[-1].get("phase") == "final")
        require(sum(len(window.get("nodes", [])) for window in ax["windows"]) <= 256)
        for window in ax["windows"]:
            require(window.get("sampleId") == self.task_id)
            interval(window.get("acquisition"), start, end)
            before, after = window.get("inventoryBefore"), window.get("inventoryAfter")
            require(integer(before) and integer(after) and 0 <= before < after < len(checks))
            require(checks[before].get("phase") == "before-ax" and checks[after].get("phase") == "before-capture"
                    and checks[before]["completedTicks"] <= window["acquisition"]["startedTicks"]
                    and window["acquisition"]["completedTicks"] <= checks[after]["startedTicks"])
            require(window.get("contentAssociation") == {
                "status": "unknown", "gameBiz": "unknown", "channel": "unknown", "installPath": "unknown"})
            for node in window.get("nodes", []):
                raw = node.get("uia", {})
                require(isinstance(raw, dict) and runtime_id(raw.get("runtimeId"))
                        and valid_node_owner(raw, window.get("ownerProcess", window), window["hwnd"], root_node=node.get("parent") == -1)
                        and raw.get("occlusion") == "unknown")
                for field in ("invokePatternAvailable", "selectionItemPatternAvailable", "selected"):
                    require(type(raw.get(field)) is bool or raw.get(field) == "unknown")
                require(raw.get("selectionItemPatternAvailable") is True or raw.get("selected") == "unknown")
                container = raw.get("selectionContainer")
                require(container == "unknown" or isinstance(container, dict)
                        and container.get("ownerPid") == raw.get("ownerPid") and runtime_id(container.get("runtimeId")))


_NATIVE_WORKER = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$root = 'D:\Game\Launchers\HoYoPlay'
$game = 'D:\Game\Games\ZenlessZoneZero Game\ZenlessZoneZero.exe'
$timer = [Diagnostics.Stopwatch]::StartNew()
$sampleId = ([Guid]::Parse($env:YEYU_HYP_SAMPLE_ID)).ToString()
$sampleStartedAt = [DateTime]::UtcNow.ToString('o')
$sampleStartedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
$inventoryChecks = [Collections.Generic.List[object]]::new()
$installationAssociation = @{}
function Limit-Time { if ($timer.Elapsed.TotalSeconds -gt 12) { throw 'zzz_observation_native_timeout' } }
function Hash-Bytes([byte[]]$bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Document-Location([string]$name) {
    # A UIA Document may expose the page URL as Name or Value. Never persist its
    # device/app query values, and do not lose the route behind a long query.
    if ($name -notmatch '^https?://') { return $null }
    $result = @{status='unqualified'; source='UIA.Document.Name';
        origin='unknown'; path='unknown'; route='unknown'; queryOmitted=$true;
        label='[document URL omitted]'}
    $uri = $null
    if ($name.Length -gt 8192 -or -not [Uri]::TryCreate($name,[UriKind]::Absolute,[ref]$uri)) { return $result }
    if ($uri.Scheme -cne 'https' -or $uri.DnsSafeHost -cne 'hyp-api.mihoyo.com' -or
        -not $uri.IsDefaultPort -or $uri.UserInfo -ne '' -or $uri.AbsolutePath -cne '/hypfe/index.html') { return $result }
    $result.origin='https://hyp-api.mihoyo.com'; $result.path='/hypfe/index.html'
    if ($uri.Fragment -ceq '#/nap_cn') { $result.route='/nap_cn' }
    $result.label=$result.origin + $result.path
    if ($result.route -cne 'unknown') { $result.label += '#' + $result.route }
    return $result
}
function Project-Document([string]$name, [string]$value, [string]$text, [bool]$textAvailable) {
    $nameLocation = Document-Location $name
    $location = $nameLocation
    $valueLocation = Document-Location $value
    if ($null -ne $valueLocation) {
        $valueLocation.source='UIA.Document.Value'
        if ($null -eq $location) { $location=$valueLocation }
        elseif ($valueLocation.label -cne $location.label -or $valueLocation.route -cne $location.route) {
            $location=@{status='conflicting'; source='UIA.Document.Name+Value';
                origin='unknown'; path='unknown'; route='unknown'; queryOmitted=$true;
                label='[conflicting document URLs omitted]'}
        }
    }
    $result=@{location=$location; nameRedacted=($null -ne $nameLocation);
        nameLabel=$null; text='unknown'; truncated=$false}
    if ($null -ne $nameLocation) { $result.nameLabel=$nameLocation.label }
    if (($null -ne $nameLocation -and $name.Length -gt 8192) -or
        ($null -ne $valueLocation -and $value.Length -gt 8192)) { $result.truncated=$true }
    if ($null -ne $location -and $location.origin -ceq 'https://hyp-api.mihoyo.com' -and
        $location.route -ceq '/nap_cn' -and $textAvailable) {
        if ($text.Length -gt 4096) { $result.truncated=$true }
        else { $result.text=[Regex]::Replace($text,'https?://\S+','[URL omitted]',[Text.RegularExpressions.RegexOptions]::IgnoreCase) }
    }
    return $result
}
function Local-Path([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    if ($full.StartsWith('\\') -or [IO.DriveInfo]::new([IO.Path]::GetPathRoot($full)).DriveType -ne 'Fixed') { throw 'zzz_observation_nonlocal_path' }
    $item = Get-Item -LiteralPath $full
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'zzz_observation_reparse_path' }
        $parent = [IO.Directory]::GetParent($item.FullName)
        if ($null -eq $parent) { break }; $item = Get-Item -LiteralPath $parent.FullName
    }
    return $full
}
function Install-Association {
    if ([IO.Path]::GetFullPath($env:YEYU_HYP_GAME) -ine $game) { throw 'zzz_observation_game_binding_mismatch' }
    $null = Local-Path $game
    $config = Join-Path $root 'config.ini'; $null = Local-Path $config
    if ((Get-Item -LiteralPath $config).Length -gt 16384) { throw 'zzz_observation_config_limit' }
    $section = ''; $hypCount = 0; $gameCount = 0
    $configStream = [IO.File]::Open($config,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite)
    try {
        $buffer = [byte[]]::new(16385); $count=0
        while ($count -lt $buffer.Length) {
            $read = $configStream.Read($buffer,$count,$buffer.Length-$count)
            if ($read -eq 0) { break }; $count += $read
        }
        if ($count -gt 16384) { throw 'zzz_observation_config_limit' }
        $configBytes = [byte[]]::new($count); [Array]::Copy($buffer,$configBytes,$count)
    } finally { $configStream.Dispose() }
    foreach ($line in ([Text.Encoding]::UTF8.GetString($configBytes).TrimStart([char]0xFEFF) -split '\r?\n')) {
        $line = $line.Trim()
        if ($line -match '^\[([^]]+)\]$') { $section = $Matches[1]; if ($section -ceq 'hyp') { $hypCount++ } }
        elseif ($section -ceq 'hyp' -and $line -match '^primary_game\s*=\s*(.*)$') {
            $gameCount++; if ($Matches[1] -cne 'nap_cn') { throw 'zzz_observation_game_association_mismatch' }
        }
    }
    if ($hypCount -ne 1 -or $gameCount -ne 1) { throw 'zzz_observation_game_association_missing' }
    $association = @{status='configuration-only'; primaryGame='nap_cn'; gameExecutable=$game;
        configSha256=(Hash-Bytes $configBytes)}
    if ($installationAssociation.Count -gt 0 -and $installationAssociation.configSha256 -cne $association.configSha256) {
        throw 'zzz_observation_install_association_changed'
    }
    foreach ($key in $association.Keys) { $installationAssociation[$key] = $association[$key] }
}
function Signed-Image([string]$path) {
    Limit-Time
    $full = Local-Path $path
    if ($full -ine (Join-Path $root 'launcher.exe') -and $full -notmatch ('^' + [Regex]::Escape($root) + '\\\d+\.\d+\.\d+\.\d+\\HYP\.exe$')) { throw 'zzz_observation_image_outside_binding' }
    $sig = Get-AuthenticodeSignature -LiteralPath $full
    $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($full)
    if ($null -eq $sig.SignerCertificate -or $sig.Status -ne 'Valid') { throw 'zzz_observation_signature_invalid' }
    $signer = $sig.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if ($signer -cne 'miHoYo Co.,Ltd.' -or $info.ProductName -cne '米哈游启动器') { throw 'zzz_observation_product_mismatch' }
    return @{signatureStatus='Valid'; signer=$signer; productName=$info.ProductName; fileVersion=$info.FileVersion}
}
function Resolve-InventoryImage([int]$number) {
    # CIM may omit protected process paths even when limited native queries are
    # permitted. Resolve the actual image; never whitelist a process by name.
    if (-not ('HYPInventoryImage' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public sealed class HYPInventoryImage {
    public string ImagePath;
    public bool Exited;
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr handle, out uint code);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr handle, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static HYPInventoryImage Read(int pid) {
        if (pid <= 0) throw new ArgumentOutOfRangeException("pid");
        IntPtr handle = OpenProcess(0x1000, false, pid);
        if (handle == IntPtr.Zero) {
            int error = Marshal.GetLastWin32Error();
            if (error == 87) return new HYPInventoryImage { Exited = true };
            throw new Win32Exception(error);
        }
        try {
            uint code;
            if (!GetExitCodeProcess(handle, out code)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (code != 259) return new HYPInventoryImage { Exited = true };
            var name = new StringBuilder(32768); uint size = (uint)name.Capacity;
            if (!QueryFullProcessImageName(handle, 0, name, ref size)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (!GetExitCodeProcess(handle, out code)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (code != 259) return new HYPInventoryImage { Exited = true };
            return new HYPInventoryImage { ImagePath = name.ToString() };
        } finally { CloseHandle(handle); }
    }
}
'@
    }
    return [HYPInventoryImage]::Read($number)
}
function No-Games([string]$phase) {
    Limit-Time
    $startedAt = [DateTime]::UtcNow.ToString('o'); $startedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
    # PS5 emits a JSON array as one pipeline object; @() would nest it and report Count=1.
    $known = ConvertFrom-Json $env:YEYU_HYP_GAME_NAMES
    $expectedCount = 0
    $rulesHash = Hash-Bytes ([Text.Encoding]::UTF8.GetBytes($env:YEYU_HYP_GAME_NAMES))
    if ($known -isnot [array] -or $known.Count -lt 13 -or
        -not [int]::TryParse($env:YEYU_HYP_GAME_NAMES_COUNT, [ref]$expectedCount) -or
        $known.Count -ne $expectedCount -or $rulesHash -cne $env:YEYU_HYP_GAME_NAMES_SHA256) {
        throw 'zzz_observation_game_registry_missing'
    }
    $uniqueNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($name in $known) {
        if ($name -isnot [string] -or $name -cne $name.ToLowerInvariant() -or
            $name -notmatch '^[^\\/\x00-\x1f:]+\.exe$' -or -not $uniqueNames.Add($name)) {
            throw 'zzz_observation_game_registry_missing'
        }
    }
    $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $all = @(Get-CimInstance Win32_Process -Property ProcessId,Name,ExecutablePath,SessionId)
    if ($all.Count -lt 1 -or $all.Count -gt 4096) { throw 'zzz_observation_process_limit' }
    $unreadable = 0; $resolvedCount = 0; $exitedCount = 0
    $observed = [Collections.Generic.List[object]]::new()
    foreach ($p in $all) {
        if ($null -eq $p.ProcessId -or $null -eq $p.SessionId -or -not $p.Name) { throw 'zzz_observation_inventory_incomplete' }
        if ($known -contains $p.Name.ToLowerInvariant()) { throw 'zzz_observation_game_already_running' }
        $image = $p.ExecutablePath
        if ($p.SessionId -eq $session -and $p.ProcessId -ne 0 -and -not $image) {
            try { $identity = Resolve-InventoryImage $p.ProcessId }
            catch { throw 'zzz_observation_process_identity_unreadable' }
            Limit-Time
            if ($identity.Exited -eq $true) { $exitedCount++; continue }
            $image = $identity.ImagePath
            if (-not $image -or [IO.Path]::GetFileName($image) -ine $p.Name) { throw 'zzz_observation_process_identity_unreadable' }
            $resolvedCount++
        }
        if ($image -and $image.StartsWith('D:\Game\Games\',[StringComparison]::OrdinalIgnoreCase)) { throw 'zzz_observation_unknown_game_process' }
        if (-not $image) { $unreadable++ }
        $observed.Add([pscustomobject]@{ProcessId=$p.ProcessId;Name=$p.Name;ExecutablePath=$image;SessionId=$p.SessionId})
    }
    $inventoryChecks.Add(@{sampleId=$sampleId; index=$inventoryChecks.Count; phase=$phase;
        startedAt=$startedAt; startedTicks=$startedTicks; completedAt=[DateTime]::UtcNow.ToString('o');
        completedTicks=[Diagnostics.Stopwatch]::GetTimestamp(); enumerationComplete=$true;
        scope='all-Win32_Process'; policy='known-games+game-root+same-session-readable';
        rulesSha256=$rulesHash;
        processCount=$all.Count; unreadableImagePathCount=$unreadable; sameSessionUnreadableCount=0;
        resolvedImagePathCount=$resolvedCount; exitedDuringReadCount=$exitedCount;
        imagePathResolution='QueryFullProcessImageNameW/query-limited';
        knownGameMatches=0; gameRootMatches=0; unknownExecutableAbsence='unknown'})
    return $observed.ToArray()
}
function Identity([int]$number) {
    Limit-Time
    $p = [Diagnostics.Process]::GetProcessById($number)
    $path = $p.MainModule.FileName
    $created = $p.StartTime.ToUniversalTime().ToFileTimeUtc()
    $signed = Signed-Image $path
    if ($p.HasExited) { throw 'zzz_observation_process_changed' }
    return @{pid=$number; createdAt=$created; imagePath=$path; signatureStatus=$signed.signatureStatus;
             sessionId=$p.SessionId; sampleId=$sampleId; identityCheckedAt=[DateTime]::UtcNow.ToString('o'); identityCheckedTicks=[Diagnostics.Stopwatch]::GetTimestamp();
             signer=$signed.signer; productName=$signed.productName; fileVersion=$signed.fileVersion}
}
function Same-Identity($old) {
    $now = Identity $old.pid
    if ($now.createdAt -ne $old.createdAt -or $now.imagePath -ine $old.imagePath) { throw 'zzz_observation_process_changed' }
}
try {
    Install-Association
    $null = Signed-Image (Join-Path $root 'launcher.exe')
    $all = @(No-Games 'admission')
    $processes = @()
    foreach ($p in $all) {
        if ($p.Name -in @('launcher.exe','HYP.exe')) {
            if (-not $p.ExecutablePath) { throw 'zzz_observation_launcher_identity_unreadable' }
            if ($p.ExecutablePath.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) {
                $processes += Identity $p.ProcessId
            }
        }
    }
    if ($processes.Count -gt 32) { throw 'zzz_observation_launcher_process_limit' }
    if ($env:YEYU_HYP_MODE -eq 'admit') {
        @{ok=$true; processes=@($processes)} | ConvertTo-Json -Depth 8 -Compress
        exit 0
    }
    if ($env:YEYU_HYP_MODE -ne 'observe') { throw 'zzz_observation_invalid_mode' }
    if ($processes.Count -eq 0) { throw 'zzz_observation_launcher_not_observed' }
    $output = Local-Path $env:YEYU_HYP_OUTPUT
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName System.Drawing
    Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
public static class HYPReadOnly {
    public delegate bool Visit(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] static extern bool EnumWindows(Visit cb, IntPtr param);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    public static bool Descendant(long root, long child) { return root > 0 && child > 0 && root != child && IsChild(new IntPtr(root),new IntPtr(child)); }
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    public static int Owner(long hwnd) { uint pid; GetWindowThreadProcessId(new IntPtr(hwnd),out pid); return (int)pid; }
    public static long[] Windows(int[] pids) {
        var allowed = new HashSet<int>(pids); var result = new List<long>();
        bool complete = EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
            if (allowed.Contains(Owner(hwnd.ToInt64())) && IsWindowVisible(hwnd) && !IsIconic(hwnd)) {
                result.Add(hwnd.ToInt64()); if (result.Count > 3) return false;
            } return true;
        },IntPtr.Zero);
        if (!complete && result.Count <= 3) throw new Exception("zzz_observation_window_enumeration_failed");
        if (result.Count > 3) throw new Exception("zzz_observation_window_limit");
        return result.ToArray();
    }
    public static int[] Capture(long hwndValue, int pid, string path) {
        IntPtr hwnd = new IntPtr(hwndValue); RECT rect;
        if (Owner(hwndValue) != pid || !IsWindowVisible(hwnd) || IsIconic(hwnd) || !GetWindowRect(hwnd,out rect))
            throw new Exception("zzz_observation_window_changed");
        int w = rect.Right-rect.Left, h = rect.Bottom-rect.Top;
        if (w <= 0 || h <= 0 || (long)w*h > 8000000) throw new Exception("zzz_observation_frame_limit");
        using(var bitmap = new Bitmap(w,h,PixelFormat.Format32bppRgb)) {
            using(var g = Graphics.FromImage(bitmap)) {
                IntPtr dc = g.GetHdc();
                try { if (!PrintWindow(hwnd,dc,2)) throw new Exception("zzz_observation_printwindow_failed"); }
                finally { g.ReleaseHdc(dc); }
            }
            if (Owner(hwndValue) != pid) throw new Exception("zzz_observation_window_changed");
            using(var buffer = new MemoryStream()) {
                bitmap.Save(buffer,ImageFormat.Png);
                if(buffer.Length > 16777216) throw new Exception("zzz_observation_frame_limit");
                using(var file = new FileStream(path,FileMode.CreateNew,FileAccess.Write)) { buffer.Position=0; buffer.CopyTo(file); }
            }
        }
        return new int[]{w,h};
    }
}
'@
''' + OWNER_FAMILY_NATIVE + r'''
    function Short-Text($value) { $text = [string]$value; if ($text.Length -gt 128) { return $text.Substring(0,128) }; return $text }
    # Only property getters; availability and selected state are not action permission.
    function Optional-Property($element, $property) {
        try {
            $value = $element.GetCurrentPropertyValue($property,$true)
            if ([object]::ReferenceEquals($value,[Windows.Automation.AutomationElement]::NotSupported)) { return 'unknown' }
            return $value
        } catch { return 'unknown' }
    }
    function Runtime-Identity($element) {
        try {
            $value = @($element.GetRuntimeId())
            if ($value.Count -lt 1 -or $value.Count -gt 32) { return 'unknown' }
            return ,$value
        } catch { return 'unknown' }
    }
    function Optional-Boolean($element, $property) {
        $value = Optional-Property $element $property
        if ($value -is [bool]) { return $value }; return 'unknown'
    }
    function Optional-Text($element, $property) {
        $value = Optional-Property $element $property
        if ($value -is [string] -and $value.Length -gt 0 -and $value.Length -le 128) { return $value }
        return 'unknown'
    }
    function Element-Reference($element, [int]$owner) {
        try {
            if ($null -eq $element -or $element.Current.ProcessId -ne $owner) { return 'unknown' }
            return @{ownerPid=$owner; runtimeId=(Runtime-Identity $element)}
        } catch { return 'unknown' }
    }
    function Owner-Diagnostic($current, [int]$expected, [int]$observed, [long]$hwnd,
                              [int]$node, [int]$parent, [int]$depth, [string]$source, [int]$sequence) {
        # Only numeric identity and fixed enums; never text, URLs or exceptions.
        $type=-1; $nodeHwnd=0
        if($null -ne $current) {
            try { if($null -ne $current.ControlType) {$type=[int]$current.ControlType.Id} } catch { $type=-1 }
            try { $nodeHwnd=[long]$current.NativeWindowHandle } catch { $nodeHwnd=0 }
        }
        $relation=if($parent -eq -1) {'root'} else {'descendant'}
        return @{contract='hoyoplay.uia-owner-diagnostic@1';source=$source;taskId=$sampleId;
            helperPid=$PID;sequence=$sequence;expectedPid=$expected;observedPid=$observed;
            nodeIndex=$node;parentIndex=$parent;depth=$depth;relation=$relation;
            controlTypeId=$type;rootHwnd=$hwnd;nodeHwnd=$nodeHwnd}
    }
    function Read-UIA($element, $current, [long]$hwnd, $ownerProof) {
        $invoke = Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty)
        $selection = Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsSelectionItemPatternAvailableProperty)
        $expand = Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsExpandCollapsePatternAvailableProperty)
        $expandState='unknown'
        if ($expand -ceq $true) {
            $state = Optional-Property $element ([Windows.Automation.ExpandCollapsePattern]::ExpandCollapseStateProperty)
            if ($state -is [Windows.Automation.ExpandCollapseState]) { $expandState=[int]$state }
        }
        $rangeAvailable = Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsRangeValuePatternAvailableProperty)
        $range='unknown'
        if ($rangeAvailable -ceq $true -and $current.ControlType -eq [Windows.Automation.ControlType]::ProgressBar) {
            $value = Optional-Property $element ([Windows.Automation.RangeValuePattern]::ValueProperty)
            $minimum = Optional-Property $element ([Windows.Automation.RangeValuePattern]::MinimumProperty)
            $maximum = Optional-Property $element ([Windows.Automation.RangeValuePattern]::MaximumProperty)
            $numbers = @($value,$minimum,$maximum)
            if (@($numbers | Where-Object { $_ -isnot [double] -or [double]::IsNaN($_) -or [double]::IsInfinity($_) }).Count -eq 0 -and
                $minimum -lt $maximum -and $value -ge $minimum -and $value -le $maximum) {
                $range=@{value=$value; minimum=$minimum; maximum=$maximum}
            }
        }
        $selected='unknown'; $container='unknown'
        if ($selection -ceq $true) {
            $selected = Optional-Boolean $element ([Windows.Automation.SelectionItemPattern]::IsSelectedProperty)
            $owner = Optional-Property $element ([Windows.Automation.SelectionItemPattern]::SelectionContainerProperty)
            $container = Element-Reference $owner $current.ProcessId
        }
        return @{runtimeId=(Runtime-Identity $element); ownerPid=$current.ProcessId; ownerHwnd=$hwnd; ownerIdentity=$ownerProof;
            invokePatternAvailable=$invoke; selectionItemPatternAvailable=$selection; selected=$selected;
            expandCollapsePatternAvailable=$expand; expandCollapseState=$expandState;
            valuePatternAvailable=(Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsValuePatternAvailableProperty));
            textPatternAvailable=(Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsTextPatternAvailableProperty));
            rangeValuePatternAvailable=$rangeAvailable; rangeValue=$range;
            selectionContainer=$container;
            frameworkId=(Optional-Text $element ([Windows.Automation.AutomationElement]::FrameworkIdProperty));
            className=(Optional-Text $element ([Windows.Automation.AutomationElement]::ClassNameProperty));
            isControlElement=(Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsControlElementProperty));
            isContentElement=(Optional-Boolean $element ([Windows.Automation.AutomationElement]::IsContentElementProperty));
            occlusion='unknown';
            occlusionReason='IsOffscreen and PrintWindow do not establish desktop occlusion'}
    }
    function Take-NodeBudget($budget, $node) {
        $size = [Text.Encoding]::UTF8.GetByteCount(($node | ConvertTo-Json -Depth 8 -Compress)) + 1
        if ($budget.used + $size -gt $budget.limit) { return $false }
        $budget.used += $size; return $true
    }
    $windows = @(); $shots = @(); $truncated = $false; $nodeTotal = 0
    $nodeBudget = @{used=0; limit=196608}
    foreach ($hwnd in [HYPReadOnly]::Windows([int[]]@($processes | ForEach-Object { $_.pid }))) {
        Limit-Time; $null = No-Games 'before-ax'; $inventoryBefore = $inventoryChecks.Count-1; Install-Association
        $number = [HYPReadOnly]::Owner($hwnd)
        $identity = @($processes | Where-Object { $_.pid -eq $number })[0]
        Same-Identity $identity
        Begin-OwnerFamily $identity $hwnd
        $element = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
        if ($null -eq $element) {
            $script:ownerDiagnostic=Owner-Diagnostic $null $number -1 $hwnd 0 -1 0 'observation' 0
            throw 'zzz_observation_ax_owner_mismatch'
        }
        $rootCurrent=$element.Current; $rootPid=$rootCurrent.ProcessId
        if ($rootPid -ne $number) {
            $script:ownerDiagnostic=Owner-Diagnostic $rootCurrent $number $rootPid $hwnd 0 -1 0 'observation' 0
            throw 'zzz_observation_ax_owner_mismatch'
        }
        $queue = [Collections.Generic.Queue[object]]::new()
        $axStartedAt = [DateTime]::UtcNow.ToString('o'); $axStartedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
        $queue.Enqueue(@{element=$element; depth=0; parent=-1})
        $nodes = @(); $windowTruncated = $false; $truncationReasons = [Collections.Generic.HashSet[string]]::new()
        $walker = [Windows.Automation.TreeWalker]::RawViewWalker
        while ($queue.Count -gt 0) {
            Limit-Time
            if ($nodeTotal -ge 256) { $windowTruncated=$true; $null=$truncationReasons.Add('node-count'); break }
            $item = $queue.Dequeue(); $current = $item.element.Current
            $observedPid=$current.ProcessId
            try { $ownerProof=Node-OwnerProof $item.element $current ($item.parent -eq -1) } catch {
                $script:ownerDiagnostic=Owner-Diagnostic $current $number $observedPid $hwnd $nodes.Count $item.parent $item.depth 'observation' 0
                throw 'zzz_observation_ax_owner_mismatch'
            }
            $index = $nodes.Count; $box = $current.BoundingRectangle
            $private = $current.IsPassword -or $current.ControlType -eq [Windows.Automation.ControlType]::Edit
            $document = $null
            if (-not $private -and $current.ControlType -eq [Windows.Automation.ControlType]::Document) {
                $documentValue = Optional-Property $item.element ([Windows.Automation.ValuePattern]::ValueProperty)
                if ($documentValue -isnot [string]) { $documentValue='' }
                $text=''; $textAvailable=$false; $pattern=$null
                $document = Project-Document $current.Name $documentValue '' $false
                if ($null -ne $document.location -and $document.location.origin -ceq 'https://hyp-api.mihoyo.com' -and
                    $document.location.route -ceq '/nap_cn') {
                    try {
                        if ($item.element.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern,[ref]$pattern)) {
                            $text=$pattern.DocumentRange.GetText(4097); $textAvailable=$true
                        }
                    } catch { $text=''; $textAvailable=$false }
                    $document = Project-Document $current.Name $documentValue $text $textAvailable
                }
            }
            $label = ''
            if (-not $private) {
                $label = if ($null -ne $document -and $document.nameRedacted) { $document.nameLabel } else { Short-Text $current.Name }
            }
            if ((-not $private -and -not ($null -ne $document -and $document.nameRedacted) -and $current.Name.Length -gt 128) -or $current.AutomationId.Length -gt 128) {
                $windowTruncated=$true; $null=$truncationReasons.Add('text-length')
            }
            $node = @{index=$index; parent=$item.parent; depth=$item.depth; name=$label;
                automationId=(Short-Text $current.AutomationId); controlType=$current.ControlType.ProgrammaticName;
                enabled=$current.IsEnabled; offscreen=$current.IsOffscreen; password=$current.IsPassword;
                textOmitted=$private; bounds=@($box.X,$box.Y,$box.Width,$box.Height);
                uia=(Read-UIA $item.element $current $hwnd $ownerProof)}
            if ($null -ne $document -and $null -ne $document.location) {
                $node.documentLocation = $document.location
                $node.documentText = $document.text
                if ($document.nameRedacted) { $node.nameRedaction = 'document-url-query-omitted' }
                if ($document.truncated) { $windowTruncated=$true; $null=$truncationReasons.Add('document-projection-length') }
            }
            if (-not (Take-NodeBudget $nodeBudget $node)) {
                $windowTruncated=$true; $null=$truncationReasons.Add('node-byte-budget'); break
            }
            $nodes += $node
            $nodeTotal++
            if ($item.depth -ge 12) { $windowTruncated=$true; $null=$truncationReasons.Add('depth'); continue }
            $child = $walker.GetFirstChild($item.element)
            while ($null -ne $child) {
                Limit-Time
                if ($nodeTotal + $queue.Count -ge 256) { $windowTruncated=$true; $null=$truncationReasons.Add('node-count'); break }
                $queue.Enqueue(@{element=$child; depth=$item.depth+1; parent=$index})
                $child = $walker.GetNextSibling($child)
            }
        }
        $axCompletedAt = [DateTime]::UtcNow.ToString('o'); $axCompletedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
        $null = No-Games 'before-capture'; $inventoryAfter = $inventoryChecks.Count-1; Install-Association; Same-Identity $identity
        Recheck-OwnerFamily
        if ([HYPReadOnly]::Owner($hwnd) -ne $number) { throw 'zzz_observation_window_changed' }
        $captured = [DateTime]::UtcNow.ToString('o'); $name = 'window-' + $shots.Count + '.png'
        $captureStartedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
        $dimensions = [HYPReadOnly]::Capture($hwnd,$number,(Join-Path $output $name))
        $captureCompletedAt = [DateTime]::UtcNow.ToString('o'); $captureCompletedTicks = [Diagnostics.Stopwatch]::GetTimestamp()
        Same-Identity $identity
        Recheck-OwnerFamily
        $shots += @{path=$name; pid=$number; createdAt=$identity.createdAt; hwnd=$hwnd; sampleId=$sampleId;
            acquisition=@{startedAt=$captured; completedAt=$captureCompletedAt; startedTicks=$captureStartedTicks; completedTicks=$captureCompletedTicks};
            capturedAt=$captured; captureCompletedAt=$captureCompletedAt; captureTimeSemantics='printwindow-call-start';
            width=$dimensions[0]; height=$dimensions[1]; method='PrintWindow'; watermarked=$false}
        $windows += @{pid=$number; createdAt=$identity.createdAt; hwnd=$hwnd; ownerProcess=$identity; nodes=@($nodes); truncated=$windowTruncated;
            truncationReasons=@($truncationReasons | Sort-Object);
            sampleId=$sampleId; inventoryBefore=$inventoryBefore; inventoryAfter=$inventoryAfter;
            acquisition=@{startedAt=$axStartedAt; completedAt=$axCompletedAt; startedTicks=$axStartedTicks; completedTicks=$axCompletedTicks};
            contentAssociation=@{status='unknown'; gameBiz='unknown'; channel='unknown'; installPath='unknown'}}
        if ($windowTruncated) { $truncated=$true }
        Close-OwnerFamily
    }
    if ($windows.Count -eq 0) { throw 'zzz_observation_no_visible_window' }
    $null = No-Games 'final'; Install-Association
    foreach ($p in $processes) { Same-Identity $p }
    $ax = @{schemaVersion=2; selectedGameVerified=$false; actions=@(); qualification=$false; productionUpdateAllowed=$false;
        acquisition=@{sampleId=$sampleId; workerPid=$PID; clock='System.Diagnostics.Stopwatch.GetTimestamp';
            clockFrequency=[Diagnostics.Stopwatch]::Frequency; startedAt=$sampleStartedAt; startedTicks=$sampleStartedTicks;
            completedAt=[DateTime]::UtcNow.ToString('o'); completedTicks=[Diagnostics.Stopwatch]::GetTimestamp(); atomicSnapshot=$false};
        nodeByteBudget=$nodeBudget.limit; nodeByteCount=$nodeBudget.used;
        installationAssociation=$installationAssociation; inventoryChecks=@($inventoryChecks.ToArray()); windows=@($windows)} | ConvertTo-Json -Depth 16 -Compress
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($ax)
    if ($bytes.Length -gt 262144) { throw 'zzz_observation_ax_limit' }
    $file = [IO.FileStream]::new((Join-Path $output 'ax.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
    try { $file.Write($bytes,0,$bytes.Length) } finally { $file.Dispose() }
    @{ok=$true; processes=@($processes); axPath='ax.json'; screenshots=@($shots); truncated=$truncated} | ConvertTo-Json -Depth 12 -Compress
    exit 0
} catch {
    $code = $_.Exception.Message
    if ($code -notmatch '^zzz_observation_[a-z_]{1,80}$') { $code = 'zzz_observation_native_failed' }
    $failure=@{ok=$false; reasonCode=$code}
    if($code -ceq 'zzz_observation_ax_owner_mismatch' -and $null -ne $script:ownerDiagnostic) {
        $failure.ownerDiagnostic=$script:ownerDiagnostic
    }
    $failure | ConvertTo-Json -Depth 3 -Compress
    exit 2
} finally {
    if (Get-Command Close-OwnerFamily -ErrorAction SilentlyContinue) { Close-OwnerFamily }
}
'''
