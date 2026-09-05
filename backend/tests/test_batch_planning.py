from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.batch_planning import (
    classify_batch_todo_plans,
)


class BatchPlanningTests(unittest.TestCase):
    def test_classifies_executable_deferred_and_completed_without_dropping_scope(
        self,
    ) -> None:
        plans = {
            "ReadyGame": {
                "requiredRemaining": 2,
                "executableTodoInstanceIds": ["ready.todo"],
                "deferredTodoInstanceIds": ["ready.manual"],
                "deferredReasons": {},
                "runtimeBinding": {
                    "status": "promoted",
                    "manifestVerified": True,
                    "bindings": [{"bindingId": "ready"}],
                },
            },
            "DeferredGame": {
                "requiredRemaining": 1,
                "executableTodoInstanceIds": [],
                "deferredTodoInstanceIds": ["deferred.todo"],
                "deferredReasons": {
                    "deferred.todo": {
                        "code": "execution_package_unpromoted",
                        "reason": "Execution package is installed but not promoted.",
                    }
                },
                "runtimeBinding": {
                    "status": "installed-unpromoted",
                    "manifestVerified": False,
                    "bindings": [],
                },
            },
            "CompletedGame": {
                "requiredRemaining": 0,
                "executableTodoInstanceIds": [],
                "deferredTodoInstanceIds": [],
                "deferredReasons": {},
                "runtimeBinding": {
                    "status": "unavailable",
                    "manifestVerified": False,
                    "bindings": [],
                },
            },
        }

        decision = classify_batch_todo_plans(
            ["ReadyGame", "DeferredGame", "CompletedGame", "ReadyGame"],
            plans,
        )

        self.assertEqual(
            decision.candidate_game_ids,
            ("ReadyGame", "DeferredGame", "CompletedGame"),
        )
        self.assertEqual(
            decision.unresolved_game_ids, ("ReadyGame", "DeferredGame")
        )
        self.assertEqual(decision.executable_game_ids, ("ReadyGame",))
        self.assertEqual(decision.deferred_game_ids, ("DeferredGame",))
        self.assertEqual(decision.skipped_completed_game_ids, ("CompletedGame",))
        self.assertTrue(decision.has_executable_binding)

        details = decision.execution_unavailable_details(plans)
        self.assertEqual(
            details["candidateGameIds"],
            ["ReadyGame", "DeferredGame", "CompletedGame"],
        )
        self.assertEqual(details["deferredGameIds"], ["DeferredGame"])
        self.assertEqual(
            details["games"],
            [
                {
                    "gameId": "DeferredGame",
                    "reasonCode": "execution_package_unpromoted",
                    "reason": "Execution package is installed but not promoted.",
                    "reasonCodes": ["execution_package_unpromoted"],
                    "deferredTodoInstanceIds": ["deferred.todo"],
                    "runtimeBinding": {
                        "status": "installed-unpromoted",
                        "manifestVerified": False,
                        "bindingCount": 0,
                    },
                }
            ],
        )

    def test_zero_executable_binding_produces_small_per_game_reasons(self) -> None:
        plans = {
            "Alpha": {
                "requiredRemaining": 1,
                "executableTodoInstanceIds": [],
                "deferredTodoInstanceIds": ["alpha.todo"],
                "deferredReasons": {},
                "runtimeBinding": {
                    "status": "game_not_promoted",
                    "manifestVerified": False,
                    "bindings": "invalid-not-a-binding-list",
                },
            }
        }

        decision = classify_batch_todo_plans(["Alpha"], plans)
        details = decision.execution_unavailable_details(plans)

        self.assertTrue(decision.has_unresolved_todos)
        self.assertFalse(decision.has_executable_binding)
        self.assertEqual(details["deferredGameIds"], ["Alpha"])
        self.assertEqual(details["games"][0]["reasonCode"], "game_not_promoted")
        self.assertEqual(details["games"][0]["runtimeBinding"]["bindingCount"], 0)


if __name__ == "__main__":
    unittest.main()
