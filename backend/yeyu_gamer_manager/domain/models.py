from __future__ import annotations

import ntpath
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .completion_contract import (
    AgentTodoReview,
    CompletionContractDecision,
    StrictMetricValue,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _redact_fencing_material(value: Any) -> Any:
    """Remove fencing secrets from a public projection, including legacy rows."""

    if isinstance(value, dict):
        return {
            key: _redact_fencing_material(item)
            for key, item in value.items()
            if "fencingtoken"
            not in "".join(
                character.lower()
                for character in str(key)
                if character.isalnum()
            )
        }
    if isinstance(value, (list, tuple)):
        return [_redact_fencing_material(item) for item in value]
    return value


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        use_enum_values=True,
    )


class EntityState(StrEnum):
    PLANNED = "planned"
    PENDING_EXECUTION = "pending_execution"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"
    CANCELLED = "cancelled"


class Cadence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    HUMAN_REQUIRED = "human_required"


class RequestMode(StrEnum):
    PLAN = "plan"
    EXECUTE = "execute"


class MetaResponse(ApiModel):
    name: str = "YeYu Gamer Manager"
    version: str
    api_version: str = "v1"
    manager_id: str
    started_at: datetime
    host_policy: str = "loopback-only"
    web_gui_available: bool
    legacy_execution_enabled: bool
    openapi_sha256: str = ""
    web_asset_sha256: str = ""


class HealthCheck(ApiModel):
    status: Literal["ok", "degraded", "error"]
    detail: str


class HealthResponse(ApiModel):
    status: Literal["ok", "degraded", "error"]
    manager: str
    storage: str
    event_stream: str
    checked_at: datetime
    checks: dict[str, HealthCheck]


class GameSummary(ApiModel):
    game_id: str
    display_name: str
    order_index: int
    enabled: bool
    runtime_state: str = "planned"
    acceptance_state: str = "unknown"
    review_state: str = "none"
    reward_claimed: bool = False
    next_action: str = ""
    updated_at: datetime
    policy: dict[str, Any] = Field(default_factory=dict)
    todo_summary: dict[str, Any] = Field(default_factory=dict)


class BatchRunMembershipRecord(ApiModel):
    batch_id: str
    run_id: str
    ordinal: int
    role: Literal["initial", "continuation"]
    state: Literal[
        "queued",
        "resume_pending",
        "active",
        "terminal",
        "cancelled",
        "reconciliation_required",
    ]
    resume_intent_id: str | None = None
    latest_run_attempt_id: str | None = None
    terminal_outcome: str | None = None
    created_at: datetime
    updated_at: datetime


class BatchCancelRequestRecord(ApiModel):
    cancel_request_id: str
    batch_id: str
    state: Literal[
        "requested",
        "signal_delivered",
        "reconciliation_required",
        "acknowledged",
        "sealed",
    ]
    reason: str
    requested_by: str
    active_run_attempt_ids: list[str] = Field(default_factory=list)
    work_item_id: str | None = None
    delivery_attempt_count: int = 0
    last_delivery_at: datetime | None = None
    next_retry_at: datetime | None = None
    last_error_class: str = ""
    created_at: datetime
    updated_at: datetime


class BatchRecord(ApiModel):
    batch_id: str
    cadence: Cadence
    mode: RequestMode = RequestMode.PLAN
    state: EntityState
    game_ids: list[str]
    requested_by: str
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)
    root_batch_id: str | None = None
    predecessor_batch_id: str | None = None
    continuation_ordinal: int = 0
    continuation_resume_intent_id: str | None = None
    member_run_ids: list[str] = Field(default_factory=list)
    run_memberships: list[BatchRunMembershipRecord] = Field(default_factory=list)
    cancel_request: BatchCancelRequestRecord | None = None


class GameRunRecord(ApiModel):
    completion_contract: dict[str, Any] | None = None
    run_id: str
    game_id: str
    account_id: str = "default"
    account_snapshot: dict[str, Any] = Field(default_factory=dict)
    cadence: Cadence
    state: EntityState
    mode: RequestMode
    requested_by: str
    created_at: datetime
    updated_at: datetime
    exit_code: int | None = None
    message: str = ""
    # ``todo_instance_ids`` is the executable subset dispatched to Adapter Host.
    # The completion scope is frozen separately so completed, review-only,
    # forbidden and optional Todos cannot disappear from later adjudication.
    todo_instance_ids: list[str] = Field(default_factory=list)
    completed_todo_instance_ids: list[str] = Field(default_factory=list)
    completion_todo_instance_ids: list[str] = Field(default_factory=list)
    completion_scope_version: int = Field(default=0, ge=0)


class WorkItemKind(StrEnum):
    RUN_GAME = "run_game"
    RUN_BATCH = "run_batch"
    DIAGNOSE_GAME = "diagnose_game"
    CANCEL_RUN = "cancel_run"
    OBSERVATION = "observation"
    INCIDENT_REVIEW = "incident_review"
    EVIDENCE_REVIEW = "evidence_review"
    REPAIR_VALIDATION = "repair_validation"


