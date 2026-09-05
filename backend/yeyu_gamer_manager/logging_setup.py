"""Manager diagnostic logging.

Before this module existed the Manager wrote no application log at all: the
only text on disk was uvicorn's startup banner captured through a stdout
redirect.  Everything here is file-first because the packaged host runs as a
console-less ``pythonw``/PyInstaller process.

Layout under ``<data_dir>/logs``::

    manager/manager.log            rotated daily, ``manager.log.YYYY-MM-DD``
    runs/<YYYY-MM-DD>/<game>-<attemptId>/attempt.log
    runs/<YYYY-MM-DD>/<game>-<attemptId>/adapter.stdout.jsonl
    runs/<YYYY-MM-DD>/<game>-<attemptId>/adapter.stderr.log

Every record carries the correlation fields bound through
``bind_log_context`` (batch, run, attempt, game, todo, phase) so the file log
and the SQLite event stream can be joined on the same identifiers.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .settings import Settings

LOGGER_NAME = "yeyu_gamer"
MANAGER_LOG_DIRNAME = "manager"
RUN_LOG_DIRNAME = "runs"
DEFAULT_RETENTION_DAYS = 14
CONTEXT_FIELDS = ("batch", "run", "attempt", "game", "todo", "phase")

_log_context: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "yeyu_gamer_log_context", default={}
)
_configured_targets: dict[str, logging.Handler] = {}
_configure_lock = threading.Lock()
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def log_context() -> Mapping[str, str]:
    """Return the correlation fields bound to the current thread/task."""

    return _log_context.get()


@contextlib.contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields for the dynamic extent of the block."""

    merged = dict(_log_context.get())
    for key, value in fields.items():
        if key not in CONTEXT_FIELDS:
            raise ValueError(f"unknown log context field {key!r}")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = str(value)
    token = _log_context.set(merged)
    try:
        yield
    finally:
        _log_context.reset(token)


