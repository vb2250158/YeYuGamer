from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import test_adapter_protocol as fixtures
from yeyu_gamer_manager.services import adapter_host as host_module
from yeyu_gamer_manager.services.adapter_host import AdapterHostProtocolError
from yeyu_gamer_manager.services.queue_timeout_budget import load_queue_timeout_budget
from yeyu_gamer_manager.services.owned_input_execution import OwnedInputExecution
from yeyu_gamer_manager.services.owned_host_job import OwnedHostJobError


class HostRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_legacy_nonreader_request_is_bounded_and_not_reported_completed(self):
        host = self.fixture.host_service
        host._queue_timeout_budget_resolver = lambda: load_queue_timeout_budget(
            {"queue_timeout_budget": {"host_stop_grace_seconds": 0.2}})
        spawned = []
        real_spawn = subprocess.Popen

        def start(command, **kwargs):
            self.assertEqual(Path(command[0]), self.fixture.host)
            self.assertNotIn("YEYU_GAMER_OWNED_INPUT_JOB", kwargs["env"])
            process = real_spawn([sys._base_executable, "-B", "-c", "import time; time.sleep(15)"], **kwargs)
            spawned.append(process)
            return process

        proxy = SimpleNamespace(**vars(subprocess))
        proxy.Popen = start
        complete = mock.Mock()
        started = time.monotonic()
        try:
            with mock.patch.object(host_module, "subprocess", proxy), \
                    mock.patch.object(host_module, "serialize_execute_request", return_value=b"x" * 131072 + b"\n"), \
                    self.assertRaises(AdapterHostProtocolError):
                host.execute(self.fixture.plan.run_id, self.fixture.plan.game_id, complete,
                             plan=self.fixture.plan, installation_binding=self.fixture.installation_binding)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].poll())
            self.assertIsNone(host.active_execution())
            self.assertIsNone(host._request_writer)
            complete.assert_not_called()
        finally:
            for process in spawned:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_environment_failure_precedes_native_owner_creation(self):
        host = self.fixture.host_service
        host._owned_input_qualification_resolver = lambda *args: True
        host._owned_input_scope_resolver = lambda *args: {"fixture": "startup"}
        with mock.patch.object(host, "_host_environment", side_effect=OSError("offline environment failure")), \
                mock.patch("yeyu_gamer_manager.services.owned_input_execution.OwnedInputExecution") as create, \
                mock.patch.object(host_module.subprocess, "Popen") as spawn, \
                self.assertRaises(AdapterHostProtocolError):
            host.execute(self.fixture.plan.run_id, self.fixture.plan.game_id, mock.Mock(),
                         plan=self.fixture.plan, installation_binding=self.fixture.installation_binding)
        create.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(host._owned_execution)
        self.assertFalse(host._control_authority_path(self.fixture.plan.run_attempt_id).exists())

    def test_startup_close_failure_retains_native_owner_and_original_error(self):
        host = self.fixture.host_service
        scope = {"fixture": "startup"}
        host._owned_input_qualification_resolver = lambda *args: True
        host._owned_input_scope_resolver = lambda *args: scope
        execution = OwnedInputExecution(scope)
        original = OSError("offline process creation failure")
        real_close = execution.close
        closes = []

        def fail_once():
            closes.append(True)
            if len(closes) == 1:
                raise OwnedHostJobError("offline close failure")
            real_close()

        try:
            with mock.patch("yeyu_gamer_manager.services.owned_input_execution.OwnedInputExecution", return_value=execution), \
                    mock.patch.object(execution, "close", side_effect=fail_once), \
                    mock.patch.object(host_module.subprocess, "Popen", side_effect=original), \
                    self.assertRaises(AdapterHostProtocolError) as failure:
                host.execute(self.fixture.plan.run_id, self.fixture.plan.game_id, mock.Mock(),
                             plan=self.fixture.plan, installation_binding=self.fixture.installation_binding)
            self.assertIs(failure.exception.__cause__, original)
            self.assertIs(host._owned_execution, execution)
            self.assertIs(host.active_plan_for_run(self.fixture.plan.run_id), self.fixture.plan)
            self.assertTrue(host._control_authority_path(self.fixture.plan.run_attempt_id).exists())
        finally:
            execution.close()


if __name__ == "__main__":
    unittest.main()