class AgentWorkItemRecord(ApiModel):
    work_item_id: str
    kind: WorkItemKind
    state: EntityState
    game_id: str | None = None
    cadence: Cadence | None = None
    run_id: str | None = None
    requested_by: str
    note: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    allowed_capability_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)

    _result_redacts_fencing_material = field_validator("result", mode="before")(
        _redact_fencing_material
    )


class ClaimDecisionRecord(ApiModel):
    decision_id: str
    claim_id: str
    decision: Literal["accepted", "rejected", "review_required"]
    reason: str
    evidence_ids: list[str]
    requested_by: str
    created_at: datetime


class CompletionPredicateReview(ApiModel):
    predicate_id: str = Field(min_length=1, max_length=200)
    metrics: dict[str, StrictMetricValue] = Field(min_length=1, max_length=20)
    artifact_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("metrics")
    @classmethod
    def metric_names_are_bounded(cls, values: dict[str, StrictMetricValue]):
        for key in values:
            if not key or len(key) > 80:
                raise ValueError("completionReview metric names must be non-empty and bounded")
        return values

    @field_validator("artifact_refs")
    @classmethod
    def artifact_refs_are_opaque(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("completionReview artifactRefs contains duplicates")
        for value in values:
            if not value or len(value) > 160 or "/" in value or "\\" in value:
                raise ValueError(
                    "completionReview artifactRefs must contain opaque IDs"
                )
        return values


class CompletionReviewSubmission(ApiModel):
    account_id: str | None = Field(default=None, min_length=1, max_length=80)
    game_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=160)
    run_attempt_id: str = Field(min_length=1, max_length=160)
    game_day_key: str = Field(min_length=1, max_length=160)
    predicates: list[CompletionPredicateReview] = Field(
        default_factory=list, max_length=20
    )
    todo_reviews: list[AgentTodoReview] = Field(
        default_factory=list, max_length=100
    )

    @model_validator(mode="after")
    def predicate_ids_are_unique(self) -> "CompletionReviewSubmission":
        predicate_ids = [item.predicate_id for item in self.predicates]
        if len(predicate_ids) != len(set(predicate_ids)):
            raise ValueError("completionReview predicateId values must be unique")
        todo_ids = [item.todo_instance_id for item in self.todo_reviews]
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("completionReview contains duplicate Todo verdicts")
        return self


class CompletionReviewRecord(ApiModel):
    account_id: str = "default"
    completion_review_id: str
    work_item_id: str
    claim_id: str
    decision_id: str
    reviewer_principal_id: str
    decision: Literal["accepted", "rejected", "review_required"]
    game_id: str
    run_id: str
    run_attempt_id: str
    game_day_key: str
    predicates: list[CompletionPredicateReview]
    todo_reviews: list[AgentTodoReview]
    artifact_refs: list[str]
    reviewed_at: datetime
    created_at: datetime


class CompletionAdjudicationRecord(ApiModel):
    account_id: str = "default"
    completion_adjudication_id: str
    batch_id: str
    game_id: str
    run_id: str
    run_attempt_id: str | None = None
    game_day_key: str
    contract: CompletionContractDecision
    created_at: datetime


class AutomationAssessmentRecord(ApiModel):
    assessment_id: str
    todo_instance_id: str
    work_item_id: str
    claim_id: str
    decision_id: str
    game_id: str
    run_id: str | None = None
    difficulty: Literal["easy", "moderate", "hard", "unsupported", "unknown"]
    automatable: bool | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    basis: list[str] = Field(default_factory=list)
    failure_stage: str = ""
    issue: str = ""
    recommendation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    requested_by: str
    created_at: datetime


class CapabilityDefinition(ApiModel):
    capability_id: str
    version: str = "1.0"
    description: str
    display_name: str
    risk: Literal[
        "observe_only",
        "controlled_write",
        "routine_action",
        "approval_required",
        "forbidden",
    ]
    enabled: bool
    requires_idempotency_key: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    pre_evidence: list[str] = Field(default_factory=list)
    post_evidence: list[str] = Field(default_factory=list)
    implementation_hash: str = ""


class CapabilityInvocationRecord(ApiModel):
    invocation_id: str
    capability: str
    state: EntityState
    arguments: dict[str, Any]
    requested_by: str
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] = Field(default_factory=dict)


class IncidentRecord(ApiModel):
    incident_id: str
    fingerprint: str
    title: str
    game_id: str | None = None
    state: str
    severity: str
    occurrence_count: int = 1
    retry_eligibility: str = "manual-review"
    last_seen_at: datetime


class EvidenceArtifactRecord(ApiModel):
    account_id: str = "default"
    artifact_id: str
    kind: str
    captured_at: datetime
    source: str
    raw: bool
    content_type: str
    game_id: str | None = None
    run_id: str | None = None
    run_attempt_id: str | None = None
    todo_instance_id: str | None = None
    todo_attempt_id: str | None = None
    game_day_key: str | None = None
    verdict: str = "unknown"
    content_hash: str = Field(default="", alias="hash")
    size_bytes: int = 0
    file_name: str = ""


