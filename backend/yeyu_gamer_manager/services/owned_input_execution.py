from __future__ import annotations

from contextlib import contextmanager
import math
import subprocess
import threading
import time

from .owned_host_job import OwnedHostJob, OwnedHostJobError


def _freeze(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("scope keys must be strings")
        return (dict, tuple(sorted((key, _freeze(item)) for key, item in value.items())))
    if type(value) in (list, tuple):
        return (type(value), tuple(_freeze(item) for item in value))
    if value is None or type(value) in (str, bool, int):
        return (type(value), value)
    if type(value) is float and math.isfinite(value):
        return (float, value.hex())
    raise ValueError("scope must contain finite plain values")


def _deadline(timeout_seconds):
    if (type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("positive finite timeout_seconds required")
    return time.monotonic() + timeout_seconds


def _thaw(frozen):
    kind, value = frozen
    if kind is dict:
        return {key: _thaw(item) for key, item in value}
    if kind in (list, tuple):
        return kind(_thaw(item) for item in value)
    if kind is float:
        return float.fromhex(value)
    return value


def _process_handle(process):
    handle = getattr(process, "_handle", None)
    if handle is None or isinstance(handle, bool):
        raise TypeError("exact native process handle required, not a PID")
    return int(handle)


class _QuietReceipt:
    __slots__ = ()


class OwnedInputExecution:
    """Native quiet owner, not a Store completion or command-launch API.

    Caller supplies the complete scope. Trusted lazy roots self-join job_name
    before reading stdin or creating helpers. No post-hoc PID adoption occurs.
    """

    def __init__(self, scope: dict):
        if type(scope) is not dict or not scope:
            raise ValueError("nonempty exact scope required")
        self._scope = _freeze(scope)
        self._condition = threading.Condition(threading.RLock())
        self._process = None
        self._candidate_process = None
        self._writer = None
        self._writer_start_failed = False
        self._writer_errors = []
        self._readers = []
        self._auxiliary_writers = []
        self._callbacks = 0
        self._stopping = False
        self._closed = False
        self._receipt = None
        self._forced_stop = False
        self._callbacks_sealed = False
        self._finalizer_started = False
        self._finalizer_thread = None
        self._finalizer_active = False
        self._job = OwnedHostJob()

    @property
    def scope(self) -> dict:
        return _thaw(self._scope)

    @property
    def job_name(self) -> str:
        return self._job.name

    @property
    def last_receipt(self) -> _QuietReceipt | None:
        with self._condition:
            return self._receipt

    @property
    def forced_stop(self) -> bool:
        with self._condition:
            return self._forced_stop

    def matches(self, scope: dict) -> bool:
        try:
            return type(scope) is dict and _freeze(scope) == self._scope
        except (ValueError, TypeError, RecursionError):
            return False

    def _acquire(self, deadline):
        if not self._condition.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("execution lock deadline")

    def _require_active(self):
        if self._closed or self._stopping:
            raise OwnedHostJobError("execution_stopping_or_closed")

    def bind(self, process: subprocess.Popen, timeout_seconds: float) -> None:
        deadline = _deadline(timeout_seconds)
        handle = _process_handle(process)
        self._acquire(deadline)
        try:
            self._require_active()
            if self._process is not None:
                raise OwnedHostJobError("execution_already_bound")
            if self._candidate_process is not None and process is not self._candidate_process:
                raise OwnedHostJobError("startup_root_changed")
            self._job._pin_startup_root_until(handle, deadline)
            self._candidate_process = process
            if time.monotonic() >= deadline or not self._job._wait_for_member_until(handle, deadline):
                raise TimeoutError("native member not confirmed")
            self._process = process
        finally:
            self._condition.release()

    def abort_before_dispatch(self, process, timeout_seconds: float) -> _QuietReceipt | None:
        """Stop an exact startup root and this Job without adopting by PID.

        Valid only before any writer starts. Failure retains all native handles;
        callers preserve the original startup exception and retain this context.
        """
        deadline = _deadline(timeout_seconds)
        try:
            self._acquire(deadline)
        except TimeoutError:
            return None
        try:
            if self._writer is not None:
                raise OwnedHostJobError("dispatch_already_attempted")
            if self._candidate_process is not None and process is not self._candidate_process:
                raise OwnedHostJobError("startup_root_changed")
            if self._receipt is not None:
                return self._receipt
            if self._closed or self._readers or self._callbacks:
                return None
            self._stopping = True
            self._callbacks_sealed = True
            self._forced_stop = True
            if self._candidate_process is None:
                self._job._pin_startup_root_until(_process_handle(process), deadline)
                self._candidate_process = process
            if not self._job._abort_startup_until(deadline):
                return None
            self._receipt = _QuietReceipt()
            return self._receipt
        except (OwnedHostJobError, TimeoutError):
            return None
        finally:
            self._condition.release()

    def dispatch(self, payload: str | bytes, timeout_seconds: float) -> None:
        deadline = _deadline(timeout_seconds)
        self._acquire(deadline)
        try:
            self._require_active()
            if self._process is None or self._writer is not None:
                raise OwnedHostJobError("unbound_or_already_dispatched")
            if self._process.stdin is None:
                raise OwnedHostJobError("stdin_unavailable")
            if type(payload) not in (str, bytes) or isinstance(payload, str) != bool(self._process.text_mode):
                raise TypeError("serialized payload must match Popen text mode")
            if time.monotonic() >= deadline:
                raise TimeoutError("stdin dispatch deadline before write")
            pipe = self._process.stdin

            def write():
                try:
                    if pipe.write(payload) != len(payload):
                        raise OSError("incomplete stdin write")
                    pipe.flush()
                except BaseException as error:
                    self._writer_errors.append(error)

            self._writer = threading.Thread(target=write, name="owned-input-writer", daemon=True)
            try:
                self._writer.start()
            except BaseException:
                self._writer_start_failed = self._writer.ident is None
                self._stopping = True
                raise
            writer = self._writer
        finally:
            self._condition.release()
        writer.join(max(0, deadline - time.monotonic()))
        if writer.is_alive():
            with self._condition:
                self._stopping = True
                self._forced_stop = True
            self._job.terminate()
            writer.join(max(0, deadline - time.monotonic()))
            raise TimeoutError("stdin writer deadline; stop_and_drain required")
        if self._writer_errors:
            with self._condition:
                self._stopping = True
                self._forced_stop = True
            failure = OwnedHostJobError("stdin_write_failed")
            try:
                self._job.terminate()
            except OwnedHostJobError as stop_error:
                failure.add_note(str(stop_error))
            raise failure from self._writer_errors[0]

    def register_reader(self, thread: threading.Thread) -> None:
        """Register every reader before starting it (or before exposing callbacks)."""
        if not isinstance(thread, threading.Thread):
            raise TypeError("reader must be a Thread")
        with self._condition:
            self._require_active()
            if thread is threading.current_thread():
                raise ValueError("reader must not be current thread")
            if thread in self._readers:
                raise ValueError("reader already registered")
            self._readers.append(thread)

    def start_writer(self, thread: threading.Thread) -> None:
        if not isinstance(thread, threading.Thread) or thread is threading.current_thread():
            raise TypeError("external writer Thread required")
        with self._condition:
            self._require_active()
            if thread in self._auxiliary_writers:
                raise ValueError("writer already registered")
            self._auxiliary_writers.append(thread)
            try:
                thread.start()
            except BaseException:
                if thread.ident is None:
                    self._auxiliary_writers.remove(thread)
                raise

    @contextmanager
    def event_callback(self):
        with self._condition:
            if (self._closed or self._callbacks_sealed or (self._stopping
                    and threading.current_thread() not in self._readers)):
                raise OwnedHostJobError("callback_gate_closed")
            self._callbacks += 1
        try:
            yield
        finally:
            with self._condition:
                self._callbacks -= 1
                self._condition.notify_all()

    @contextmanager
    def finalization_callback(self):
        """Admit the complete production completion callback once, after quiet.

        Only this finalizer thread may verify and hand off while it is active.
        Recovery callers must drain it within their own stop deadline.
        """
        with self._condition:
            if self._closed or self._receipt is None or self._finalizer_started:
                raise OwnedHostJobError("finalizer_gate_closed")
            self._finalizer_started = True
            self._finalizer_thread = threading.current_thread()
            self._finalizer_active = True
        try:
            yield
        finally:
            with self._condition:
                self._finalizer_active = False
                self._condition.notify_all()

    def _receipt_after_finalizer(self, deadline):
        while self._finalizer_active:
            if self._finalizer_thread is threading.current_thread():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._condition.wait(remaining)
        return self._receipt if time.monotonic() < deadline else None

    def stop_and_drain(self, timeout_seconds: float) -> _QuietReceipt | None:
        """Seal external callbacks, drain registered readers, then seal all callbacks.

        None retains this owner for retry. All waits share the caller deadline;
        native syscall latency cannot be bounded by a Python timeout.
        """
        deadline = _deadline(timeout_seconds)
        try:
            self._acquire(deadline)
        except TimeoutError:
            return None
        try:
            if self._receipt is not None:
                return self._receipt_after_finalizer(deadline)
            self._stopping = True
            if self._closed or self._process is None:
                return None
            threads = ([self._writer] if self._writer is not None
                       and not self._writer_start_failed else []) + list(self._auxiliary_writers) + list(self._readers)
        finally:
            self._condition.release()
        try:
            observation = self._job._observe_until(deadline)
            if observation.active_processes or not observation.root_signaled:
                with self._condition:
                    self._forced_stop = True
            if time.monotonic() >= deadline or not self._job._stop_and_wait_until(deadline):
                return None
            for thread in threads:
                if thread is threading.current_thread():
                    return None
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    return None
            self._acquire(deadline)
            try:
                self._callbacks_sealed = True
                while self._callbacks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                if time.monotonic() >= deadline:
                    return None
                if self._receipt is None:
                    self._receipt = _QuietReceipt()
                return self._receipt_after_finalizer(deadline)
            finally:
                self._condition.release()
        except (OwnedHostJobError, TimeoutError, RuntimeError):
            return None

    def verify_receipt(self, receipt, scope: dict) -> bool:
        with self._condition:
            return (self._receipt is not None and receipt is self._receipt and self.matches(scope)
                    and (not self._finalizer_active
                         or self._finalizer_thread is threading.current_thread()))

    def close(self) -> None:
        with self._condition:
            if self._finalizer_active and self._finalizer_thread is not threading.current_thread():
                raise OwnedHostJobError("finalizer_inflight")
            if self._closed:
                return
            if self._receipt is None:
                if (self._candidate_process is not None or self._process is not None or self._writer is not None
                        or self._auxiliary_writers or self._readers or self._callbacks):
                    raise OwnedHostJobError("quiet_receipt_required")
                self._stopping = True
                self._callbacks_sealed = True
            self._job.close()
            self._closed = True
