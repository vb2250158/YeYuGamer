"""Safe local Manager process startup.

Stopping and restarting are always requested through Manager HTTP endpoints.
This module intentionally has no force-kill path.
"""

from __future__ import annotations

import json
import errno
import os
import socket
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from .api_client import ManagerApiClient, ManagerApiError
from .config import MANAGER_MODULE, PlatformConfig
from .lifecycle_lock import InstallLifecycleLock


CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
PYTHON_ENVIRONMENT_VARIABLES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONINSPECT",
)
TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE = "YEYU_GAMER_TRAY_BOOTSTRAP_SECRET"
MANAGER_INSTANCE_ENVIRONMENT_VARIABLE = "YEYU_GAMER_MANAGER_INSTANCE_ID"
MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE = "YEYU_GAMER_MANAGER_PID_RECORD"
PID_RECORD_SCHEMA_VERSION = 2
MAX_PID_RECORD_BYTES = 8_192
MANAGER_PID_RECORD_MUTEX_NAME = r"Local\YeYuGamer.ManagerPidRecord.v1"


def windows_creation_flags() -> int:
    return CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _windowless_python_executable(executable: str) -> str:
    """Use the sibling pythonw launcher for Windows-resident Python processes."""

    if os.name != "nt":
        return executable
    path = Path(executable)
    if path.name.casefold() == "pythonw.exe":
        return str(path)
    if path.name.casefold() != "python.exe":
        return executable
    candidate = path.with_name("pythonw.exe")
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Windowless Python executable does not exist: {candidate}"
        )
    return str(candidate)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(_absolute_path(path)))


def _is_reparse_point(path: Path) -> bool:
    stat_result = os.lstat(path)
    if os.name == "nt":
        return bool(getattr(stat_result, "st_file_attributes", 0) & 0x0400)
    return path.is_symlink()


def _assert_no_reparse_ancestors(path: Path, *, purpose: str) -> Path:
    resolved = _absolute_path(path)
    existing = resolved
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    for candidate in (existing, *existing.parents):
        if candidate.exists() and _is_reparse_point(candidate):
            raise ValueError(f"{purpose} has a reparse-point ancestor: {candidate}")
    return resolved


@dataclass(frozen=True, slots=True)
class ManagerLaunchSpec:
    """Explicit process seam used by tests; production derives a fixed layout."""

    host_executable: Path
    manager_executable: Path
    working_directory: Path


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        raise RuntimeError("recorded Manager PID is not a positive integer")
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            error_code = int(kernel32.GetLastError())
            if error_code == 87:  # ERROR_INVALID_PARAMETER: PID does not exist.
                return False
            raise RuntimeError(
                f"recorded Manager PID {pid} could not be queried (Win32 {error_code})"
            )
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError(
            f"recorded Manager PID {pid} could not be queried"
        ) from error
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise RuntimeError(
            f"recorded Manager PID {pid} query failed"
        ) from error
    return True


