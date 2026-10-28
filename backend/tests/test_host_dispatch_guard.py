import unittest
from unittest import mock

import test_adapter_protocol as fixtures
from yeyu_gamer_manager.services.dispatch_reservation import DispatchReservationRejected


class HostDispatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdapterHostSupervisorTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_expired_dispatch_is_rejected_before_probe_files_or_process(self):
        host = self.fixture.host_service
        host._execution_start_guard = mock.Mock(side_effect=DispatchReservationRejected("Expired dispatch"))
        with mock.patch.object(host, "probe") as probe, \
                mock.patch.object(host, "_artifact_staging_dir") as staging, \
                mock.patch("subprocess.Popen") as spawn, \
                self.assertRaises(DispatchReservationRejected):
            host.execute(self.fixture.plan.run_id, self.fixture.plan.game_id, mock.Mock(),
                         plan=self.fixture.plan, installation_binding=self.fixture.installation_binding)
        host._execution_start_guard.assert_called_once_with(self.fixture.plan)
        probe.assert_not_called()
        staging.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(host.active_execution())

    def test_guard_failure_is_not_converted_to_completion(self):
        host = self.fixture.host_service
        host._execution_start_guard = mock.Mock(side_effect=OSError("Store unavailable"))
        complete = mock.Mock()
        with mock.patch("subprocess.Popen") as spawn, self.assertRaises(OSError):
            host.execute(self.fixture.plan.run_id, self.fixture.plan.game_id, complete,
                         plan=self.fixture.plan, installation_binding=self.fixture.installation_binding)
        complete.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
