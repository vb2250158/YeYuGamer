"""Restricted Android-emulator lifecycle bindings for Manager-owned game runs.

This module owns emulator instance identity and package lifecycle only.  It
does not run a daily workflow, click Android UI, or emit completion evidence.
All executable arguments come from the fixed LDPlayer CLI contract below;
callers cannot inject an arbitrary command or Android package.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import time


class EmulatorBindingError(RuntimeError):
    """A configured emulator instance or fixed game package is unavailable."""


@dataclass(frozen=True, slots=True)
class EmulatorGameProfile:
    game_id: str
    package_name: str
    excluded_package_names: tuple[str, ...] = ()


EMULATOR_GAME_PROFILES: dict[str, EmulatorGameProfile] = {
    "FGO": EmulatorGameProfile("FGO", "com.bilibili.fatego"),
    "BD2": EmulatorGameProfile("BD2", "com.neowizgames.game.browndust2"),
    "CZN": EmulatorGameProfile(
        "CZN",
        "com.tencent.czn",
        ("com.smilegate.chaoszero.stove.google",),
    ),
}


@dataclass(frozen=True, slots=True)
class LDPlayerBinding:
    """A concrete LDPlayer instance, separate from a Windows game EXE path."""

    game_id: str
    console_path: str
    adb_path: str
    instance_index: int
    adb_serial: str
    instance_name: str | None = None


@dataclass(frozen=True, slots=True)
class LDPlayerInstanceState:
    index: int
    name: str
    running: bool
    player_process_id: int | None
    virtual_machine_process_id: int | None


@dataclass(frozen=True, slots=True)
class EmulatorInspection:
    instance: LDPlayerInstanceState
    adb_online: bool
    package_installed: bool
    package_running: bool
    excluded_packages_installed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmulatorLaunchReceipt:
    game_id: str
    instance_index: int
    adb_serial: str
    package_name: str
    instance_started: bool
    package_started: bool

    def as_result(self) -> dict[str, object]:
        return {
            "provider": "ldplayer",
            "gameId": self.game_id,
            "instanceIndex": self.instance_index,
            "adbSerial": self.adb_serial,
            "packageName": self.package_name,
            "managerOwnedInstance": self.instance_started,
            "managerOwnedPackage": self.package_started,
        }


@dataclass(frozen=True, slots=True)
class EmulatorCloseReceipt:
    state: str
    package_close_requested: bool
    instance_close_requested: bool
    package_remaining: bool
    instance_remaining: bool

    def as_result(self) -> dict[str, object]:
        return {
            "state": self.state,
            "packageCloseRequested": self.package_close_requested,
            "instanceCloseRequested": self.instance_close_requested,
            "packageRemaining": self.package_remaining,
            "instanceRemaining": self.instance_remaining,
        }


class LDPlayerBindingService:
    """Start and close one allowlisted Android game on one LDPlayer instance."""

    START_TIMEOUT_SECONDS = 120.0
    CLOSE_TIMEOUT_SECONDS = 30.0
    POLL_INTERVAL_SECONDS = 1.0
    _ADB_SERIAL_PATTERN = re.compile(r"^(?:emulator-\d+|127\.0\.0\.1:\d+)$")

    def validate_configured_binding(self, binding: LDPlayerBinding) -> None:
        """Validate the fixed local executables and allowlisted GameId only."""

        self._validate(binding)

    def inspect(self, binding: LDPlayerBinding) -> EmulatorInspection:
        profile, console_path, adb_path = self._validate(binding)
        instances = self._list_instances(console_path)
        instance = instances.get(binding.instance_index)
        if instance is None:
            raise EmulatorBindingError(
                f"LDPlayer instance index was not found: {binding.instance_index}"
            )
        if binding.instance_name is not None and instance.name != binding.instance_name:
            raise EmulatorBindingError(
                "LDPlayer instance name does not match the configured index"
            )

        online_serials = self._list_online_adb_serials(adb_path)
        adb_online = binding.adb_serial in online_serials
        if not adb_online:
            return EmulatorInspection(instance, False, False, False, ())

        package_installed = self._package_installed(
            adb_path, binding.adb_serial, profile.package_name
        )
        package_running = package_installed and self._package_running(
            adb_path, binding.adb_serial, profile.package_name
        )
        excluded = tuple(
            package_name
            for package_name in profile.excluded_package_names
            if self._package_installed(adb_path, binding.adb_serial, package_name)
        )
        return EmulatorInspection(
            instance,
            True,
            package_installed,
            package_running,
            excluded,
        )

    def ensure_started(self, binding: LDPlayerBinding) -> EmulatorLaunchReceipt:
        profile, console_path, _adb_path = self._validate(binding)
        initial = self.inspect(binding)
        instance_started = not initial.instance.running
        package_started = not initial.package_running
        try:
            if instance_started:
                self._run_console(
                    console_path,
                    "launch",
                    "--index",
                    str(binding.instance_index),
                )
                initial = self._wait_until_ready(binding)

            if not initial.adb_online:
                initial = self._wait_until_ready(binding)
            if not initial.package_installed:
                raise EmulatorBindingError(
                    f"configured Android package is not installed: {profile.package_name}"
                )

            if package_started:
                self._run_console(
                    console_path,
                    "runapp",
                    "--index",
                    str(binding.instance_index),
                    "--packagename",
                    profile.package_name,
                )
                self._wait_for_package(binding, expected_running=True)
        except Exception:
            # Manager cannot own a receipt until this function returns.  Roll
            # back anything started inside the failed call so a missing package
            # or failed runapp cannot leak an emulator into the next game.
            self._best_effort_close(
                console_path,
                binding.instance_index,
                profile.package_name,
                package_started=package_started,
                instance_started=instance_started,
            )
            raise

        return EmulatorLaunchReceipt(
            binding.game_id,
            binding.instance_index,
            binding.adb_serial,
            profile.package_name,
            instance_started,
            package_started,
        )

    def close_started(
        self, binding: LDPlayerBinding, receipt: EmulatorLaunchReceipt
    ) -> EmulatorCloseReceipt:
        profile, console_path, _adb_path = self._validate(binding)
        self._validate_receipt(binding, profile, receipt)

        package_requested = receipt.package_started
        instance_requested = receipt.instance_started
        cleanup_error = False
        if package_requested:
            try:
                self._run_console(
                    console_path,
                    "killapp",
                    "--index",
                    str(binding.instance_index),
                    "--packagename",
                    profile.package_name,
                )
                self._wait_for_package(binding, expected_running=False)
            except EmulatorBindingError:
                cleanup_error = True

        if instance_requested:
            try:
                self._run_console(
                    console_path,
                    "quit",
                    "--index",
                    str(binding.instance_index),
                )
                self._wait_for_instance(binding, expected_running=False)
            except EmulatorBindingError:
                cleanup_error = True

        try:
            final = self.inspect(binding)
        except EmulatorBindingError:
            return EmulatorCloseReceipt(
                "close-failed",
                package_requested,
                instance_requested,
                package_requested,
                instance_requested,
            )
        package_remaining = receipt.package_started and final.package_running
        instance_remaining = receipt.instance_started and final.instance.running
        if cleanup_error or package_remaining or instance_remaining:
            state = "close-failed"
        elif not package_requested and not instance_requested:
            state = "preserved-preexisting"
        else:
            state = "closed"
        return EmulatorCloseReceipt(
            state,
            package_requested,
            instance_requested,
            package_remaining,
            instance_remaining,
        )

    def _best_effort_close(
        self,
        console_path: Path,
        instance_index: int,
        package_name: str,
        *,
        package_started: bool,
        instance_started: bool,
    ) -> None:
        if package_started:
            try:
                self._run_console(
                    console_path,
                    "killapp",
                    "--index",
                    str(instance_index),
                    "--packagename",
                    package_name,
                )
            except EmulatorBindingError:
                pass
        if instance_started:
            try:
                self._run_console(
                    console_path,
                    "quit",
                    "--index",
                    str(instance_index),
                )
            except EmulatorBindingError:
                pass

    def _wait_until_ready(self, binding: LDPlayerBinding) -> EmulatorInspection:
        deadline = time.monotonic() + self.START_TIMEOUT_SECONDS
        last: EmulatorInspection | None = None
        while time.monotonic() < deadline:
            last = self.inspect(binding)
            if last.instance.running and last.adb_online:
                return last
            time.sleep(self.POLL_INTERVAL_SECONDS)
        detail = "unknown" if last is None else (
            f"running={last.instance.running}, adbOnline={last.adb_online}"
        )
        raise EmulatorBindingError(
            f"LDPlayer instance did not become ready before timeout: {detail}"
        )

    def _wait_for_package(
        self, binding: LDPlayerBinding, *, expected_running: bool
    ) -> None:
        deadline = time.monotonic() + (
            self.START_TIMEOUT_SECONDS if expected_running else self.CLOSE_TIMEOUT_SECONDS
        )
        while time.monotonic() < deadline:
            state = self.inspect(binding)
            if state.package_running is expected_running:
                return
            time.sleep(self.POLL_INTERVAL_SECONDS)
        action = "start" if expected_running else "stop"
        raise EmulatorBindingError(
            f"configured Android package did not {action} before timeout"
        )

    def _wait_for_instance(
        self, binding: LDPlayerBinding, *, expected_running: bool
    ) -> None:
        deadline = time.monotonic() + self.CLOSE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = self.inspect(binding)
            if state.instance.running is expected_running:
                return
            time.sleep(self.POLL_INTERVAL_SECONDS)
        raise EmulatorBindingError("LDPlayer instance did not close before timeout")

    @classmethod
    def _validate(
        cls, binding: LDPlayerBinding
    ) -> tuple[EmulatorGameProfile, Path, Path]:
        if os.name != "nt":
            raise EmulatorBindingError("LDPlayer binding is available only on Windows")
        profile = EMULATOR_GAME_PROFILES.get(binding.game_id)
        if profile is None:
            raise EmulatorBindingError(
                f"game has no registered emulator profile: {binding.game_id}"
            )
        if binding.instance_index < 0:
            raise EmulatorBindingError("LDPlayer instance index must be non-negative")
        if not cls._ADB_SERIAL_PATTERN.fullmatch(binding.adb_serial):
            raise EmulatorBindingError("ADB serial is not an allowlisted local emulator address")
        if binding.instance_name is not None:
            if not binding.instance_name.strip() or any(
                character in binding.instance_name for character in ("\x00", "\r", "\n")
            ):
                raise EmulatorBindingError("LDPlayer instance name is invalid")

        console_path = cls._validate_executable(binding.console_path, "ldconsole.exe")
        adb_path = cls._validate_executable(binding.adb_path, "adb.exe")
        if console_path.parent != adb_path.parent:
            raise EmulatorBindingError(
                "LDPlayer console and adb must belong to the same installation"
            )
        return profile, console_path, adb_path

    @staticmethod
    def _validate_executable(value: str, expected_name: str) -> Path:
        executable = Path(value)
        if executable.name.casefold() != expected_name:
            raise EmulatorBindingError(f"binding must point to {expected_name}")
        if not executable.is_file():
            raise EmulatorBindingError(f"configured emulator executable was not found: {expected_name}")
        return executable

    @staticmethod
    def _validate_receipt(
        binding: LDPlayerBinding,
        profile: EmulatorGameProfile,
        receipt: EmulatorLaunchReceipt,
    ) -> None:
        if (
            receipt.game_id != binding.game_id
            or receipt.instance_index != binding.instance_index
            or receipt.adb_serial != binding.adb_serial
            or receipt.package_name != profile.package_name
        ):
            raise EmulatorBindingError("launch receipt does not belong to this binding")

    def _list_instances(self, console_path: Path) -> dict[int, LDPlayerInstanceState]:
        completed = self._run(
            [str(console_path), "list2"],
            encoding="gbk",
        )
        instances: dict[int, LDPlayerInstanceState] = {}
        for line in completed.stdout.splitlines():
            fields = [part.strip() for part in line.split(",")]
            if len(fields) < 7:
                continue
            try:
                index = int(fields[0])
                running = fields[4] == "1"
                player_pid = int(fields[5]) or None
                virtual_machine_pid = int(fields[6]) or None
            except ValueError:
                continue
            instances[index] = LDPlayerInstanceState(
                index,
                fields[1],
                running,
                player_pid,
                virtual_machine_pid,
            )
        return instances

    def _list_online_adb_serials(self, adb_path: Path) -> set[str]:
        completed = self._run([str(adb_path), "devices", "-l"], encoding="utf-8")
        online: set[str] = set()
        for line in completed.stdout.splitlines():
            match = re.match(r"^(\S+)\s+device(?:\s|$)", line.strip())
            if match:
                online.add(match.group(1))
        return online

    def _package_installed(self, adb_path: Path, serial: str, package: str) -> bool:
        completed = self._run(
            [str(adb_path), "-s", serial, "shell", "pm", "path", package],
            encoding="utf-8",
            check=False,
        )
        return completed.returncode == 0 and any(
            line.startswith("package:") for line in completed.stdout.splitlines()
        )

    def _package_running(self, adb_path: Path, serial: str, package: str) -> bool:
        completed = self._run(
            [str(adb_path), "-s", serial, "shell", "pidof", package],
            encoding="utf-8",
            check=False,
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())

    def _run_console(self, console_path: Path, *arguments: str) -> None:
        self._run([str(console_path), *arguments], encoding="gbk")

    @staticmethod
    def _run(
        command: list[str], *, encoding: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=15,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EmulatorBindingError(f"emulator command failed: {error}") from error
        if check and completed.returncode != 0:
            raise EmulatorBindingError(
                f"emulator command exited with code {completed.returncode}"
            )
        return completed
