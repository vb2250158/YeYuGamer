"""Consoleless host that honors the Manager's explicit restart exit code."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MANAGER_MODULE
from .process_control import (
    PYTHON_ENVIRONMENT_VARIABLES,
    MANAGER_INSTANCE_ENVIRONMENT_VARIABLE,
    MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE,
    PID_RECORD_SCHEMA_VERSION,
    _process_creation_identity,
    _windowless_python_executable,
    publish_manager_pid_record,
    remove_manager_pid_record_if_owned,
    windows_creation_flags,
)


def run_manager(
    command: Sequence[str],
    *,
    restart_exit_code: int = 75,
    maximum_restarts: int = 3,
    restart_window_seconds: float = 60.0,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if not command:
        raise ValueError("Manager command is empty")
    child_environment = os.environ.copy()
    for variable in PYTHON_ENVIRONMENT_VARIABLES:
        child_environment.pop(variable, None)
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    restart_times: deque[float] = deque()
    while True:
        child = popen_factory(
            tuple(command),
            env=child_environment,
            stdin=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=windows_creation_flags() if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        exit_code = int(child.wait())
        if exit_code != restart_exit_code:
            return exit_code

        now = monotonic()
        while restart_times and now - restart_times[0] > restart_window_seconds:
            restart_times.popleft()
        restart_times.append(now)
        if len(restart_times) > maximum_restarts:
            # A genuine API restart happens once. Repeated 75 exits indicate a
            # faulty Manager build; stop instead of entering a tight loop.
            return 76
        sleep(0.25)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yeyu-gamer-manager-host")
    parser.add_argument("--restart-exit-code", type=int, default=75)
    return parser


def _pid_record_context_from_environment() -> tuple[Path, str, int, str] | None:
    record_text = os.environ.get(MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE)
    instance_id = os.environ.get(MANAGER_INSTANCE_ENVIRONMENT_VARIABLE)
    if record_text is None and instance_id is None:
        # Direct invocation remains useful for isolated module tests.  The
        # installed controller always supplies both values.
        return None
    if not record_text or not instance_id:
        raise ValueError("Manager PID ownership environment is incomplete")
    uuid.UUID(instance_id)
    runtime_root = os.environ.get("YEYU_GAMER_RUNTIME_ROOT")
    if not runtime_root:
        raise ValueError("Manager runtime root is unavailable")
    expected = Path(runtime_root) / "state" / "manager-process.json"
    record_path = Path(record_text)
    if os.path.normcase(os.path.abspath(record_path)) != os.path.normcase(
        os.path.abspath(expected)
    ):
        raise ValueError("Manager PID record escaped the fixed runtime state path")
    pid = os.getpid()
    identity = _process_creation_identity(pid)
    if identity is None:
        raise RuntimeError("Manager host process identity is unavailable")
    return record_path, instance_id, pid, identity


def _publish_owned_pid_record(
    context: tuple[Path, str, int, str],
    *,
    manager_command: Sequence[str],
) -> None:
    record_path, instance_id, pid, identity = context
    recorded_at = datetime.now(timezone.utc).isoformat()
    publish_manager_pid_record(
        record_path,
        {
            "schemaVersion": PID_RECORD_SCHEMA_VERSION,
            "pid": pid,
            "instanceId": instance_id,
            "processCreationIdentity": identity,
            "startedAt": recorded_at,
            "recordedAt": recorded_at,
            "hostExecutable": sys.executable,
            "managerExecutable": manager_command[0],
            "managerArgumentCount": len(manager_command) - 1,
            "workingDirectory": str(Path.cwd()),
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    command = (
        _windowless_python_executable(sys.executable),
        "-I",
        "-B",
        "-m",
        MANAGER_MODULE,
    )
    context: tuple[Path, str, int, str] | None = None
    published = False
    try:
        context = _pid_record_context_from_environment()
        if context is not None:
            _publish_owned_pid_record(context, manager_command=command)
            published = True
        return run_manager(command, restart_exit_code=arguments.restart_exit_code)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"YeYu Gamer Manager host: {error}", file=sys.stderr)
        return 3
    finally:
        if context is not None and published:
            record_path, instance_id, pid, identity = context
            remove_manager_pid_record_if_owned(
                record_path,
                instance_id=instance_id,
                pid=pid,
                process_creation_identity=identity,
            )


if __name__ == "__main__":
    raise SystemExit(main())