class NotificationDeliveryRecord(ApiModel):
    notification_id: str
    batch_id: str
    seal_version: int = Field(ge=1)
    channel: Literal["email"] = "email"
    recipient_binding_id: str
    message_id: str
    state: Literal["draft", "sending", "sent", "failed"]
    dispatch_gate: Literal[
        "automatic", "disabled", "secret_missing", "manual_review", "stale_batch"
    ]
    outcome: Literal["completed", "blocked"]
    subject: str
    attachment_refs: list[str] = Field(default_factory=list)
    attempt_count: int = Field(ge=0, le=3)
    next_attempt_at: datetime | None = None
    last_error_class: str = ""
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class NotificationAttemptRecord(ApiModel):
    attempt_id: str
    notification_id: str
    attempt_number: int = Field(ge=1, le=3)
    state: Literal["sending", "sent", "failed"]
    outcome: Literal[
        "sent", "transient_failure", "permanent_failure", "ambiguous"
    ] | None = None
    error_class: str = ""
    retry_scheduled_at: datetime | None = None
    transport_receipt_hash: str = ""
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime


def _reject_fencing_material(value: Any) -> Any:
    """Fail closed if a public diagnostic projection contains a fencing secret."""

    def visit(candidate: Any) -> None:
        if isinstance(candidate, dict):
            for key, child in candidate.items():
                normalized = "".join(
                    character.lower()
                    for character in str(key)
                    if character.isalnum()
                )
                if "fencingtoken" in normalized:
                    raise ValueError("public attempt records cannot contain fencing material")
                visit(child)
        elif isinstance(candidate, (list, tuple)):
            for child in candidate:
                visit(child)

    visit(value)
    return value


class RunAttemptRecord(ApiModel):
    account_id: str = "default"
    run_attempt_id: str
    run_id: str
    game_id: str
    cadence: Cadence
    state: Literal[
        "starting",
        "running",
        "cancelling",
        "completed",
        "partial",
        "blocked",
        "review_required",
        "human_required",
        "cancelled",
        "failed",
    ]
    executable_todo_instance_ids: list[str] = Field(default_factory=list)
    attempt_ordinal: int = Field(default=1, ge=1)
    process_id: int | None = Field(default=None, ge=1)
    exit_code: int | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    _result_has_no_fencing_material = field_validator("result")(
        _reject_fencing_material
    )


class TodoAttemptRecord(ApiModel):
    todo_attempt_id: str
    run_attempt_id: str
    todo_instance_id: str
    attempt_number: int = Field(ge=1)
    operation: str
    state: Literal[
        "running",
        "completed",
        "skipped",
        "blocked",
        "review_required",
        "human_required",
    ]
    reason_code: str = ""
    reason: str = ""
    retryable: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AdapterEventRecord(ApiModel):
    run_attempt_id: str
    sequence: int = Field(ge=0)
    event_type: Literal[
        "hello",
        "todo_attempt_started",
        "todo_progress",
        "artifact_staged",
        "todo_terminal",
        "run_terminal",
    ]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    _payload_has_no_fencing_material = field_validator("payload")(
        _reject_fencing_material
    )


class NotificationPreviewResponse(ApiModel):
    notification_id: str
    subject: str
    text_body: str
    html_body: str
    attachment_decisions: list[dict[str, Any]] = Field(default_factory=list)


class NotificationPolicyRecord(ApiModel):
    enabled: bool
    automatic_dispatch: bool
    channel: Literal["email"] = "email"
    recipient_binding_id: str
    secret_state: Literal["configured", "missing", "invalid"]
    updated_by: str
    created_at: datetime
    updated_at: datetime


class WeeklyTaskRecord(ApiModel):
    weekly_id: str
    game_id: str
    display_name: str
    enabled: bool
    state: str = "not_started"
    acceptance_state: str = "not_started"
    capability_ref: str | None = None


class TodoDefinitionRecord(ApiModel):
    todo_definition_id: str
    definition_version: int = Field(ge=1)
    catalog_version: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    game_id: str
    cadence: Cadence
    operation: str
    title: str
    category: str
    order_index: int
    required: bool
    risk: Literal[
        "observe_only", "routine_action", "approval_required", "forbidden"
    ]
    automation_difficulty: Literal["low", "medium", "high", "unknown"]
    adapter_capability_ref: str | None = None
    automation_state: str
    initial_status: TodoStatus
    initial_reason: str = ""
    reset_rule: dict[str, Any]
    source_refs: list[str] = Field(default_factory=list)
    active: bool = True
    updated_at: datetime


TodoDispatchDisposition = Literal[
    "eligible",
    "completed_skip",
    "terminal_skip",
    "deferred_review",
    "deferred_human",
    "deferred_forbidden",
    "reconcile_required",
    "unsupported",
]


class TodoActionAvailability(ApiModel):
    execute: bool
    resume: bool
    review: bool
    release_human: bool
    submit_manual_evidence: bool
    next_action: str


