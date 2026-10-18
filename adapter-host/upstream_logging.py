"""Passive, best-effort capture of an upstream tool's Python logging records.

The observer appends its own handlers to root, ``ok``, and loggers which have
already opted out of propagation.  It never replaces upstream handlers or
mutates logger levels, propagation, or formatter settings.  A record that
travels through more than one observed logger is written once per destination.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import threading
import traceback
from typing import Any
import weakref


_HANDLER_MARKER = "_yeyu_gamer_upstream_logging_handler_v1"
_WRITE_LOCK = threading.Lock()
_LOGURU_SINKS: dict[str, int] = {}
_RECORD_PATHS: weakref.WeakKeyDictionary[logging.LogRecord, set[str]] = weakref.WeakKeyDictionary()
_RECORD_PATHS_LOCK = threading.Lock()


def _exception_text(record: logging.LogRecord) -> str | None:
    if record.exc_info:
        try:
            # Exception text is evidence from the upstream tool.  Preserve the
            # complete formatted traceback rather than applying the normal
            # message-size guard.
            return "".join(traceback.format_exception(*record.exc_info))
        except Exception:
            return "[exception formatting failed]"
    if record.exc_text:
        return str(record.exc_text)
    return None


def _record_document(record: logging.LogRecord) -> dict[str, Any]:
    try:
        message = record.getMessage()
    except Exception:
        message = "[message formatting failed]"
    document: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        "logger": str(record.name),
        "level": str(record.levelname),
        "message": str(message),
    }
    exception = _exception_text(record)
    if exception:
        document["exception"] = exception
    return document


def _append(path: str, document: dict[str, Any]) -> None:
    """Append one record, treating observation storage as non-authoritative."""

    try:
        serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        # No directory creation, rotation or persistent state is performed.
        # A single short append + flush bounds this observer's work per record.
        with _WRITE_LOCK:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
                stream.flush()
    except Exception:
        # Observation must never affect the upstream logger's handlers, its
        # return values, or any exception that the task is already handling.
        return


class _UpstreamLogHandler(logging.Handler):
    def __init__(self, path: str) -> None:
        super().__init__(logging.NOTSET)
        self._yeyu_path = path
        setattr(self, _HANDLER_MARKER, True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # A LogRecord is passed unchanged while the logging hierarchy
            # dispatches it.  Keep the de-duplication state outside that
            # upstream object: third-party JSON handlers may serialize its
            # __dict__ exactly and must not see observer-private fields.
            with _RECORD_PATHS_LOCK:
                written_paths = _RECORD_PATHS.setdefault(record, set())
                if self._yeyu_path in written_paths:
                    return
                written_paths.add(self._yeyu_path)
            _append(self._yeyu_path, _record_document(record))
        except Exception:
            return


def _install_loguru_sink(path: str) -> None:
    """Optionally observe loguru when it is already provided by the tool."""

    try:
        from loguru import logger as loguru_logger  # type: ignore[import-not-found]
    except Exception:
        return

    if path in _LOGURU_SINKS:
        return

    def sink(message: Any) -> None:
        try:
            record = message.record
            document: dict[str, Any] = {
                "timestamp": record["time"].astimezone(timezone.utc).isoformat(),
                "logger": str(record["name"]),
                "level": str(record["level"].name),
                "message": str(record["message"]),
            }
            if record["exception"] is not None:
                exception = record["exception"]
                document["exception"] = "".join(traceback.format_exception(
                    exception.type, exception.value, exception.traceback))
            _append(path, document)
        except Exception:
            return

    try:
        sink_id = loguru_logger.add(sink, level=0, catch=True)
    except Exception:
        return
    _LOGURU_SINKS[path] = int(sink_id)


def _existing_non_propagating_loggers() -> list[logging.Logger]:
    """Return named loggers which root cannot observe through propagation."""

    manager = logging.Logger.manager
    return [
        candidate
        for candidate in manager.loggerDict.values()
        if isinstance(candidate, logging.Logger) and not candidate.propagate
    ]


def _install_handler(logger: logging.Logger, path: str) -> logging.Handler | None:
    for handler in logger.handlers:
        if (
            getattr(handler, _HANDLER_MARKER, False)
            and getattr(handler, "_yeyu_path", None) == path
        ):
            return handler
    handler = _UpstreamLogHandler(path)
    try:
        logger.addHandler(handler)
    except Exception:
        return None
    return handler


def install(log_path: str) -> logging.Handler | None:
    """Install passive observers for standard logging and optional loguru.

    ``None`` denotes an unavailable observation path.  Repeating an install is
    idempotent for each logger.  If upstream configuration later replaces
    ``ok.handlers``, calling ``install`` again attaches a new observer without
    changing that configuration.
    """

    if not isinstance(log_path, str) or not log_path or os.path.isdir(log_path):
        return None
    try:
        path = str(Path(log_path))
    except Exception:
        return None
    root_logger = logging.getLogger()
    ok_logger = logging.getLogger("ok")
    ok_handler = _install_handler(ok_logger, path)
    _install_handler(root_logger, path)
    for logger in _existing_non_propagating_loggers():
        if logger is not root_logger and logger is not ok_logger:
            _install_handler(logger, path)
    _install_loguru_sink(path)
    return ok_handler
