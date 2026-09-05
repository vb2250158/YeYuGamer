from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from yeyu_gamer_manager.store.sqlite_store import SqliteStore


class SqliteStoreLockingTests(unittest.TestCase):
    def test_concurrent_batch_projections_share_one_safe_sqlite_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore(Path(directory) / "manager.sqlite3")
            store.initialize()
            try:
                for index in range(32):
                    store.create_batch(
                        {
                            "cadence": "daily",
                            "mode": "plan",
                            "state": "planned",
                            "game_ids": ["WW"],
                            "requested_by": f"test-{index}",
                            "result": {},
                        }
                    )
                start = threading.Barrier(8)
                errors: list[BaseException] = []

                def read_batches() -> None:
                    try:
                        start.wait(timeout=5)
                        for _ in range(100):
                            self.assertEqual(len(store.list_batches()), 32)
                    except BaseException as error:  # captured by the parent test
                        errors.append(error)

                threads = [threading.Thread(target=read_batches) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse([thread for thread in threads if thread.is_alive()])
                self.assertEqual(errors, [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
