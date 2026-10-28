import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_adapter_protocol as fixtures


class DurableCancelTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.host = self.fixture.host_service
        self.plan = self.fixture.plan
        self.host._active_plan = self.plan
        self.host._active_process = SimpleNamespace(poll=lambda: None, stdin=mock.Mock())
        self.host._write_control_authority(self.plan)

    def cancel(self):
        return self.host.cancel(run_attempt_id=self.plan.run_attempt_id,
                                fencing_token=self.plan.fencing_token)

    def pending(self):
        return self.host._durable_control_path(self.plan.run_attempt_id)

    def test_unread_stdin_is_never_written_or_flushed(self):
        pipe = self.host._active_process.stdin
        pipe.write.side_effect = AssertionError("Cancel must not enter a blocking pipe write")
        pipe.flush.side_effect = AssertionError("Cancel must not enter a blocking pipe flush")
        self.assertTrue(self.cancel())
        pipe.write.assert_not_called()
        pipe.flush.assert_not_called()
        self.assertTrue(self.host._cancel_sent)
        self.assertTrue(self.pending().exists())

    def test_missing_stdin_does_not_disable_durable_cancellation(self):
        self.host._active_process.stdin = None
        self.assertTrue(self.cancel())
        self.assertTrue(self.pending().exists())
        self.assertTrue(self.host._cancel_sent)

    def test_runtime_is_derived_not_inherited(self):
        with mock.patch.dict(os.environ, {"YEYU_GAMER_RUNTIME_DIR": "untrusted-runtime"}):
            environment = self.host._host_environment()
        self.assertEqual(environment["YEYU_GAMER_RUNTIME_DIR"], str(self.host.runtime_dir.resolve()))
        self.assertNotIn("YEYU_GAMER_RUNTIME_DIR", self.host.HOST_ENV_ALLOWLIST)

    def test_cancel_file_is_scoped_authenticated_and_idempotent(self):
        self.assertTrue(self.cancel())
        payload = self.pending().read_bytes()
        document = json.loads(payload)
        self.assertEqual(document["runId"], self.plan.run_id)
        self.assertEqual(document["runAttemptId"], self.plan.run_attempt_id)
        self.assertNotIn(self.plan.fencing_token.encode(), payload)
        self.assertNotIn(self.plan.cancel_authority.encode(), payload)
        self.assertTrue(self.host._durable_control_is_authentic(self.pending(),
            run_id=self.plan.run_id, run_attempt_id=self.plan.run_attempt_id,
            cancel_authority=self.plan.cancel_authority, maximum_bytes=self.host.MAX_CONTROL_FILE_BYTES))
        self.assertFalse(self.cancel())
        self.assertEqual(self.pending().read_bytes(), payload)

    def test_wrong_owner_and_exited_owner_write_nothing(self):
        self.assertFalse(self.host.cancel(run_attempt_id=self.plan.run_attempt_id, fencing_token="wrong"))
        self.assertFalse(self.host.cancel(run_attempt_id="other", fencing_token=self.plan.fencing_token))
        self.host._active_process.poll = lambda: 0
        self.assertFalse(self.cancel())
        self.assertFalse(self.pending().exists())
        self.assertFalse(self.host._cancel_sent)

    def test_persistence_failure_does_not_mark_cancel_sent_or_release_owner(self):
        owner = self.host._active_process
        with mock.patch.object(self.host, "_atomic_write_private_file", side_effect=OSError("offline write failure")):
            with self.assertRaises(OSError):
                self.cancel()
        self.assertFalse(self.host._cancel_sent)
        self.assertIs(self.host._active_process, owner)
        self.assertFalse(self.pending().exists())
        self.assertTrue(self.cancel())

    def test_unsuccessful_persistence_does_not_mark_cancel_sent(self):
        with mock.patch.object(self.host, "_write_durable_cancel", return_value=False):
            self.assertFalse(self.cancel())
        self.assertFalse(self.host._cancel_sent)

    def test_real_saturated_unread_pipe_cannot_hold_cancel_or_execution_lock(self):
        process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(15)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(json.dumps({"owner": self.id(), "purpose": "offline-unread-stdin",
                          "pid": process.pid, "parentPid": os.getpid()}), file=sys.stderr)
        self.host._active_process = process
        writing = threading.Event()
        outcome = []

        def fill():
            writing.set()
            try:
                process.stdin.write(b"x" * 1048576)
            except (BrokenPipeError, OSError, ValueError):
                pass

        def cancel():
            try:
                outcome.append(self.cancel())
            except Exception as error:
                outcome.append(error)

        writer = threading.Thread(target=fill, daemon=True)
        canceller = threading.Thread(target=cancel, daemon=True)
        try:
            writer.start()
            self.assertTrue(writing.wait(1))
            writer.join(0.1)
            self.assertTrue(writer.is_alive())
            started = time.monotonic()
            canceller.start()
            canceller.join(2)
            self.assertFalse(canceller.is_alive())
            self.assertEqual(outcome, [True])
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(self.host._execution_lock.acquire(timeout=0.1))
            self.host._execution_lock.release()
            self.assertTrue(writer.is_alive())
            self.assertTrue(self.pending().exists())
            self.assertIs(self.host._active_process, process)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
            writer.join(2)
            if canceller.ident is not None:
                canceller.join(2)
            try:
                process.stdin.close()
            except OSError:
                pass
            self.assertFalse(writer.is_alive())
            self.assertFalse(canceller.is_alive())


if __name__ == "__main__":
    unittest.main()
