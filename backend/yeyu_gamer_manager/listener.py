"""Bind the Manager's loopback listener before any state recovery runs.

Uvicorn runs the ASGI lifespan (where ``ManagerService`` revokes stale leases
and reconciles active batches in the shared SQLite file) *before* it binds the
TCP port.  A second Manager process therefore used to mutate the live
instance's state and only afterwards discover that the port was taken.  Every
process entry point must obtain its socket from here first and hand it to
``uvicorn.Server.run(sockets=[...])`` so an occupied port fails closed without
touching the database.
"""

from __future__ import annotations

import errno
import os
import socket

MANAGER_PORT_BUSY_EXIT_CODE = 3
"""Matches uvicorn's ``STARTUP_FAILURE`` so supervisors keep one meaning."""

_ADDRESS_IN_USE_CODES = {errno.EADDRINUSE, errno.EACCES, 10048, 10013}


class ManagerPortUnavailable(OSError):
    """The canonical loopback port is already owned by another listener."""

    def __init__(self, host: str, port: int, cause: OSError) -> None:
        super().__init__(
            cause.errno,
            f"YeYu Gamer Manager port {host}:{port} is already in use; "
            "another Manager instance owns it",
        )
        self.host = host
        self.port = port
        self.cause = cause


def bind_manager_listener(host: str, port: int, *, backlog: int = 128) -> socket.socket:
    """Exclusively bind and listen on ``host:port`` or raise ``ManagerPortUnavailable``.

    Windows uses ``SO_EXCLUSIVEADDRUSE`` so a bind cannot silently overlap a
    live listener; other platforms mirror uvicorn's ``SO_REUSEADDR`` default
    so a recently closed socket does not block a legitimate restart.
    """

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is None:
                raise RuntimeError(
                    "Windows exclusive TCP bind support is unavailable; refusing to start"
                )
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(backlog)
        listener.set_inheritable(False)
    except OSError as error:
        listener.close()
        if {error.errno, getattr(error, "winerror", None)} & _ADDRESS_IN_USE_CODES:
            raise ManagerPortUnavailable(host, port, error) from error
        raise
    except Exception:
        listener.close()
        raise
    return listener
