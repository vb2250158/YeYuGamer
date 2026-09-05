"""Immutable facts for Manager-owned desktop execution control.

This module deliberately contains no Win32, persistence, process-control, game,
or notification behavior.  A Manager integration is responsible for observing
the operating system and persisting accepted facts.  The sibling application
service only validates relationships and legal state transitions between these
facts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution-control timestamps must be timezone-aware")
    return value


OpaqueId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ReasonText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=2000),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
Sha256Fingerprint = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{16}$"),
]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ExecutionFact(BaseModel):
    """Strict, immutable base for facts crossing the service boundary."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ExecutionScope(ExecutionFact):
    """The smallest scope in which execution evidence may be reused."""

    manager_id: OpaqueId
    game_id: OpaqueId
    run_id: OpaqueId
    run_attempt_id: OpaqueId
    game_day_key: OpaqueId


class FencingIdentity(ExecutionFact):
    """Safe fencing material; a raw fencing token has no accepted field."""

    token_hash: Sha256Digest
    public_fingerprint: Sha256Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_hash(self) -> "FencingIdentity":
        expected = f"sha256:{self.token_hash.removeprefix('sha256:')[:16]}"
        if self.public_fingerprint != expected:
            raise ValueError("public fencing fingerprint must match token hash")
        return self


class LeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class WindowBindingState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    INVALIDATED = "invalidated"


class ObservationPhase(StrEnum):
    BEFORE_ACTION = "before_action"
    AFTER_ACTION = "after_action"
    CHECKPOINT = "checkpoint"
    BLOCKER = "blocker"
    RECOVERY = "recovery"


class ActionResult(StrEnum):
    SUCCEEDED = "succeeded"
    NO_EFFECT = "no_effect"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReplayDisposition(StrEnum):
    NEVER = "never"
    SAFE_RETRY = "safe_retry"


class CheckpointState(StrEnum):
    VALID = "valid"
    INVALIDATED = "invalidated"


class TodoBlockerKind(StrEnum):
    CONTROLLER_CONFLICT = "controller_conflict"
    HUMAN_REQUIRED = "human_required"
    REVIEW_REQUIRED = "review_required"
    SAFETY_GATE = "safety_gate"
    AMBIGUOUS_ACTION = "ambiguous_action"
    EVIDENCE_MISSING = "evidence_missing"
    CONTRACT_INVARIANT = "contract_invariant"


