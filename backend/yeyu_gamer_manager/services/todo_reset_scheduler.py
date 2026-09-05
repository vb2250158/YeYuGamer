from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable


class TodoResetScheduler:
    """Wake the Manager at Todo period boundaries.

    Period calculation and reconciliation remain Manager responsibilities.  The
    scheduler owns no domain state and never executes an Adapter; it only asks
    the Manager to materialize the newly-current Todo instances after a frozen
    period boundary has passed.
    """

    def __init__(
        self,
        *,
        next_boundary: Callable[[datetime], datetime | None],
        reconcile: Callable[[datetime], None],
        on_error: Callable[[Exception], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        maximum_poll_seconds: float = 30.0,
    ) -> None:
        if maximum_poll_seconds <= 0:
            raise ValueError("maximum_poll_seconds must be positive")
        self._next_boundary = next_boundary
        self._reconcile = reconcile
        self._on_error = on_error
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._maximum_poll_seconds = maximum_poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="yeyu-gamer-todo-reset",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run(self) -> None:
        scheduled: datetime | None = None
        last_processed: datetime | None = None
        while not self._stop.is_set():
            now = self._aware_utc(self._clock())
            if scheduled is not None and now >= scheduled:
                try:
                    self._reconcile(now)
                except Exception as error:  # keep the lifecycle worker alive
                    if self._on_error is not None:
                        self._on_error(error)
                    self._stop.wait(self._maximum_poll_seconds)
                else:
                    last_processed = scheduled
                    scheduled = None
                continue

            candidate = self._next_boundary(now)
            if candidate is not None:
                candidate = self._aware_utc(candidate)
                if last_processed is not None and candidate <= last_processed:
                    candidate = None
                # Runtime configuration may move the next boundary earlier.
                # Never replace an already-observed boundary with a later one:
                # at the instant of rollover period calculation already points
                # at tomorrow, but the boundary we waited for is still due.
                if scheduled is None or candidate < scheduled:
                    scheduled = candidate

            if scheduled is None:
                self._stop.wait(self._maximum_poll_seconds)
                continue
            delay = (scheduled - now).total_seconds()
            if delay > 0:
                self._stop.wait(min(delay, self._maximum_poll_seconds))
                continue

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
