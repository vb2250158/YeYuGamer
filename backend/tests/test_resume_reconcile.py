from __future__ import annotations

import unittest

from pydantic import ValidationError

from yeyu_gamer_manager.domain.resume_reconcile import (
    ActionIdempotency,
    ActionReceiptOutcome,
    ActionReceiptResumeFact,
    CheckpointResumeFact,
    CheckpointStage,
    CurrentAttemptFact,
    GameRunResumeFact,
    HumanBlockerReleaseFact,
    ReconcileRequirement,
    ResumeAttemptState,
    ResumeBindingFact,
    ResumeBlockerKind,
    ResumeDisposition,
    ResumeReconcileConflict,
    ResumeReconcileRequest,
    ResumeReconcileSnapshot,
    ResumeRisk,
    ResumeRunState,
    ResumeTodoState,
    SuccessorTodoMode,
    TodoAttemptResumeFact,
    TodoAttemptState,
    TodoBlockerResumeFact,
    TodoResumeFact,
    plan_resume_reconciliation,
)


RUN_ID = "run-current"
GAME_DAY = "2026-08-28"
RUN_ATTEMPT_ID = "run-attempt-2"


class ResumeReconcilePlannerTests(unittest.TestCase):
    def run_fact(self, **changes: object) -> GameRunResumeFact:
        values: dict[str, object] = {
            "run_id": RUN_ID,
            "game_id": "StarRail",
            "game_day_key": GAME_DAY,
            "state": ResumeRunState.FAILED,
            "revision": 7,
        }
        values.update(changes)
        return GameRunResumeFact(**values)

    def current_attempt(self, **changes: object) -> CurrentAttemptFact:
        values: dict[str, object] = {
            "run_attempt_id": RUN_ATTEMPT_ID,
            "run_id": RUN_ID,
            "game_id": "StarRail",
            "game_day_key": GAME_DAY,
            "state": ResumeAttemptState.FAILED,
            "attempt_ordinal": 2,
            "revision": 4,
        }
        values.update(changes)
        return CurrentAttemptFact(**values)

    def request(self, **changes: object) -> ResumeReconcileRequest:
        values: dict[str, object] = {
            "run_id": RUN_ID,
            "game_day_key": GAME_DAY,
            "expected_run_revision": 7,
            "expected_current_attempt_id": RUN_ATTEMPT_ID,
            "expected_current_attempt_revision": 4,
        }
        values.update(changes)
        return ResumeReconcileRequest(**values)

    def todo_attempt(
        self, todo_id: str, **changes: object
    ) -> TodoAttemptResumeFact:
        values: dict[str, object] = {
            "todo_attempt_id": f"todo-attempt-{todo_id}",
            "run_attempt_id": RUN_ATTEMPT_ID,
            "run_id": RUN_ID,
            "todo_instance_id": todo_id,
            "game_day_key": GAME_DAY,
            "attempt_number": 1,
            "state": TodoAttemptState.RUNNING,
            "revision": 3,
            "retryable": False,
        }
        values.update(changes)
        return TodoAttemptResumeFact(**values)

    def receipt(
        self,
        todo_id: str,
        attempt: TodoAttemptResumeFact,
        **changes: object,
    ) -> ActionReceiptResumeFact:
        values: dict[str, object] = {
            "action_receipt_id": f"receipt-{todo_id}",
            "run_id": RUN_ID,
            "run_attempt_id": attempt.run_attempt_id,
            "todo_instance_id": todo_id,
            "todo_attempt_id": attempt.todo_attempt_id,
            "game_day_key": GAME_DAY,
            "outcome": ActionReceiptOutcome.UNKNOWN,
            "idempotency": ActionIdempotency.NON_IDEMPOTENT,
            "revision": 2,
        }
        values.update(changes)
        return ActionReceiptResumeFact(**values)

    def todo(self, todo_id: str, **changes: object) -> TodoResumeFact:
        values: dict[str, object] = {
            "todo_instance_id": todo_id,
            "run_id": RUN_ID,
            "game_id": "StarRail",
            "game_day_key": GAME_DAY,
            "revision": 1,
            "status": ResumeTodoState.PENDING,
            "operation": f"operation-{todo_id}",
            "risk": ResumeRisk.ROUTINE_ACTION,
            "binding": ResumeBindingFact(
                operation=f"operation-{todo_id}", supports_resume=True
            ),
        }
        values.update(changes)
        return TodoResumeFact(**values)

    def plan(self, *todos: TodoResumeFact):
        snapshot = ResumeReconcileSnapshot(
            run=self.run_fact(), current_attempt=self.current_attempt(), todos=todos
        )
        return plan_resume_reconciliation(snapshot, self.request())

    def test_partitions_completed_pending_and_unknown_with_fenced_successor(self) -> None:
        interrupted_attempt = self.todo_attempt("unknown")
        interrupted_receipt = self.receipt("unknown", interrupted_attempt)
        checkpoint = CheckpointResumeFact(
            checkpoint_id="checkpoint-unknown",
            run_id=RUN_ID,
            run_attempt_id=RUN_ATTEMPT_ID,
            todo_instance_id="unknown",
            todo_attempt_id=interrupted_attempt.todo_attempt_id,
            game_day_key=GAME_DAY,
            stage=CheckpointStage.ACTION_DISPATCHED,
            revision=5,
            action_receipt_id=interrupted_receipt.action_receipt_id,
        )

        result = self.plan(
            self.todo("done", status=ResumeTodoState.COMPLETED),
            self.todo("pending"),
            self.todo(
                "unknown",
                status=ResumeTodoState.IN_PROGRESS,
                latest_attempt=interrupted_attempt,
                action_receipt=interrupted_receipt,
                checkpoint=checkpoint,
            ),
        )

        self.assertEqual(result.completed_skip, ("done",))
        self.assertEqual(result.eligible_pending, ("pending",))
        self.assertEqual(result.reconcile_unknown, ("unknown",))
        self.assertEqual(result.deferred_review, ())
        successor = result.successor_attempt_intent
        self.assertIsNotNone(successor)
        assert successor is not None
        self.assertEqual(successor.predecessor_run_attempt_id, RUN_ATTEMPT_ID)
        self.assertEqual(successor.expected_run_revision, 7)
        self.assertEqual(successor.expected_predecessor_attempt_revision, 4)
        self.assertEqual(successor.successor_attempt_ordinal, 3)
        self.assertTrue(successor.requires_new_fencing_token)
        self.assertEqual(
            [item.mode for item in successor.todo_attempts],
            [SuccessorTodoMode.EXECUTE, SuccessorTodoMode.RECONCILE_ONLY],
        )
        unknown_intent = successor.todo_attempts[1]
        self.assertFalse(unknown_intent.blind_replay_allowed)
        self.assertEqual(unknown_intent.predecessor_todo_attempt_id, interrupted_attempt.todo_attempt_id)
        self.assertEqual(unknown_intent.expected_predecessor_todo_attempt_revision, 3)
        self.assertEqual(unknown_intent.expected_action_receipt_revision, 2)
        self.assertEqual(unknown_intent.expected_checkpoint_revision, 5)

    def test_non_idempotent_unknown_requires_observation_and_never_blind_replay(self) -> None:
        attempt = self.todo_attempt("claim-reward")
        result = self.plan(
            self.todo(
                "claim-reward",
                status=ResumeTodoState.IN_PROGRESS,
                latest_attempt=attempt,
                action_receipt=self.receipt("claim-reward", attempt),
            )
        )

        item = result.items[0]
        self.assertEqual(item.disposition, ResumeDisposition.RECONCILE_UNKNOWN)
        self.assertIn(ReconcileRequirement.FRESH_OBSERVATION, item.requirements)
        self.assertIn(
            ReconcileRequirement.ACTION_RECEIPT_RECONCILIATION,
            item.requirements,
        )
        self.assertIn(ReconcileRequirement.NON_IDEMPOTENT_GUARD, item.requirements)
        self.assertFalse(item.blind_replay_allowed)
        assert result.successor_attempt_intent is not None
        self.assertEqual(
            result.successor_attempt_intent.todo_attempts[0].mode,
            SuccessorTodoMode.RECONCILE_ONLY,
        )

    def test_human_required_needs_explicit_release_then_only_reconciles(self) -> None:
        unreleased = TodoBlockerResumeFact(
            blocker_id="human-blocker",
            run_id=RUN_ID,
            todo_instance_id="login",
            game_day_key=GAME_DAY,
            kind=ResumeBlockerKind.HUMAN_REQUIRED,
            revision=8,
            active=False,
        )
        deferred = self.plan(
            self.todo(
                "login",
                status=ResumeTodoState.HUMAN_REQUIRED,
                blocker=unreleased,
            )
        )
        self.assertEqual(deferred.deferred_human, ("login",))
        self.assertIsNone(deferred.successor_attempt_intent)

        released = TodoBlockerResumeFact(
            blocker_id="human-blocker",
            run_id=RUN_ID,
            todo_instance_id="login",
            game_day_key=GAME_DAY,
            kind=ResumeBlockerKind.HUMAN_REQUIRED,
            revision=8,
            active=False,
            release=HumanBlockerReleaseFact(
                release_id="human-release",
                blocker_id="human-blocker",
                blocker_revision=8,
                run_id=RUN_ID,
                todo_instance_id="login",
                game_day_key=GAME_DAY,
            ),
        )
        reconciled = self.plan(
            self.todo(
                "login",
                status=ResumeTodoState.HUMAN_REQUIRED,
                blocker=released,
            )
        )
        self.assertEqual(reconciled.eligible_pending, ())
        self.assertEqual(reconciled.reconcile_unknown, ("login",))
        self.assertIn(
            ReconcileRequirement.BINDING_REVALIDATION,
            reconciled.items[0].requirements,
        )
        assert reconciled.successor_attempt_intent is not None
        self.assertEqual(
            reconciled.successor_attempt_intent.todo_attempts[0].mode,
            SuccessorTodoMode.RECONCILE_ONLY,
        )

    def test_review_forbidden_and_unsupported_retry_are_deferred(self) -> None:
        failed_attempt = self.todo_attempt(
            "unsupported",
            state=TodoAttemptState.FAILED,
            retryable=True,
        )
        result = self.plan(
            self.todo("review", status=ResumeTodoState.REVIEW_REQUIRED),
            self.todo("forbidden", risk=ResumeRisk.FORBIDDEN),
            self.todo(
                "unsupported",
                status=ResumeTodoState.BLOCKED,
                latest_attempt=failed_attempt,
                action_receipt=self.receipt(
                    "unsupported",
                    failed_attempt,
                    outcome=ActionReceiptOutcome.FAILED,
                    idempotency=ActionIdempotency.IDEMPOTENT,
                ),
                binding=ResumeBindingFact(
                    operation="operation-unsupported", supports_resume=False
                ),
            ),
        )

        self.assertEqual(result.deferred_review, ("review", "unsupported"))
        self.assertEqual(result.deferred_forbidden, ("forbidden",))
        self.assertIsNone(result.successor_attempt_intent)

    def test_resolved_retryable_blocker_can_create_successor_attempt(self) -> None:
        attempt = self.todo_attempt(
            "retry",
            state=TodoAttemptState.FAILED,
            retryable=True,
            attempt_number=2,
            revision=9,
        )
        result = self.plan(
            self.todo(
                "retry",
                status=ResumeTodoState.BLOCKED,
                revision=3,
                latest_attempt=attempt,
                action_receipt=self.receipt(
                    "retry",
                    attempt,
                    outcome=ActionReceiptOutcome.FAILED,
                    idempotency=ActionIdempotency.IDEMPOTENT,
                    revision=6,
                ),
                blocker=TodoBlockerResumeFact(
                    blocker_id="technical-blocker",
                    run_id=RUN_ID,
                    todo_instance_id="retry",
                    game_day_key=GAME_DAY,
                    kind=ResumeBlockerKind.TECHNICAL,
                    revision=4,
                    active=False,
                    retryable=True,
                ),
            )
        )

        self.assertEqual(result.eligible_pending, ("retry",))
        assert result.successor_attempt_intent is not None
        intent = result.successor_attempt_intent.todo_attempts[0]
        self.assertEqual(intent.expected_todo_revision, 3)
        self.assertEqual(intent.expected_predecessor_todo_attempt_revision, 9)
        self.assertEqual(intent.successor_todo_attempt_number, 3)
        self.assertEqual(intent.expected_action_receipt_revision, 6)

    def test_scope_and_revision_fences_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "same run and GameDay"):
            ResumeReconcileSnapshot(
                run=self.run_fact(),
                current_attempt=self.current_attempt(),
                todos=(self.todo("other-day", game_day_key="2026-08-27"),),
            )

        snapshot = ResumeReconcileSnapshot(
            run=self.run_fact(), current_attempt=self.current_attempt(), todos=()
        )
        with self.assertRaises(ResumeReconcileConflict) as stale:
            plan_resume_reconciliation(
                snapshot,
                self.request(expected_current_attempt_revision=3),
            )
        self.assertEqual(stale.exception.code, "attempt_revision_mismatch")

        with self.assertRaises(ResumeReconcileConflict) as wrong_run:
            plan_resume_reconciliation(snapshot, self.request(run_id="other-run"))
        self.assertEqual(wrong_run.exception.code, "run_scope_mismatch")

    def test_active_attempt_cannot_spawn_a_successor(self) -> None:
        snapshot = ResumeReconcileSnapshot(
            run=self.run_fact(state=ResumeRunState.RUNNING),
            current_attempt=self.current_attempt(state=ResumeAttemptState.RUNNING),
            todos=(self.todo("pending"),),
        )
        with self.assertRaises(ResumeReconcileConflict) as conflict:
            plan_resume_reconciliation(snapshot, self.request())
        self.assertEqual(conflict.exception.code, "attempt_still_active")


if __name__ == "__main__":
    unittest.main()
