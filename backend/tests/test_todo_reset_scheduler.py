from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from yeyu_gamer_manager.services.todo_reset_scheduler import TodoResetScheduler


class TodoResetSchedulerTests(unittest.TestCase):
    def test_reconciles_only_after_boundary_and_stops(self) -> None:
        now = datetime(2026, 8, 27, 19, 59, 59, tzinfo=timezone.utc)
        boundary = now + timedelta(milliseconds=30)
        calls: list[datetime] = []
        called = threading.Event()

        def next_boundary(current: datetime) -> datetime:
            if calls:
                return current + timedelta(days=1)
            return boundary

        def reconcile(current: datetime) -> None:
            calls.append(current)
            called.set()

        scheduler = TodoResetScheduler(
            next_boundary=next_boundary,
            reconcile=reconcile,
            maximum_poll_seconds=0.01,
        )
        scheduler.start()
        self.assertTrue(called.wait(1.0))
        scheduler.stop()

        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], boundary)
        self.assertFalse(scheduler.running)

    def test_no_boundary_is_idle_and_start_is_idempotent(self) -> None:
        calls: list[datetime] = []
        scheduler = TodoResetScheduler(
            next_boundary=lambda _now: None,
            reconcile=calls.append,
            maximum_poll_seconds=0.01,
        )
        scheduler.start()
        thread = scheduler._thread
        scheduler.start()
        time.sleep(0.04)
        scheduler.stop()

        self.assertIsNotNone(thread)
        self.assertEqual(calls, [])
        self.assertFalse(scheduler.running)

    def test_reconcile_failure_is_reported_and_retried(self) -> None:
        boundary = datetime.now(timezone.utc) - timedelta(seconds=1)
        attempts = 0
        errors: list[str] = []
        succeeded = threading.Event()

        def reconcile(_current: datetime) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic reset failure")
            succeeded.set()

        scheduler = TodoResetScheduler(
            next_boundary=lambda _now: boundary,
            reconcile=reconcile,
            on_error=lambda error: errors.append(type(error).__name__),
            maximum_poll_seconds=0.01,
        )
        scheduler.start()
        self.assertTrue(succeeded.wait(1.0))
        scheduler.stop()

        self.assertEqual(attempts, 2)
        self.assertEqual(errors, ["RuntimeError"])

    def test_rejects_invalid_poll_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            TodoResetScheduler(
                next_boundary=lambda _now: None,
                reconcile=lambda _now: None,
                maximum_poll_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