class TodoInstanceRecord(ApiModel):
    account_id: str = "default"
    todo_instance_id: str
    todo_definition_id: str
    definition_version: int = Field(ge=1)
    catalog_version: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    game_id: str
    cadence: Cadence
    period_key: str
    period_starts_at: datetime
    period_ends_at: datetime
    operation: str
    title: str
    category: str
    order_index: int
    required: bool
    risk: Literal[
        "observe_only", "routine_action", "approval_required", "forbidden"
    ]
    automation_difficulty: Literal["low", "medium", "high", "unknown"]
    adapter_capability_ref: str | None = None
    automation_state: str
    reset_rule: dict[str, Any]
    source_refs: list[str] = Field(default_factory=list)
    status: TodoStatus
    dispatch_disposition: TodoDispatchDisposition
    dispatch_reason_code: str
    dispatch_reason: str
    action_availability: TodoActionAvailability
    attempts: int = Field(ge=0)
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    run_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TodoResetPreviewItem(ApiModel):
    account_id: str = "default"
    todo_definition_id: str
    todo_instance_id: str
    game_id: str
    cadence: Cadence
    period_key: str
    period_starts_at: datetime
    period_ends_at: datetime
    exists: bool
    current_status: TodoStatus | None = None
    effective_reset_rule: dict[str, Any]
    next_period_reset_rule: dict[str, Any]
    policy_change_deferred: bool = False


class TodoResetPreviewResponse(ApiModel):
    state_version: int
    generated_at: datetime
    action: Literal["reconcile", "reset"]
    game_ids: list[str]
    cadence: Cadence | None = None
    definition_count: int
    existing_count: int
    would_create_count: int
    items: list[TodoResetPreviewItem]


class TodoDefinitionPage(ApiModel):
    items: list[TodoDefinitionRecord]
    total: int = Field(ge=0)


class GameIntegrationOperationRecord(ApiModel):
    operation: str
    todo_definition_id: str
    title: str
    source_refs: list[str] = Field(default_factory=list)


class GameIntegrationParameterRecord(ApiModel):
    """A reviewed daily-intent field and its current execution boundary.

    These records make the control panel explain *what* a game can eventually
    do without turning an upstream preset or a free-form config file into an
    executable surface. Only ``active`` parameters are projected into a
    promoted Adapter binding.
    """

    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    value_type: Literal["enum", "integer", "boolean", "notice"]
    dispatch_status: Literal["active", "requires_adapter"]
    options: list[str] = Field(default_factory=list, max_length=24)
    minimum: int | None = None
    maximum: int | None = None
    note: str = Field(default="", max_length=500)


class GameIntegrationRecord(ApiModel):
    game_id: str
    integration_id: str | None = None
    tool_name: str | None = None
    source: str | None = None
    entry_operation: str | None = None
    mapping_status: Literal["registered", "incomplete", "not_registered"]
    mapping_note: str
    operations: list[GameIntegrationOperationRecord] = Field(default_factory=list)
    parameters: list[GameIntegrationParameterRecord] = Field(default_factory=list)


class GameIntegrationPage(ApiModel):
    items: list[GameIntegrationRecord]
    total: int = Field(ge=0)


class TodoInstancePage(ApiModel):
    items: list[TodoInstanceRecord]
    total: int = Field(ge=0)


class NotificationAttemptPage(ApiModel):
    items: list[NotificationAttemptRecord]
    total: int = Field(ge=0)


class RunAttemptPage(ApiModel):
    items: list[RunAttemptRecord]
    total: int = Field(ge=0)


class TodoAttemptPage(ApiModel):
    items: list[TodoAttemptRecord]
    total: int = Field(ge=0)


class AdapterEventPage(ApiModel):
    items: list[AdapterEventRecord]
    total: int = Field(ge=0)


class AdapterInfoRecord(ApiModel):
    adapter_id: str
    display_name: str
    game_id: str
    active_version: str | None = None
    active_version_id: str | None = None
    candidate_version: str | None = None
    candidate_version_id: str | None = None
    rollback_version_id: str | None = None
    previous_verified_version_id: str | None = None
    stage: str
    health: str
    capability_count: int
    implementation_hash: str | None = None
    host_id: str
    host_version: str
    host_healthy: bool
    host_status: str
    execution_ready: bool
    execution_package_status: str


class LogEntry(ApiModel):
    sequence: int
    log_id: str
    timestamp: datetime
    level: Literal["debug", "info", "warning", "error"]
    source: str
    message: str
    event_type: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    batch_id: str | None = None
    game_id: str | None = None
    run_id: str | None = None
    run_attempt_id: str | None = None
    todo_instance_id: str | None = None
    todo_attempt_id: str | None = None
    phase: str | None = None
    observed_state: str | None = None
    decision: str | None = None
    reason_code: str | None = None
    detail: str | None = None


class WorkItemClaimRequest(ApiModel):
    claimant: str = Field(default="agent", min_length=1, max_length=80)
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class WorkItemClaimRecord(ApiModel):
    claim_id: str
    work_item_id: str
    claimant: str
    claimed_at: datetime
    expires_at: datetime
    state: str = "active"


