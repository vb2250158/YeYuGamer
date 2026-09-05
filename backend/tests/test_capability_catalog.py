from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.capability_catalog import (
    build_capability_registry,
)


class CapabilityCatalogTests(unittest.TestCase):
    def test_builds_stable_catalog_with_runtime_required_arguments(self) -> None:
        unavailable = build_capability_registry(execution_ready=False)
        available = build_capability_registry(execution_ready=True)

        self.assertFalse(unavailable["game.daily.run"].enabled)
        self.assertTrue(available["game.daily.run"].enabled)
        self.assertFalse(unavailable["batch.daily.run"].enabled)
        self.assertTrue(available["batch.daily.run"].enabled)
        self.assertFalse(available["game.weekly.run"].enabled)
        self.assertTrue(available["adapter.canary.request"].enabled)
        self.assertTrue(available["observation.capture.request"].enabled)

        required_arguments = {
            "game.daily.plan": ["gameId"],
            "game.daily.run": ["gameId"],
            "game.weekly.plan": ["gameId", "weeklyId"],
            "game.weekly.run": ["gameId", "weeklyId"],
            "adapter.canary.request": ["adapterId"],
            "observation.capture.request": ["gameId", "runId"],
        }
        for capability_id, expected in required_arguments.items():
            with self.subTest(capability_id=capability_id):
                self.assertEqual(
                    available[capability_id].input_schema["required"], expected
                )

        for definition in available.values():
            with self.subTest(capability_id=definition.capability_id):
                self.assertEqual(len(definition.implementation_hash), 64)
                self.assertTrue(definition.policy["managerOnly"])
                if definition.enabled:
                    self.assertEqual(
                        definition.policy["implementationStatus"], "implemented"
                    )


if __name__ == "__main__":
    unittest.main()
