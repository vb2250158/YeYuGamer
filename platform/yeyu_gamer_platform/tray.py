"""Lightweight YeYu Gamer system tray host.

There is deliberately no desktop main window and no embedded browser.  Every
business operation remains in Manager; the tray only controls local lifecycle.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import queue
import secrets
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit

from .api_client import ManagerApiClient
from .config import PlatformConfig
from .lifecycle_lock import InstallLifecycleLock
from .process_control import LocalManagerController
from .single_instance import SingleInstance
from .tray_ipc import TrayIpcError, TrayIpcServer, send_tray_command


TRAY_LIFECYCLE_ACTOR = "tray-lifecycle"


@dataclass(slots=True)
class _IpcCommandRequest:
    command: str
    completion: concurrent.futures.Future[dict[str, str]]


def _acquire_tray_guard(config: PlatformConfig) -> tuple[SingleInstance, bool]:
    """Serialize tray admission with installation preflight and rotation."""

    lifecycle_lock = InstallLifecycleLock(
        config.state_directory / "install-lifecycle.lock"
    )
    with lifecycle_lock:
        guard = SingleInstance(
            "Local\\YeYuGamer.Tray.v3",
            config.state_directory / "tray.lock",
        )
        return guard, guard.acquire()


def _bootstrap_web_url(web_url: str, nonce: str) -> str:
    parsed = urlsplit(web_url)
    fragment = urlencode({"bootstrap": nonce})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", fragment))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yeyu-gamer-tray")
    parser.add_argument("--config", help="Path to platform.json")
    parser.add_argument(
        "--ensure-manager",
        action="store_true",
        help="Start the local Manager before showing the tray",
    )
    parser.add_argument(
        "--open-webgui",
        action="store_true",
        help="Open the loopback WebGUI after the tray is ready",
    )
    return parser


def _import_qt() -> dict[str, Any]:
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtGui import QAction, QDesktopServices, QIcon
        from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon
    except ImportError as error:
        raise RuntimeError(
            "PySide6 is not installed. Run Install-YeYuGamer.ps1 to install "
            "the tray dependency, or use the yeyu-gamer CLI without a tray."
        ) from error
    return {
        "QAction": QAction,
        "QApplication": QApplication,
        "QDesktopServices": QDesktopServices,
        "QIcon": QIcon,
        "QMenu": QMenu,
        "QStyle": QStyle,
        "QSystemTrayIcon": QSystemTrayIcon,
        "QTimer": QTimer,
        "QUrl": QUrl,
    }


class TrayHost:
    def __init__(
        self,
        config: PlatformConfig,
        qt: dict[str, Any],
        tray_bootstrap_secret: str,
    ) -> None:
        self.config = config
        self.qt = qt
        self.bootstrap_client = ManagerApiClient(
            config,
            actor="tray",
            actor_token_override=tray_bootstrap_secret,
        )
        self.lifecycle_client = ManagerApiClient(
            config, actor=TRAY_LIFECYCLE_ACTOR
        )
        self.controller = LocalManagerController(
            config,
            actor=TRAY_LIFECYCLE_ACTOR,
            tray_bootstrap_secret=tray_bootstrap_secret,
        )
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="yeyu-gamer-tray"
        )
        self.health_future: concurrent.futures.Future[Any] | None = None
        self.operation_future: concurrent.futures.Future[Any] | None = None
        self.ipc_commands: queue.SimpleQueue[_IpcCommandRequest] = queue.SimpleQueue()
        self.operation_ipc_request: _IpcCommandRequest | None = None
        self.ipc_server = TrayIpcServer(config, self._enqueue_ipc_command)

        QSystemTrayIcon = qt["QSystemTrayIcon"]
        QApplication = qt["QApplication"]
        QStyle = qt["QStyle"]
        QIcon = qt["QIcon"]

        self.tray = QSystemTrayIcon()
        icon_candidates = (
            config.install_root / "app" / "assets" / "yeyu-gamer.ico",
            config.install_root / "app" / "assets" / "logo.png",
        )
        icon = next((QIcon(str(path)) for path in icon_candidates if path.is_file()), None)
        if icon is None or icon.isNull():
            icon = QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray.setIcon(icon)
        self.tray.setToolTip("YeYu Gamer · Manager 状态检查中")

        menu = qt["QMenu"]()
        self.open_action = menu.addAction("打开 YeYu Gamer WebGUI")
        self.status_action = menu.addAction("Manager 状态：检查中")
        self.status_action.setEnabled(False)
        menu.addSeparator()
        self.start_action = menu.addAction("启动 Manager")
        self.stop_action = menu.addAction("安全停止 Manager")
        self.restart_action = menu.addAction("重启 Manager")
        menu.addSeparator()
        self.logs_action = menu.addAction("打开本机日志目录")
        self.exit_action = menu.addAction("退出托盘")
        self.tray.setContextMenu(menu)

        self.open_action.triggered.connect(self.open_webgui)
        self.start_action.triggered.connect(self.start_manager)
        self.stop_action.triggered.connect(self.safe_stop_manager)
        self.restart_action.triggered.connect(self.restart_manager)
        self.logs_action.triggered.connect(self.open_logs)
        self.exit_action.triggered.connect(QApplication.instance().quit)
        self.tray.activated.connect(self._on_activated)

        self.health_timer = qt["QTimer"]()
        self.health_timer.timeout.connect(self._schedule_health)
        self.health_timer.start(int(config.health_refresh_seconds * 1000))
        self.future_timer = qt["QTimer"]()
        self.future_timer.timeout.connect(self._collect_futures)
        self.future_timer.start(250)
        self.ipc_timer = qt["QTimer"]()
        self.ipc_timer.timeout.connect(self._collect_ipc_commands)
        self.ipc_timer.start(100)

    def show(self) -> None:
        self.ipc_server.start()
        self.tray.show()
        self._schedule_health()

    def shutdown(self) -> None:
        if (
            self.operation_ipc_request is not None
            and not self.operation_ipc_request.completion.done()
        ):
            self.operation_ipc_request.completion.set_result(
                {"status": "rejected", "message": "primary tray is shutting down"}
            )
        while True:
            try:
                pending = self.ipc_commands.get_nowait()
            except queue.Empty:
                break
            if not pending.completion.done():
                pending.completion.set_result(
                    {"status": "rejected", "message": "primary tray is shutting down"}
                )
        self.ipc_timer.stop()
        self.health_timer.stop()
        self.future_timer.stop()
        self.tray.hide()
        self.ipc_server.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _enqueue_ipc_command(self, command: str) -> dict[str, str]:
        # Ping proves only that this authenticated IPC endpoint is alive. All
        # lifecycle and browser commands return a signed completion result;
        # enqueue acknowledgment is never treated as product readiness.
        if command == "ping":
            return {"status": "completed", "message": "primary tray IPC is ready"}
        completion: concurrent.futures.Future[dict[str, str]] = (
            concurrent.futures.Future()
        )
        self.ipc_commands.put(_IpcCommandRequest(command, completion))
        timeout_seconds = min(
            300.0,
            max(15.0, float(self.config.startup_timeout_seconds) * 4.0 + 15.0),
        )
        try:
            return completion.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            return {
                "status": "rejected",
                "message": "primary tray operation did not complete before its deadline",
            }

    def _collect_ipc_commands(self) -> None:
        while True:
            try:
                request = self.ipc_commands.get_nowait()
            except queue.Empty:
                return
            command = request.command
            if command == "open_webgui":
                self._run_operation(
                    "打开 WebGUI", self._issue_webgui_bootstrap, ipc_request=request
                )
            elif command == "ensure_manager":
                self._run_operation(
                    "启动 Manager", self._ensure_manager_bootstrap, ipc_request=request
                )
            elif command == "exit":
                request.completion.set_result(
                    {"status": "accepted", "message": "primary tray exit was queued"}
                )
                self.qt["QApplication"].instance().quit()
                return
            elif command == "ping":
                request.completion.set_result(
                    {"status": "completed", "message": "primary tray IPC is ready"}
                )

    def open_webgui(self) -> None:
        self._run_operation(
            "打开 WebGUI", self._issue_webgui_bootstrap
        )

    def _issue_webgui_bootstrap(self) -> Any:
        self._ensure_manager_bootstrap()
        return self.bootstrap_client.issue_webgui_bootstrap()

    def _ensure_manager_bootstrap(self) -> Any:
        result = self.controller.ensure_bootstrap_ready()
        if not result.healthy or not self.controller.bootstrap_identity_is_ready():
            raise RuntimeError("Manager did not authenticate the current tray instance")
        return result

    def open_logs(self) -> None:
        self.config.log_directory.mkdir(parents=True, exist_ok=True)
        url = self.qt["QUrl"].fromLocalFile(str(self.config.log_directory))
        self.qt["QDesktopServices"].openUrl(url)

    def start_manager(self) -> None:
        self._run_operation(
            "启动 Manager", self._ensure_manager_bootstrap
        )

    def safe_stop_manager(self) -> None:
        self._run_operation(
            "安全停止 Manager",
            lambda: self.lifecycle_client.request_safe_stop(
                idempotency_key=f"tray-stop-{uuid.uuid4()}"
            ),
        )

    def restart_manager(self) -> None:
        self._run_operation(
            "重启 Manager",
            lambda: self.lifecycle_client.request_restart(
                idempotency_key=f"tray-restart-{uuid.uuid4()}"
            ),
        )

    def _on_activated(self, reason: Any) -> None:
        if reason == self.qt["QSystemTrayIcon"].DoubleClick:
            self.open_webgui()

    def _run_operation(
        self,
        label: str,
        operation: Callable[[], Any],
        *,
        ipc_request: _IpcCommandRequest | None = None,
    ) -> bool:
        if self.operation_future is not None:
            self.tray.showMessage(
                "YeYu Gamer", "已有生命周期操作正在进行，请稍候。"
            )
            if ipc_request is not None and not ipc_request.completion.done():
                ipc_request.completion.set_result(
                    {
                        "status": "rejected",
                        "message": "primary tray is busy with another lifecycle operation",
                    }
                )
            return False
        self._set_lifecycle_actions_enabled(False)

        def wrapped() -> tuple[str, Any]:
            return label, operation()

        self.operation_ipc_request = ipc_request
        self.operation_future = self.executor.submit(wrapped)
        return True

    def _schedule_health(self) -> None:
        if self.health_future is None or self.health_future.done():
            self.health_future = self.executor.submit(self.controller.health_client.health)

    def _collect_futures(self) -> None:
        if self.health_future is not None and self.health_future.done():
            future = self.health_future
            self.health_future = None
            try:
                health = future.result()
            except Exception:
                self._set_health(False, "离线", reachable=False)
            else:
                state = "健康" if health.ok else health.status
                if health.active_batch_id:
                    state = f"{state} · 批次运行中"
                self._set_health(health.ok, state, reachable=True)

        if self.operation_future is not None and self.operation_future.done():
            future = self.operation_future
            ipc_request = self.operation_ipc_request
            self.operation_future = None
            self.operation_ipc_request = None
            self._set_lifecycle_actions_enabled(True)
            try:
                label, result = future.result()
            except Exception as error:
                self.tray.showMessage("YeYu Gamer", f"操作失败：{error}")
                if ipc_request is not None and not ipc_request.completion.done():
                    ipc_request.completion.set_result(
                        {
                            "status": "rejected",
                            "message": "primary tray lifecycle operation failed",
                        }
                    )
            else:
                message = getattr(result, "message", None)
                command_id = getattr(result, "command_id", None)
                nonce = getattr(result, "nonce", None)
                completed = True
                if isinstance(nonce, str):
                    opened = self.qt["QDesktopServices"].openUrl(
                        self.qt["QUrl"](
                            _bootstrap_web_url(self.config.web_url, nonce)
                        )
                    )
                    message = "已在浏览器打开" if opened else "浏览器未接受打开请求"
                    completed = bool(opened)
                elif command_id:
                    message = f"请求已受理：{command_id}"
                self.tray.showMessage("YeYu Gamer", f"{label}：{message or '已提交'}")
                if ipc_request is not None and not ipc_request.completion.done():
                    if ipc_request.command == "ensure_manager":
                        completion_message = "Manager is paired with the primary tray"
                    elif completed:
                        completion_message = (
                            "Windows accepted the authenticated WebGUI open request"
                        )
                    else:
                        completion_message = "Windows rejected the WebGUI open request"
                    ipc_request.completion.set_result(
                        {
                            "status": "completed" if completed else "rejected",
                            "message": completion_message,
                        }
                    )
            self._schedule_health()

    def _set_health(self, ok: bool, status: str, *, reachable: bool) -> None:
        self.status_action.setText(f"Manager 状态：{status}")
        self.tray.setToolTip(f"YeYu Gamer · Manager {status}")
        idle = self.operation_future is None
        self.start_action.setEnabled(not reachable and idle)
        self.stop_action.setEnabled(reachable and idle)
        self.restart_action.setEnabled(reachable and idle)

    def _set_lifecycle_actions_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.start_action.setEnabled(False)
            self.stop_action.setEnabled(False)
            self.restart_action.setEnabled(False)
        else:
            self._schedule_health()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = PlatformConfig.load(args.config)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"YeYu Gamer tray: {error}", file=sys.stderr)
        return 2

    try:
        guard, guard_acquired = _acquire_tray_guard(config)
    except (RuntimeError, OSError) as error:
        print(f"YeYu Gamer tray: {error}", file=sys.stderr)
        return 5
    if not guard_acquired:
        # A secondary process never creates a bootstrap secret and never calls
        # Manager lifecycle APIs.  Its sole behavior is to ask the authenticated
        # primary tray to open WebGUI.
        try:
            send_tray_command(
                config,
                "open_webgui",
                timeout_seconds=min(
                    300.0,
                    max(15.0, float(config.startup_timeout_seconds) * 4.0 + 20.0),
                ),
            )
        except TrayIpcError as error:
            print(f"YeYu Gamer tray: {error}", file=sys.stderr)
            return error.exit_code
        return 0

    try:
        qt = _import_qt()
        QApplication = qt["QApplication"]
        QSystemTrayIcon = qt["QSystemTrayIcon"]
        app = QApplication.instance() or QApplication(["YeYu Gamer"])
        app.setQuitOnLastWindowClosed(False)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print(
                "YeYu Gamer tray: the Windows system tray is unavailable",
                file=sys.stderr,
            )
            guard.release()
            return 3
    except (RuntimeError, ValueError, OSError) as error:
        guard.release()
        print(f"YeYu Gamer tray: {error}", file=sys.stderr)
        return 2

    # Only the mutex-owning primary tray reaches this point.  This value is
    # never written to config, argv, logs, receipts or actor token files.
    tray_bootstrap_secret = secrets.token_urlsafe(48)

    startup_error: Exception | None = None
    if args.ensure_manager:
        try:
            result = LocalManagerController(
                config,
                actor=TRAY_LIFECYCLE_ACTOR,
                tray_bootstrap_secret=tray_bootstrap_secret,
            ).ensure_bootstrap_ready()
            if not result.healthy:
                raise RuntimeError(result.message)
        except Exception as error:  # surfaced through the local tray, never a shell
            startup_error = error

    host = TrayHost(config, qt, tray_bootstrap_secret)
    app.aboutToQuit.connect(host.shutdown)
    app.aboutToQuit.connect(guard.release)
    try:
        host.show()
    except (OSError, RuntimeError, ValueError) as error:
        host.shutdown()
        guard.release()
        print(f"YeYu Gamer tray: IPC startup failed: {error}", file=sys.stderr)
        return 6
    if args.open_webgui:
        host.open_webgui()
    if startup_error is not None:
        host.tray.showMessage("YeYu Gamer", f"Manager 启动失败：{startup_error}")
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
