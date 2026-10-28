import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_adapter_protocol as fixtures
from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget


class HostControlLockBudgetTests(unittest.TestCase):
    def test_blocked_cancel_file_writer_yields_control_without_claiming_delivery(self):
        fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        host = fixture.host_service
        host._active_plan = fixture.plan
        host._active_process = SimpleNamespace(pid=34567, poll=lambda: None, stdin=None)
        host._active_timeout_budget = load_queue_timeout_budget(
            {"queue_timeout_budget": {"host_stop_grace_seconds": 0.03}})
        release = threading.Event()
        entered = threading.Event()

        def blocked_write(**kwargs):
            entered.set()
            release.wait(3)
            return False

        try:
            started = time.monotonic()
            with mock.patch.object(host, "_write_durable_cancel", side_effect=blocked_write):
                self.assertFalse(host.cancel(run_attempt_id=fixture.plan.run_attempt_id,
                                             fencing_token=fixture.plan.fencing_token))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(entered.is_set())
            self.assertFalse(host._cancel_sent)
            self.assertTrue(host.active_execution()["cancelRequested"])
            self.assertTrue(host._cancel_writer.is_alive())
            self.assertTrue(host._execution_lock.acquire(timeout=0.1))
            host._execution_lock.release()
        finally:
            release.set()
            writer = getattr(host, "_cancel_writer", None)
            if writer is not None:
                writer.join(1)
                self.assertFalse(writer.is_alive())

    def test_contended_owner_lock_has_bounded_fail_closed_control_views(self):
        fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        host = fixture.host_service
        host._active_plan = fixture.plan
        owner = SimpleNamespace(pid=34567, poll=lambda: None)
        host._active_process = owner
        host._active_timeout_budget = load_queue_timeout_budget(
            {"queue_timeout_budget": {"host_stop_grace_seconds": 0.02}})
        ready = threading.Event()
        release = threading.Event()

        def hold():
            with host._execution_lock:
                ready.set()
                release.wait(3)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1))
        try:
            started = time.monotonic()
            self.assertFalse(host.cancel(run_attempt_id=fixture.plan.run_attempt_id,
                                         fencing_token=fixture.plan.fencing_token))
            self.assertEqual(host.cancel_run(run_id=fixture.plan.run_id), ("", False))
            self.assertTrue(host.active_execution()["ownershipUnknown"])
            self.assertIsNone(host.owned_execution_for(fixture.plan))
            self.assertFalse(host.release_owned_execution(fixture.plan, object(), object()))
            self.assertFalse(host.release_terminal_owner(fixture.plan.run_attempt_id))
            self.assertIs(host.active_plan_for_run(fixture.plan.run_id), fixture.plan)
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertIs(host._active_process, owner)
        finally:
            release.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
