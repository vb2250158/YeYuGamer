from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yeyu_gamer_platform.api_client import ManagerApiError
from yeyu_gamer_platform.config import PlatformConfig
from yeyu_gamer_platform.lifecycle_lock import InstallLifecycleLock
from yeyu_gamer_platform.process_control import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    LocalManagerController,
    ManagerLaunchSpec,
    ManagerStartResult,
    MANAGER_INSTANCE_ENVIRONMENT_VARIABLE,
    MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE,
    PID_RECORD_SCHEMA_VERSION,
    TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE,
    _process_creation_identity,
    _wait_for_manager_pid_record_publication,
    publish_manager_pid_record,
    remove_manager_pid_record_if_owned,
    _windowless_python_executable,
    windows_creation_flags,
)


class ProcessControlTests(unittest.TestCase):
    @staticmethod
    def _fixed_config(root: Path) -> PlatformConfig:
        return PlatformConfig(
            install_root=root / "install",
            runtime_root=root / "runtime",
            manager_base_url="http://127.0.0.1:8877/api/v1",
            web_url="http://127.0.0.1:8877/",
            legacy_root=None,
            web_dist=root / "web-dist",
            actor_token_file=None,
        )

    def test_windows_children_are_consoleless_and_grouped(self) -> None:
        flags = windows_creation_flags()
        self.assertEqual(flags & CREATE_NO_WINDOW, CREATE_NO_WINDOW)
        self.assertEqual(flags & CREATE_NEW_PROCESS_GROUP, CREATE_NEW_PROCESS_GROUP)
        if sys.platform == "win32":
            self.assertEqual(
                Path(_windowless_python_executable(sys.executable)).name.casefold(),
                "pythonw.exe",
            )
            identity = _process_creation_identity(os.getpid())
            self.assertIsNotNone(identity)
            self.assertEqual(int(identity.split(":", 1)[1]) % 10, 0)

    def test_refuses_duplicate_when_port_is_owned_but_health_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = PlatformConfig.for_test(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:8877/api/v1",
                web_url="http://127.0.0.1:8877/",
                legacy_root=None,
                web_dist=root / "web-dist",
                actor_token_file=None,
            )
            controller = LocalManagerController(config, actor="test")
            with (
                patch.object(controller, "is_healthy", return_value=False),
                patch.object(
                    controller, "recorded_process_is_running", return_value=False
                ),
                patch.object(controller, "endpoint_port_is_open", return_value=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "second instance"):
                    controller.start()

    def test_install_lock_blocks_manager_start_before_any_app_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("app", "app.incoming", "app.previous"):
                directory = root / "install" / name
                directory.mkdir(parents=True)
                (directory / "sentinel.txt").write_text(name, encoding="utf-8")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            config = self._fixed_config(root)
            controller = LocalManagerController(config, actor="test")
            install_lock = InstallLifecycleLock(
                config.state_directory / "install-lifecycle.lock"
            )
            with install_lock, concurrent.futures.ThreadPoolExecutor(
                max_workers=1
            ) as executor:
                future = executor.submit(controller.start, wait=False)
                with self.assertRaisesRegex(RuntimeError, "installation is in progress"):
                    future.result(timeout=5)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_existing_invalid_pid_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._fixed_config(root)
            config.state_directory.mkdir(parents=True)
            (config.state_directory / "manager-process.json").write_text(
                "{broken-json", encoding="utf-8"
            )
            controller = LocalManagerController(config, actor="test")
            with self.assertRaisesRegex(RuntimeError, "cannot be trusted"):
                controller.recorded_process_is_running()

    def test_existing_pid_record_query_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._fixed_config(root)
            config.state_directory.mkdir(parents=True)
            (config.state_directory / "manager-process.json").write_text(
                '{"pid": 1234}', encoding="utf-8"
            )
            controller = LocalManagerController(config, actor="test")
            with patch(
                "yeyu_gamer_platform.process_control._pid_is_running",
                side_effect=RuntimeError("simulated query failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated query failure"):
                    controller.recorded_process_is_running()

    def test_unknown_bind_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = LocalManagerController(
                self._fixed_config(Path(temporary)), actor="test"
            )
            with patch(
                "yeyu_gamer_platform.process_control.socket.socket",
                side_effect=OSError(12345, "simulated bind failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not prove"):
                    controller.endpoint_port_is_open()

    def test_access_denied_bind_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = LocalManagerController(
                self._fixed_config(Path(temporary)), actor="test"
            )
            with patch(
                "yeyu_gamer_platform.process_control.socket.socket",
                side_effect=PermissionError(13, "simulated access denial"),
            ):
                self.assertTrue(controller.endpoint_port_is_open())

    def test_free_loopback_port_is_known_closed_without_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reserve = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reserve.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reserve.bind(("127.0.0.1", 0))
            port = int(reserve.getsockname()[1])
            reserve.close()
            root = Path(temporary)
            controller = LocalManagerController(
                PlatformConfig.for_test(
                    install_root=root / "install",
                    runtime_root=root / "runtime",
                    manager_base_url=f"http://127.0.0.1:{port}/api/v1",
                    web_url=f"http://127.0.0.1:{port}/",
                    legacy_root=None,
                    web_dist=root / "web-dist",
                    actor_token_file=None,
                ),
                actor="test",
            )
            with patch(
                "yeyu_gamer_platform.process_control.socket.create_connection",
                side_effect=AssertionError("outbound connect must not be used"),
            ) as connect:
                self.assertFalse(controller.endpoint_port_is_open())
            connect.assert_not_called()

    def test_exact_loopback_listener_is_reported_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = int(listener.getsockname()[1])
                controller = LocalManagerController(
                    PlatformConfig.for_test(
                        install_root=root / "install",
                        runtime_root=root / "runtime",
                        manager_base_url=f"http://127.0.0.1:{port}/api/v1",
                        web_url=f"http://127.0.0.1:{port}/",
                        legacy_root=None,
                        web_dist=root / "web-dist",
                        actor_token_file=None,
                    ),
                    actor="test",
                )
                self.assertTrue(controller.endpoint_port_is_open())

    def test_wildcard_listener_blocks_canonical_loopback_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                # Windows permits an exact-address exclusive bind to overlap a
                # non-exclusive wildcard listener. The second wildcard probe
                # must still classify this fixed port as unavailable.
                listener.bind(("0.0.0.0", 0))
                listener.listen(1)
                port = int(listener.getsockname()[1])
                controller = LocalManagerController(
                    PlatformConfig.for_test(
                        install_root=root / "install",
                        runtime_root=root / "runtime",
                        manager_base_url=f"http://127.0.0.1:{port}/api/v1",
                        web_url=f"http://127.0.0.1:{port}/",
                        legacy_root=None,
                        web_dist=root / "web-dist",
                        actor_token_file=None,
                    ),
                    actor="test",
                )
                self.assertTrue(controller.endpoint_port_is_open())

    def test_manager_receives_protected_actor_token_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actor_tokens = root / "runtime" / "secrets" / "actors"
            working_directory = root / "install" / "app"
            working_directory.mkdir(parents=True)
            executable = Path(_windowless_python_executable(sys.executable))
            config = PlatformConfig.for_test(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:18877/api/v1",
                web_url="http://127.0.0.1:18877/",
                legacy_root=root / "runtime" / "import" / "legacy-config",
                web_dist=root / "web-dist",
                actor_token_file=actor_tokens,
            )
            ephemeral_secret = (
                "ephemeral-tray-bootstrap-secret-"
                "0123456789abcdefghijklmnopqrstuv"
            )
            controller = LocalManagerController.for_test(
                config,
                actor="tray-lifecycle",
                tray_bootstrap_secret=ephemeral_secret,
                launch_spec=ManagerLaunchSpec(
                    host_executable=executable,
                    manager_executable=executable,
                    working_directory=working_directory,
                ),
            )
            injected_python_environment = {
                "PYTHONPATH": "attacker-path",
                "PYTHONHOME": "attacker-home",
                "PYTHONSTARTUP": "attacker-startup",
                "PYTHONUSERBASE": "attacker-userbase",
                "PYTHONINSPECT": "1",
            }
            attacker_secret = (
                "attacker-selected-tray-bootstrap-secret-"
                "000000000000000000000000"
            )
            with patch.dict(
                os.environ,
                {
                    **injected_python_environment,
                    TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE: attacker_secret,
                },
            ):
                with (
                    patch.object(controller, "is_healthy", return_value=False),
                    patch.object(
                        controller, "recorded_process_is_running", return_value=False
                    ),
                    patch.object(controller, "endpoint_port_is_open", return_value=False),
                    patch(
                        "yeyu_gamer_platform.process_control.subprocess.Popen"
                    ) as popen,
                    patch(
                        "yeyu_gamer_platform.process_control._wait_for_manager_pid_record_publication",
                        side_effect=lambda path, *, instance_id, timeout_seconds: {
                            "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                            "pid": 4321,
                            "instanceId": instance_id,
                            "processCreationIdentity": "windows-filetime:4321",
                        },
                    ) as wait_for_publication,
                ):
                    popen.return_value.pid = 1234
                    result = controller.start(wait=False)
            self.assertEqual(result.pid, 4321)
            self.assertNotEqual(result.pid, popen.return_value.pid)
            host_command = popen.call_args.args[0]
            if sys.platform == "win32":
                self.assertEqual(Path(host_command[0]).name.casefold(), "pythonw.exe")
            self.assertEqual(
                host_command[1:5],
                ("-I", "-B", "-m", "yeyu_gamer_platform.manager_host"),
            )
            self.assertNotIn("--", host_command)
            self.assertEqual(
                popen.call_args.kwargs["cwd"],
                working_directory,
            )
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(
                environment["YEYU_GAMER_ACTOR_TOKENS_DIR"], str(actor_tokens)
            )
            self.assertEqual(
                environment[TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE],
                ephemeral_secret,
            )
            self.assertIn(MANAGER_INSTANCE_ENVIRONMENT_VARIABLE, environment)
            self.assertEqual(
                environment[MANAGER_PID_RECORD_ENVIRONMENT_VARIABLE],
                str(config.state_directory / "manager-process.json"),
            )
            wait_for_publication.assert_called_once()
            self.assertEqual(
                wait_for_publication.call_args.args,
                (config.state_directory / "manager-process.json",),
            )
            self.assertEqual(
                wait_for_publication.call_args.kwargs["instance_id"],
                environment[MANAGER_INSTANCE_ENVIRONMENT_VARIABLE],
            )
            self.assertEqual(
                wait_for_publication.call_args.kwargs["timeout_seconds"],
                config.startup_timeout_seconds,
            )
            self.assertNotIn(attacker_secret, environment.values())
            self.assertNotIn(ephemeral_secret, " ".join(host_command))
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            for variable in injected_python_environment:
                self.assertNotIn(variable, environment)
            persisted_bytes = b"".join(
                path.read_bytes()
                for path in config.runtime_root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(ephemeral_secret.encode("ascii"), persisted_bytes)
            self.assertNotIn(ephemeral_secret, repr(config))

            unpaired = LocalManagerController.for_test(
                config,
                actor="cli",
                launch_spec=ManagerLaunchSpec(
                    host_executable=executable,
                    manager_executable=executable,
                    working_directory=working_directory,
                ),
            )
            with patch.dict(
                os.environ,
                {TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE: attacker_secret},
            ):
                with (
                    patch.object(unpaired, "is_healthy", return_value=False),
                    patch.object(
                        unpaired, "recorded_process_is_running", return_value=False
                    ),
                    patch.object(
                        unpaired, "endpoint_port_is_open", return_value=False
                    ),
                    patch(
                        "yeyu_gamer_platform.process_control.subprocess.Popen"
                    ) as unpaired_popen,
                    patch(
                        "yeyu_gamer_platform.process_control._wait_for_manager_pid_record_publication",
                        side_effect=lambda path, *, instance_id, timeout_seconds: {
                            "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                            "pid": 5432,
                            "instanceId": instance_id,
                            "processCreationIdentity": "windows-filetime:5432",
                        },
                    ),
                ):
                    unpaired_popen.return_value.pid = 2345
                    unpaired.start(wait=False)
            self.assertNotIn(
                TRAY_BOOTSTRAP_ENVIRONMENT_VARIABLE,
                unpaired_popen.call_args.kwargs["env"],
            )

    def test_pid_reuse_does_not_match_v2_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._fixed_config(root)
            config.state_directory.mkdir(parents=True)
            instance_id = "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e"
            (config.state_directory / "manager-process.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                        "pid": 1234,
                        "instanceId": instance_id,
                        "processCreationIdentity": "windows-filetime:old",
                    }
                ),
                encoding="utf-8",
            )
            controller = LocalManagerController(config, actor="test")
            with patch(
                "yeyu_gamer_platform.process_control._process_creation_identity",
                return_value="windows-filetime:reused",
            ):
                self.assertFalse(controller.recorded_process_is_running())
            with patch(
                "yeyu_gamer_platform.process_control._process_creation_identity",
                return_value="windows-filetime:old",
            ):
                self.assertTrue(controller.recorded_process_is_running())

    def test_pid_record_cleanup_is_instance_and_creation_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 1234,
                "instanceId": "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
                "processCreationIdentity": "windows-filetime:123",
            }
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertFalse(
                remove_manager_pid_record_if_owned(
                    path,
                    instance_id=record["instanceId"],
                    pid=record["pid"],
                    process_creation_identity="windows-filetime:other",
                )
            )
            self.assertTrue(path.exists())
            self.assertTrue(
                remove_manager_pid_record_if_owned(
                    path,
                    instance_id=record["instanceId"],
                    pid=record["pid"],
                    process_creation_identity=record["processCreationIdentity"],
                )
            )
            self.assertFalse(path.exists())

    def test_delayed_old_host_cannot_overwrite_a_live_new_host_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            live_identity = _process_creation_identity(os.getpid())
            self.assertIsNotNone(live_identity)
            new_record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": os.getpid(),
                "instanceId": "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
                "processCreationIdentity": live_identity,
            }
            path.write_text(json.dumps(new_record), encoding="utf-8")
            old_record = {
                **new_record,
                "instanceId": "780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
            }
            with self.assertRaisesRegex(RuntimeError, "another live Manager host"):
                publish_manager_pid_record(path, old_record)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), new_record)

    def test_stale_proxy_record_can_be_replaced_by_the_live_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            stale = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 1234,
                "instanceId": "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
                "processCreationIdentity": "windows-filetime:1234",
            }
            current = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 4321,
                "instanceId": "780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
                "processCreationIdentity": "windows-filetime:4321",
            }
            path.write_text(json.dumps(stale), encoding="utf-8")
            with patch(
                "yeyu_gamer_platform.process_control._process_creation_identity",
                side_effect=lambda pid: {
                    1234: "windows-filetime:reused",
                    4321: "windows-filetime:4321",
                }[pid],
            ):
                publish_manager_pid_record(path, current)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), current)

    def test_old_cleanup_never_hides_a_newer_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            new_record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 4321,
                "instanceId": "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
                "processCreationIdentity": "windows-filetime:4321",
            }
            path.write_text(json.dumps(new_record), encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()
            from yeyu_gamer_platform import process_control as process_control_module

            original_read = process_control_module._read_manager_pid_record

            def delayed_read(record_path: Path) -> dict[str, object]:
                record = original_read(record_path)
                if threading.current_thread() is not threading.main_thread():
                    entered.set()
                    self.assertTrue(release.wait(timeout=5))
                return record

            with patch(
                "yeyu_gamer_platform.process_control._read_manager_pid_record",
                side_effect=delayed_read,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    cleanup = executor.submit(
                        remove_manager_pid_record_if_owned,
                        path,
                        instance_id="780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
                        pid=1234,
                        process_creation_identity="windows-filetime:1234",
                    )
                    self.assertTrue(entered.wait(timeout=5))
                    self.assertTrue(path.is_file())
                    self.assertEqual(
                        json.loads(path.read_text(encoding="utf-8")), new_record
                    )
                    release.set()
                    self.assertFalse(cleanup.result(timeout=5))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), new_record)

    def test_old_cleanup_and_new_publication_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            old_record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 1234,
                "instanceId": "3992d4ef-daf0-4ed0-8495-eb6bb5de4e0e",
                "processCreationIdentity": "windows-filetime:1234",
            }
            new_record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": 4321,
                "instanceId": "780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
                "processCreationIdentity": "windows-filetime:4321",
            }
            for _ in range(25):
                path.write_text(json.dumps(old_record), encoding="utf-8")
                barrier = threading.Barrier(2)

                def publish_new() -> None:
                    barrier.wait(timeout=5)
                    publish_manager_pid_record(path, new_record)

                def cleanup_old() -> bool:
                    barrier.wait(timeout=5)
                    return remove_manager_pid_record_if_owned(
                        path,
                        instance_id=old_record["instanceId"],
                        pid=old_record["pid"],
                        process_creation_identity=old_record[
                            "processCreationIdentity"
                        ],
                    )

                with patch(
                    "yeyu_gamer_platform.process_control._process_creation_identity",
                    side_effect=lambda pid: {
                        1234: None,
                        4321: "windows-filetime:4321",
                    }[pid],
                ):
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=2
                    ) as executor:
                        publish_future = executor.submit(publish_new)
                        cleanup_future = executor.submit(cleanup_old)
                        publish_future.result(timeout=5)
                        cleanup_future.result(timeout=5)
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")), new_record
                )

    def test_abandoned_record_mutation_owner_does_not_block_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "manager-process.json"
            ready_path = root / "ready"
            crash_script = root / "crash_with_lock.py"
            platform_root = Path(__file__).resolve().parents[1]
            crash_script.write_text(
                """import os
from pathlib import Path
import sys
import time
sys.path.insert(0, sys.argv[1])
from yeyu_gamer_platform.process_control import _manager_pid_record_mutation_lock
record_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
with _manager_pid_record_mutation_lock(record_path, owner='crash-fixture'):
    ready_path.write_text('ready', encoding='ascii')
    time.sleep(0.35)
    os._exit(0)
""",
                encoding="utf-8",
            )
            executable = _windowless_python_executable(sys.executable)
            crash_owner = subprocess.Popen(
                (
                    executable,
                    "-I",
                    "-B",
                    str(crash_script),
                    str(platform_root),
                    str(path),
                    str(ready_path),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                creationflags=windows_creation_flags() if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + 5.0
            while not ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue(ready_path.is_file())
            identity = _process_creation_identity(os.getpid())
            self.assertIsNotNone(identity)
            record = {
                "schemaVersion": PID_RECORD_SCHEMA_VERSION,
                "pid": os.getpid(),
                "instanceId": "780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
                "processCreationIdentity": identity,
            }
            publish_manager_pid_record(path, record)
            crash_owner.wait(timeout=5)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
            self.assertTrue(
                remove_manager_pid_record_if_owned(
                    path,
                    instance_id=record["instanceId"],
                    pid=record["pid"],
                    process_creation_identity=record["processCreationIdentity"],
                )
            )

    def test_controller_pid_record_publication_wait_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manager-process.json"
            with self.assertRaisesRegex(RuntimeError, "did not publish"):
                _wait_for_manager_pid_record_publication(
                    path,
                    instance_id="780b89a9-9567-4769-a7ee-8bc9d3e9b5fd",
                    timeout_seconds=0.05,
                )

    def test_controller_waits_through_proxy_exit_for_real_host_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime_root = root / "runtime"
            state_root = runtime_root / "state"
            state_root.mkdir(parents=True)
            record_path = state_root / "manager-process.json"
            child_script = root / "controlled_host.py"
            proxy_script = root / "controlled_proxy.py"
            child_pid_path = root / "child.pid"
            stop_path = root / "stop"
            done_path = root / "done"
            platform_root = Path(__file__).resolve().parents[1]
            instance_id = "780b89a9-9567-4769-a7ee-8bc9d3e9b5fd"
            child_script.write_text(
                """from pathlib import Path
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from yeyu_gamer_platform.manager_host import _pid_record_context_from_environment, _publish_owned_pid_record
from yeyu_gamer_platform.process_control import remove_manager_pid_record_if_owned
runtime_root = Path(sys.argv[2])
instance_id = sys.argv[3]
pid_path = Path(sys.argv[4])
stop_path = Path(sys.argv[5])
done_path = Path(sys.argv[6])
record_path = runtime_root / 'state' / 'manager-process.json'
os.environ['YEYU_GAMER_RUNTIME_ROOT'] = str(runtime_root)
os.environ['YEYU_GAMER_MANAGER_PID_RECORD'] = str(record_path)
os.environ['YEYU_GAMER_MANAGER_INSTANCE_ID'] = instance_id
context = _pid_record_context_from_environment()
published = False
try:
    _publish_owned_pid_record(context, manager_command=(sys.executable, '-m', 'controlled-manager'))
    published = True
    pid_path.write_text(str(os.getpid()), encoding='ascii')
    deadline = time.monotonic() + 10.0
    while not stop_path.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
finally:
    if published:
        remove_manager_pid_record_if_owned(
            context[0], instance_id=context[1], pid=context[2],
            process_creation_identity=context[3]
        )
    done_path.write_text('done', encoding='ascii')
""",
                encoding="utf-8",
            )
            proxy_script.write_text(
                """import os
import subprocess
import sys
flags = 0x08000200 if os.name == 'nt' else 0
subprocess.Popen(
    [sys.executable, '-I', '-B', *sys.argv[1:]],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
    start_new_session=os.name != 'nt'
)
""",
                encoding="utf-8",
            )
            executable = _windowless_python_executable(sys.executable)
            proxy = subprocess.Popen(
                (
                    executable,
                    "-I",
                    "-B",
                    str(proxy_script),
                    str(child_script),
                    str(platform_root),
                    str(runtime_root),
                    instance_id,
                    str(child_pid_path),
                    str(stop_path),
                    str(done_path),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                creationflags=windows_creation_flags() if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            try:
                proxy.wait(timeout=5)
                record = _wait_for_manager_pid_record_publication(
                    record_path,
                    instance_id=instance_id,
                    timeout_seconds=5.0,
                )
                child_pid: int | None = None
                pid_deadline = time.monotonic() + 5.0
                while child_pid is None and time.monotonic() < pid_deadline:
                    try:
                        child_pid = int(
                            child_pid_path.read_text(encoding="ascii").strip()
                        )
                    except (FileNotFoundError, ValueError):
                        time.sleep(0.025)
                if child_pid is None:
                    self.fail("controlled host did not publish a parseable PID")
                self.assertEqual(record["pid"], child_pid)
                self.assertNotEqual(proxy.pid, child_pid)
                self.assertEqual(
                    _process_creation_identity(child_pid),
                    record["processCreationIdentity"],
                )
            finally:
                stop_path.touch()
                deadline = time.monotonic() + 5.0
                while not done_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.025)
            self.assertTrue(done_path.is_file())
            self.assertFalse(record_path.exists())

    def test_unknown_running_manager_is_safely_restarted_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = "ephemeral-tray-bootstrap-secret-0123456789abcdefghijkl"
            config = PlatformConfig.for_test(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:18877/api/v1",
                web_url="http://127.0.0.1:18877/",
                legacy_root=None,
                web_dist=root / "web-dist",
                actor_token_file=root / "runtime" / "secrets" / "actors",
            )
            controller = LocalManagerController(
                config,
                actor="tray-lifecycle",
                tray_bootstrap_secret=secret,
            )
            started = ManagerStartResult(False, True, 1234, "Manager is healthy")
            with (
                patch.object(
                    controller,
                    "bootstrap_identity_is_ready",
                    side_effect=[False, True],
                ),
                patch.object(controller, "is_healthy", return_value=True),
                patch.object(
                    controller, "_request_safe_stop_for_bootstrap_rotation"
                ) as stop,
                patch.object(controller, "_wait_for_manager_exit") as wait,
                patch.object(controller, "start", return_value=started) as start,
            ):
                result = controller.ensure_bootstrap_ready()
            self.assertEqual(result, started)
            stop.assert_called_once_with()
            wait.assert_called_once_with()
            start.assert_called_once_with(wait=True)
            self.assertEqual(controller.client.actor, "tray-lifecycle")
            self.assertEqual(controller.bootstrap_client.actor, "tray")

    def test_known_running_manager_is_not_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = PlatformConfig.for_test(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:18877/api/v1",
                web_url="http://127.0.0.1:18877/",
                legacy_root=None,
                web_dist=root / "web-dist",
                actor_token_file=root / "runtime" / "secrets" / "actors",
            )
            controller = LocalManagerController(
                config,
                actor="tray-lifecycle",
                tray_bootstrap_secret=(
                    "ephemeral-tray-bootstrap-secret-0123456789abcdefghijkl"
                ),
            )
            with (
                patch.object(
                    controller, "bootstrap_identity_is_ready", return_value=True
                ),
                patch.object(controller, "start") as start,
            ):
                result = controller.ensure_bootstrap_ready()
            self.assertTrue(result.already_running)
            start.assert_not_called()

    def test_legacy_tray_token_is_used_only_for_safe_stop_then_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actor_tokens = root / "runtime" / "secrets" / "actors"
            actor_tokens.mkdir(parents=True)
            legacy_path = actor_tokens / "tray.token"
            legacy_secret = "retired-tray-bootstrap-token-0123456789abcdefghijkl"
            legacy_path.write_text(legacy_secret, encoding="ascii")
            config = PlatformConfig.for_test(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:18877/api/v1",
                web_url="http://127.0.0.1:18877/",
                legacy_root=None,
                web_dist=root / "web-dist",
                actor_token_file=actor_tokens,
            )
            controller = LocalManagerController(
                config,
                actor="tray-lifecycle",
                tray_bootstrap_secret=(
                    "ephemeral-tray-bootstrap-secret-0123456789abcdefghijkl"
                ),
            )
            controller.client.request_safe_stop = Mock(
                side_effect=ManagerApiError("unknown actor", status_code=401)
            )
            migration_client = Mock()
            with patch(
                "yeyu_gamer_platform.process_control.ManagerApiClient",
                return_value=migration_client,
            ) as client_type:
                controller._request_safe_stop_for_bootstrap_rotation()
            client_type.assert_called_once_with(
                config,
                actor="tray",
                actor_token_override=legacy_secret,
            )
            migration_client.request_safe_stop.assert_called_once()
            self.assertFalse(legacy_path.exists())


if __name__ == "__main__":
    unittest.main()