def _process_creation_identity(pid: int) -> str | None:
    """Return an OS process-birth identity, not merely a reusable PID."""

    if pid <= 0:
        raise RuntimeError("recorded Manager PID is not a positive integer")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = (
                ("low", wintypes.DWORD),
                ("high", wintypes.DWORD),
            )

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == 87:  # ERROR_INVALID_PARAMETER
                return None
            raise RuntimeError(
                f"recorded Manager PID {pid} identity could not be queried "
                f"(Win32 {error_code})"
            )
        try:
            created = FILETIME()
            exited = FILETIME()
            kernel = FILETIME()
            user = FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raise RuntimeError(
                    f"recorded Manager PID {pid} creation time could not be queried "
                    f"(Win32 {ctypes.get_last_error()})"
                )
            value = (int(created.high) << 32) | int(created.low)
            # Win32_Process.CreationDate exposes microsecond precision.  Keep
            # the Python/PowerShell lifecycle readers on the same canonical
            # 10 * 100 ns boundary so an exact comparison does not misclassify
            # one process as PID reuse because of sub-microsecond truncation.
            value -= value % 10
            return f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        content = proc_stat.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(
            f"recorded Manager PID {pid} identity could not be queried"
        ) from error
    closing_parenthesis = content.rfind(")")
    fields = content[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 0 or len(fields) <= 19:
        raise RuntimeError(f"recorded Manager PID {pid} identity is malformed")
    return f"proc-startticks:{fields[19]}"


def _write_manager_pid_record(path: Path, record: dict[str, object]) -> None:
    payload = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    if len(payload) > MAX_PID_RECORD_BYTES:
        raise RuntimeError("Manager PID record exceeds its size bound")
    temporary = path.with_name(f".{path.name}.{record['instanceId']}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _manager_pid_record_mutation_lock(
    path: Path,
    *,
    owner: str,
    timeout_seconds: float = 5.0,
) -> Iterator[None]:
    """Serialize publication/cleanup with crash-released OS ownership.

    Windows named mutex abandonment grants ownership to the waiter; POSIX
    ``flock`` is released by the kernel when its process exits.  Callers always
    re-read and fence the canonical record after acquisition, so an abandoned
    owner never authorizes a mutation by itself.
    """

    del owner  # The canonical record, not mutable lock metadata, proves ownership.
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, MANAGER_PID_RECORD_MUTEX_NAME)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        wait_milliseconds = max(1, min(int(timeout_seconds * 1000), 0xFFFFFFFE))
        wait_result = int(kernel32.WaitForSingleObject(handle, wait_milliseconds))
        if wait_result not in {0x00000000, 0x00000080}:
            kernel32.CloseHandle(handle)
            if wait_result == 0x00000102:
                raise RuntimeError("Manager PID-record mutation lock timed out")
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        try:
            yield
        finally:
            try:
                if not kernel32.ReleaseMutex(handle):
                    raise OSError(ctypes.get_last_error(), "ReleaseMutex failed")
            finally:
                kernel32.CloseHandle(handle)
        return

    import fcntl

    lock_path = path.with_name(f".{path.name}.mutation.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Manager PID-record mutation lock timed out"
                    ) from error
                time.sleep(0.025)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def publish_manager_pid_record(path: Path, record: dict[str, object]) -> None:
    """Publish one host-owned PID record without replacing a live instance.

    The controller serializes ordinary launches, while this short publication
    lock also protects the record against a stale or independently invoked host.
    A dead or PID-reused record may be replaced; a malformed or still-live
    record always fails closed.
    """

    try:
        instance_id = str(record["instanceId"])
        uuid.UUID(instance_id)
        pid = int(record["pid"])
        process_creation_identity = str(record["processCreationIdentity"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Manager PID publication record is malformed") from error
    if (
        record.get("schemaVersion") != PID_RECORD_SCHEMA_VERSION
        or pid <= 0
        or not process_creation_identity
    ):
        raise ValueError("Manager PID publication record is malformed")
    if _process_creation_identity(pid) != process_creation_identity:
        raise RuntimeError(
            "Manager host cannot publish a non-live process creation identity"
        )

    with _manager_pid_record_mutation_lock(path, owner=instance_id):
        try:
            existing = _read_manager_pid_record(path)
        except FileNotFoundError:
            existing = None
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "existing Manager PID record cannot be trusted; refusing publication"
            ) from error

        if existing is not None:
            try:
                existing_pid = int(existing["pid"])
                if existing_pid <= 0:
                    raise ValueError("pid is not positive")
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "existing Manager PID record cannot be trusted; refusing publication"
                ) from error

            if existing.get("schemaVersion") != PID_RECORD_SCHEMA_VERSION:
                if _pid_is_running(existing_pid):
                    raise RuntimeError(
                        "a live legacy Manager PID record blocks host publication"
                    )
            else:
                try:
                    existing_instance_id = str(existing["instanceId"])
                    uuid.UUID(existing_instance_id)
                    existing_identity = str(existing["processCreationIdentity"])
                    if not existing_identity:
                        raise ValueError("process identity is empty")
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        "existing Manager PID record cannot be trusted; refusing publication"
                    ) from error
                actual_identity = _process_creation_identity(existing_pid)
                if actual_identity == existing_identity:
                    if (
                        existing_instance_id == instance_id
                        and existing_pid == pid
                        and existing_identity == process_creation_identity
                    ):
                        return
                    raise RuntimeError(
                        "another live Manager host owns the PID record"
                    )

        _write_manager_pid_record(path, record)
        observed = _read_manager_pid_record(path)
        if any(
            observed.get(field) != record.get(field)
            for field in (
                "schemaVersion",
                "instanceId",
                "pid",
                "processCreationIdentity",
            )
        ):
            raise RuntimeError("Manager host PID record changed during publication")


