from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock


def inert_host():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.OpenJobObjectW(1, False, os.environ.pop("YEYU_GAMER_OWNED_INPUT_JOB"))
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(job)
    plan = json.loads(sys.stdin.buffer.readline())
    hello = {name: plan[name] for name in ("runId", "runAttemptId", "fencingToken", "gameId")}
    hello.update(schemaVersion=1, protocolVersion="1.1", eventType="hello", sequence=0,
                 at=datetime.now(timezone.utc).isoformat(), packageId="legacy-night-rain-gamer",
                 packageVersion=sys.argv[3], packageDigest=sys.argv[4], runnerPid=os.getpid(),
                 acceptedTodoInstanceIds=plan["executableTodoInstanceIds"])
    print(json.dumps(hello), flush=True)
    children = [subprocess.Popen(
        [sys.executable, "-B", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True) for index in range(2)]
    record = dict(rootPid=os.getpid(), parentPid=os.getppid(),
                  children=[dict(pid=child.pid, parentPid=os.getpid()) for child in children],
                  inputEffects=0, requestReadAfterNativeJoin=True)
    destination = Path(sys.argv[2])
    staging = destination.with_suffix(".pending")
    staging.write_text(json.dumps(record), encoding="utf-8")
    staging.replace(destination)
    time.sleep(60)


class NativeWitnesses:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenJobObjectW.restype = wintypes.HANDLE
        self.kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.kernel.IsProcessInJob.restype = wintypes.BOOL
        self.handles = []

    def capture(self, pid):
        handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles.append(handle)
        return handle

    def alive(self, handle):
        result = self.kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def belongs(self, job_name, process_handle):
        job = self.kernel.OpenJobObjectW(4, False, job_name)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            belongs = wintypes.BOOL()
            if not self.kernel.IsProcessInJob(process_handle, job, ctypes.byref(belongs)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(belongs.value)
        finally:
            self.kernel.CloseHandle(job)

    def close(self):
        for handle in self.handles:
            try:
                if self.alive(handle):
                    if not self.kernel.TerminateProcess(handle, 91):
                        raise ctypes.WinError(ctypes.get_last_error())
                if self.kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise AssertionError("Owned fixture did not terminate")
            finally:
                self.kernel.CloseHandle(handle)


def committed_rows(case, plan):
    connection = sqlite3.connect(case.fixture.settings.database_path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        attempt = dict(connection.execute("SELECT * FROM run_attempts WHERE run_attempt_id=?",
                                          (plan.run_attempt_id,)).fetchone())
        member = dict(connection.execute("SELECT * FROM batch_run_memberships WHERE batch_id=? AND run_id=?",
                                         (case.batch["batch_id"], plan.run_id)).fetchone())
        lease = dict(connection.execute("SELECT * FROM controller_leases WHERE run_attempt_id=?",
                                        (plan.run_attempt_id,)).fetchone())
        quarantine = connection.execute(
            "SELECT state, document_json FROM manager_resources WHERE resource_type='quarantine' AND resource_id=?",
            (lease["desktop_id"],)).fetchone()
        return attempt, member, lease, quarantine
    finally:
        connection.close()


def run_owned_case(case, mode, *, manager_bridge=False):
    from yeyu_gamer_manager.services import adapter_host as host_module
    from yeyu_gamer_manager.services import dispatch_reservation, input_recovery
    from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget

    case.patches.close()
    case.clock.now = lambda: datetime.now(timezone.utc)
    case.store.update_config({"queue_timeout_budget": dict(
        callback_overhead_seconds=1, callback_cancel_grace_seconds=0.1,
        host_stop_grace_seconds=3, poll_seconds=0.05)})
    previous_host = case.manager.adapter_host
    runtime = previous_host.execution_bindings(case.first["game_id"])
    package = previous_host._verified_execution_manifest(case.first["game_id"])
    expected_digest = runtime["packageDigest"]
    expected_sha = hashlib.sha256(case.fixture.host_entrypoint.read_bytes()).hexdigest()
    qualifications = []

    def qualify(game, digest, host_sha):
        qualifications.append((game, digest, host_sha))
        return (game == case.first["game_id"] and digest == expected_digest
                and host_sha.removeprefix("sha256:") == expected_sha)

    host = type(previous_host)(
        previous_host.compatibility_adapter,
        promotion_receipt_resolver=previous_host._promotion_receipt_resolver,
        queue_timeout_budget_resolver=lambda: load_queue_timeout_budget(case.store.get_config()["values"]),
        execution_start_guard=lambda plan: dispatch_reservation.validate_start(case.manager, plan),
        owned_input_scope_resolver=lambda plan, digest: input_recovery.scope_for_plan(case.manager, plan, digest),
        owned_input_qualification_resolver=qualify)
    case.manager.adapter_host = host
    script = case.fixture.root / "owned_recovery_fixture.py"
    script.write_bytes(Path(__file__).read_bytes())
    record_path = case.fixture.root / "owned-host-processes.json"
    spawned = []
    contexts = []
    received_events = []
    withheld_completions = []
    release_observations = []
    reader_threads = []
    original_release = host.release_owned_execution

    def observe_release(plan, execution, receipt):
        attempt, member, lease, quarantine = committed_rows(case, plan)
        case.assertTrue(attempt["completed_at"])
        case.assertEqual(member["state"], "terminal")
        case.assertEqual(member["latest_run_attempt_id"], plan.run_attempt_id)
        case.assertEqual(lease["run_attempt_id"], plan.run_attempt_id)
        case.assertEqual(lease["controller_lease_id"], execution.scope["controllerLeaseId"])
        case.assertEqual(lease["generation"], execution.scope["controllerLeaseGeneration"])
        if manager_bridge:
            case.assertEqual(attempt["state"], "failed")
            case.assertEqual(member["terminal_outcome"], "failed")
            case.assertEqual(lease["state"], "revoked")
        else:
            case.assertEqual(attempt["state"], "review_required")
            case.assertEqual(member["terminal_outcome"], "review_required")
            case.assertEqual(lease["state"], "revoked")
            case.assertIsNotNone(quarantine)
            case.assertEqual(quarantine["state"], "quarantined")
            document = json.loads(quarantine["document_json"])
            case.assertEqual(document["runAttemptId"], plan.run_attempt_id)
            case.assertEqual(document["controllerLeaseId"], lease["controller_lease_id"])
            case.assertEqual(document["controllerLeaseGeneration"], lease["generation"])
            case.assertTrue(document["proofVerified"])
        readers = tuple(execution._readers)
        case.assertGreaterEqual(len(readers), 2)
        case.assertTrue(all(not thread.is_alive() for thread in readers))
        reader_threads.extend(readers)
        release_observations.append(plan.run_attempt_id)
        return original_release(plan, execution, receipt)
    witnesses = NativeWitnesses()
    case.addCleanup(witnesses.close)
    client = subprocess.Popen(
        [sys._base_executable, "-B", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps(dict(owner=case.id(), pid=client.pid, parentPid=os.getpid(),
                          purpose="outside-client", source="inert-sleep", log=str(record_path))))

    def cleanup():
        for context in contexts:
            receipt = context.stop_and_drain(5)
            case.assertIsNotNone(receipt, "Fixture cleanup could not prove native stop")
            context.close()
        for process in [*spawned, client]:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    case.addCleanup(cleanup)
    real_spawn = subprocess.Popen
    real_started = time.monotonic()

    def spawn_fixed(command, **kwargs):
        case.assertEqual(Path(command[0]), case.fixture.host_entrypoint)
        case.assertEqual(command[1:3], ["--operation", "execute"])
        case.assertIn("YEYU_GAMER_OWNED_INPUT_JOB", kwargs["env"])
        process = real_spawn([sys._base_executable, "-B", "-u", str(script), "--host",
                              str(record_path), package.package_version, expected_digest], **kwargs)
        spawned.append(process)
        print(json.dumps(dict(owner=case.id(), pid=process.pid, parentPid=os.getpid(),
                              purpose=mode, source=str(script), log=str(record_path))))
        return process

    subprocess_proxy = SimpleNamespace(**vars(subprocess))
    subprocess_proxy.Popen = spawn_fixed
    original_plan_builder = case.plan_for_run

    def plan_for_run(run, **kwargs):
        return replace(original_plan_builder(run, **kwargs), timeout_seconds=3,
                       expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())

    def start(run_id, completed_callback):
        if run_id == case.second["run_id"]:
            case.assertEqual(len(release_observations), 1)
            case.assertGreaterEqual(len(reader_threads), 2)
            case.assertTrue(all(not thread.is_alive() for thread in reader_threads))
            case.assertIsNone(host.active_execution(), "Next start preceded owned handoff")
            previous = case.plans[case.first["run_id"]]
            case.assertIsNone(case.store.get_controller_lease_for_attempt(previous.run_attempt_id, active_only=True))
            case.assertEqual(case.store.get_run_attempt(previous.run_attempt_id)["state"], "review_required")
            case.assertEqual(case.store.get_batch_run_membership(case.batch["batch_id"], previous.run_id)
                             ["terminal_outcome"], "review_required")
            case.assertTrue(all(not witnesses.alive(handle) for handle in case.child_handles))
            case.assertIsNone(client.poll(), "Next start found outside client terminated")
        case.started.append(run_id)
        case.started_at[run_id] = time.monotonic() - real_started
        plan = case.manager._prepare_run_attempt(case.store.get_game_run(run_id))
        case.plans[run_id] = plan
        dispatch_reservation.claim_start(case.manager, plan)
        if run_id == case.first["run_id"]:
            case.original_lease = case.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True)
            case.late_callback = completed_callback
            case.store.update_run_attempt(plan.run_attempt_id, state="running", completed=False)

            def finished(result, *, handoff):
                withheld_completions.append(result)

            pid = host.execute(run_id, plan.game_id, finished, plan=plan,
                               installation_binding={"gamePath": str(script), "toolPath": str(script.parent)},
                               on_event=lambda event, manifest: received_events.append(event))
            context = host.owned_execution_for(plan)
            case.assertIsNotNone(context)
            contexts.append(context)
            case.assertEqual(context.scope, input_recovery.scope_for_plan(case.manager, plan, expected_digest))
            deadline = time.monotonic() + 2
            while not record_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            case.assertTrue(record_path.exists(), "Trusted inert Host did not create its helpers")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            case.assertEqual(record["rootPid"], pid)
            case.assertEqual(record["inputEffects"], 0)
            case.assertTrue(record["requestReadAfterNativeJoin"])
            case.assertEqual(len(record["children"]), 2)
            for child in record["children"]:
                case.assertEqual(child["parentPid"], pid)
                print(json.dumps(dict(owner=case.id(), pid=child["pid"], parentPid=pid,
                                      purpose="owned-helper", source=str(script), log=str(record_path))))
            child_handles = [witnesses.capture(child["pid"]) for child in record["children"]]
            case.child_handles = child_handles
            case.assertTrue(all(witnesses.alive(handle) for handle in child_handles))
            case.assertTrue(witnesses.belongs(context.job_name, int(spawned[0]._handle)))
            case.assertTrue(all(witnesses.belongs(context.job_name, handle) for handle in child_handles))
            case.assertFalse(witnesses.belongs(context.job_name, int(client._handle)))
            if mode == "killed":
                kill_errors = []

                def kill_owned_root():
                    try:
                        spawned[0].kill()
                        spawned[0].wait(timeout=2)
                    except BaseException as error:
                        kill_errors.append(error)

                killer = threading.Thread(target=kill_owned_root, name="independent-owned-root-killer", daemon=True)
                killer.start()
                killer.join(3)
                case.assertFalse(killer.is_alive(), "Owned root killer exceeded its fixture deadline")
                if kill_errors:
                    raise kill_errors[0]
                case.assertTrue(all(witnesses.alive(handle) for handle in child_handles),
                                "Root death must leave live helpers before production recovery")
            return plan, pid
        case.assertIsNone(host.active_execution(), "Next start preceded owned handoff")
        case.assertIsNone(case.store.get_controller_lease_for_attempt(
            case.plans[case.first["run_id"]].run_attempt_id, active_only=True))
        case.assertTrue(all(not witnesses.alive(handle) for handle in case.child_handles))
        case.assertIsNone(client.poll(), "Production recovery killed the outside client")
        previous = case.plans[case.first["run_id"]]
        recovery = case.store.get_resource(input_recovery.RECOVERY_RESOURCE, previous.run_attempt_id)
        case.assertTrue(recovery["document"]["proofVerified"])
        case.assertFalse(recovery["document"]["ownerRetained"])
        case.assertEqual(case.store.get_controller_lease_for_attempt(previous.run_attempt_id)["state"], "revoked")
        case.assertEqual(case.store.get_run_attempt(previous.run_attempt_id)["state"], "review_required")
        case.assertEqual(case.store.get_batch_run_membership(case.batch["batch_id"], previous.run_id)
                         ["terminal_outcome"], "review_required")
        case.assertEqual(case.store.get_resource(input_recovery.QUARANTINE_RESOURCE,
                                                 input_recovery.DESKTOP_ID)["state"], "released")
        case.store.update_run_attempt(plan.run_attempt_id, state="running", completed=False)
        case.record_terminal(plan, "completed")
        case.release_fixture_lease(plan)
        completed_callback(case.result(plan, "completed"))
        return plan, 0

    with mock.patch.object(host_module, "subprocess", subprocess_proxy), \
            mock.patch.object(host, "execution_bindings", return_value=runtime), \
            mock.patch.object(host, "release_owned_execution", side_effect=observe_release), \
            mock.patch.object(case.manager, "_build_execution_plan", side_effect=plan_for_run):
        if manager_bridge:
            from yeyu_gamer_manager.services.game_launcher import GameCloseReceipt, GameLaunchReceipt

            callback_finished = threading.Event()
            delivered = []

            def completed(result):
                case.assertIsNone(host.active_execution())
                case.assertEqual(len(release_observations), 1)
                case.assertTrue(all(not thread.is_alive() for thread in reader_threads))
                delivered.append(result)
                callback_finished.set()

            with mock.patch.object(case.manager.game_launcher, "ensure_started",
                                   return_value=GameLaunchReceipt("started", client.pid, str(script))) as launch, \
                    mock.patch.object(case.manager, "_close_other_batch_games"), \
                    mock.patch.object(case.manager, "_close_finished_batch_game",
                                      return_value=GameCloseReceipt("closed", (), ())) as cleanup_game:
                plan, pid = case.manager._start_game_run(case.first["run_id"], completed)
                context = host.owned_execution_for(plan)
                case.assertIsNotNone(context)
                contexts.append(context)
                deadline = time.monotonic() + 2
                while not record_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                case.assertTrue(record_path.exists())
                record = json.loads(record_path.read_text(encoding="utf-8"))
                case.assertEqual(record["rootPid"], pid)
                for child in record["children"]:
                    case.assertEqual(child["parentPid"], pid)
                    print(json.dumps(dict(owner=case.id(), pid=child["pid"], parentPid=pid,
                                          purpose="owned-helper", source=str(script), log=str(record_path))))
                children = [witnesses.capture(child["pid"]) for child in record["children"]]
                case.assertEqual(len(children), 2)
                case.assertTrue(all(witnesses.belongs(context.job_name, handle) for handle in children))
                case.assertFalse(witnesses.belongs(context.job_name, int(client._handle)))
                case.assertTrue(callback_finished.wait(10), "Production Manager callback/handoff did not complete")
                launch.assert_called_once()
                cleanup_game.assert_called_once()
                case.assertEqual(len(delivered), 1)
                case.assertEqual(delivered[0].run_attempt_id, plan.run_attempt_id)
                case.assertEqual(delivered[0].status, "failed")
                case.assertEqual(delivered[0].completed_todo_instance_ids, ())
                case.assertIsNone(host.active_execution())
                case.assertIsNone(host.owned_execution_for(plan))
                case.assertTrue(all(not witnesses.alive(handle) for handle in children))
                case.assertIsNone(input_recovery.attempt_recovery_record(case.manager, plan.run_attempt_id))
                case.assertIsNone(client.poll())
                case.assertEqual(len(spawned), 1)
                case.assertEqual(len(qualifications), 1)
                case.assertEqual(len(release_observations), 1)
                case.assertLess(time.monotonic() - real_started, 15)
                return
        with mock.patch.object(case.manager, "_start_game_run", side_effect=start):
            case.manager._run_batch(case.batch["batch_id"], [dict(runId=run["run_id"], gameId=run["game_id"])
                for run in (case.first, case.second)], case.batch["batch_id"])
    case.assertLess(time.monotonic() - real_started, 15)
    case.assertEqual(len(spawned), 1)
    case.assertEqual(len(qualifications), 1)
    case.assertEqual(len(received_events), 1)
    case.assertEqual(received_events[0].event_type, "hello")
    case.assertIsNone(client.poll())
    case.assertTrue(all(not witnesses.alive(handle) for handle in case.child_handles))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--host":
        inert_host()
    else:
        raise SystemExit("Only the owned inert Host fixture entry point is supported")
