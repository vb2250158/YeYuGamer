from __future__ import annotations

import sys
from typing import Any

import uvicorn

from .app import create_app
from .listener import (
    MANAGER_PORT_BUSY_EXIT_CODE,
    ManagerPortUnavailable,
    bind_manager_listener,
)
from .logging_setup import configure_manager_logging, uvicorn_log_config
from .settings import Settings


def run() -> int:
    settings = Settings.from_env()
    logger = configure_manager_logging(settings)
    holder: dict[str, Any] = {"server": None, "action": None}

    def lifecycle(action: str) -> None:
        holder["action"] = action
        server = holder.get("server")
        if server is not None:
            server.should_exit = True

    # Bind before create_app so an occupied port never reaches the lifespan
    # and its state recovery.
    try:
        listener = bind_manager_listener(settings.host, settings.port)
    except ManagerPortUnavailable as error:
        logger.error("manager.start.port_busy host=%s port=%s", settings.host, settings.port)
        print(f"YeYu Gamer Manager: {error}", file=sys.stderr)
        return MANAGER_PORT_BUSY_EXIT_CODE

    application = create_app(settings, lifecycle_callback=lifecycle)
    config = uvicorn.Config(
        application,
        host=settings.host,
        port=settings.port,
        log_config=uvicorn_log_config(),
        access_log=False,
        limit_concurrency=64,
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    holder["server"] = server
    logger.info("manager.start.listener_bound host=%s port=%s", settings.host, settings.port)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    exit_code = 75 if holder.get("action") == "restart" else 0
    logger.info("manager.process.exit code=%s action=%s", exit_code, holder.get("action"))
    return exit_code


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()