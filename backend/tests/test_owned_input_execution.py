import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from yeyu_gamer_manager.services.owned_input_execution import OwnedInputExecution
from yeyu_gamer_manager.services.owned_host_job import OwnedHostJobError, _WindowsApi


@unittest.skipUnless(os.name == "nt", "native Windows qualification")
class OwnedInputExecutionTests(unittest.TestCase):
    def setUp(self):
        self.scope = dict(runId="run", runAttemptId="attempt", leaseId="lease", generation=1,
                          member="member", desktop="desktop", game="fixture", account="fixture",
                          packageDigest="fixture-digest")
        self.contexts = []
        self.processes = []
        self.handles = []
        self.threads = []
        self.releases = []
        self.api = _WindowsApi()

    def tearDown(self):
        for release in self.releases:
            release.set()
        for context in self.contexts:
            if context._process is not None:
                self.assertIsNotNone(context.stop_and_drain(5))
                context.close()
            elif context._candidate_process is not None:
                self.assertIsNotNone(context.abort_before_dispatch(context._candidate_process, 5))
                context.close()
            else:
                context._job.close()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    pipe.close()
        for handle in self.handles:
            self.assertEqual(self.api.kernel.WaitForSingleObject(handle, 5000), 0)
            self.api.close(handle)
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def context(self):
        context = OwnedInputExecution(self.scope)
        self.contexts.append(context)
        return context

    def spawn(self, context, mode="normal"):
        current = self.api.kernel.GetCurrentProcess()
        parent = wintypes.HANDLE()
        self.api.check(self.api.kernel.DuplicateHandle(
            current, current, current, ctypes.byref(parent), 0, True, 2), "fixture.Parent")
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [parent.value]}
        environment = dict(os.environ, PRIVATE_OWNED_JOB=context.job_name)
        try:
            process = subprocess.Popen(
                [sys._base_executable, "-B", "-u", str(Path(__file__).with_name("owned_input_fixture.py")),
                 str(parent.value), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment, startupinfo=startup,
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        finally:
            self.api.close(parent.value)
        self.processes.append(process)
        print(json.dumps(dict(owner=self.id(), pid=process.pid, parentPid=os.getpid(),
                              purpose=mode, source="owned_input_fixture.py", log="unittest-output")))
        return process

    def line(self, process):
        received = queue.Queue()
        thread = threading.Thread(target=lambda: received.put(process.stdout.readline()), daemon=True)
        self.threads.append(thread)
        thread.start()
        return json.loads(received.get(timeout=5))

    def bound(self):
        context = self.context()
        process = self.spawn(context)
        context.bind(process, 5)
        self.assertEqual(self.line(process)["phase"], "bootstrap")
        return context, process

    def children(self):
        context, process = self.bound()
        context.dispatch("GO\n", 2)
        record = self.line(process)
        for child in record["children"]:
            self.handles.append(child["handle"])
            print(json.dumps(dict(owner=self.id(), purpose="owned-helper", source="owned_input_fixture.py",
                                  log="unittest-output", **child)))
        self.assertGreaterEqual(context._job.observe().active_processes, 3)
        return context, process

    def test_hung_root_helpers_and_duplicate_stop(self):
        context, process = self.children()
        with self.assertRaises(OwnedHostJobError):
            context.close()
        receipt = context.stop_and_drain(5)
        self.assertTrue(context.verify_receipt(receipt, self.scope))
        self.assertIs(receipt, context.last_receipt)
        self.assertIs(receipt, context.stop_and_drain(1))
        self.assertTrue(context.forced_stop)
        process.wait(5)

    def test_killed_root_does_not_hide_helpers(self):
        context, process = self.children()
        process.kill()
        process.wait(5)
        self.assertGreaterEqual(context._job.observe().active_processes, 2)
        self.assertIsNotNone(context.stop_and_drain(5))
        self.assertTrue(context.forced_stop)

    def test_fake_receipt_scope_mutation_and_aba(self):
        context, _ = self.bound()
        changed = dict(self.scope, generation=True)
        self.assertFalse(context.matches(changed))
        original = dict(self.scope)
        self.scope["generation"] = 2
        self.assertTrue(context.matches(original))
        receipt = context.stop_and_drain(5)
        for fake in (None, {}, {"quiet": True}, object(), type(receipt)()):
            self.assertFalse(context.verify_receipt(fake, original))
        other = self.context()
        self.assertFalse(other.verify_receipt(receipt, original))
        self.assertFalse(context.verify_receipt(receipt, self.scope))
        self.assertTrue(context.verify_receipt(receipt, original))

    def test_dispatch_once_and_bind_once(self):
        context, process = self.bound()
        context.dispatch("GO\n", 2)
        record = self.line(process)
        self.handles.extend(child["handle"] for child in record["children"])
        with self.assertRaises(OwnedHostJobError):
            context.dispatch("GO\n", 1)
        with self.assertRaises(OwnedHostJobError):
            context.bind(process, 1)

    def test_unbound_and_nonmember_fail_closed_external_preserved(self):
        context = self.context()
        with self.assertRaises(OwnedHostJobError):
            context.dispatch("GO\n", 1)
        process = self.spawn(context, "no-join")
        self.line(process)
        with self.assertRaises(TimeoutError):
            context.bind(process, 0.05)
        self.assertIsNone(context.stop_and_drain(0.05))
        self.assertIsNone(process.poll())
        owned, _ = self.children()
        self.assertIsNotNone(owned.stop_and_drain(5))
        self.assertIsNone(process.poll())

    def test_blocking_native_writer_stops_job_before_join(self):
        context = self.context()
        process = self.spawn(context, "no-read")
        context.bind(process, 5)
        self.line(process)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            context.dispatch("X" * (4 * 1024 * 1024), 0.05)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(context.forced_stop)
        self.assertIsNotNone(context.stop_and_drain(5))
        self.assertFalse(context._writer.is_alive())
        process.wait(5)

    def blocked_thread(self, target=None):
        release = threading.Event()
        entered = threading.Event()
        self.releases.append(release)
        def run():
            if target:
                target(entered, release)
            else:
                entered.set()
                release.wait(5)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        return thread, entered, release

    def test_reader_drain_timeout_retains_owner_and_retries(self):
        context, _ = self.bound()
        thread, entered, release = self.blocked_thread()
        context.register_reader(thread)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertIsNone(context.stop_and_drain(0.05))
        self.assertIsNone(context.last_receipt)
        with self.assertRaises(OwnedHostJobError):
            context.close()
        release.set()
        self.assertIsNotNone(context.stop_and_drain(5))

    def test_inflight_callback_timeout_and_late_callback_rejected(self):
        context, _ = self.bound()
        def callback(entered, release):
            with context.event_callback():
                entered.set()
                release.wait(5)
        thread, entered, release = self.blocked_thread(callback)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertIsNone(context.stop_and_drain(0.05))
        with self.assertRaises(OwnedHostJobError), context.event_callback():
            self.fail("late external callback admitted")
        release.set()
        self.assertIsNotNone(context.stop_and_drain(5))

    def test_normal_exit_registered_reader_can_callback_while_draining(self):
        context, process = self.bound()
        context.dispatch("EXIT\n", 2)
        process.wait(5)
        callback_done = threading.Event()
        def reader(entered, release):
            entered.set()
            release.wait(5)
            with context.event_callback():
                callback_done.set()
        thread, entered, release = self.blocked_thread(reader)
        context.register_reader(thread)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertIsNone(context.stop_and_drain(0.05))
        release.set()
        self.assertIsNotNone(context.stop_and_drain(5))
        self.assertTrue(callback_done.is_set())
        self.assertFalse(context.forced_stop)
        with self.assertRaises(OwnedHostJobError), context.event_callback():
            self.fail("sealed callback admitted")

    def test_close_failure_retains_handle_and_retries(self):
        context, _ = self.bound()
        receipt = context.stop_and_drain(5)
        original = context._job._api.close
        for fail_index in (1, 2):
            calls = []
            def close(handle):
                calls.append(handle)
                if len(calls) == fail_index:
                    raise OwnedHostJobError("fixture.close_failure")
                original(handle)
            if fail_index == 2 and context._job._root_handle is None:
                continue
            with mock.patch.object(context._job._api, "close", side_effect=close):
                with self.assertRaises(OwnedHostJobError):
                    context.close()
            self.assertFalse(context._closed)
            self.assertTrue(context.verify_receipt(receipt, self.scope))
        context.close()
        context.close()

    def test_no_native_support_and_invalid_scope_fail_before_job_creation(self):
        with mock.patch("yeyu_gamer_manager.services.owned_input_execution.OwnedHostJob") as factory:
            for scope in ({}, {"bad": object()}, {"bad": float("nan")}):
                with self.assertRaises(ValueError):
                    OwnedInputExecution(scope)
            factory.assert_not_called()
        with mock.patch("yeyu_gamer_manager.services.owned_host_job.os.name", "posix"):
            with self.assertRaises(OSError):
                OwnedInputExecution(self.scope)

    def test_real_stdout_reader_drains_after_normal_exit(self):
        context, process = self.bound()
        events = []
        def read():
            for line in process.stdout:
                with context.event_callback():
                    events.append(line)
        reader = threading.Thread(target=read, daemon=True)
        self.threads.append(reader)
        context.register_reader(reader)
        reader.start()
        context.dispatch("EXIT\n", 2)
        process.wait(5)
        self.assertIsNotNone(context.stop_and_drain(5))
        self.assertFalse(reader.is_alive())
        self.assertFalse(context.forced_stop)

    def test_native_failure_keeps_context_without_receipt(self):
        context, process = self.bound()
        with mock.patch.object(context._job._api.kernel, "TerminateJobObject", return_value=0):
            self.assertIsNone(context.stop_and_drain(0.05))
        self.assertIsNone(context.last_receipt)
        self.assertFalse(context._closed)
        self.assertIsNone(process.poll())
        self.assertIsNotNone(context.stop_and_drain(5))

    def test_shared_deadline_bounds_multiple_reader_joins(self):
        context, _ = self.bound()
        for _ in range(2):
            thread, entered, _ = self.blocked_thread()
            context.register_reader(thread)
            thread.start()
            self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertIsNone(context.stop_and_drain(0.08))
        self.assertLess(time.monotonic() - started, 0.15)

    def test_concurrent_stops_share_one_real_receipt(self):
        context, _ = self.bound()
        receipts = []
        threads = [threading.Thread(target=lambda: receipts.append(context.stop_and_drain(5)))
                   for _ in range(2)]
        self.threads.extend(threads)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(receipts), 2)
        self.assertIsNotNone(receipts[0])
        self.assertIs(receipts[0], receipts[1])

    def test_reject_pid_fake_process_and_bad_native_handle(self):
        context = self.context()
        for process in (123, object()):
            with self.assertRaises(TypeError):
                context.bind(process, 0.05)
        process = self.spawn(context, "no-join")
        self.line(process)
        with mock.patch.object(process, "_handle", 0x12345678):
            with self.assertRaises(OwnedHostJobError):
                context.bind(process, 0.05)
        self.assertIsNone(context.last_receipt)
        self.assertIsNone(process.poll())

    def test_empty_owner_can_close_without_fabricating_receipt(self):
        context = self.context()
        context.close()
        self.assertIsNone(context.last_receipt)
        self.assertFalse(context.verify_receipt(None, self.scope))

    def test_scope_property_returns_independent_deep_plain_dict(self):
        self.scope["extra"] = {"items": [1, {"key": "value"}]}
        context = self.context()
        snapshot = context.scope
        self.assertIs(type(snapshot), dict)
        snapshot["extra"]["items"][1]["key"] = "changed"
        snapshot["runId"] = "changed"
        self.assertTrue(context.matches(self.scope))
        self.assertFalse(context.matches(snapshot))
        self.assertEqual(context.scope, self.scope)

    def test_patched_popen_factory_returning_real_process_binds_native_handle(self):
        real_popen = subprocess.Popen
        with mock.patch("yeyu_gamer_manager.services.adapter_host.subprocess.Popen", side_effect=real_popen):
            context, process = self.bound()
            self.assertIsNotNone(context.stop_and_drain(5))
            process.wait(5)

    def test_abort_nonjoining_root_after_bind_failure_no_adoption(self):
        context = self.context()
        process = self.spawn(context, "no-join")
        self.line(process)
        with self.assertRaises(TimeoutError):
            context.bind(process, 0.03)
        self.assertIsNone(context._job._root_handle)
        receipt = context.abort_before_dispatch(process, 5)
        self.assertTrue(context.verify_receipt(receipt, self.scope))
        self.assertIs(receipt, context.abort_before_dispatch(process, 1))
        self.assertIsNone(context._job._root_handle)
        process.wait(5)
        context.close()

    def test_abort_joined_root_before_bind_and_preserve_external_client(self):
        context = self.context()
        process = self.spawn(context)
        self.line(process)
        outside_context = self.context()
        outside = self.spawn(outside_context, "no-join")
        self.line(outside)
        self.assertIsNotNone(context.abort_before_dispatch(process, 5))
        self.assertIsNone(outside.poll())
        self.assertIsNone(context._job._root_handle)
        context.close()

    def test_abort_timeout_and_close_retry_retains_precise_handle(self):
        context = self.context()
        process = self.spawn(context, "no-join")
        self.line(process)
        with self.assertRaises(TimeoutError):
            context.bind(process, 0.03)
        handle = context._job._startup_handle
        with mock.patch.object(context._job._api.kernel, "TerminateProcess", return_value=0):
            self.assertIsNone(context.abort_before_dispatch(process, 0.05))
        self.assertEqual(context._job._startup_handle, handle)
        self.assertIsNone(context.last_receipt)
        self.assertIsNone(process.poll())
        self.assertIsNotNone(context.abort_before_dispatch(process, 5))
        with mock.patch.object(context._job._api, "close", side_effect=OwnedHostJobError("fixture.close")):
            with self.assertRaises(OwnedHostJobError):
                context.close()
        self.assertFalse(context._closed)
        self.assertEqual(context._job._startup_handle, handle)
        context.close()

    def test_abort_refuses_half_write_and_drain_handles_it(self):
        context, process = self.bound()
        pipe = process.stdin
        class PartialWrite:
            def write(self, payload):
                written = pipe.write(payload[:1])
                pipe.flush()
                return written
            def flush(self):
                pipe.flush()
        process.stdin = PartialWrite()
        try:
            with self.assertRaises(OwnedHostJobError):
                context.dispatch("GO\n", 1)
            self.assertTrue(context.forced_stop)
            self.assertIsNone(context.abort_before_dispatch(process, 1))
            self.assertIsNotNone(context.stop_and_drain(5))
        finally:
            process.stdin = pipe

    def test_writer_thread_start_failure_still_drains_and_closes(self):
        context, _ = self.bound()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("fixture.start")):
            with self.assertRaises(RuntimeError):
                context.dispatch("GO\n", 1)
        self.assertIsNotNone(context.stop_and_drain(5))
        context.close()

    def test_all_three_close_handle_failures_can_retry(self):
        for failure_index in (1, 2, 3):
            context, _ = self.bound()
            self.assertIsNotNone(context.stop_and_drain(5))
            original = context._job._api.close
            calls = []
            def close(handle):
                calls.append(handle)
                if len(calls) == failure_index:
                    raise OwnedHostJobError("fixture.close")
                original(handle)
            with mock.patch.object(context._job._api, "close", side_effect=close):
                with self.assertRaises(OwnedHostJobError):
                    context.close()
            self.assertFalse(context._closed)
            context.close()

    def test_native_compiled_bootstrap_abort_and_invalid_request(self):
        root = Path(__file__).resolve().parents[2]
        (root / ".cache").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="owned-native-", dir=root / ".cache") as directory:
            executable = Path(directory) / "host.exe"
            compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
            compiled = subprocess.run([str(compiler), "/nologo", "/target:winexe",
                "/reference:System.Web.Extensions.dll", "/out:" + str(executable),
                str(root / "adapter-host/YeYuGamerAdapterHost.cs")], capture_output=True,
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(compiled.returncode, 0, compiled.stdout.decode(errors="replace"))
            for abort in (True, False):
                context = self.context()
                process = subprocess.Popen([str(executable), "--operation", "execute",
                    "--protocol-version", "1.1", "--run-id", str(uuid.uuid4()), "--game-id", "StarRail"],
                    env=dict(os.environ, YEYU_GAMER_OWNED_INPUT_JOB=context.job_name),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                self.processes.append(process)
                print(json.dumps(dict(owner=self.id(), source=str(executable), pid=process.pid,
                    parentPid=os.getpid(), purpose="compiled-native-bootstrap", log="unittest-output")))
                context.bind(process, 3)
                self.assertIsNone(process.poll())
                if abort:
                    self.assertIsNotNone(context.abort_before_dispatch(process, 3))
                else:
                    context.dispatch(b"invalid-json\n", 2)
                    process.wait(3)
                    self.assertEqual(process.returncode, 64)
                    self.assertIsNotNone(context.stop_and_drain(3))
                    self.assertFalse(context.forced_stop)
                process.wait(3)
                context.close()


    def test_finalizer_hang_blocks_recovery_receipt_until_exit(self):
        context, _ = self.bound()
        receipt = context.stop_and_drain(5)
        def finalize(entered, release):
            with context.finalization_callback():
                entered.set()
                release.wait(5)
        thread, entered, release = self.blocked_thread(finalize)
        thread.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertIsNone(context.stop_and_drain(0.05))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(context.verify_receipt(receipt, self.scope))
        with self.assertRaises(OwnedHostJobError):
            context.close()
        release.set()
        self.assertIs(receipt, context.stop_and_drain(5))
        self.assertTrue(context.verify_receipt(receipt, self.scope))

    def test_finalizer_thread_alone_can_verify_and_close_for_handoff(self):
        context, _ = self.bound()
        receipt = context.stop_and_drain(5)
        observations = []
        with context.finalization_callback():
            self.assertTrue(context.verify_receipt(receipt, self.scope))
            self.assertIsNone(context.stop_and_drain(0.05))
            def outsider():
                observations.append(context.verify_receipt(receipt, self.scope))
                try:
                    context.close()
                except OwnedHostJobError:
                    observations.append("close_rejected")
                try:
                    with context.finalization_callback():
                        observations.append("unexpected_entry")
                except OwnedHostJobError:
                    observations.append("entry_rejected")
            thread = threading.Thread(target=outsider)
            self.threads.append(thread)
            thread.start()
            thread.join(2)
            self.assertEqual(observations, [False, "close_rejected", "entry_rejected"])
            context.close()
            self.assertTrue(context.verify_receipt(receipt, self.scope))
        self.assertIs(receipt, context.stop_and_drain(1))
        with self.assertRaises(OwnedHostJobError), context.finalization_callback():
            self.fail("duplicate finalizer admitted")

    def test_recovery_close_prevents_late_finalizer_body(self):
        context, _ = self.bound()
        with self.assertRaises(OwnedHostJobError), context.finalization_callback():
            self.fail("nonquiet finalizer admitted")
        receipt = context.stop_and_drain(5)
        self.assertTrue(context.verify_receipt(receipt, self.scope))
        context.close()
        entered = []
        with self.assertRaises(OwnedHostJobError), context.finalization_callback():
            entered.append(True)
        self.assertEqual(entered, [])

    def test_finalizer_exception_drains_but_does_not_allow_second_callback(self):
        context, _ = self.bound()
        receipt = context.stop_and_drain(5)
        with self.assertRaisesRegex(RuntimeError, "fixture.finalizer"):
            with context.finalization_callback():
                raise RuntimeError("fixture.finalizer")
        self.assertIs(receipt, context.stop_and_drain(1))
        with self.assertRaises(OwnedHostJobError), context.finalization_callback():
            self.fail("finalizer retry admitted")


if __name__ == "__main__":
    unittest.main()
