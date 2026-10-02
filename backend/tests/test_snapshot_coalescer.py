from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from yeyu_gamer_manager.services.snapshot_coalescer import SnapshotCoalescer


class SnapshotCoalescerTests(unittest.TestCase):
    def exercise(self, fail=False):
        entered, release, waiting = Event(), Event(), Event()
        calls = []

        class ObservedFuture(Future):
            def result(self, timeout=None):
                if not self.done():
                    waiting.set()
                return super().result(timeout)

        def build():
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            if fail:
                raise ValueError("projection failed")
            return len(calls)

        coalescer = SnapshotCoalescer()
        with patch('yeyu_gamer_manager.services.snapshot_coalescer.Future', ObservedFuture):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(coalescer.run, build)
                self.assertTrue(entered.wait(5))
                second = pool.submit(coalescer.run, build)
                try:
                    self.assertTrue(waiting.wait(5))
                finally:
                    release.set()
                for result in (first, second):
                    if fail:
                        with self.assertRaisesRegex(ValueError, 'projection failed'):
                            result.result(5)
                    else:
                        self.assertEqual(result.result(5), 1)
        self.assertEqual(len(calls), 1)
        # A later call must observe new state, including after a failed build.
        self.assertEqual(coalescer.run(lambda: 'fresh'), 'fresh')
        self.assertEqual(coalescer.run(lambda: 'newer'), 'newer')

    def test_concurrent_readers_share_work_without_persistent_cache(self):
        self.exercise()

    def test_failure_reaches_waiters_and_does_not_poison_next_request(self):
        self.exercise(fail=True)


if __name__ == '__main__':
    unittest.main()
