from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from yeyu_gamer_platform.config import PlatformConfig
from yeyu_gamer_platform.tray_ipc import (
    MAX_ENDPOINT_BYTES,
    TrayIpcError,
    TrayIpcServer,
    _assert_current_user_acl,
    _read_endpoint_record,
    _remove_endpoint_record_if_owned,
    _set_current_user_acl,
    _write_endpoint_record,
    endpoint_record_path,
    main,
    send_tray_command,
)


class TrayIpcTests(unittest.TestCase):
    @staticmethod
    def _config(root: Path) -> PlatformConfig:
        return PlatformConfig.for_test(
            install_root=root / "install",
            runtime_root=root / "runtime",
            manager_base_url="http://127.0.0.1:18877/api/v1",
            web_url="http://127.0.0.1:18877/",
            legacy_root=None,
            web_dist=root / "web-dist",
            actor_token_file=None,
        )

    def test_authenticated_commands_are_queued_by_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            commands: list[str] = []

            def handler(command: str) -> dict[str, str] | None:
                commands.append(command)
                if command == "exit":
                    return None
                return {"status": "completed", "message": f"{command} complete"}

            server = TrayIpcServer(config, handler)
            server.start()
            try:
                for command in ("ping", "ensure_manager", "open_webgui", "exit"):
                    response = send_tray_command(
                        config, command, timeout_seconds=1.0
                    )
                    expected_status = "accepted" if command == "exit" else "completed"
                    self.assertEqual(response["status"], expected_status)
                self.assertEqual(
                    commands, ["ping", "ensure_manager", "open_webgui", "exit"]
                )
                record_path = endpoint_record_path(config)
                self.assertEqual(record_path.parent, config.install_root.parent / ".yeyu-gamer-user-state")
                _assert_current_user_acl(record_path)
                record = _read_endpoint_record(record_path)
                self.assertNotIn(record["authKey"], repr(response))
            finally:
                server.stop()
            self.assertFalse(endpoint_record_path(config).exists())

    def test_only_exit_accepts_an_acknowledgement_without_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            commands: list[str] = []
            server = TrayIpcServer(config, commands.append)
            server.start()
            try:
                for command in ("ping", "ensure_manager", "open_webgui"):
                    with self.subTest(command=command):
                        with self.assertRaises(TrayIpcError) as captured:
                            send_tray_command(config, command, timeout_seconds=1.0)
                        self.assertEqual(captured.exception.exit_code, 4)
                        self.assertIn("without reporting completion", str(captured.exception))

                accepted_exit = send_tray_command(
                    config, "exit", timeout_seconds=1.0
                )
                self.assertEqual(accepted_exit["status"], "accepted")
                self.assertEqual(
                    commands, ["ping", "ensure_manager", "open_webgui", "exit"]
                )
            finally:
                server.stop()

    def test_wrong_endpoint_key_is_rejected_as_authentication_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            server = TrayIpcServer(config, lambda _: None)
            server.start()
            try:
                path = endpoint_record_path(config)
                record = _read_endpoint_record(path)
                record.pop("authKeyBytes")
                record["authKey"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
                _write_endpoint_record(path, record)
                with self.assertRaises(TrayIpcError) as captured:
                    send_tray_command(config, "ping", timeout_seconds=0.5)
                self.assertEqual(captured.exception.exit_code, 4)
            finally:
                server.stop()

    def test_completion_result_is_authenticated_and_rejection_is_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))

            def handler(command: str) -> dict[str, str]:
                if command == "ensure_manager":
                    return {"status": "completed", "message": "paired"}
                return {"status": "rejected", "message": "busy"}

            server = TrayIpcServer(config, handler)
            server.start()
            try:
                completed = send_tray_command(
                    config, "ensure_manager", timeout_seconds=1.0
                )
                self.assertEqual(completed["status"], "completed")
                with self.assertRaises(TrayIpcError) as rejected:
                    send_tray_command(config, "open_webgui", timeout_seconds=1.0)
                self.assertEqual(rejected.exception.exit_code, 4)
            finally:
                server.stop()

    def test_stale_or_missing_endpoint_has_unavailable_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            with self.assertRaises(TrayIpcError) as captured:
                send_tray_command(config, "ping", timeout_seconds=0.1)
            self.assertEqual(captured.exception.exit_code, 3)

    def test_endpoint_rejects_reparse_and_oversize_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            path = endpoint_record_path(config)
            path.parent.mkdir(parents=True)
            _set_current_user_acl(path.parent, directory=True)
            path.write_bytes(b"x" * (MAX_ENDPOINT_BYTES + 1))
            _set_current_user_acl(path, directory=False)
            with self.assertRaises(TrayIpcError) as oversized:
                _read_endpoint_record(path)
            self.assertEqual(oversized.exception.exit_code, 4)
            with patch(
                "yeyu_gamer_platform.tray_ipc._is_reparse", return_value=True
            ):
                with self.assertRaises(TrayIpcError) as reparse:
                    _read_endpoint_record(path)
            self.assertEqual(reparse.exception.exit_code, 4)

    def test_cleanup_never_deletes_another_instance_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            path = endpoint_record_path(config)
            other_id = str(uuid.uuid4())
            record = {
                "schemaVersion": 1,
                "instanceId": other_id,
                "pid": 1,
                "ownerIdentity": "test",
                "host": "127.0.0.1",
                "port": 1234,
                "authKey": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "createdAt": "test",
            }
            _write_endpoint_record(path, record)
            self.assertFalse(
                _remove_endpoint_record_if_owned(path, str(uuid.uuid4()))
            )
            restored = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(restored["instanceId"], other_id)

    def test_cli_preserves_stable_ipc_exit_code(self) -> None:
        error = TrayIpcError("unavailable", exit_code=3)
        with (
            patch(
                "yeyu_gamer_platform.tray_ipc.PlatformConfig.load",
                return_value=object(),
            ),
            patch(
                "yeyu_gamer_platform.tray_ipc.send_tray_command",
                side_effect=error,
            ),
        ):
            self.assertEqual(main(["ping"]), 3)

    def test_cli_requires_completion_except_for_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            server = TrayIpcServer(config, lambda _: None)
            server.start()
            try:
                with patch(
                    "yeyu_gamer_platform.tray_ipc.PlatformConfig.load",
                    return_value=config,
                ):
                    for command in ("ping", "ensure_manager", "open_webgui"):
                        with self.subTest(command=command), patch(
                            "sys.stderr", new_callable=io.StringIO
                        ):
                            self.assertEqual(
                                main(["--timeout-seconds", "1", command]), 4
                            )
                    with patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(
                            main(["--timeout-seconds", "1", "exit"]), 0
                        )
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
