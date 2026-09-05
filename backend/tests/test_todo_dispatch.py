from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.todo_dispatch import (
    TodoDispatchFacts,
    evaluate_todo_dispatch,
)


class TodoDispatchPolicyTests(unittest.TestCase):
    def facts(self, **changes):
        values = {
            "status": "pending",
            "risk": "routine_action",
            "todo_definition_id": "todo.v1.game.daily.alpha",
            "operation": "alpha",
            "adapter_capability_ref": "game.daily.run@1.0",
            "runtime": {"manifestVerified": True, "status": "ready"},
            "binding": {
                "todoDefinitionIds": ["todo.v1.game.daily.alpha"],
                "adapterCapabilityRefs": ["game.daily.run@1.0"],
                "risk": "routine_action",
                "supportsResume": True,
            },
        }
        values.update(changes)
        return TodoDispatchFacts(**values)

    def test_pending_promoted_todo_is_eligible(self):
        projection = evaluate_todo_dispatch(self.facts())
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_completed_is_skipped(self):
        projection = evaluate_todo_dispatch(self.facts(status="completed"))
        self.assertEqual(projection["dispatchDisposition"], "completed_skip")

    def test_interrupted_routine_todo_can_be_started_again(self):
        projection = evaluate_todo_dispatch(
            self.facts(status="blocked", latest_attempt={"retryable": True})
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertFalse(projection["actionAvailability"]["resume"])
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_retryable_contract_failure_can_enter_a_fresh_daily_batch(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="blocked",
                latest_attempt={"retryable": True},
                active_blocker={
                    "kind": "contract_invariant",
                    "code": "upstream_stage_failed",
                    "retryable": True,
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertFalse(projection["actionAvailability"]["resume"])
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_non_retryable_contract_failure_stays_deferred(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="blocked",
                latest_attempt={"retryable": False},
                active_blocker={
                    "kind": "contract_invariant",
                    "code": "contract_violation",
                    "retryable": False,
                },
            )
        )
        self.assertNotEqual(projection["dispatchDisposition"], "eligible")
        self.assertFalse(projection["actionAvailability"]["execute"])

    def test_whole_batch_cancellation_does_not_hide_older_retryable_diagnostic(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="blocked",
                latest_attempt={"retryable": False},
                active_blocker={
                    "kind": "contract_invariant",
                    "code": "upstream_stage_failed",
                    "retryable": True,
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_blocked_cancellation_does_not_hide_retryable_review_blocker(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="blocked",
                latest_attempt={
                    "retryable": False,
                    "reasonCode": "outcome_unknown_event_scope_mismatch",
                    "reason": "artifact RunAttempt is not active",
                },
                active_blocker={
                    "kind": "review_required",
                    "code": "game_not_started_by_manager",
                    "retryable": True,
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_human_blocker_never_grants_execution(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="human_required",
                active_blocker={"kind": "human_required"},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "deferred_human")
        self.assertFalse(projection["actionAvailability"]["execute"])

    def test_retryable_routine_review_can_enter_a_fresh_daily_attempt(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={"retryable": True},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_non_retryable_review_stays_deferred(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={"retryable": False},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "deferred_review")
        self.assertFalse(projection["actionAvailability"]["execute"])

    def test_explicit_human_release_reobserves_only_in_a_new_daily_batch(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                current_reason=(
                    "human takeover released; fresh observation/rebind is required"
                ),
                latest_attempt={"retryable": False},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_unreleased_human_gate_review_is_not_a_fresh_batch_retry(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                current_reason="operator login is required",
                latest_attempt={"retryable": False},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "deferred_review")
        self.assertFalse(projection["actionAvailability"]["execute"])

    def test_legacy_formal_gui_bootstrap_review_can_enter_fresh_batch(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={
                    "retryable": False,
                    "reasonCode": "upstream_stage_unverified",
                    "reason": "formal_gui_exited_before_update_ready; stage_event_missing",
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_legacy_formal_gui_bootstrap_blocker_can_enter_fresh_batch(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={
                    "retryable": False,
                    "reason_code": "upstream_stage_unverified",
                    "reason": "formal_gui_exited_before_update_ready; stage_event_missing",
                },
                active_blocker={
                    "kind": "review_required",
                    "code": "upstream_stage_unverified",
                    "retryable": False,
                    "reason": "formal_gui_exited_before_update_ready; stage_event_missing",
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])

    def test_unrelated_unverified_review_is_not_auto_retried(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={
                    "retryable": False,
                    "reasonCode": "upstream_stage_unverified",
                    "reason": "missing_reward_evidence",
                },
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "deferred_review")
        self.assertFalse(projection["actionAvailability"]["execute"])

    def test_retryable_diagnostic_review_blocker_allows_a_fresh_attempt(self):
        projection = evaluate_todo_dispatch(
            self.facts(
                status="review_required",
                latest_attempt={"retryable": True},
                active_blocker={"kind": "review_required", "retryable": True},
            )
        )
        self.assertEqual(projection["dispatchDisposition"], "eligible")
        self.assertTrue(projection["actionAvailability"]["execute"])


if __name__ == "__main__":
    unittest.main()
