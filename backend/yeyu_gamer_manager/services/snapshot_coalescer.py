"""Share only an in-flight read; the next request always computes fresh state."""
from concurrent.futures import Future
from threading import Lock
from typing import Callable, TypeVar

T = TypeVar("T")


class SnapshotCoalescer:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: Future | None = None

    def run(self, build: Callable[[], T]) -> T:
        with self._lock:
            pending = self._pending
            owner = pending is None
            if owner:
                pending = Future()
                self._pending = pending
        assert pending is not None
        if owner:
            try:
                pending.set_result(build())
            except BaseException as error:
                pending.set_exception(error)
            finally:
                with self._lock:
                    self._pending = None
        # Exceptions reach every waiting reader. No completed result is cached.
        return pending.result()
