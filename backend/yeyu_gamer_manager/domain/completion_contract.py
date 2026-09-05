"""Typed facts and decisions for evidence-gated game completion.

The models in this module deliberately contain no persistence or process-control
behavior.  A Manager integration must first project its current GameRun,
RunAttempt, TodoAttempt, artifact and Agent-review records into these immutable
facts.  The adjudicator can then decide whether the business completion contract
is satisfied without trusting an Adapter exit code or mutating hot state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ContractModel(BaseModel):
    """Immutable API-shaped model used at the adjudicator boundary."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


StrictMetricValue = (
    Annotated[bool, Strict()]
    | Annotated[int, Strict()]
    | Annotated[str, Strict()]
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("completion contract timestamps must be timezone-aware")
    return value


class CompletionOutcome(StrEnum):
    ACCEPTED_DONE = "accepted_done"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class PredicateResultState(StrEnum):
    SATISFIED = "satisfied"
    MISSING = "missing"
    BLOCKED = "blocked"


class CompletionPredicateCode(StrEnum):
    POLICY_AVAILABLE = "policy_available"
    GAME_DAY_WINDOW = "game_day_window"
    CURRENT_ATTEMPT_SCOPE = "current_attempt_scope"
    CURRENT_ATTEMPT_COMPLETED = "current_attempt_completed"
    RUN_ATTEMPT_LINEAGE_SCOPE = "run_attempt_lineage_scope"
    FRESH_EVIDENCE = "fresh_evidence"
    EVIDENCE_ENTITY_INTEGRITY = "evidence_entity_integrity"
    EVIDENCE_SEMANTIC_UNIQUENESS = "evidence_semantic_uniqueness"
    REQUIRED_TODOS_PRESENT = "required_todos_present"
    REQUIRED_TODOS_COMPLETED = "required_todos_completed"
    REQUIRED_TODOS_CURRENT_SCOPE = "required_todos_current_scope"
    REQUIRED_TODOS_LINEAGE_SCOPE = "required_todos_lineage_scope"
    REQUIRED_TODOS_CURRENT_GAME_DAY = "required_todos_current_game_day"
    REQUIRED_TODOS_FRESH_EVIDENCE = "required_todos_fresh_evidence"
    ACTIVE_BLOCKERS_CLEAR = "active_blockers_clear"
    AGENT_REVIEW_CURRENT_SCOPE = "agent_review_current_scope"
    AGENT_REVIEW_ACCEPTED = "agent_review_accepted"
    AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED = (
        "agent_review_required_todos_confirmed"
    )
    PROMOTED_ADAPTER_OPERATION_PROOF = "promoted_adapter_operation_proof"
    STARRAIL_DAILY_TRAINING_500 = "starrail_daily_training_500"
    STARRAIL_FIVE_REWARD_TIERS_CLAIMED = (
        "starrail_five_reward_tiers_claimed"
    )


class EvidenceExclusionReason(StrEnum):
    ACCOUNT_MISMATCH = "account_mismatch"
    GAME_MISMATCH = "game_mismatch"
    RUN_MISMATCH = "run_mismatch"
    ATTEMPT_MISMATCH = "attempt_mismatch"
    GAME_DAY_MISMATCH = "game_day_mismatch"
    CAPTURED_BEFORE_RESET = "captured_before_reset"
    CAPTURED_AFTER_PERIOD = "captured_after_period"
    ENTITY_INTEGRITY_FAILED = "entity_integrity_failed"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    DUPLICATE_SEMANTIC_REUSE = "duplicate_semantic_reuse"


class CompletionTodoState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    CASCADED = "cascaded"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompletionRunAttemptState(StrEnum):
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


class AgentReviewDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVIEW_REQUIRED = "review_required"


class TodoReviewVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    REVIEW_REQUIRED = "review_required"


class BlockerKind(StrEnum):
    BLOCKED = "blocked"
    HUMAN_REQUIRED = "human_required"
    SAFETY_GATE = "safety_gate"
    CONTRACT_INVARIANT = "contract_invariant"


class GameDayWindow(ContractModel):
    period_key: str = Field(min_length=1, max_length=160)
    starts_at: datetime
    ends_at: datetime

    _starts_at_is_aware = field_validator("starts_at")(_aware)
    _ends_at_is_aware = field_validator("ends_at")(_aware)

    @model_validator(mode="after")
    def _ordered_window(self) -> "GameDayWindow":
        if self.ends_at <= self.starts_at:
            raise ValueError("game day must end after it starts")
        return self


class RunAttemptCompletionFact(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    game_id: str = Field(min_length=1, max_length=80)
    cadence: Literal["daily", "weekly"] = "daily"
    state: CompletionRunAttemptState
    started_at: datetime
    completed_at: datetime | None = None

    _started_at_is_aware = field_validator("started_at")(_aware)
    _completed_at_is_aware = field_validator("completed_at")(
        lambda value: None if value is None else _aware(value)
    )


class TodoCompletionFact(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    game_id: str = Field(min_length=1, max_length=80)
    game_day_key: str = Field(min_length=1, max_length=160)
    required: bool
    status: CompletionTodoState
    run_id: str | None = Field(default=None, min_length=1, max_length=160)
    run_attempt_id: str | None = Field(default=None, min_length=1, max_length=160)
    completed_at: datetime | None = None
    evidence_refs: tuple[str, ...] = ()
    reason_code: str = Field(default="", max_length=160)
    reason: str = Field(default="", max_length=2000)

    _completed_at_is_aware = field_validator("completed_at")(
        lambda value: None if value is None else _aware(value)
    )


class EvidenceArtifactFact(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=160)
    content_type: str = Field(min_length=1, max_length=160)
    captured_at: datetime
    source: str = Field(default="", max_length=240)
    raw: bool = True
    game_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    todo_instance_id: str = Field(min_length=1, max_length=200)
    game_day_key: str = Field(min_length=1, max_length=160)
    content_hash: str = Field(
        default="", pattern=r"^(?:[0-9a-f]{64})?$", max_length=64
    )
    integrity_valid: bool = False
    integrity_reason_code: str = Field(default="not_verified", max_length=160)

    _captured_at_is_aware = field_validator("captured_at")(_aware)

    @property
    def is_screenshot(self) -> bool:
        return self.content_type.lower() in {"image/png", "image/jpeg"}

    @model_validator(mode="after")
    def _integrity_fact_is_explicit(self) -> "EvidenceArtifactFact":
        if self.integrity_valid and not self.content_hash:
            raise ValueError("verified evidence requires its re-read SHA-256")
        if not self.integrity_valid and not self.integrity_reason_code:
            raise ValueError("unverified evidence requires an integrity reason")
        return self


class AgentPredicateObservation(ContractModel):
    """A structured Agent observation tied to opaque artifact IDs.

    Adapter-produced text must not be converted into one of these observations.
    The Manager integration should create it only from the typed visual-review
    result owned by the Agent claim.
    """

    predicate_id: str = Field(min_length=1, max_length=200)
    metrics: dict[str, StrictMetricValue]
    artifact_refs: tuple[str, ...] = Field(min_length=1)


class AgentTodoReview(ContractModel):
    """One semantic verdict for one frozen required Todo."""

    todo_instance_id: str = Field(min_length=1, max_length=200)
    verdict: TodoReviewVerdict
    reason_code: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    artifact_refs: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("artifact_refs")
    @classmethod
    def _artifact_refs_are_unique_and_opaque(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Todo review artifactRefs contains duplicates")
        if any(
            not value or len(value) > 160 or "/" in value or "\\" in value
            for value in values
        ):
            raise ValueError("Todo review artifactRefs must contain opaque IDs")
        return values


class AgentReviewFact(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    review_id: str = Field(min_length=1, max_length=160)
    reviewer_principal_id: str = Field(min_length=1, max_length=160)
    decision: AgentReviewDecision
    game_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    reviewed_at: datetime
    artifact_refs: tuple[str, ...] = ()
    observations: tuple[AgentPredicateObservation, ...] = ()
    todo_reviews: tuple[AgentTodoReview, ...] = ()

    _reviewed_at_is_aware = field_validator("reviewed_at")(_aware)

    @model_validator(mode="after")
    def _todo_review_ids_are_unique(self) -> "AgentReviewFact":
        todo_ids = [item.todo_instance_id for item in self.todo_reviews]
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("Agent review contains duplicate Todo verdicts")
        return self


class CompletionBlockerFact(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    blocker_id: str = Field(min_length=1, max_length=160)
    kind: BlockerKind
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=2000)
    active: bool = True
    game_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    todo_instance_id: str | None = Field(default=None, min_length=1, max_length=200)
    artifact_refs: tuple[str, ...] = ()


class CompletionContractSnapshot(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    game_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=160)
    cadence: Literal["daily", "weekly"] = "daily"
    game_day: GameDayWindow
    current_attempt: RunAttemptCompletionFact | None
    attempt_lineage: tuple[RunAttemptCompletionFact, ...] = ()
    todos: tuple[TodoCompletionFact, ...]
    evidence: tuple[EvidenceArtifactFact, ...]
    agent_review: AgentReviewFact | None = None
    blockers: tuple[CompletionBlockerFact, ...] = ()

    @model_validator(mode="after")
    def _attempt_lineage_is_unique(self) -> "CompletionContractSnapshot":
        attempt_ids = [item.run_attempt_id for item in self.attempt_lineage]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("completion attempt lineage contains duplicates")
        if (
            self.current_attempt is not None
            and self.attempt_lineage
            and self.current_attempt.run_attempt_id not in set(attempt_ids)
        ):
            raise ValueError("current attempt is missing from the attempt lineage")
        return self


class PredicateEvaluation(ContractModel):
    code: CompletionPredicateCode
    state: PredicateResultState
    message: str = Field(min_length=1, max_length=2000)
    artifact_refs: tuple[str, ...] = ()
    todo_instance_ids: tuple[str, ...] = ()


class ExcludedEvidence(ContractModel):
    artifact_id: str
    reasons: tuple[EvidenceExclusionReason, ...] = Field(min_length=1)
    integrity_reason_code: str = Field(default="", max_length=160)


class CompletionContractDecision(ContractModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    outcome: CompletionOutcome
    accepted_done: bool
    policy_id: str = ""
    policy_version: str = ""
    game_id: str
    run_id: str
    run_attempt_id: str | None
    attempt_lineage_ids: tuple[str, ...] = ()
    game_day_key: str
    evaluations: tuple[PredicateEvaluation, ...]
    missing_predicates: tuple[CompletionPredicateCode, ...]
    blocking_predicates: tuple[CompletionPredicateCode, ...]
    blocker_ids: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    screenshot_artifact_refs: tuple[str, ...]
    accepted_evidence_refs: tuple[str, ...]
    current_attempt_evidence_refs: tuple[str, ...] = ()
    carried_evidence_refs: tuple[str, ...] = ()
    supporting_artifact_refs: tuple[str, ...]
    excluded_evidence: tuple[ExcludedEvidence, ...]
    review_id: str | None = None
    message: str

    @model_validator(mode="after")
    def _outcome_matches_boolean(self) -> "CompletionContractDecision":
        expected = self.outcome == CompletionOutcome.ACCEPTED_DONE
        if self.accepted_done != expected:
            raise ValueError("accepted_done must exactly match the accepted_done outcome")
        return self
