"""Single-process YeYu Gamer desktop host.

The installed application owns the Manager API, bundled WebGUI assets and the
optional system tray in one process.  A browser is only a view onto that local
host; it is never used to bootstrap, supervise or restart the Manager.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import uvicorn

from .config import PlatformConfig
from .single_instance import SingleInstance


class ManagerAlreadyRunning(RuntimeError):
    """Another process already owns the Manager port; this host must not start."""


def _bundled_web_dist(config: PlatformConfig) -> Path:
    """Return the immutable WebGUI asset root in a frozen executable."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "webgui"
    return config.web_dist


def _configure_manager_environment(config: PlatformConfig) -> None:
    """Set only fixed, process-owned Manager settings before importing it."""

    web_dist = _bundled_web_dist(config)
    os.environ.update(
        {
            "YEYU_GAMER_INSTALL_ROOT": str(config.install_root),
            "YEYU_GAMER_RUNTIME_ROOT": str(config.runtime_root),
            "YEYU_GAMER_MANAGER_BASE_URL": config.manager_base_url,
            "YEYU_GAMER_DATA_DIR": str(config.runtime_root),
            "YEYU_GAMER_WEB_DIST": str(web_dist),
            "YEYU_GAMER_ACTOR_TOKENS_DIR": str(config.actor_token_directory),
            # The installed desktop host is the trusted owner of product
            # execution. Do not make one-button runs depend on an inherited
            # developer-shell flag.
            "YEYU_GAMER_LEGACY_EXECUTION_ENABLED": "true",
            "YEYU_GAMER_HOST": "127.0.0.1",
            "YEYU_GAMER_PORT": "8877",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if config.legacy_root is not None:
        os.environ["YEYU_GAMER_LEGACY_ROOT"] = str(config.legacy_root)
    else:
        os.environ.pop("YEYU_GAMER_LEGACY_ROOT", None)
    # The old tray-to-Manager bootstrap secret deliberately has no role in the
    # single host.  The same-origin local session endpoint recovers a browser
    # view after an in-process Manager restart.
    os.environ.pop("YEYU_GAMER_TRAY_BOOTSTRAP_SECRET", None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="YeYuGamer")
    parser.add_argument("--config", help="Path to platform.json")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the local host without opening a browser window",
    )
    return parser


@dataclass(slots=True)
class DesktopHost:
    """Own one Manager server and its browser WebGUI in this process."""

    config: PlatformConfig
    open_browser: bool = True
    server: uvicorn.Server | None = field(default=None, init=False)
    server_thread: threading.Thread | None = field(default=None, init=False)
    lifecycle_action: str | None = field(default=None, init=False)
    stop_requested: bool = field(default=False, init=False)
    server_error: str | None = field(default=None, init=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _log_lifecycle(self, phase: str, detail: str = "") -> None:
        """Keep startup failures visible even though the packaged EXE has no console."""

        try:
            self.config.log_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat()
            suffix = f" {detail}" if detail else ""
            with (self.config.log_directory / "desktop-host.log").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(f"{stamp} {phase}{suffix}\n")
        except OSError:
            # Logging cannot become a second reason for the desktop host to die.
            pass

    def _request_lifecycle(self, action: str) -> None:
        with self._state_lock:
            self.lifecycle_action = action
            if action == "stop":
                self.stop_requested = True
            if self.server is not None:
                self.server.should_exit = True

    def _start_server(self) -> None:
        if self.server_thread is not None and self.server_thread.is_alive():
            return
        _configure_manager_environment(self.config)
        from yeyu_gamer_manager.app import create_app
        from yeyu_gamer_manager.listener import (
            ManagerPortUnavailable,
            bind_manager_listener,
        )
        from yeyu_gamer_manager.logging_setup import (
            configure_manager_logging,
            uvicorn_log_config,
        )
        from yeyu_gamer_manager.settings import Settings

        settings = Settings.from_env()
        logger = configure_manager_logging(settings)
        # Bind the loopback port before the ASGI lifespan can run its state
        # recovery.  A live Manager started through any entry point (this host,
        # the legacy manager_host chain, a developer shell) keeps the port, so a
        # second host must never touch the shared SQLite file.
        try:
            listener = bind_manager_listener(settings.host, settings.port)
        except ManagerPortUnavailable as error:
            logger.warning("desktop-host.port-busy %s", error)
            self._log_lifecycle("manager-port-busy", str(error))
            raise ManagerAlreadyRunning(str(error)) from error
        application = create_app(settings, lifecycle_callback=self._request_lifecycle)
        self.server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=settings.host,
                port=settings.port,
                # A Windows-subsystem EXE has no console stream.  The Manager's
                # own rotating file log (configure_manager_logging) receives
                # uvicorn's records through propagation instead.
                log_config=uvicorn_log_config(),
                access_log=False,
                limit_concurrency=64,
                server_header=False,
                date_header=False,
                timeout_graceful_shutdown=5,
            )
        )
        self.server_error = None

        def run_server() -> None:
            try:
                assert self.server is not None
                self.server.run(sockets=[listener])
            except BaseException as error:  # surfaced to the owning host thread
                self.server_error = f"{type(error).__name__}: {error}"
                self._log_lifecycle("manager-thread-failed", self.server_error)
            finally:
                listener.close()

        self._log_lifecycle("manager-start-requested")
        self.server_thread = threading.Thread(
            target=run_server,
            name="yeyu-gamer-manager",
            daemon=True,
        )
        self.server_thread.start()
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.server.started:
                self._log_lifecycle("manager-ready")
                return
            if not self.server_thread.is_alive():
                detail = f": {self.server_error}" if self.server_error else ""
                raise RuntimeError(
                    f"YeYu Gamer Manager exited before becoming ready{detail}"
                )
            time.sleep(0.05)
        self._log_lifecycle(
            "manager-start-timeout",
            f"budgetSeconds={self.config.startup_timeout_seconds:g}",
        )
        self._request_lifecycle("stop")
        raise RuntimeError("YeYu Gamer Manager did not become ready before timeout")

    def _stop_server(self) -> None:
        with self._state_lock:
            self.stop_requested = True
            if self.server is not None:
                self.server.should_exit = True
            thread = self.server_thread
        if thread is not None:
            thread.join(timeout=self.config.startup_timeout_seconds)

    def _server_exited(self) -> bool:
        return self.server_thread is not None and not self.server_thread.is_alive()

    def _restart_if_requested(self) -> bool:
        if not self._server_exited():
            return False
        with self._state_lock:
            action = self.lifecycle_action
            self.lifecycle_action = None
        if action == "restart" and not self.stop_requested:
            # The previous listener closes when its server thread exits; give
            # the kernel a moment to release the port before declaring another
            # owner.
            for attempt in range(20):
                try:
                    self._start_server()
                    break
                except ManagerAlreadyRunning:
                    if attempt == 19:
                        self._log_lifecycle(
                            "manager-restart-port-busy",
                            "port stayed busy after restart; leaving host",
                        )
                        return True
                    time.sleep(0.5)
            return False
        return True

    def _run_event_loop(self) -> int:
        if self.open_browser:
            webbrowser.open(self.config.web_url)
        while not self._restart_if_requested():
            time.sleep(0.2)
        return 0

    def run(self) -> int:
        guard = SingleInstance(
            "Local\\YeYuGamer.Host.v1", self.config.state_directory / "host.lock"
        )
        if not guard.acquire():
            # A second launch is not another app or another Manager.  It only
            # asks Windows to show the running host's same local page.
            webbrowser.open(self.config.web_url)
            return 0
        try:
            try:
                self._start_server()
            except ManagerAlreadyRunning:
                # The port is owned by a Manager that did not come through this
                # host's mutex (legacy manager_host chain or a developer run).
                # Behave exactly like the second-launch case above.
                if self.open_browser:
                    webbrowser.open(self.config.web_url)
                return 0
            return self._run_event_loop()
        finally:
            self._stop_server()
            guard.release()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        host = DesktopHost(
            PlatformConfig.load(arguments.config), open_browser=not arguments.no_browser
        )
        return host.run()
    except (OSError, RuntimeError, ValueError) as error:
        if sys.stderr is not None:
            print(f"YeYu Gamer: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
