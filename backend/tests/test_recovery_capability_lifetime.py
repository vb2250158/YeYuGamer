import gc
import unittest
import weakref
import threading

import test_input_recovery as fixtures

from yeyu_gamer_manager.services import input_recovery


class RecoveryCapabilityLifetimeTests(unittest.TestCase):
    def test_another_threads_sqlite_transaction_is_not_our_outer_transaction(self):
        fixture = fixtures.InputRecoveryTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        ready = threading.Event()
        release = threading.Event()

        def transaction():
            with fixture.store.atomic():
                ready.set()
                release.wait(2)

        thread = threading.Thread(target=transaction, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1))
        timer = threading.Timer(0.05, release.set)
        try:
            self.assertTrue(fixture.store.connection.in_transaction)
            self.assertFalse(fixture.store._lock._is_owned())
            timer.start()
            self.assertTrue(fixture.recover())
            self.assertEqual(fixture.store.get_run_attempt(fixture.plan.run_attempt_id)["state"], "review_required")
        finally:
            release.set()
            thread.join(1)
            if timer.ident is not None:
                timer.join(1)
            self.assertFalse(thread.is_alive())

    def test_manager_owned_capability_cycles_do_not_acquire_a_module_root(self):
        class Owner:
            pass

        owner = Owner()
        reference = weakref.ref(owner)
        pending = input_recovery._pending_for(owner, create=True)
        pending["scope"] = {"owner": owner}
        self.assertIs(input_recovery._pending_for(owner), pending)
        del owner, pending
        gc.collect()
        self.assertIsNone(reference())
