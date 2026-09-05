from __future__ import annotations

import concurrent.futures
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yeyu_gamer_platform import tray


class TrayAdmissionTests(unittest.TestCase):
    def test_secondary_tray_only_requests_primary_open(self) -> None:
        config = Mock()
        config.startup_timeout_seconds = 20
        guard = Mock()
        with (
            patch.object(tray.PlatformConfig, "load", return_value=config),
            patch.object(tray, "_acquire_tray_guard", return_value=(guard, False)),
            patch.object(tray, "send_tray_command") as send,
            patch.object(tray, "_import_qt") as import_qt,
            patch.object(tray.secrets, "token_urlsafe") as mint_secret,
            patch.object(tray, "LocalManagerController") as controller,
        ):
            result = tray.main(["--ensure-manager", "--open-webgui"])
        self.assertEqual(result, 0)
        send.assert_called_once_with(config, "open_webgui", timeout_seconds=100.0)
        import_qt.assert_not_called()
        mint_secret.assert_not_called()
        controller.assert_not_called()


class TrayCompletionTests(unittest.TestCase):
    @staticmethod
    def _host() -> tray.TrayHost:
        host = object.__new__(tray.TrayHost)
        host.config = SimpleNamespace(
            startup_timeout_seconds=1,
            web_url="http://127.0.0.1:8877/",
        )
        host.ipc_commands = tray.queue.SimpleQueue()
        host.operation_future = None
        host.operation_ipc_request = None
        host.health_future = None
        host.tray = Mock()
        host._set_lifecycle_actions_enabled = Mock()
        host._schedule_health = Mock()
        return host

    def test_ipc_waits_for_primary_tray_completion(self) -> None:
        host = self._host()
        received: dict[str, str] = {}

        def invoke() -> None:
            received.update(host._enqueue_ipc_command("ensure_manager"))

        worker = threading.Thread(target=invoke)
        worker.start()
        request = host.ipc_commands.get(timeout=1.0)
        self.assertTrue(worker.is_alive(), "enqueue acknowledgment returned too early")
        request.completion.set_result(
            {"status": "completed", "message": "paired"}
        )
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received["status"], "completed")

    def test_busy_operation_is_explicitly_rejected(self) -> None:
        host = self._host()
        host.operation_future = concurrent.futures.Future()
        request = tray._IpcCommandRequest(
            "open_webgui", concurrent.futures.Future()
        )
        accepted = host._run_operation(
            "打开 WebGUI", Mock(), ipc_request=request
        )
        self.assertFalse(accepted)
        self.assertEqual(request.completion.result()["status"], "rejected")

    def test_open_completion_requires_windows_acceptance(self) -> None:
        for opened, expected in ((True, "completed"), (False, "rejected")):
            with self.subTest(opened=opened):
                host = self._host()
                desktop = SimpleNamespace(openUrl=Mock(return_value=opened))
                host.qt = {
                    "QDesktopServices": desktop,
                    "QUrl": lambda value: value,
                }
                operation = concurrent.futures.Future()
                operation.set_result(("打开 WebGUI", SimpleNamespace(nonce="n" * 43)))
                request = tray._IpcCommandRequest(
                    "open_webgui", concurrent.futures.Future()
                )
                host.operation_future = operation
                host.operation_ipc_request = request
                host._collect_futures()
                self.assertEqual(request.completion.result()["status"], expected)
                desktop.openUrl.assert_called_once()

    def test_ensure_rechecks_current_tray_identity(self) -> None:
        host = self._host()
        host.controller = Mock()
        host.controller.ensure_bootstrap_ready.return_value = SimpleNamespace(
            healthy=True
        )
        host.controller.bootstrap_identity_is_ready.return_value = False
        with self.assertRaisesRegex(RuntimeError, "current tray"):
            host._ensure_manager_bootstrap()


if __name__ == "__main__":
    unittest.main()