class _ContextFilter(logging.Filter):
    """Inject the bound correlation fields into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _log_context.get()
        for field in CONTEXT_FIELDS:
            setattr(record, field, context.get(field, "-"))
        record.correlation = " ".join(
            f"{field}={context[field]}" for field in CONTEXT_FIELDS if field in context
        ) or "-"
        return True


class _AttemptFilter(logging.Filter):
    """Route only records bound to one RunAttempt into its dedicated file."""

    def __init__(self, run_attempt_id: str) -> None:
        super().__init__()
        self.run_attempt_id = run_attempt_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _log_context.get().get("attempt") == self.run_attempt_id


_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s [%(correlation)s] %(message)s"


def _formatter() -> logging.Formatter:
    return logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")


def _log_level() -> int:
    configured = os.getenv("YEYU_GAMER_LOG_LEVEL", "INFO").strip().upper()
    return logging._nameToLevel.get(configured, logging.INFO)  # noqa: SLF001


def _retention_days() -> int:
    raw = os.getenv("YEYU_GAMER_LOG_RETENTION_DAYS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(1, min(value, 365))


def manager_log_directory(settings: Settings) -> Path:
    return settings.data_dir / "logs" / MANAGER_LOG_DIRNAME


def run_log_root(settings: Settings) -> Path:
    return settings.data_dir / "logs" / RUN_LOG_DIRNAME


def configure_manager_logging(settings: Settings) -> logging.Logger:
    """Attach the rotating Manager file log to the root logger exactly once.

    uvicorn's own loggers propagate to root, so its startup/shutdown lines end
    up in the same file without a stderr handler that a console-less process
    lacks.  A stderr handler is added only when a real console exists.
    """

    directory = manager_log_directory(settings)
    key = str(directory.resolve()) if directory.exists() else str(directory)
    root = logging.getLogger()
    with _configure_lock:
        root.setLevel(min(root.level or logging.INFO, _log_level()))
        # One process owns one runtime root.  A different target (tests,
        # relocated runtime) replaces the previous file handler so no stale
        # handle keeps an old directory locked.
        for stale_key in [existing for existing in _configured_targets if existing != key]:
            stale = _configured_targets.pop(stale_key)
            root.removeHandler(stale)
            stale.close()
        if key not in _configured_targets:
            directory.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.TimedRotatingFileHandler(
                directory / "manager.log",
                when="midnight",
                backupCount=_retention_days(),
                encoding="utf-8",
                utc=False,
            )
            handler.setFormatter(_formatter())
            handler.addFilter(_ContextFilter())
            root.addHandler(handler)
            _configured_targets[key] = handler
            if sys.stderr is not None and not any(
                isinstance(existing, logging.StreamHandler)
                and not isinstance(existing, logging.FileHandler)
                for existing in root.handlers
            ):
                console = logging.StreamHandler(sys.stderr)
                console.setFormatter(_formatter())
                console.addFilter(_ContextFilter())
                root.addHandler(console)
        for noisy in ("uvicorn.access", "httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        for propagated in ("uvicorn", "uvicorn.error"):
            logger = logging.getLogger(propagated)
            logger.handlers.clear()
            logger.propagate = True
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_log_level())
    return logger


def shutdown_manager_logging() -> None:
    """Detach and close the Manager file handler (tests and clean exits)."""

    root = logging.getLogger()
    with _configure_lock:
        for key in list(_configured_targets):
            handler = _configured_targets.pop(key)
            root.removeHandler(handler)
            handler.close()


def uvicorn_log_config() -> None:
    """uvicorn must not install its own stderr handlers; root owns the file."""

    return None


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("_", value.strip())
    return cleaned[:96] or "unknown"


class AttemptLogSession:
    """Own the per-RunAttempt log directory for the lifetime of one attempt.

    ``attempt.log`` receives every Manager record bound to the attempt through
    ``bind_log_context(attempt=...)``.  The adapter transport appends the raw
    JSONL event stream and stderr through ``write_stdout``/``write_stderr`` so a
    failed run can be replayed without the 8 KB in-memory tail limit.
    """

    def __init__(self, settings: Settings, *, game_id: str, run_attempt_id: str) -> None:
        day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        self.directory = (
            run_log_root(settings)
            / day
            / f"{_safe_segment(game_id)}-{_safe_segment(run_attempt_id)}"
        )
        self.run_attempt_id = run_attempt_id
        self._handler: logging.Handler | None = None
        self._stdout: Any = None
        self._stderr: Any = None
        self._io_lock = threading.Lock()
        self._closed = False

    def open(self) -> "AttemptLogSession":
        self.directory.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.directory / "attempt.log", encoding="utf-8")
        handler.setFormatter(_formatter())
        handler.addFilter(_ContextFilter())
        handler.addFilter(_AttemptFilter(self.run_attempt_id))
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        self._handler = handler
        return self

    def write_stdout(self, line: bytes) -> None:
        self._append("_stdout", "adapter.stdout.jsonl", line)

    def write_stderr(self, chunk: bytes) -> None:
        self._append("_stderr", "adapter.stderr.log", chunk)

    def write_text(self, name: str, text: str) -> Path:
        path = self.directory / _safe_segment(name)
        with self._io_lock:
            path.write_text(text, encoding="utf-8")
        return path

    def _append(self, attribute: str, filename: str, data: bytes) -> None:
        with self._io_lock:
            if self._closed:
                return
            stream = getattr(self, attribute)
            if stream is None:
                stream = (self.directory / filename).open("ab", buffering=0)
                setattr(self, attribute, stream)
            try:
                stream.write(data)
            except OSError:
                # Diagnostics must never become a second failure of the run.
                pass

    def close(self) -> None:
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            for attribute in ("_stdout", "_stderr"):
                stream = getattr(self, attribute)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                    setattr(self, attribute, None)
        handler = self._handler
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
            self._handler = None


def prune_run_logs(settings: Settings, *, retention_days: int | None = None) -> int:
    """Delete per-attempt log directories older than the retention window."""

    root = run_log_root(settings)
    if not root.is_dir():
        return 0
    keep_days = retention_days if retention_days is not None else _retention_days()
    cutoff = datetime.now(timezone.utc).astimezone().date().toordinal() - keep_days
    removed = 0
    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day.toordinal() >= cutoff:
            continue
        for path in sorted(day_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                continue
        try:
            day_dir.rmdir()
            removed += 1
        except OSError:
            continue
    return removed