def _wait_for_manager_pid_record_publication(
    path: Path,
    *,
    instance_id: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """Wait until the exact launched host publishes a live OS identity."""

    uuid.UUID(instance_id)
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            record = _read_manager_pid_record(path)
            if record.get("schemaVersion") != PID_RECORD_SCHEMA_VERSION:
                raise ValueError("Manager PID record schema is not current")
            if record.get("instanceId") != instance_id:
                raise ValueError("Manager PID record belongs to another instance")
            pid = int(record["pid"])
            identity = str(record["processCreationIdentity"])
            if pid <= 0 or not identity:
                raise ValueError("Manager PID record process identity is malformed")
            actual_identity = _process_creation_identity(pid)
            if actual_identity != identity:
                raise ValueError("Manager PID record process identity is not live")
            return record
        except (
            FileNotFoundError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
        time.sleep(0.025)
    raise RuntimeError(
        "Manager host did not publish its exact process identity before startup"
    ) from last_error


def _read_manager_pid_record(path: Path) -> dict[str, object]:
    result = os.lstat(path)
    if (
        not stat.S_ISREG(result.st_mode)
        or _is_reparse_point(path)
        or result.st_size <= 0
        or result.st_size > MAX_PID_RECORD_BYTES
    ):
        raise ValueError("Manager PID record is not a trusted bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(descriptor)
        if (result.st_dev, result.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Manager PID record changed while opening")
        payload = os.read(descriptor, MAX_PID_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_PID_RECORD_BYTES:
        raise ValueError("Manager PID record exceeds its size bound")
    value = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Manager PID record root is not an object")
    return value


def remove_manager_pid_record_if_owned(
    path: Path,
    *,
    instance_id: str,
    pid: int,
    process_creation_identity: str,
) -> bool:
    """Delete only this host's record without ever hiding a newer record."""

    with _manager_pid_record_mutation_lock(path, owner=instance_id):
        try:
            record = _read_manager_pid_record(path)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        owned = (
            isinstance(record, dict)
            and record.get("instanceId") == instance_id
            and record.get("pid") == pid
            and record.get("processCreationIdentity") == process_creation_identity
        )
        if owned:
            path.unlink()
            return True
        return False


@dataclass(frozen=True, slots=True)
class ManagerStartResult:
    already_running: bool
    healthy: bool
    pid: int | None
    message: str


@contextmanager
def _launch_lock(path: Path, stale_after_seconds: float = 60.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age <= stale_after_seconds:
            raise RuntimeError("another Manager launch is already in progress")
        path.unlink(missing_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


class LocalManagerController:
    def __init__(
        self,
        config: PlatformConfig,
        *,
        actor: str,
        tray_bootstrap_secret: str | None = None,
        _test_launch_spec: ManagerLaunchSpec | None = None,
    ) -> None:
        if tray_bootstrap_secret is not None and actor != "tray-lifecycle":
            raise PermissionError(
                "only the scoped tray lifecycle controller may pair a bootstrap credential"
            )
        self.config = config
        self.client = ManagerApiClient(config, actor=actor)
        self.health_client = ManagerApiClient(config, actor="health-probe")
        self.bootstrap_client = (
            ManagerApiClient(
                config,
                actor="tray",
                actor_token_override=tray_bootstrap_secret,
            )
            if tray_bootstrap_secret is not None
            else None
        )
        self._tray_bootstrap_secret = tray_bootstrap_secret
        self._test_launch_spec = _test_launch_spec

    @classmethod
    def for_test(
        cls,
        config: PlatformConfig,
        *,
        actor: str,
        launch_spec: ManagerLaunchSpec,
        tray_bootstrap_secret: str | None = None,
    ) -> "LocalManagerController":
        """Construct a controller with an explicit test-only process seam."""

        return cls(
            config,
            actor=actor,
            tray_bootstrap_secret=tray_bootstrap_secret,
            _test_launch_spec=launch_spec,
        )

    def is_healthy(self) -> bool:
        try:
            return self.health_client.health().ok
        except ManagerApiError:
            return False

    def bootstrap_identity_is_ready(self) -> bool:
        if self.bootstrap_client is None:
            return False
        try:
            return self.bootstrap_client.health().ok
        except ManagerApiError:
            return False

    def endpoint_port_is_open(self) -> bool:
        """Return whether the canonical listener port is unavailable for startup.

        Short exclusive binds to the canonical and wildcard addresses prove the
        closed case without emitting any TCP traffic. The wildcard check catches
        listeners that Windows permits an exact-address bind to overlap. Address
        conflicts and access-denied results both block startup; every other
        socket failure is surfaced rather than guessed open or closed.
        """

        parsed = urlsplit(self.config.manager_base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8877
        bind_hosts = (host, "0.0.0.0") if host == "127.0.0.1" else (host,)
        for bind_host in bind_hosts:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    if os.name == "nt":
                        exclusive_option = getattr(
                            socket, "SO_EXCLUSIVEADDRUSE", None
                        )
                        if exclusive_option is None:
                            raise RuntimeError(
                                "Windows exclusive TCP bind support is unavailable; "
                                "refusing lifecycle start"
                            )
                        probe.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
                    else:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                    probe.bind((bind_host, port))
            except OSError as error:
                error_codes = {error.errno, getattr(error, "winerror", None)}
                if error_codes & {errno.EADDRINUSE, errno.EACCES, 10048, 10013}:
                    return True
                raise RuntimeError(
                    "canonical Manager TCP bind probe could not prove that the "
                    f"loopback port is available: {error}"
                ) from error
        return False

    def _validate_launch_spec(self) -> ManagerLaunchSpec:
        production_spec = ManagerLaunchSpec(
            host_executable=self.config.manager_executable,
            manager_executable=self.config.manager_executable,
            working_directory=self.config.manager_working_directory,
        )
        spec = self._test_launch_spec or production_spec
        host_executable = _assert_no_reparse_ancestors(
            spec.host_executable, purpose="Manager host executable"
        )
        manager_executable = _assert_no_reparse_ancestors(
            spec.manager_executable, purpose="Manager executable"
        )
        working_directory = _assert_no_reparse_ancestors(
            spec.working_directory, purpose="Manager working directory"
        )
        if not host_executable.is_file():
            raise FileNotFoundError(
                f"Manager host executable does not exist: {host_executable}"
            )
        if not manager_executable.is_file():
            raise FileNotFoundError(
                f"Manager executable does not exist: {manager_executable}"
            )
        if not working_directory.is_dir():
            raise FileNotFoundError(
                f"Manager working directory does not exist: {working_directory}"
            )

        if self._test_launch_spec is None:
            expected_executable = _absolute_path(self.config.manager_executable)
            expected_working_directory = _absolute_path(
                self.config.install_root / "app"
            )
            if (
                _path_key(host_executable) != _path_key(expected_executable)
                or _path_key(manager_executable) != _path_key(expected_executable)
                or _path_key(working_directory) != _path_key(expected_working_directory)
            ):
                raise ValueError("Manager launch paths escaped the fixed install layout")

        return ManagerLaunchSpec(
            host_executable=host_executable,
            manager_executable=manager_executable,
            working_directory=working_directory,
        )

    def start(self, *, wait: bool = True) -> ManagerStartResult:
        with InstallLifecycleLock(
            self.config.state_directory / "install-lifecycle.lock"
        ):
            return self._start_with_lifecycle_lock(wait=wait)

    def _start_with_lifecycle_lock(self, *, wait: bool) -> ManagerStartResult:
        if self.is_healthy():
            return ManagerStartResult(True, True, None, "Manager is already healthy")
        if self.recorded_process_is_running() or self.endpoint_port_is_open():
            raise RuntimeError(
                "Manager health is unavailable, but its recorded process or port is still "
                "active. Refusing to start a second instance; inspect the local logs."
            )

        self.config.log_directory.mkdir(parents=True, exist_ok=True)
        self.config.state_directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.config.state_directory / "manager-launch.lock"
        with _launch_lock(lock_path):
            if self.is_healthy():
                return ManagerStartResult(True, True, None, "Manager became healthy")
            if self.recorded_process_is_running() or self.endpoint_port_is_open():
                raise RuntimeError(
                    "Manager became active without passing health; refusing a duplicate start"
                )
            launch_spec = self._validate_launch_spec()
            manager_command = (
                str(launch_spec.manager_executable),
                "-I",
                "-B",
                "-m",
                MANAGER_MODULE,
            )
            log_path = self.config.log_directory / "manager.log"
            environment = os.environ.copy()
            for variable in PYTHON_ENVIRONMENT_VARIABLES:
                environment.pop(variable, None)
            # A caller-controlled environment must never turn CLI/Agent
            # manager-start into a bootstrap credential mint. Only the paired
            # tray controller may add this value back below.
            environment.pop(TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE, None)
            environment.update(
                {
                    "YEYU_GAMER_INSTALL_ROOT": str(self.config.install_root),
                    "YEYU_GAMER_RUNTIME_ROOT": str(self.config.runtime_root),
                    "YEYU_GAMER_MANAGER_BASE_URL": self.config.manager_base_url,
                    "YEYU_GAMER_DATA_DIR": str(self.config.runtime_root),
                    "YEYU_GAMER_WEB_DIST": str(self.config.web_dist),
                    "YEYU_GAMER_ACTOR_TOKENS_DIR": str(
                        self.config.actor_token_directory
                    ),
                    # This environment is assembled by the installed platform,
                    # not accepted from the caller. Promoted Adapters should be
                    # executable without a second developer-only switch.
                    "YEYU_GAMER_LEGACY_EXECUTION_ENABLED": "true",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUNBUFFERED": "1",
                }
            )
            parsed_base_url = urlsplit(self.config.manager_base_url)
            environment["YEYU_GAMER_HOST"] = parsed_base_url.hostname or "127.0.0.1"
            environment["YEYU_GAMER_PORT"] = str(parsed_base_url.port or 8877)
            if self.config.legacy_root is not None:
                environment["YEYU_GAMER_LEGACY_ROOT"] = str(self.config.legacy_root)
            if self._tray_bootstrap_secret is not None:
                environment[TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE] = self._tray_bootstrap_secret

            record_path = self.config.state_directory / "manager-process.json"
            instance_id = str(uuid.uuid4())
            environment[MANAGER_INSTANCE_ENVIRONMENT_VARIABLE] = instance_id
            environment[MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE] = str(record_path)
            with log_path.open("ab", buffering=0) as log_stream:
                host_command = (
                    str(launch_spec.host_executable),
                    "-I",
                    "-B",
                    "-m",
                    "yeyu_gamer_platform.manager_host",
                    "--restart-exit-code",
                    "75",
                )
                process = subprocess.Popen(
                    host_command,
                    cwd=launch_spec.working_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    close_fds=True,
                    creationflags=windows_creation_flags() if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            record = _wait_for_manager_pid_record_publication(
                record_path,
                instance_id=instance_id,
                timeout_seconds=self.config.startup_timeout_seconds,
            )
            host_pid = int(record["pid"])
            host_creation_identity = str(record["processCreationIdentity"])

        if not wait:
            return ManagerStartResult(False, False, host_pid, "Manager start submitted")

        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.is_healthy():
                return ManagerStartResult(False, True, host_pid, "Manager is healthy")
            if _process_creation_identity(host_pid) != host_creation_identity:
                return ManagerStartResult(
                    False,
                    False,
                    host_pid,
                    "Manager host exited before health check",
                )
            time.sleep(0.25)
        return ManagerStartResult(
            False,
            False,
            host_pid,
            "Manager did not become healthy before the startup timeout",
        )

    def ensure_bootstrap_ready(self) -> ManagerStartResult:
        """Pair this process-local tray credential with the running Manager.

        An already-running Manager may belong to an earlier tray process and
        therefore must not accept this secret. In that case only the scoped
        lifecycle identity requests a graceful stop; after the old host and
        loopback listener are gone, a new host inherits this secret in its
        environment. No bootstrap credential is placed in argv or on disk.
        """

        if self.bootstrap_client is None or self._tray_bootstrap_secret is None:
            raise RuntimeError("an in-memory tray bootstrap credential is required")
        for _ in range(2):
            if self.bootstrap_identity_is_ready():
                return ManagerStartResult(
                    True, True, None, "Manager recognizes this tray instance"
                )
            if self.is_healthy():
                self._request_safe_stop_for_bootstrap_rotation()
                self._wait_for_manager_exit()
            result = self.start(wait=True)
            if not result.healthy:
                raise RuntimeError(result.message)
            if self.bootstrap_identity_is_ready():
                return result
        raise RuntimeError(
            "Manager became healthy with a different tray bootstrap identity; "
            "the bounded safe restart recovery did not converge"
        )

    def _request_safe_stop_for_bootstrap_rotation(self) -> None:
        idempotency_key = f"tray-bootstrap-rotate-{uuid.uuid4()}"
        try:
            self.client.request_safe_stop(idempotency_key=idempotency_key)
        except ManagerApiError as error:
            if error.status_code not in {401, 403}:
                raise
            # One migration bridge for a Manager that predates the
            # tray-lifecycle identity. The retired credential can request only
            # this safe stop here; it is deleted as soon as the request is
            # accepted and is never used to mint a nonce.
            legacy_path = self.config.actor_token_directory / "tray.token"
            try:
                legacy_secret = legacy_path.read_text(encoding="ascii").strip()
            except OSError as legacy_error:
                raise RuntimeError(
                    "The running Manager does not recognize the scoped tray "
                    "lifecycle credential. Safely stop that Manager, then relaunch "
                    "the tray to complete bootstrap recovery."
                ) from legacy_error
            legacy_client = ManagerApiClient(
                self.config,
                actor="tray",
                actor_token_override=legacy_secret,
            )
            legacy_client.request_safe_stop(idempotency_key=idempotency_key)
            legacy_path.unlink(missing_ok=True)
        else:
            # A current Manager already removed this file during startup, but
            # purging a leftover copy here closes upgrades that created the new
            # lifecycle identity before the old file was cleaned.
            (self.config.actor_token_directory / "tray.token").unlink(
                missing_ok=True
            )

    def _wait_for_manager_exit(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if (
                not self.is_healthy()
                and not self.recorded_process_is_running()
                and not self.endpoint_port_is_open()
            ):
                return
            time.sleep(0.25)
        raise RuntimeError(
            "Manager accepted the safe stop but its host or loopback listener did "
            "not exit before the recovery timeout; WebGUI was not opened"
        )

    def recorded_process_is_running(self) -> bool:
        record_path = self.config.state_directory / "manager-process.json"
        try:
            record = _read_manager_pid_record(record_path)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise RuntimeError(
                "Manager PID record state cannot be queried; refusing lifecycle start"
            ) from error
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Manager PID record exists but cannot be trusted; refusing lifecycle start"
            ) from error
        try:
            pid = int(record["pid"])
            if pid <= 0:
                raise ValueError("pid is not positive")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Manager PID record exists but cannot be trusted; refusing lifecycle start"
            ) from error
        if record.get("schemaVersion") != PID_RECORD_SCHEMA_VERSION:
            # A dead legacy record is harmless and will be replaced.  A live
            # legacy PID cannot be distinguished from reuse, so fail closed.
            if not _pid_is_running(pid):
                return False
            raise RuntimeError(
                "legacy Manager PID record lacks process identity; refusing "
                "lifecycle start until the old host exits"
            )
        try:
            instance_id = str(record["instanceId"])
            uuid.UUID(instance_id)
            expected_identity = str(record["processCreationIdentity"])
            if not expected_identity:
                raise ValueError("process identity is empty")
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                "Manager PID record exists but cannot be trusted; refusing lifecycle start"
            ) from error
        actual_identity = _process_creation_identity(pid)
        if actual_identity is None:
            return False
        # A recycled PID belongs to another process and must not block startup.
        return actual_identity == expected_identity
