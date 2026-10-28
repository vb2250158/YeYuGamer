from dataclasses import replace
import unittest
from unittest import mock

import test_adapter_protocol as protocol_fixtures
import test_manager_adapter_execution as fixtures
from yeyu_gamer_manager.services.adapter_protocol import (
    AdapterExecutionPlan, AdapterProtocolError, parse_execute_request, serialize_execute_request,
)


class ExecutionPlanBaselineContractTests(unittest.TestCase):
    def test_empty_scope_extensions_preserve_exact_v11_document_and_roundtrip(self):
        original = protocol_fixtures.plan_document()
        plan = AdapterExecutionPlan.from_document(original)
        self.assertEqual(plan.selected_operations, ())
        self.assertEqual(plan.observation_only_operations, ())
        self.assertEqual(plan.to_document(), original)
        self.assertEqual(parse_execute_request(serialize_execute_request(plan)), plan)

    def test_nonempty_scope_is_rejected_instead_of_silently_discarded(self):
        plan = AdapterExecutionPlan.from_document(protocol_fixtures.plan_document())
        for values in ({"selected_operations": ("fixture-selected",)}, {"observation_only_operations": ("fixture-observe",)}):
            with self.subTest(values=values), self.assertRaises(AdapterProtocolError) as rejected:
                serialize_execute_request(replace(plan, **values))
            self.assertEqual(rejected.exception.code, "unsupported_operation_scope")

    def test_real_manager_builder_prepares_without_protocol_shim_or_process_launch(self):
        fixture = fixtures.ManagerAdapterExecutionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        run = fixture._create_run([fixture._todo_id(fixtures.EXECUTE_BETA)])
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No execution in prepare regression")), \
                mock.patch.object(fixture.manager.adapter_host, "execute") as execute:
            plan = fixture.manager._prepare_run_attempt(run)
            execute.assert_not_called()
        self.assertIs(type(plan), AdapterExecutionPlan)
        self.assertEqual(plan.selected_operations, ())
        self.assertEqual(plan.observation_only_operations, ())
        self.assertEqual(fixture.store.get_run_attempt(plan.run_attempt_id)["state"], "starting")
        self.assertIsNotNone(fixture.store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True))
        fixture.manager.game_launcher.ensure_started.assert_not_called()


if __name__ == "__main__":
    unittest.main()
