from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta, timezone

from pydantic import ValidationError

from yeyu_gamer_manager.domain.completion_contract import (
    AgentPredicateObservation,
    AgentReviewDecision,
    AgentReviewFact,
    AgentTodoReview,
    BlockerKind,
    CompletionBlockerFact,
    CompletionContractSnapshot,
    CompletionOutcome,
    CompletionPredicateCode,
    CompletionRunAttemptState,
    CompletionTodoState,
    EvidenceArtifactFact,
    EvidenceExclusionReason,
    GameDayWindow,
    PredicateResultState,
    RunAttemptCompletionFact,
    TodoCompletionFact,
    TodoReviewVerdict,
)
from yeyu_gamer_manager.services.completion_contract import (
    DEFAULT_COMPLETION_POLICIES,
    STARRAIL_POINTS_OBSERVATION,
    STARRAIL_REWARD_TIERS_OBSERVATION,
    CompletionPolicyRegistry,
    GameCompletionPolicy,
    adjudicate_completion,
)


CHINA = timezone(timedelta(hours=8))
DAY_START = datetime(2026, 8, 28, 4, 0, tzinfo=CHINA)
DAY_END = DAY_START + timedelta(days=1)
RUN_ID = "run-current"
ATTEMPT_ID = "attempt-current"
SCREENSHOT_ID = "artifact-final-panel"