class TodoBlockerState(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"


class ControllerLease(ExecutionFact):
    """A Manager-owned, desktop-wide controller lease."""

    controller_lease_id: OpaqueId
    desktop_id: OpaqueId
    scope: ExecutionScope
    holder_principal_id: OpaqueId
    generation: PositiveInt
    fencing: FencingIdentity
    acquired_at: datetime
    expires_at: datetime
    state: LeaseState = LeaseState.ACTIVE
    ended_at: datetime | None = None
    end_reason_code: OpaqueId | None = None
    end_reason: ReasonText | None = None

    _acquired_at_is_aware = field_validator("acquired_at")(_aware)
    _expires_at_is_aware = field_validator("expires_at")(_aware)
    _ended_at_is_aware = field_validator("ended_at")(
        lambda value: None if value is None else _aware(value)
    )

    @model_validator(mode="after")
    def valid_lifetime_and_state(self) -> "ControllerLease":
        if self.expires_at <= self.acquired_at:
            raise ValueError("controller lease must expire after acquisition")
        terminal_fields = (self.ended_at, self.end_reason_code, self.end_reason)
        if self.state is LeaseState.ACTIVE:
            if any(value is not None for value in terminal_fields):
                raise ValueError("active controller lease cannot have terminal fields")
            return self
        if any(value is None for value in terminal_fields):
            raise ValueError("terminal controller lease requires end time and reason")
        assert self.ended_at is not None
        if self.ended_at < self.acquired_at:
            raise ValueError("controller lease cannot end before acquisition")
        if self.state is LeaseState.EXPIRED and self.ended_at < self.expires_at:
            raise ValueError("expired controller lease cannot end before expiry")
        return self


class WindowBinding(ExecutionFact):
    """A target HWND bound to a PID plus process-creation identity."""

    window_binding_id: OpaqueId
    controller_lease_id: OpaqueId
    desktop_id: OpaqueId
    scope: ExecutionScope
    hwnd: PositiveInt
    pid: PositiveInt
    process_created_at: datetime
    bound_at: datetime
    state: WindowBindingState = WindowBindingState.ACTIVE
    ended_at: datetime | None = None
    end_reason_code: OpaqueId | None = None
    end_reason: ReasonText | None = None

    _process_created_at_is_aware = field_validator("process_created_at")(_aware)
    _bound_at_is_aware = field_validator("bound_at")(_aware)
    _ended_at_is_aware = field_validator("ended_at")(
        lambda value: None if value is None else _aware(value)
    )

    @model_validator(mode="after")
    def valid_process_identity_and_state(self) -> "WindowBinding":
        if self.process_created_at > self.bound_at:
            raise ValueError("window process cannot be created after binding")
        terminal_fields = (self.ended_at, self.end_reason_code, self.end_reason)
        if self.state is WindowBindingState.ACTIVE:
            if any(value is not None for value in terminal_fields):
                raise ValueError("active window binding cannot have terminal fields")
            return self
        if any(value is None for value in terminal_fields):
            raise ValueError("terminal window binding requires end time and reason")
        assert self.ended_at is not None
        if self.ended_at < self.bound_at:
            raise ValueError("window binding cannot end before it was bound")
        return self


class FocusLease(ExecutionFact):
    """A short, fenced right to direct input at one bound window."""

    focus_lease_id: OpaqueId
    controller_lease_id: OpaqueId
    window_binding_id: OpaqueId
    desktop_id: OpaqueId
    scope: ExecutionScope
    generation: PositiveInt
    controller_generation: PositiveInt
    fencing_token_fingerprint: Sha256Fingerprint
    acquired_at: datetime
    expires_at: datetime
    state: LeaseState = LeaseState.ACTIVE
    ended_at: datetime | None = None
    end_reason_code: OpaqueId | None = None
    end_reason: ReasonText | None = None

    _acquired_at_is_aware = field_validator("acquired_at")(_aware)
    _expires_at_is_aware = field_validator("expires_at")(_aware)
    _ended_at_is_aware = field_validator("ended_at")(
        lambda value: None if value is None else _aware(value)
    )

    @model_validator(mode="after")
    def valid_lifetime_and_state(self) -> "FocusLease":
        if self.expires_at <= self.acquired_at:
            raise ValueError("focus lease must expire after acquisition")
        terminal_fields = (self.ended_at, self.end_reason_code, self.end_reason)
        if self.state is LeaseState.ACTIVE:
            if any(value is not None for value in terminal_fields):
                raise ValueError("active focus lease cannot have terminal fields")
            return self
        if any(value is None for value in terminal_fields):
            raise ValueError("terminal focus lease requires end time and reason")
        assert self.ended_at is not None
        if self.ended_at < self.acquired_at:
            raise ValueError("focus lease cannot end before acquisition")
        if self.state is LeaseState.EXPIRED and self.ended_at < self.expires_at:
            raise ValueError("expired focus lease cannot end before expiry")
        return self


class Observation(ExecutionFact):
    """A screenshot fact that stores only a Manager opaque artifact reference."""

    observation_id: OpaqueId
    scope: ExecutionScope
    controller_lease_id: OpaqueId
    focus_lease_id: OpaqueId
    window_binding_id: OpaqueId
    hwnd: PositiveInt
    pid: PositiveInt
    process_created_at: datetime
    phase: ObservationPhase
    action_request_id: OpaqueId | None = None
    artifact_ref: OpaqueId
    content_type: str = Field(pattern=r"^image/(?:png|jpeg)$")
    scene_code: OpaqueId
    captured_at: datetime

    _process_created_at_is_aware = field_validator("process_created_at")(_aware)
    _captured_at_is_aware = field_validator("captured_at")(_aware)

    @model_validator(mode="after")
    def action_phase_has_action_identity(self) -> "Observation":
        action_phase = self.phase in {
            ObservationPhase.BEFORE_ACTION,
            ObservationPhase.AFTER_ACTION,
        }
        if action_phase and self.action_request_id is None:
            raise ValueError("before/after observations require actionRequestId")
        return self


class ActionReceipt(ExecutionFact):
    """Manager-owned outcome for one bounded input action."""

    action_receipt_id: OpaqueId
    action_request_id: OpaqueId
    action_code: OpaqueId
    scope: ExecutionScope
    controller_lease_id: OpaqueId
    focus_lease_id: OpaqueId
    window_binding_id: OpaqueId
    controller_generation: PositiveInt
    fencing_token_fingerprint: Sha256Fingerprint
    hwnd: PositiveInt
    pid: PositiveInt
    process_created_at: datetime
    before_observation_id: OpaqueId
    after_observation_id: OpaqueId
    result: ActionResult
    replay_disposition: ReplayDisposition = ReplayDisposition.NEVER
    started_at: datetime
    completed_at: datetime
    reason_code: OpaqueId | None = None
    reason: ReasonText | None = None

    _process_created_at_is_aware = field_validator("process_created_at")(_aware)
    _started_at_is_aware = field_validator("started_at")(_aware)
    _completed_at_is_aware = field_validator("completed_at")(_aware)

    @model_validator(mode="after")
    def valid_evidence_and_replay_policy(self) -> "ActionReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("action cannot complete before it starts")
        if self.before_observation_id == self.after_observation_id:
            raise ValueError("action requires distinct before and after observations")
        if self.result in {ActionResult.FAILED, ActionResult.UNKNOWN}:
            if self.reason_code is None or self.reason is None:
                raise ValueError("failed or unknown action requires a reason")
        elif self.reason_code is not None or self.reason is not None:
            if self.reason_code is None or self.reason is None:
                raise ValueError("action reason code and text must be supplied together")
        if self.result in {ActionResult.SUCCEEDED, ActionResult.UNKNOWN}:
            if self.replay_disposition is not ReplayDisposition.NEVER:
                raise ValueError("succeeded or unknown action can never be replayed")
        if self.replay_disposition is ReplayDisposition.SAFE_RETRY:
            if self.result not in {ActionResult.NO_EFFECT, ActionResult.FAILED}:
                raise ValueError("safe retry requires a confirmed no-effect or failed result")
        return self


class Checkpoint(ExecutionFact):
    """An append-only checkpoint or an explicit checkpoint invalidation."""

    checkpoint_id: OpaqueId
    scope: ExecutionScope
    todo_instance_id: OpaqueId
    sequence: PositiveInt
    progress_index: NonNegativeInt
    state: CheckpointState = CheckpointState.VALID
    previous_checkpoint_id: OpaqueId | None = None
    invalidates_checkpoint_id: OpaqueId | None = None
    action_receipt_id: OpaqueId | None = None
    observation_ids: tuple[OpaqueId, ...] = Field(min_length=1, max_length=20)
    recorded_at: datetime
    invalidation_reason_code: OpaqueId | None = None
    invalidation_reason: ReasonText | None = None

    _recorded_at_is_aware = field_validator("recorded_at")(_aware)

    @field_validator("observation_ids")
    @classmethod
    def observation_ids_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("checkpoint observationIds contains duplicates")
        return values

    @model_validator(mode="after")
    def valid_chain_and_invalidation_shape(self) -> "Checkpoint":
        if self.sequence == 1 and self.previous_checkpoint_id is not None:
            raise ValueError("first checkpoint cannot have a predecessor")
        if self.sequence > 1 and self.previous_checkpoint_id is None:
            raise ValueError("subsequent checkpoint requires a predecessor")
        invalidation_fields = (
            self.invalidates_checkpoint_id,
            self.invalidation_reason_code,
            self.invalidation_reason,
        )
        if self.state is CheckpointState.VALID:
            if any(value is not None for value in invalidation_fields):
                raise ValueError("valid checkpoint cannot contain invalidation fields")
            return self
        if any(value is None for value in invalidation_fields):
            raise ValueError("checkpoint invalidation requires target and reason")
        if self.invalidates_checkpoint_id == self.checkpoint_id:
            raise ValueError("checkpoint invalidation must be a new fact")
        return self


class TodoBlocker(ExecutionFact):
    """Current immutable snapshot of a Todo blocker lifecycle."""

    blocker_id: OpaqueId
    scope: ExecutionScope
    todo_instance_id: OpaqueId
    kind: TodoBlockerKind
    code: OpaqueId
    state: TodoBlockerState
    revision: PositiveInt
    raised_at: datetime
    transitioned_at: datetime
    reason: ReasonText
    artifact_refs: tuple[OpaqueId, ...] = Field(min_length=1, max_length=20)
    action_receipt_id: OpaqueId | None = None
    resolved_at: datetime | None = None
    resolution_code: OpaqueId | None = None
    resolution_reason: ReasonText | None = None
    resolution_artifact_refs: tuple[OpaqueId, ...] = Field(
        default=(), max_length=20
    )

    _raised_at_is_aware = field_validator("raised_at")(_aware)
    _transitioned_at_is_aware = field_validator("transitioned_at")(_aware)
    _resolved_at_is_aware = field_validator("resolved_at")(
        lambda value: None if value is None else _aware(value)
    )

    @field_validator("artifact_refs", "resolution_artifact_refs")
    @classmethod
    def artifact_refs_are_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("blocker artifact refs contain duplicates")
        return values

    @model_validator(mode="after")
    def valid_lifecycle_shape(self) -> "TodoBlocker":
        if self.transitioned_at < self.raised_at:
            raise ValueError("blocker transition cannot precede blocker creation")
        resolution_fields = (
            self.resolved_at,
            self.resolution_code,
            self.resolution_reason,
        )
        if self.state is TodoBlockerState.ACTIVE:
            if any(value is not None for value in resolution_fields):
                raise ValueError("active blocker cannot contain resolution fields")
            if self.resolution_artifact_refs:
                raise ValueError("active blocker cannot contain resolution artifacts")
            if self.revision == 1 and self.transitioned_at != self.raised_at:
                raise ValueError("new blocker transition time must equal raised time")
            return self
        if self.revision < 2:
            raise ValueError("resolved blocker must have revision 2 or later")
        if any(value is None for value in resolution_fields):
            raise ValueError("resolved blocker requires resolution fields")
        if not self.resolution_artifact_refs:
            raise ValueError("resolved blocker requires resolution artifacts")
        if self.resolved_at != self.transitioned_at:
            raise ValueError("resolvedAt must equal transitionedAt")
        return self