class EventRecord(ApiModel):
    sequence: int
    event_id: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    created_at: datetime

    _payload_redacts_fencing_material = field_validator("payload", mode="before")(
        _redact_fencing_material
    )

    def as_stream_event(self, state_version: int) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "type": self.event_type,
            "occurredAt": self.created_at.isoformat(),
            "stateVersion": state_version,
            "cursor": str(self.sequence),
            "payload": self.payload,
        }


class EventPage(ApiModel):
    events: list[EventRecord]
    next_sequence: int


class SnapshotResponse(ApiModel):
    state_version: int
    generated_at: datetime
    event_cursor: str
    manager: dict[str, Any]
    health: dict[str, Any]
    game_day: str
    active_batch: dict[str, Any] | None = None
    recent_batches: list[BatchRecord]
    games: list[GameSummary]
    counters: dict[str, int]
    todo: dict[str, Any] = Field(default_factory=dict)
    execution_control: dict[str, Any] = Field(default_factory=dict)


class ConfigResponse(ApiModel):
    version: str
    state_version: int
    config: dict[str, Any]
    schema_document: dict[str, Any] = Field(alias="schema")
    allowed_game_ids: list[str]
    legacy_sources: dict[str, Any]
    updated_at: datetime | None = None


class GameAccountTargetRequest(ApiModel):
    game_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    account_id: str = Field(default="default", min_length=1, max_length=80)


