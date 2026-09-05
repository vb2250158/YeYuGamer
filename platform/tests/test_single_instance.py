from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from yeyu_gamer_platform.single_instance import SingleInstance


class SingleInstanceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows named mutex contract")
    def test_windows_named_mutex_allows_only_one_owner(self) -> None:
        name = f"Local\\YeYuGamer.PlatformTest.{uuid.uuid4()}"
        with tempfile.TemporaryDirectory() as temporary:
            first = SingleInstance(name, Path(temporary) / "first.lock")
            second = SingleInstance(name, Path(temporary) / "second.lock")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()