class CompletionContractAdjudicatorTests(unittest.TestCase):
    def snapshot(
        self,
        *,
        game_id: str = "StarRail",
        attempt_state: CompletionRunAttemptState = (
            CompletionRunAttemptState.COMPLETED
        ),
        review_decision: AgentReviewDecision | None = (
            AgentReviewDecision.ACCEPTED
        ),
        reviewer_principal_id: str = "agent.yeyu",
        points: int = 500,
        claimed_tiers: int = 5,
        screenshot_attempt_id: str = ATTEMPT_ID,
        screenshot_day_key: str = "2026-08-28",
        screenshot_captured_at: datetime | None = None,
        screenshot_kind: str = "game-ui-daily-training-panel",
        screenshot_content_type: str = "image/png",
        screenshot_raw: bool = True,
        todo_states: dict[str, CompletionTodoState] | None = None,
        todo_attempt_ids: dict[str, str] | None = None,
        game_day: GameDayWindow | None = None,
    ) -> CompletionContractSnapshot:
        game_day = game_day or GameDayWindow(
            period_key="2026-08-28",
            starts_at=DAY_START,
            ends_at=DAY_END,
        )
        operations = (
            "attach-home",
            "spend-trailblaze-power",
            "daily-training-objectives",
            "claim-daily-training-rewards",
            "verify-daily-task-list",
        )
        evidence: list[EvidenceArtifactFact] = []
        evidence_refs: dict[str, str] = {}
        for index, operation in enumerate(operations[:-1]):
            artifact_id = f"artifact-step-{index}"
            evidence_refs[operation] = artifact_id
            evidence.append(
                EvidenceArtifactFact(
                    artifact_id=artifact_id,
                    kind="game-ui-step-after-raw",
                    content_type="image/png",
                    captured_at=DAY_START + timedelta(minutes=10 + index),
                    source="candidate-adapter",
                    raw=True,
                    game_id=game_id,
                    run_id=RUN_ID,
                    run_attempt_id=ATTEMPT_ID,
                    todo_instance_id=f"todo-{operation}",
                    game_day_key="2026-08-28",
                    content_hash=f"{index + 1:064x}",
                    integrity_valid=True,
                    integrity_reason_code="verified",
                )
            )
        evidence_refs[operations[-1]] = SCREENSHOT_ID
        evidence.append(
            EvidenceArtifactFact(
                artifact_id=SCREENSHOT_ID,
                kind=screenshot_kind,
                content_type=screenshot_content_type,
                captured_at=(
                    screenshot_captured_at
                    or DAY_START + timedelta(minutes=35)
                ),
                source="agent-observation-request",
                raw=screenshot_raw,
                game_id=game_id,
                run_id=RUN_ID,
                run_attempt_id=screenshot_attempt_id,
                todo_instance_id="todo-verify-daily-task-list",
                game_day_key=screenshot_day_key,
                content_hash="f" * 64,
                integrity_valid=True,
                integrity_reason_code="verified",
            )
        )

        states = todo_states or {}
        attempt_ids = todo_attempt_ids or {}
        todos = tuple(
            TodoCompletionFact(
                todo_instance_id=f"todo-{operation}",
                game_id=game_id,
                game_day_key="2026-08-28",
                required=True,
                status=states.get(operation, CompletionTodoState.COMPLETED),
                run_id=RUN_ID,
                run_attempt_id=attempt_ids.get(operation, ATTEMPT_ID),
                completed_at=(
                    DAY_START + timedelta(minutes=30)
                    if states.get(operation, CompletionTodoState.COMPLETED)
                    == CompletionTodoState.COMPLETED
                    else None
                ),
                evidence_refs=(evidence_refs[operation],),
                reason_code="synthetic-test-fact",
            )
            for operation in operations
        )

        review = None
        if review_decision is not None:
            observations = (
                (
                    AgentPredicateObservation(
                        predicate_id=STARRAIL_POINTS_OBSERVATION,
                        metrics={"current": points, "target": 500},
                        artifact_refs=(SCREENSHOT_ID,),
                    ),
                    AgentPredicateObservation(
                        predicate_id=STARRAIL_REWARD_TIERS_OBSERVATION,
                        metrics={"claimed": claimed_tiers, "total": 5},
                        artifact_refs=(SCREENSHOT_ID,),
                    ),
                )
                if game_id == "StarRail"
                and reviewer_principal_id != "manager:promoted-adapter"
                else ()
            )
            review = AgentReviewFact(
                review_id="review-current",
                reviewer_principal_id=reviewer_principal_id,
                decision=review_decision,
                game_id=game_id,
                run_id=RUN_ID,
                run_attempt_id=ATTEMPT_ID,
                game_day_key="2026-08-28",
                reviewed_at=DAY_START + timedelta(minutes=45),
                artifact_refs=tuple(evidence_refs.values()),
                observations=observations,
                todo_reviews=tuple(
                    AgentTodoReview(
                        todo_instance_id=f"todo-{operation}",
                        verdict=TodoReviewVerdict.CONFIRMED,
                        reason_code="visual_completion_confirmed",
                        artifact_refs=(evidence_refs[operation],),
                    )
                    for operation in operations
                ),
            )

        return CompletionContractSnapshot(
            game_id=game_id,
            run_id=RUN_ID,
            cadence="daily",
            game_day=game_day,
            current_attempt=RunAttemptCompletionFact(
                run_attempt_id=ATTEMPT_ID,
                run_id=RUN_ID,
                game_id=game_id,
                cadence="daily",
                state=attempt_state,
                started_at=DAY_START + timedelta(minutes=2),
                completed_at=DAY_START + timedelta(minutes=40),
            ),
            todos=todos,
            evidence=tuple(evidence),
            agent_review=review,
        )

    def evaluation(self, decision, code: CompletionPredicateCode):
        return next(item for item in decision.evaluations if item.code == code)

    def test_starrail_accepts_only_complete_current_scoped_visual_contract(self) -> None:
        snapshot = self.snapshot()

        first = adjudicate_completion(snapshot)
        second = adjudicate_completion(snapshot)

        self.assertEqual(first, second)
        self.assertEqual(first.outcome, CompletionOutcome.ACCEPTED_DONE)
        self.assertTrue(first.accepted_done)
        self.assertEqual(first.missing_predicates, ())
        self.assertEqual(first.blocking_predicates, ())
        self.assertIn(SCREENSHOT_ID, first.screenshot_artifact_refs)
        self.assertIn(SCREENSHOT_ID, first.supporting_artifact_refs)
        self.assertTrue(
            all(
                item.state == PredicateResultState.SATISFIED
                for item in first.evaluations
            )
        )
        projection = first.model_dump(mode="json", by_alias=True)
        self.assertTrue(projection["acceptedDone"])
        self.assertEqual(projection["outcome"], "accepted_done")

    def test_game_level_accepted_without_todo_verdicts_requires_review(self) -> None:
        registry = DEFAULT_COMPLETION_POLICIES.with_policy(
            GameCompletionPolicy(
                game_id="ExampleGame",
                cadence="daily",
                timezone="Asia/Shanghai",
                reset_utc_offset_minutes=8 * 60,
                reset_local_time=time(4, 0),
            )
        )
        snapshot = self.snapshot(game_id="ExampleGame")
        snapshot = snapshot.model_copy(
            update={
                "agent_review": snapshot.agent_review.model_copy(
                    update={"todo_reviews": ()}
                )
            }
        )

        decision = adjudicate_completion(snapshot, policies=registry)

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        self.assertFalse(decision.accepted_done)
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            decision.missing_predicates,
        )

    def test_unique_home_frames_need_each_todo_semantically_confirmed(self) -> None:
        snapshot = self.snapshot()
        evidence = tuple(
            item.model_copy(
                update={
                    "kind": "game-ui-home-frame",
                    "content_hash": f"{index + 20:064x}",
                }
            )
            for index, item in enumerate(snapshot.evidence)
        )
        todo_reviews = tuple(
            item.model_copy(
                update={
                    "verdict": (
                        TodoReviewVerdict.CONFIRMED
                        if index == 0
                        else TodoReviewVerdict.REVIEW_REQUIRED
                    ),
                    "reason_code": (
                        "entry_confirmed"
                        if index == 0
                        else "operation_result_not_visible"
                    ),
                }
            )
            for index, item in enumerate(snapshot.agent_review.todo_reviews)
        )
        snapshot = snapshot.model_copy(
            update={
                "evidence": evidence,
                "agent_review": snapshot.agent_review.model_copy(
                    update={"todo_reviews": todo_reviews}
                ),
            }
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        review = self.evaluation(
            decision,
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
        )
        self.assertEqual(review.state, PredicateResultState.MISSING)
        self.assertEqual(len(review.todo_instance_ids), 4)

    def test_todo_verdict_cannot_borrow_another_todo_screenshot(self) -> None:
        snapshot = self.snapshot()
        first = snapshot.agent_review.todo_reviews[0].model_copy(
            update={"artifact_refs": (SCREENSHOT_ID,)}
        )
        review = snapshot.agent_review.model_copy(
            update={
                "todo_reviews": (first, *snapshot.agent_review.todo_reviews[1:])
            }
        )

        decision = adjudicate_completion(
            snapshot.model_copy(update={"agent_review": review})
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            decision.blocking_predicates,
        )

    def test_json_entity_is_not_completion_evidence(self) -> None:
        snapshot = self.snapshot()
        artifact_id = snapshot.todos[0].evidence_refs[0]
        evidence = tuple(
            item.model_copy(update={"content_type": "application/json"})
            if item.artifact_id == artifact_id
            else item
            for item in snapshot.evidence
        )

        decision = adjudicate_completion(
            snapshot.model_copy(update={"evidence": evidence})
        )

        self.assertFalse(decision.accepted_done)
        excluded = next(
            item
            for item in decision.excluded_evidence
            if item.artifact_id == artifact_id
        )
        self.assertIn(
            EvidenceExclusionReason.CONTENT_TYPE_NOT_ALLOWED,
            excluded.reasons,
        )

    def test_promoted_adapter_review_without_registered_semantics_is_blocked(self) -> None:
        snapshot = self.snapshot(
            reviewer_principal_id="manager:promoted-adapter"
        )

        decision = adjudicate_completion(snapshot)

        self.assertFalse(decision.accepted_done)
        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.PROMOTED_ADAPTER_OPERATION_PROOF,
            decision.blocking_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500,
            decision.missing_predicates,
        )

    def test_entity_hash_failure_is_an_explicit_blocking_predicate(self) -> None:
        snapshot = self.snapshot()
        evidence = tuple(
            item.model_copy(
                update={
                    "content_hash": "f" * 64,
                    "integrity_valid": False,
                    "integrity_reason_code": "artifact_hash_mismatch",
                }
            )
            if item.artifact_id == SCREENSHOT_ID
            else item
            for item in snapshot.evidence
        )

        decision = adjudicate_completion(
            snapshot.model_copy(update={"evidence": evidence})
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.EVIDENCE_ENTITY_INTEGRITY,
            decision.blocking_predicates,
        )
        excluded = next(
            item
            for item in decision.excluded_evidence
            if item.artifact_id == SCREENSHOT_ID
        )
        self.assertEqual(
            excluded.integrity_reason_code, "artifact_hash_mismatch"
        )

    def test_duplicate_image_bytes_cannot_claim_two_todo_semantics(self) -> None:
        snapshot = self.snapshot()
        duplicate_digest = "a" * 64
        first_id = snapshot.todos[0].evidence_refs[0]
        evidence = tuple(
            item.model_copy(
                update={
                    "kind": "game-ui-step-before-raw",
                    "content_type": "image/png",
                    "content_hash": duplicate_digest,
                }
            )
            if item.artifact_id == first_id
            else item.model_copy(update={"content_hash": duplicate_digest})
            if item.artifact_id == SCREENSHOT_ID
            else item
            for item in snapshot.evidence
        )

        decision = adjudicate_completion(
            snapshot.model_copy(update={"evidence": evidence})
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.EVIDENCE_SEMANTIC_UNIQUENESS,
            decision.blocking_predicates,
        )
        duplicate_exclusions = {
            item.artifact_id
            for item in decision.excluded_evidence
            if EvidenceExclusionReason.DUPLICATE_SEMANTIC_REUSE
            in item.reasons
        }
        self.assertEqual(duplicate_exclusions, {first_id, SCREENSHOT_ID})

    def test_extra_stale_screenshot_is_preserved_but_does_not_poison_fresh_proof(self) -> None:
        snapshot = self.snapshot()
        stale = EvidenceArtifactFact(
            artifact_id="artifact-yesterday",
            kind="game-ui-daily-training-panel",
            content_type="image/png",
            captured_at=DAY_START - timedelta(minutes=1),
            source="previous-run",
            raw=True,
            game_id="StarRail",
            run_id="run-yesterday",
            run_attempt_id="attempt-yesterday",
            todo_instance_id="todo-verify-daily-task-list",
            game_day_key="2026-08-27",
            content_hash="e" * 64,
            integrity_valid=True,
            integrity_reason_code="verified",
        )
        snapshot = snapshot.model_copy(
            update={"evidence": (*snapshot.evidence, stale)}
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.ACCEPTED_DONE)
        self.assertIn(stale.artifact_id, decision.artifact_refs)
        self.assertIn(stale.artifact_id, decision.screenshot_artifact_refs)
        excluded = next(
            item
            for item in decision.excluded_evidence
            if item.artifact_id == stale.artifact_id
        )
        self.assertIn(EvidenceExclusionReason.RUN_MISMATCH, excluded.reasons)
        self.assertIn(
            EvidenceExclusionReason.CAPTURED_BEFORE_RESET, excluded.reasons
        )

    def test_previous_attempt_screenshot_cannot_satisfy_current_attempt(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(screenshot_attempt_id="attempt-previous")
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertFalse(decision.accepted_done)
        self.assertIn(
            CompletionPredicateCode.REQUIRED_TODOS_FRESH_EVIDENCE,
            decision.missing_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_CURRENT_SCOPE,
            decision.missing_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500,
            decision.missing_predicates,
        )
        self.assertIn(SCREENSHOT_ID, decision.screenshot_artifact_refs)
        self.assertIn(
            EvidenceExclusionReason.ATTEMPT_MISMATCH,
            decision.excluded_evidence[-1].reasons,
        )

    def test_pre_0400_evidence_cannot_satisfy_current_game_day(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(
                screenshot_day_key="2026-08-27",
                screenshot_captured_at=DAY_START - timedelta(seconds=1),
            )
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.STARRAIL_FIVE_REWARD_TIERS_CLAIMED,
            decision.missing_predicates,
        )
        excluded = decision.excluded_evidence[-1]
        self.assertIn(
            EvidenceExclusionReason.GAME_DAY_MISMATCH, excluded.reasons
        )
        self.assertIn(
            EvidenceExclusionReason.CAPTURED_BEFORE_RESET, excluded.reasons
        )

    def test_500_points_without_five_claimed_tiers_requires_review(self) -> None:
        decision = adjudicate_completion(self.snapshot(claimed_tiers=4))

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        self.assertEqual(
            self.evaluation(
                decision, CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500
            ).state,
            PredicateResultState.SATISFIED,
        )
        rewards = self.evaluation(
            decision,
            CompletionPredicateCode.STARRAIL_FIVE_REWARD_TIERS_CLAIMED,
        )
        self.assertEqual(rewards.state, PredicateResultState.MISSING)
        self.assertEqual(rewards.artifact_refs, (SCREENSHOT_ID,))

    def test_missing_agent_review_never_accepts_adapter_todo_results(self) -> None:
        decision = adjudicate_completion(self.snapshot(review_decision=None))

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_ACCEPTED,
            decision.missing_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500,
            decision.missing_predicates,
        )

    def test_human_required_todo_is_typed_as_blocked(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(
                todo_states={
                    "verify-daily-task-list": CompletionTodoState.HUMAN_REQUIRED
                }
            )
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.REQUIRED_TODOS_COMPLETED,
            decision.blocking_predicates,
        )
        blocked = self.evaluation(
            decision, CompletionPredicateCode.REQUIRED_TODOS_COMPLETED
        )
        self.assertEqual(
            blocked.todo_instance_ids, ("todo-verify-daily-task-list",)
        )
        self.assertIn(SCREENSHOT_ID, decision.artifact_refs)

    def test_current_agent_rejection_blocks_completion(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(review_decision=AgentReviewDecision.REJECTED)
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_ACCEPTED,
            decision.blocking_predicates,
        )

    def test_explicit_scoped_blocker_and_screenshot_refs_are_preserved(self) -> None:
        snapshot = self.snapshot()
        blocker = CompletionBlockerFact(
            blocker_id="blocker-login-gate",
            kind=BlockerKind.HUMAN_REQUIRED,
            code="login_gate",
            message="Operator login is required.",
            game_id="StarRail",
            run_id=RUN_ID,
            run_attempt_id=ATTEMPT_ID,
            game_day_key="2026-08-28",
            todo_instance_id="todo-verify-daily-task-list",
            artifact_refs=(SCREENSHOT_ID,),
        )
        snapshot = snapshot.model_copy(update={"blockers": (blocker,)})

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertEqual(decision.blocker_ids, (blocker.blocker_id,))
        self.assertIn(SCREENSHOT_ID, decision.artifact_refs)
        self.assertIn(SCREENSHOT_ID, decision.screenshot_artifact_refs)
        self.assertIn(
            CompletionPredicateCode.ACTIVE_BLOCKERS_CLEAR,
            decision.blocking_predicates,
        )

    def test_tool_log_cannot_impersonate_agent_visual_observation(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(
                screenshot_kind="tool-log-outcome",
                screenshot_content_type="text/plain",
            )
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertNotIn(SCREENSHOT_ID, decision.screenshot_artifact_refs)
        self.assertIn(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            decision.blocking_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500,
            decision.missing_predicates,
        )
        self.assertIn(
            CompletionPredicateCode.STARRAIL_FIVE_REWARD_TIERS_CLAIMED,
            decision.missing_predicates,
        )

    def test_new_game_can_register_its_own_predicate_policy(self) -> None:
        registry = DEFAULT_COMPLETION_POLICIES.with_policy(
            GameCompletionPolicy(
                game_id="ExampleGame",
                cadence="daily",
                timezone="Asia/Shanghai",
                reset_utc_offset_minutes=8 * 60,
                reset_local_time=time(4, 0),
                predicate_rules=(),
            )
        )

        decision = adjudicate_completion(
            self.snapshot(game_id="ExampleGame"), policies=registry
        )

        self.assertEqual(decision.outcome, CompletionOutcome.ACCEPTED_DONE)
        self.assertIn("ExampleGame", registry.game_ids)
        self.assertIsNone(DEFAULT_COMPLETION_POLICIES.resolve("ExampleGame"))

    def test_wrong_reset_boundary_is_a_blocking_contract_invariant(self) -> None:
        midnight = datetime(2026, 8, 28, 0, 0, tzinfo=CHINA)
        decision = adjudicate_completion(
            self.snapshot(
                game_day=GameDayWindow(
                    period_key="2026-08-28",
                    starts_at=midnight,
                    ends_at=midnight + timedelta(days=1),
                )
            )
        )

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.GAME_DAY_WINDOW,
            decision.blocking_predicates,
        )

    def test_required_todo_outside_frozen_attempt_lineage_is_not_completion(self) -> None:
        decision = adjudicate_completion(
            self.snapshot(
                todo_attempt_ids={
                    "spend-trailblaze-power": "attempt-previous"
                }
            )
        )

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        scope = self.evaluation(
            decision, CompletionPredicateCode.REQUIRED_TODOS_LINEAGE_SCOPE
        )
        self.assertEqual(scope.state, PredicateResultState.MISSING)
        self.assertEqual(
            scope.todo_instance_ids, ("todo-spend-trailblaze-power",)
        )

    def test_same_game_day_predecessor_run_evidence_is_safely_carried(self) -> None:
        snapshot = self.snapshot()
        prior_run_id = "run-prior-same-day"
        prior_attempt_id = "attempt-prior-same-day"
        target = snapshot.todos[0].model_copy(
            update={
                "run_id": prior_run_id,
                "run_attempt_id": prior_attempt_id,
            }
        )
        target_evidence_id = target.evidence_refs[0]
        evidence = tuple(
            item.model_copy(
                update={
                    "run_id": prior_run_id,
                    "run_attempt_id": prior_attempt_id,
                }
            )
            if item.artifact_id == target_evidence_id
            else item
            for item in snapshot.evidence
        )
        prior_attempt = RunAttemptCompletionFact(
            run_attempt_id=prior_attempt_id,
            run_id=prior_run_id,
            game_id=snapshot.game_id,
            cadence=snapshot.cadence,
            state=CompletionRunAttemptState.PARTIAL,
            started_at=DAY_START + timedelta(minutes=1),
            completed_at=DAY_START + timedelta(minutes=20),
        )
        snapshot = snapshot.model_copy(
            update={
                "attempt_lineage": (snapshot.current_attempt, prior_attempt),
                "todos": (target, *snapshot.todos[1:]),
                "evidence": evidence,
            }
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.ACCEPTED_DONE)
        self.assertIn(target_evidence_id, decision.carried_evidence_refs)
        self.assertNotIn(
            CompletionPredicateCode.REQUIRED_TODOS_LINEAGE_SCOPE,
            decision.missing_predicates,
        )

    def test_predecessor_from_previous_game_day_is_never_carried(self) -> None:
        snapshot = self.snapshot()
        stale_attempt = RunAttemptCompletionFact(
            run_attempt_id="attempt-prior-day",
            run_id="run-prior-day",
            game_id=snapshot.game_id,
            cadence=snapshot.cadence,
            state=CompletionRunAttemptState.COMPLETED,
            started_at=DAY_START - timedelta(hours=2),
            completed_at=DAY_START - timedelta(hours=1),
        )
        snapshot = snapshot.model_copy(
            update={
                "attempt_lineage": (snapshot.current_attempt, stale_attempt),
            }
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.RUN_ATTEMPT_LINEAGE_SCOPE,
            decision.blocking_predicates,
        )

    def test_predecessor_from_another_game_is_never_carried(self) -> None:
        snapshot = self.snapshot()
        foreign_attempt = RunAttemptCompletionFact(
            run_attempt_id="attempt-other-game",
            run_id="run-other-game",
            game_id="WW",
            cadence=snapshot.cadence,
            state=CompletionRunAttemptState.COMPLETED,
            started_at=DAY_START + timedelta(minutes=1),
            completed_at=DAY_START + timedelta(minutes=2),
        )
        snapshot = snapshot.model_copy(
            update={
                "attempt_lineage": (snapshot.current_attempt, foreign_attempt),
            }
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.BLOCKED)
        self.assertIn(
            CompletionPredicateCode.RUN_ATTEMPT_LINEAGE_SCOPE,
            decision.blocking_predicates,
        )

    def test_todo_cannot_reuse_evidence_owned_by_another_todo(self) -> None:
        snapshot = self.snapshot()
        target = snapshot.todos[0]
        reused = target.model_copy(update={"evidence_refs": (SCREENSHOT_ID,)})
        snapshot = snapshot.model_copy(
            update={"todos": (reused, *snapshot.todos[1:])}
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        evidence = self.evaluation(
            decision, CompletionPredicateCode.REQUIRED_TODOS_FRESH_EVIDENCE
        )
        self.assertEqual(evidence.state, PredicateResultState.MISSING)
        self.assertEqual(evidence.todo_instance_ids, (target.todo_instance_id,))

    def test_missing_opaque_todo_artifact_reference_is_not_dropped(self) -> None:
        snapshot = self.snapshot()
        target = snapshot.todos[0]
        missing_ref = "artifact-ledger-row-unavailable"
        target = target.model_copy(update={"evidence_refs": (missing_ref,)})
        snapshot = snapshot.model_copy(
            update={"todos": (target, *snapshot.todos[1:])}
        )

        decision = adjudicate_completion(snapshot)

        self.assertEqual(decision.outcome, CompletionOutcome.REVIEW_REQUIRED)
        self.assertIn(missing_ref, decision.artifact_refs)
        self.assertIn(
            CompletionPredicateCode.REQUIRED_TODOS_FRESH_EVIDENCE,
            decision.missing_predicates,
        )

    def test_naive_timestamp_is_rejected_at_typed_boundary(self) -> None:
        with self.assertRaises(ValidationError):
            GameDayWindow(
                period_key="invalid",
                starts_at=datetime(2026, 8, 28, 4, 0),
                ends_at=DAY_END,
            )


if __name__ == "__main__":
    unittest.main()