class BatchCreateRequest(ApiModel):
    targets: list[GameAccountTargetRequest] | None = Field(default=None, max_length=100)
    cadence: Cadence = Cadence.DAILY
    kind: Cadence | None = None
    game_ids: list[str] | None = None
    mode: RequestMode = RequestMode.PLAN
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class BatchResumeRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=1000)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class GameRunCreateRequest(ApiModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    game_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    cadence: Cadence = Cadence.DAILY
    mode: RequestMode = RequestMode.PLAN
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class GameRunAliasRequest(ApiModel):
    account_id: str = Field(default="default", min_length=1, max_length=80)
    kind: Cadence = Cadence.DAILY
    requested_by: str = Field(default="tray", min_length=1, max_length=80)


class AgentWorkItemCreateRequest(ApiModel):
    kind: WorkItemKind
    game_id: str | None = Field(
        default=None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$"
    )
    cadence: Cadence | None = None
    run_id: str | None = Field(default=None, max_length=80)
    mode: RequestMode = RequestMode.PLAN
    requested_by: str = Field(default="agent", min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)
    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    allowed_capability_refs: list[str] = Field(default_factory=list, max_length=50)


class TodoDiagnosis(ApiModel):
    todo_instance_id: str = Field(min_length=1, max_length=160)
    difficulty: Literal["easy", "moderate", "hard", "unsupported", "unknown"]
    automatable: bool | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    basis: list[str] = Field(min_length=1, max_length=20)
    failure_stage: str = Field(default="", max_length=160)
    issue: str = Field(default="", max_length=1000)
    recommendation: str = Field(default="", max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("todo_instance_id")
    @classmethod
    def todo_id_is_opaque(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("todoInstanceId must be an opaque ID")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_opaque(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Todo diagnosis evidenceIds contains duplicates")
        for value in values:
            if not value or len(value) > 160 or "/" in value or "\\" in value:
                raise ValueError(
                    "Todo diagnosis evidenceIds must contain opaque IDs"
                )
        return values


class ClaimDecisionCreateRequest(ApiModel):
    claim_id: str = Field(min_length=1, max_length=120)
    decision: Literal["accepted", "rejected", "review_required"]
    reason: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    requested_by: str = Field(default="agent", min_length=1, max_length=80)
    fencing_token: str = Field(min_length=16, max_length=160)
    todo_diagnosis: TodoDiagnosis | None = None
    todo_diagnoses: list[TodoDiagnosis] = Field(default_factory=list, max_length=100)
    completion_review: CompletionReviewSubmission | None = None

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_opaque_identifiers(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("evidenceIds contains duplicates")
        for value in values:
            if not value or len(value) > 160 or "/" in value or "\\" in value:
                raise ValueError("evidenceIds must contain opaque IDs, not file paths")
        return values

    @model_validator(mode="after")
    def todo_diagnosis_scope_is_unambiguous(self) -> "ClaimDecisionCreateRequest":
        if self.todo_diagnosis is not None and self.todo_diagnoses:
            raise ValueError(
                "send either todoDiagnosis or todoDiagnoses, not both"
            )
        diagnoses = (
            self.todo_diagnoses
            if self.todo_diagnoses
            else ([self.todo_diagnosis] if self.todo_diagnosis is not None else [])
        )
        todo_ids = [item.todo_instance_id for item in diagnoses]
        if len(todo_ids) != len(set(todo_ids)):
            raise ValueError("todoDiagnoses contains duplicate todoInstanceId values")
        return self


class CapabilityInvocationCreateRequest(ApiModel):
    capability: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="agent", min_length=1, max_length=80)
    work_item_id: str | None = Field(default=None, min_length=1, max_length=120)
    claim_id: str | None = Field(default=None, min_length=1, max_length=120)
    fencing_token: str | None = Field(default=None, min_length=16, max_length=160)


class TodoResetOverrideConfig(ApiModel):
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    time: str | None = Field(
        default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    week_start_day: Literal[
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ] | None = None


class TodoResetPolicyConfig(ApiModel):
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    time: str = Field(default="04:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    week_start_day: Literal[
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ] = "Monday"
    per_game: dict[str, TodoResetOverrideConfig] = Field(default_factory=dict)
    per_definition: dict[str, TodoResetOverrideConfig] = Field(default_factory=dict)


class LDPlayerGameBindingConfig(ApiModel):
    """One fixed local LDPlayer instance; the Android package is GameId-owned."""

    provider: Literal["ldplayer"] = "ldplayer"
    console_path: str = Field(max_length=260)
    adb_path: str = Field(max_length=260)
    instance_index: int = Field(default=0, ge=0, le=32)
    instance_name: str | None = Field(default=None, min_length=1, max_length=100)
    adb_serial: str = Field(
        default="emulator-5554",
        pattern=r"^(?:emulator-\d+|127\.0\.0\.1:\d+)$",
        max_length=40,
    )

    @field_validator("console_path", "adb_path")
    @classmethod
    def local_windows_executable_only(cls, value: str) -> str:
        candidate = value.strip()
        if any(character in candidate for character in ("\x00", "\r", "\n", '"', "|", "<", ">")):
            raise PydanticCustomError(
                "emulator_path_invalid", "path contains unsupported characters"
            )
        drive, tail = ntpath.splitdrive(candidate)
        if (
            len(drive) != 2
            or drive[0].isalpha() is False
            or drive[1] != ":"
            or not tail.startswith("\\")
        ):
            raise PydanticCustomError(
                "emulator_path_invalid",
                "path must be an absolute local Windows path",
            )
        return ntpath.normpath(candidate)

    @model_validator(mode="after")
    def validate_fixed_executable_names(self) -> "LDPlayerGameBindingConfig":
        if ntpath.basename(self.console_path).casefold() != "ldconsole.exe":
            raise PydanticCustomError(
                "emulator_path_invalid", "consolePath must point to ldconsole.exe"
            )
        if ntpath.basename(self.adb_path).casefold() != "adb.exe":
            raise PydanticCustomError(
                "emulator_path_invalid", "adbPath must point to adb.exe"
            )
        if ntpath.dirname(self.console_path).casefold() != ntpath.dirname(
            self.adb_path
        ).casefold():
            raise PydanticCustomError(
                "emulator_path_invalid",
                "consolePath and adbPath must belong to the same LDPlayer installation",
            )
        return self


class GamePathConfig(ApiModel):
    """A local installation binding only; it never contains a command or arguments."""

    game_path: str | None = Field(default=None, max_length=260)
    tool_path: str | None = Field(default=None, max_length=260)
    emulator: LDPlayerGameBindingConfig | None = None

    @field_validator("game_path", "tool_path")
    @classmethod
    def local_windows_path_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if any(character in candidate for character in ("\x00", "\r", "\n", '"', "|", "<", ">")):
            raise PydanticCustomError(
                "game_path_invalid", "path contains unsupported characters"
            )
        drive, tail = ntpath.splitdrive(candidate)
        if len(drive) != 2 or drive[0].isalpha() is False or drive[1] != ":":
            raise PydanticCustomError(
                "game_path_invalid", "path must be an absolute local Windows path"
            )
        if not tail.startswith("\\"):
            raise PydanticCustomError(
                "game_path_invalid", "path must be an absolute local Windows path"
            )
        normalized = ntpath.normpath(candidate)
        if normalized.startswith("\\\\"):
            raise PydanticCustomError(
                "game_path_invalid", "UNC paths are not supported for local game runtime"
            )
        return normalized


ZZZ_ONE_DRAGON_APP_IDS = frozenset(
    {
        "charge_plan",
        "trigrams_collection",
        "coffee",
        "suibian_temple",
        "random_play",
        "ridu_weekly",
        "city_fund",
        "engagement_reward",
        "intel_board",
        "notorious_hunt",
        "email",
        "drive_disc_dismantle",
        "lost_void",
        "withered_domain",
        "life_on_line",
        "notify",
        "redemption_code",
        "scratch_card",
        "shiyu_defense",
        "world_patrol",
        "hou_hou_bakery",
    }
)


class ZZZOneDragonProfileConfig(ApiModel):
    """The only OneDragon queue knobs Manager is allowed to own.

    App IDs are deliberately allowlisted.  This is a queue profile, not an
    escape hatch for arbitrary launcher arguments or third-party YAML writes.
    """

    daily_app_ids: list[str] = Field(default_factory=list, max_length=21)
    weekly_app_ids: list[str] = Field(default_factory=list, max_length=21)

    @model_validator(mode="after")
    def validate_app_scopes(self) -> "ZZZOneDragonProfileConfig":
        daily = set(self.daily_app_ids)
        weekly = set(self.weekly_app_ids)
        unknown = (daily | weekly) - ZZZ_ONE_DRAGON_APP_IDS
        if unknown:
            raise PydanticCustomError(
                "zzz_one_dragon_unknown_app",
                "ZZZ OneDragon profile contains an unknown app ID",
            )
        if len(self.daily_app_ids) != len(daily) or len(self.weekly_app_ids) != len(weekly):
            raise PydanticCustomError(
                "zzz_one_dragon_duplicate_app",
                "ZZZ OneDragon profile contains duplicate app IDs",
            )
        if daily & weekly:
            raise PydanticCustomError(
                "zzz_one_dragon_overlapping_app",
                "a ZZZ OneDragon app cannot be both daily and weekly",
            )
        return self


class PGRMfwProfileConfig(ApiModel):
    """A fixed MFW saved-configuration reference; never an executable command."""

    profile_id: str = Field(
        min_length=3,
        max_length=100,
        pattern=r"^c_[A-Za-z0-9_-]+$",
    )


class OKWWProfileConfig(ApiModel):
    """Safe, Manager-owned subset of OK-WW DailyTask intent.

    Weekly garden, inventory mutation, echo farming after the daily, and
    tool-exit policy are deliberately absent. The runner always projects
    those upstream switches to their safe values.
    """

    which_to_farm: Literal[
        "Tacet Suppression", "Forgery Challenge", "Simulation Challenge"
    ] = "Tacet Suppression"
    # The upstream adapter stores these as one-based target positions.  They
    # are adapter routing details, not keyboard actions.
    tacet_suppression_number: int = Field(default=1, ge=1)
    forgery_challenge_number: int = Field(default=1, ge=1)
    material_selection: Literal[
        "Resonator EXP", "Weapon EXP", "Shell Credit"
    ] = "Shell Credit"
    farm_nightmare_nest_for_daily_echo: bool = True


EndfieldStaminaStage = Literal[
    "干员经验",
    "干员进阶",
    "钱币收集",
    "技能提升",
    "武器经验",
    "武器进阶",
    "罗丹",
    "三位一体",
    "白垩界卫",
    "阮一",
    "聂菲斯",
    "D96钢",
    "超距辉映管",
    "快子遴捡晶格",
    "象限拟合液",
    "三相纳米片",
    "枢纽区",
    "源石研究园",
    "试验园区",
    "矿脉源区",
    "供能高地",
    "武陵城",
    "清波寨",
    "首墩",
    "藏剑谷",
]


class EndfieldProfileConfig(ApiModel):
    """Manager-owned Endfield daily-intent draft, not an upstream file patch.

    The current tool has not been promoted to a selected-stage Adapter.  This
    model intentionally preserves the user's exact target for that promotion
    while refusing medicine, continued runs, trade, stores, and crafting.
    """

    stamina_stage: EndfieldStaminaStage = "超距辉映管"
    reward_tier: Literal["保持当前", "低阶", "高阶"] = "保持当前"
    stamina_rotation_start_date: str = Field(
        default="2026-04-06", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    stamina_rotation: list[EndfieldStaminaStage] = Field(
        default_factory=lambda: ["超距辉映管"], min_length=1, max_length=31
    )
    team_slot: Literal["不换队伍", "1", "2", "3", "4", "5"] = "不换队伍"


class NTEProfileConfig(ApiModel):
    """Manager-owned NTE daily-intent draft; no upstream config file is edited."""

    anomaly_task_type: Literal[
        "经验与甲硬币", "异能升级材料", "弧盘突破材料", "空幕"
    ] = "经验与甲硬币"
    exp_reward_target: Literal["角色经验", "弧盘经验", "甲硬币"] = "甲硬币"
    material_index: int = Field(default=1, ge=1, le=6)
    stamina_target: int = Field(default=200, ge=40, le=360, multiple_of=40)
    auto_cycle_sub_task: bool = False
    coffee_mode: Literal["不执行", "领取/补货", "完整自动化"] = "不执行"


class CZNProfileConfig(ApiModel):
    """Manager-owned safe subset of the Maa_KES stamina route."""

    stamina_category: Literal["成长", "主战员", "辅战员", "潜能"] = "成长"
    stamina_target: Literal[
        "单元币",
        "主战员升级材料",
        "辅战员升级材料",
        "前锋",
        "守卫",
        "游侠",
        "猎人",
        "奥义师",
        "操控师",
        "热情",
        "秩序",
        "本能",
        "虚无",
        "正义",
    ] = "单元币"
    battle_efficiency: int = Field(default=4, ge=1, le=5)
    until_exhausted: Literal[True] = True

    @model_validator(mode="after")
    def validate_target_for_category(self) -> "CZNProfileConfig":
        targets = {
            "成长": {"单元币", "主战员升级材料", "辅战员升级材料"},
            "主战员": {"前锋", "守卫", "游侠", "猎人", "奥义师", "操控师"},
            "辅战员": {"前锋", "守卫", "游侠", "猎人", "奥义师", "操控师"},
            "潜能": {"热情", "秩序", "本能", "虚无", "正义"},
        }
        if self.stamina_target not in targets[self.stamina_category]:
            raise PydanticCustomError(
                "czn_stamina_target_invalid",
                "staminaTarget is not valid for the selected staminaCategory",
            )
        return self


class DailyToolProfilesConfig(ApiModel):
    """Per-tool daily intent owned by Manager and shared by WebGUI/API callers."""

    zzz_one_dragon: ZZZOneDragonProfileConfig | None = None
    pgr_mfw: PGRMfwProfileConfig | None = None
    ok_ww: OKWWProfileConfig | None = None
    endfield: EndfieldProfileConfig | None = None
    nte: NTEProfileConfig | None = None
    czn: CZNProfileConfig | None = None


class GameAccountConfig(ApiModel):
    account_id: str | None = Field(default=None, min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    saved_account_label: str | None = Field(default=None, max_length=160)


class ConfigPatchRequest(ApiModel):
    game_accounts: dict[str, list[GameAccountConfig]] | None = None
    enabled: dict[str, bool] | None = None
    order: list[str] | None = None
    daily_schedule_enabled: bool | None = None
    daily_schedule_time: str | None = Field(
        default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    weekly_enabled: bool | None = None
    weekly_day: Literal[
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ] | None = None
    skip_blocked_on_run_all: bool | None = None
    execution_strategy: Literal["continue", "stop"] | None = None
    step_timeout_seconds: int | None = Field(default=None, ge=600, le=7200)
    todo_reset_policy: TodoResetPolicyConfig | None = None
    game_paths: dict[str, GamePathConfig] | None = None
    daily_tool_profiles: DailyToolProfilesConfig | None = None
    daily_todo_selection: dict[str, list[str]] | None = None


class TodoTransitionRequest(ApiModel):
    status: TodoStatus
    reason: str = Field(default="", max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    run_id: str | None = Field(default=None, max_length=160)
    increment_attempt: bool = False
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)

    @field_validator("evidence_refs")
    @classmethod
    def todo_evidence_refs_are_opaque(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 160 or "/" in value or "\\" in value:
                raise ValueError("evidenceRefs must contain opaque IDs, not file paths")
        return values


class TodoReconcileRequest(ApiModel):
    account_id: str | None = Field(default=None, min_length=1, max_length=80)
    game_ids: list[str] | None = Field(default=None, max_length=50)
    cadence: Cadence | None = None
    reason: str = Field(default="operator-request", min_length=1, max_length=500)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)

    @field_validator("game_ids")
    @classmethod
    def todo_game_ids_are_unique(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("gameIds contains duplicates")
        return values


class NotificationPolicyPatchRequest(ApiModel):
    enabled: bool | None = None
    automatic_dispatch: bool | None = None
    recipient_binding_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
    )


class NotificationSendRequest(ApiModel):
    reason: str = Field(default="operator-request", min_length=1, max_length=500)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class NotificationRetryRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)
    confirm_ambiguous: bool = False
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class CancelRequest(ApiModel):
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)
    reason: str = Field(default="operator-request", min_length=1, max_length=500)


class ManagerLifecycleRequest(ApiModel):
    requested_by: str = Field(default="tray", min_length=1, max_length=80)
    reason: str = Field(default="operator-request", min_length=1, max_length=500)


class RepairSessionCreateRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=1000)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class RepairVerificationRequest(ApiModel):
    verdict: Literal["passed", "failed", "needs_more_evidence"]
    note: str = Field(default="", max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class EvidenceReviewRequest(ApiModel):
    artifact_id: str = Field(min_length=1, max_length=160)
    verdict: Literal["accepted", "rejected"]
    note: str = Field(default="", max_length=2000)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class AdapterGovernanceRequest(ApiModel):
    adapter_id: str = Field(min_length=1, max_length=160)
    target_stage: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="operator-request", max_length=1000)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class AdapterDiagnosticCanaryRequest(ApiModel):
    note: str = Field(default="manager-owned diagnostic canary", max_length=500)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class DiagnosticBundleCreateRequest(ApiModel):
    batch_id: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=160)
    incident_id: str | None = Field(default=None, max_length=160)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)


class RunControlRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=1000)
    requested_by: str = Field(default="webgui", min_length=1, max_length=80)
    expected_run_revision: int | None = Field(default=None, ge=0)
    expected_current_attempt_id: str | None = Field(
        default=None, min_length=1, max_length=160
    )
    expected_current_attempt_revision: int | None = Field(default=None, ge=0)


class CommandReceipt(ApiModel):
    command_id: str
    idempotency_key: str
    request_id: str | None = None
    status_url: str | None = None
    accepted_state_version: int
    state: Literal["accepted", "running", "succeeded", "rejected", "failed"]
    message: str = ""
    result: dict[str, Any]
    submitted_at: datetime
    completed_at: datetime | None = None
    replayed: bool = False


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody
