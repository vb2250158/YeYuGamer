"""Pure resume/reconcile planning for an interrupted GameRun.

This module owns no persistence, process, lease, window, or Adapter behavior.
The Manager integration is expected to project one consistent read snapshot
into the immutable facts below, call :func:`plan_resume_reconciliation`, then
persist the returned intent with compare-and-swap against the echoed run and
attempt revisions.  Only after that commit may Manager mint a new fencing token
and dispatch the successor attempt.

An ``unknown`` ActionReceipt is never converted into an executable retry here.
It produces a reconcile-only intent.  A non-idempotent unknown additionally
requires a fresh observation before any later plan may authorize another
action.  Human takeover has the same fail-closed shape: only a recorded explicit
release permits reconcile-only work, and that release never authorizes a direct
click from the old checkpoint.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ResumeDomainModel(BaseModel):
    """Immutable, API-shaped fact or decision at the domain boundary."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class ResumeReconcileConflict(ValueError):
    """The projected snapshot is stale or cannot safely spawn a successor."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResumeRunState(StrEnum):
    PLANNED = "planned"
    PENDING_EXECUTION = "pending_execution"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    CANCELLED = "cancelled"
    DONE = "done"


class ResumeAttemptState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ResumeTodoState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    FORBIDDEN = "forbidden"


class TodoAttemptState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    FAILED = "failed"


class ResumeRisk(StrEnum):
    OBSERVE_ONLY = "observe_only"
    ROUTINE_ACTION = "routine_action"
    CONTROLLED_WRITE = "controlled_write"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


class ActionReceiptOutcome(StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ActionIdempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class CheckpointStage(StrEnum):
    PRE_ACTION = "pre_action"
    ACTION_DISPATCHED = "action_dispatched"
    POST_ACTION_OBSERVED = "post_action_observed"
    RECONCILED = "reconciled"


class ResumeBlockerKind(StrEnum):
    TECHNICAL = "technical"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    FORBIDDEN = "forbidden"


class ResumeDisposition(StrEnum):
    SKIP_COMPLETED = "skip_completed"
    SKIP_TERMINAL = "skip_terminal"
    ELIGIBLE_PENDING = "eligible_pending"
    RECONCILE_UNKNOWN = "reconcile_unknown"
    DEFERRED_REVIEW = "deferred_review"
    DEFERRED_HUMAN = "deferred_human"
    DEFERRED_FORBIDDEN = "deferred_forbidden"


class ReconcileRequirement(StrEnum):
    ACTION_RECEIPT_RECONCILIATION = "action_receipt_reconciliation"
    FRESH_OBSERVATION = "fresh_observation"
    CHECKPOINT_VALIDATION = "checkpoint_validation"
    BINDING_REVALIDATION = "binding_revalidation"
    NON_IDEMPOTENT_GUARD = "non_idempotent_guard"
    TODO_STATE_COMMIT = "todo_state_commit"


class SuccessorTodoMode(StrEnum):
    EXECUTE = "execute"
    RECONCILE_ONLY = "reconcile_only"


class GameRunResumeFact(ResumeDomainModel):
    run_id: str = Field(min_length=1, max_length=160)
    game_id: str = Field(min_length=1, max_length=80)
    game_day_key: str = Field(min_length=1, max_length=160)
    state: ResumeRunState
    revision: int = Field(ge=0)


class CurrentAttemptFact(ResumeDomainModel):
    run_attempt_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    game_id: str = Field(min_length=1, max_length=80)
    game_day_key: str = Field(min_length=1, max_length=160)
    state: ResumeAttemptState
    attempt_ordinal: int = Field(ge=1)
    revision: int = Field(ge=0)


class TodoAttemptResumeFact(ResumeDomainModel):
    todo_attempt_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    game_day_key: str = Field(min_length=1, max_length=160)
    attempt_number: int = Field(ge=1)
    state: TodoAttemptState
    revision: int = Field(ge=0)
    retryable: bool = False


class ActionReceiptResumeFact(ResumeDomainModel):
    action_receipt_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    todo_attempt_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    outcome: ActionReceiptOutcome
    idempotency: ActionIdempotency
    revision: int = Field(ge=0)


class CheckpointResumeFact(ResumeDomainModel):
    checkpoint_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    todo_attempt_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    stage: CheckpointStage
    revision: int = Field(ge=0)
    valid: bool = True
    action_receipt_id: str | None = Field(default=None, min_length=1, max_length=160)
    observation_refs: tuple[str, ...] = ()


class HumanBlockerReleaseFact(ResumeDomainModel):
    release_id: str = Field(min_length=1, max_length=160)
    blocker_id: str = Field(min_length=1, max_length=160)
    blocker_revision: int = Field(ge=0)
    run_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    game_day_key: str = Field(min_length=1, max_length=160)
    explicit: Literal[True] = True


class TodoBlockerResumeFact(ResumeDomainModel):
    blocker_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    game_day_key: str = Field(min_length=1, max_length=160)
    kind: ResumeBlockerKind
    revision: int = Field(ge=0)
    active: bool = True
    retryable: bool = False
    release: HumanBlockerReleaseFact | None = None

    @model_validator(mode="after")
    def _release_is_an_explicit_human_resolution(self) -> "TodoBlockerResumeFact":
        release = self.release
        if release is None:
            return self
        if self.kind != ResumeBlockerKind.HUMAN_REQUIRED:
            raise ValueError("only a human_required blocker accepts a human release")
        if self.active:
            raise ValueError("an explicitly released human blocker cannot remain active")
        if release.blocker_id != self.blocker_id:
            raise ValueError("human release blockerId does not match the blocker")
        if release.blocker_revision != self.revision:
            raise ValueError("human release does not fence the blocker revision")
        if (
            release.run_id != self.run_id
            or release.todo_instance_id != self.todo_instance_id
            or release.game_day_key != self.game_day_key
        ):
            raise ValueError("human release scope does not match the blocker")
        return self


class ResumeBindingFact(ResumeDomainModel):
    operation: str = Field(min_length=1, max_length=160)
    supports_resume: bool


class TodoResumeFact(ResumeDomainModel):
    todo_instance_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=160)
    game_id: str = Field(min_length=1, max_length=80)
    game_day_key: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    status: ResumeTodoState
    operation: str = Field(min_length=1, max_length=160)
    risk: ResumeRisk
    latest_attempt: TodoAttemptResumeFact | None = None
    action_receipt: ActionReceiptResumeFact | None = None
    checkpoint: CheckpointResumeFact | None = None
    blocker: TodoBlockerResumeFact | None = None
    binding: ResumeBindingFact | None = None

    @model_validator(mode="after")
    def _nested_facts_match_todo_scope(self) -> "TodoResumeFact":
        attempt = self.latest_attempt
        receipt = self.action_receipt
        checkpoint = self.checkpoint
        blocker = self.blocker

        if attempt is None and (receipt is not None or checkpoint is not None):
            raise ValueError("receipt/checkpoint requires a latest Todo attempt")
        if attempt is not None and (
            attempt.run_id != self.run_id
            or attempt.todo_instance_id != self.todo_instance_id
            or attempt.game_day_key != self.game_day_key
        ):
            raise ValueError("latest Todo attempt scope does not match the Todo")
        if receipt is not None and attempt is not None and (
            receipt.run_id != self.run_id
            or receipt.run_attempt_id != attempt.run_attempt_id
            or receipt.todo_instance_id != self.todo_instance_id
            or receipt.todo_attempt_id != attempt.todo_attempt_id
            or receipt.game_day_key != self.game_day_key
        ):
            raise ValueError("ActionReceipt scope does not match the latest Todo attempt")
        if checkpoint is not None and attempt is not None and (
            checkpoint.run_id != self.run_id
            or checkpoint.run_attempt_id != attempt.run_attempt_id
            or checkpoint.todo_instance_id != self.todo_instance_id
            or checkpoint.todo_attempt_id != attempt.todo_attempt_id
            or checkpoint.game_day_key != self.game_day_key
        ):
            raise ValueError("Checkpoint scope does not match the latest Todo attempt")
        if (
            checkpoint is not None
            and checkpoint.action_receipt_id is not None
            and (
                receipt is None
                or checkpoint.action_receipt_id != receipt.action_receipt_id
            )
        ):
            raise ValueError("Checkpoint references a different ActionReceipt")
        if blocker is not None and (
            blocker.run_id != self.run_id
            or blocker.todo_instance_id != self.todo_instance_id
            or blocker.game_day_key != self.game_day_key
        ):
            raise ValueError("blocker scope does not match the Todo")
        if self.binding is not None and self.binding.operation != self.operation:
            raise ValueError("resume binding operation does not match the Todo")
        return self


class ResumeReconcileSnapshot(ResumeDomainModel):
    run: GameRunResumeFact
    current_attempt: CurrentAttemptFact
    todos: tuple[TodoResumeFact, ...]

    @model_validator(mode="after")
    def _all_facts_share_run_and_game_day(self) -> "ResumeReconcileSnapshot":
        run = self.run
        attempt = self.current_attempt
        if (
            attempt.run_id != run.run_id
            or attempt.game_id != run.game_id
            or attempt.game_day_key != run.game_day_key
        ):
            raise ValueError("current attempt must belong to the same run and GameDay")
        todo_ids = [todo.todo_instance_id for todo in self.todos]
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("resume snapshot contains duplicate Todo instances")
        for todo in self.todos:
            if (
                todo.run_id != run.run_id
                or todo.game_id != run.game_id
                or todo.game_day_key != run.game_day_key
            ):
                raise ValueError("every Todo must belong to the same run and GameDay")
        return self


class ResumeReconcileRequest(ResumeDomainModel):
    run_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    expected_run_revision: int = Field(ge=0)
    expected_current_attempt_id: str = Field(min_length=1, max_length=160)
    expected_current_attempt_revision: int = Field(ge=0)


class TodoResumeDecision(ResumeDomainModel):
    todo_instance_id: str
    todo_revision: int
    disposition: ResumeDisposition
    reason_code: str
    requirements: tuple[ReconcileRequirement, ...] = ()
    blind_replay_allowed: Literal[False] = False


class SuccessorTodoAttemptIntent(ResumeDomainModel):
    todo_instance_id: str
    expected_todo_revision: int
    mode: SuccessorTodoMode
    predecessor_todo_attempt_id: str | None = None
    expected_predecessor_todo_attempt_revision: int | None = Field(
        default=None, ge=0
    )
    successor_todo_attempt_number: int = Field(ge=1)
    action_receipt_id: str | None = None
    expected_action_receipt_revision: int | None = Field(default=None, ge=0)
    checkpoint_id: str | None = None
    expected_checkpoint_revision: int | None = Field(default=None, ge=0)
    requirements: tuple[ReconcileRequirement, ...] = ()
    blind_replay_allowed: Literal[False] = False


class SuccessorAttemptIntent(ResumeDomainModel):
    run_id: str
    game_day_key: str
    predecessor_run_attempt_id: str
    expected_run_revision: int
    expected_predecessor_attempt_revision: int
    successor_attempt_ordinal: int = Field(ge=2)
    requires_new_fencing_token: Literal[True] = True
    todo_attempts: tuple[SuccessorTodoAttemptIntent, ...] = Field(min_length=1)


class ResumeReconcileDecision(ResumeDomainModel):
    run_id: str
    game_day_key: str
    source_run_revision: int
    source_current_attempt_id: str
    source_current_attempt_revision: int
    items: tuple[TodoResumeDecision, ...]
    completed_skip: tuple[str, ...]
    terminal_skip: tuple[str, ...]
    eligible_pending: tuple[str, ...]
    reconcile_unknown: tuple[str, ...]
    deferred_review: tuple[str, ...]
    deferred_human: tuple[str, ...]
    deferred_forbidden: tuple[str, ...]
    successor_attempt_intent: SuccessorAttemptIntent | None


_ACTIVE_ATTEMPT_STATES = {
    ResumeAttemptState.STARTING,
    ResumeAttemptState.RUNNING,
    ResumeAttemptState.CANCELLING,
}
_TERMINAL_RUN_STATES = {ResumeRunState.DONE, ResumeRunState.CANCELLED}
_ALLOWED_EXECUTION_RISKS = {
    ResumeRisk.OBSERVE_ONLY,
    ResumeRisk.ROUTINE_ACTION,
}
_UNCERTAIN_RECEIPT_OUTCOMES = {
    ActionReceiptOutcome.IN_FLIGHT,
    ActionReceiptOutcome.UNKNOWN,
    ActionReceiptOutcome.SUCCEEDED,
}


def _decision(
    todo: TodoResumeFact,
    disposition: ResumeDisposition,
    reason_code: str,
    *requirements: ReconcileRequirement,
) -> TodoResumeDecision:
    return TodoResumeDecision(
        todo_instance_id=todo.todo_instance_id,
        todo_revision=todo.revision,
        disposition=disposition,
        reason_code=reason_code,
        requirements=tuple(dict.fromkeys(requirements)),
    )


def _reconcile_requirements(todo: TodoResumeFact) -> tuple[ReconcileRequirement, ...]:
    requirements = [
        ReconcileRequirement.ACTION_RECEIPT_RECONCILIATION,
        ReconcileRequirement.FRESH_OBSERVATION,
        ReconcileRequirement.CHECKPOINT_VALIDATION,
    ]
    receipt = todo.action_receipt
    if (
        receipt is not None
        and receipt.idempotency == ActionIdempotency.NON_IDEMPOTENT
    ):
        requirements.append(ReconcileRequirement.NON_IDEMPOTENT_GUARD)
    return tuple(requirements)


def _classify_todo(todo: TodoResumeFact) -> TodoResumeDecision:
    if todo.status == ResumeTodoState.COMPLETED:
        return _decision(todo, ResumeDisposition.SKIP_COMPLETED, "already_completed")
    if todo.status in {ResumeTodoState.SKIPPED, ResumeTodoState.NOT_APPLICABLE}:
        return _decision(todo, ResumeDisposition.SKIP_TERMINAL, "terminal_non_action")

    blocker = todo.blocker
    if (
        todo.status == ResumeTodoState.FORBIDDEN
        or todo.risk == ResumeRisk.FORBIDDEN
        or (
            blocker is not None
            and blocker.active
            and blocker.kind == ResumeBlockerKind.FORBIDDEN
        )
    ):
        return _decision(
            todo, ResumeDisposition.DEFERRED_FORBIDDEN, "forbidden_by_policy"
        )

    human_gated = todo.status == ResumeTodoState.HUMAN_REQUIRED or (
        blocker is not None and blocker.kind == ResumeBlockerKind.HUMAN_REQUIRED
    )
    if human_gated:
        if blocker is None or blocker.release is None:
            return _decision(
                todo,
                ResumeDisposition.DEFERRED_HUMAN,
                "explicit_human_release_required",
            )
        return _decision(
            todo,
            ResumeDisposition.RECONCILE_UNKNOWN,
            "human_takeover_released_reconcile_first",
            ReconcileRequirement.FRESH_OBSERVATION,
            ReconcileRequirement.CHECKPOINT_VALIDATION,
            ReconcileRequirement.BINDING_REVALIDATION,
        )

    if todo.status == ResumeTodoState.REVIEW_REQUIRED or (
        blocker is not None
        and blocker.active
        and blocker.kind == ResumeBlockerKind.REVIEW_REQUIRED
    ):
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "review_resolution_required"
        )

    attempt = todo.latest_attempt
    receipt = todo.action_receipt
    if todo.status in {
        ResumeTodoState.IN_PROGRESS,
        ResumeTodoState.RECONCILING,
    } or (attempt is not None and attempt.state == TodoAttemptState.RUNNING):
        return _decision(
            todo,
            ResumeDisposition.RECONCILE_UNKNOWN,
            "interrupted_action_requires_reconciliation",
            *_reconcile_requirements(todo),
        )
    if receipt is not None and receipt.outcome in _UNCERTAIN_RECEIPT_OUTCOMES:
        requirements = list(_reconcile_requirements(todo))
        if receipt.outcome == ActionReceiptOutcome.SUCCEEDED:
            requirements.append(ReconcileRequirement.TODO_STATE_COMMIT)
        return _decision(
            todo,
            ResumeDisposition.RECONCILE_UNKNOWN,
            (
                "action_succeeded_todo_not_committed"
                if receipt.outcome == ActionReceiptOutcome.SUCCEEDED
                else "action_result_unknown"
            ),
            *requirements,
        )

    if blocker is not None and blocker.active:
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "active_blocker_requires_review"
        )
    if todo.risk not in _ALLOWED_EXECUTION_RISKS:
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "risk_not_resume_executable"
        )
    if todo.binding is None:
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "resume_binding_missing"
        )

    has_prior_attempt = attempt is not None
    if has_prior_attempt and not todo.binding.supports_resume:
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "binding_resume_not_supported"
        )

    if todo.status == ResumeTodoState.PENDING:
        if receipt is not None and receipt.outcome == ActionReceiptOutcome.FAILED:
            if attempt is None or not attempt.retryable:
                return _decision(
                    todo,
                    ResumeDisposition.DEFERRED_REVIEW,
                    "failed_attempt_not_retryable",
                )
        return _decision(
            todo, ResumeDisposition.ELIGIBLE_PENDING, "pending_and_resume_eligible"
        )

    if todo.status == ResumeTodoState.BLOCKED:
        if attempt is not None and attempt.retryable and (
            blocker is None or (not blocker.active and blocker.retryable)
        ):
            return _decision(
                todo,
                ResumeDisposition.ELIGIBLE_PENDING,
                "resolved_retryable_blocker",
            )
        return _decision(
            todo, ResumeDisposition.DEFERRED_REVIEW, "blocked_not_retry_eligible"
        )

    return _decision(
        todo, ResumeDisposition.DEFERRED_REVIEW, "todo_state_not_resume_eligible"
    )


def _todo_attempt_intent(
    todo: TodoResumeFact, decision: TodoResumeDecision
) -> SuccessorTodoAttemptIntent:
    attempt = todo.latest_attempt
    receipt = todo.action_receipt
    checkpoint = todo.checkpoint
    return SuccessorTodoAttemptIntent(
        todo_instance_id=todo.todo_instance_id,
        expected_todo_revision=todo.revision,
        mode=(
            SuccessorTodoMode.EXECUTE
            if decision.disposition == ResumeDisposition.ELIGIBLE_PENDING
            else SuccessorTodoMode.RECONCILE_ONLY
        ),
        predecessor_todo_attempt_id=(
            attempt.todo_attempt_id if attempt is not None else None
        ),
        expected_predecessor_todo_attempt_revision=(
            attempt.revision if attempt is not None else None
        ),
        successor_todo_attempt_number=(
            attempt.attempt_number + 1 if attempt is not None else 1
        ),
        action_receipt_id=(
            receipt.action_receipt_id if receipt is not None else None
        ),
        expected_action_receipt_revision=(
            receipt.revision if receipt is not None else None
        ),
        checkpoint_id=(checkpoint.checkpoint_id if checkpoint is not None else None),
        expected_checkpoint_revision=(
            checkpoint.revision if checkpoint is not None else None
        ),
        requirements=decision.requirements,
    )


def _ids_for(
    decisions: tuple[TodoResumeDecision, ...], disposition: ResumeDisposition
) -> tuple[str, ...]:
    return tuple(
        decision.todo_instance_id
        for decision in decisions
        if decision.disposition == disposition
    )


def plan_resume_reconciliation(
    snapshot: ResumeReconcileSnapshot,
    request: ResumeReconcileRequest,
) -> ResumeReconcileDecision:
    """Partition one fenced same-run/same-GameDay snapshot into safe intents."""

    run = snapshot.run
    attempt = snapshot.current_attempt
    if request.run_id != run.run_id:
        raise ResumeReconcileConflict("run_scope_mismatch", "resume runId is stale")
    if request.game_day_key != run.game_day_key:
        raise ResumeReconcileConflict(
            "game_day_scope_mismatch", "resume GameDay is stale"
        )
    if request.expected_run_revision != run.revision:
        raise ResumeReconcileConflict(
            "run_revision_mismatch", "GameRun revision changed before resume planning"
        )
    if request.expected_current_attempt_id != attempt.run_attempt_id:
        raise ResumeReconcileConflict(
            "attempt_fence_mismatch", "another run attempt superseded the request"
        )
    if request.expected_current_attempt_revision != attempt.revision:
        raise ResumeReconcileConflict(
            "attempt_revision_mismatch",
            "current attempt revision changed before resume planning",
        )
    if run.state in _TERMINAL_RUN_STATES:
        raise ResumeReconcileConflict(
            "terminal_run", "terminal GameRuns cannot spawn successor attempts"
        )
    if attempt.state in _ACTIVE_ATTEMPT_STATES:
        raise ResumeReconcileConflict(
            "attempt_still_active",
            "an active current attempt cannot be replaced by a successor",
        )

    decisions = tuple(_classify_todo(todo) for todo in snapshot.todos)
    by_id = {todo.todo_instance_id: todo for todo in snapshot.todos}
    successor_decisions = tuple(
        decision
        for decision in decisions
        if decision.disposition
        in {
            ResumeDisposition.ELIGIBLE_PENDING,
            ResumeDisposition.RECONCILE_UNKNOWN,
        }
    )
    successor_intent = (
        SuccessorAttemptIntent(
            run_id=run.run_id,
            game_day_key=run.game_day_key,
            predecessor_run_attempt_id=attempt.run_attempt_id,
            expected_run_revision=run.revision,
            expected_predecessor_attempt_revision=attempt.revision,
            successor_attempt_ordinal=attempt.attempt_ordinal + 1,
            todo_attempts=tuple(
                _todo_attempt_intent(by_id[decision.todo_instance_id], decision)
                for decision in successor_decisions
            ),
        )
        if successor_decisions
        else None
    )

    return ResumeReconcileDecision(
        run_id=run.run_id,
        game_day_key=run.game_day_key,
        source_run_revision=run.revision,
        source_current_attempt_id=attempt.run_attempt_id,
        source_current_attempt_revision=attempt.revision,
        items=decisions,
        completed_skip=_ids_for(decisions, ResumeDisposition.SKIP_COMPLETED),
        terminal_skip=_ids_for(decisions, ResumeDisposition.SKIP_TERMINAL),
        eligible_pending=_ids_for(decisions, ResumeDisposition.ELIGIBLE_PENDING),
        reconcile_unknown=_ids_for(decisions, ResumeDisposition.RECONCILE_UNKNOWN),
        deferred_review=_ids_for(decisions, ResumeDisposition.DEFERRED_REVIEW),
        deferred_human=_ids_for(decisions, ResumeDisposition.DEFERRED_HUMAN),
        deferred_forbidden=_ids_for(
            decisions, ResumeDisposition.DEFERRED_FORBIDDEN
        ),
        successor_attempt_intent=successor_intent,
    )
