"""Pure validation and state transitions for desktop execution control.

Every function accepts immutable domain facts and either returns the accepted
candidate fact or raises :class:`ExecutionControlViolation`.  There are no
locks, clocks, Win32 calls, database writes, game actions, or notifications in
this module; those remain Manager integration responsibilities.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import NoReturn

from yeyu_gamer_manager.domain.execution_control import (
    ActionReceipt,
    ActionResult,
    Checkpoint,
    CheckpointState,
    ControllerLease,
    ExecutionScope,
    FocusLease,
    LeaseState,
    Observation,
    ObservationPhase,
    ReplayDisposition,
    TodoBlocker,
    TodoBlockerKind,
    TodoBlockerState,
    WindowBinding,
    WindowBindingState,
)


class ExecutionControlViolation(ValueError):
    """Stable rejection raised before the Manager mutates hot state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject(code: str, message: str) -> NoReturn:
    raise ExecutionControlViolation(code, message)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        _reject("naive_timestamp", "execution-control timestamps must be timezone-aware")


def _require_scope(actual: ExecutionScope, expected: ExecutionScope) -> None:
    if actual != expected:
        _reject(
            "execution_scope_mismatch",
            "manager, game, run, run attempt, and game day must all match",
        )


def _require_active_controller(lease: ControllerLease, at: datetime) -> None:
    _require_aware(at)
    if lease.state is not LeaseState.ACTIVE:
        _reject("controller_lease_inactive", "controller lease is not active")
    if at < lease.acquired_at or at >= lease.expires_at:
        _reject("controller_lease_outside_lifetime", "time is outside controller lease")


def _require_active_binding(binding: WindowBinding) -> None:
    if binding.state is not WindowBindingState.ACTIVE:
        _reject("window_binding_inactive", "window binding is not active")


def _require_active_focus(lease: FocusLease, at: datetime) -> None:
    _require_aware(at)
    if lease.state is not LeaseState.ACTIVE:
        _reject("focus_lease_inactive", "focus lease is not active")
    if at < lease.acquired_at or at >= lease.expires_at:
        _reject("focus_lease_outside_lifetime", "time is outside focus lease")


def grant_controller_lease(
    candidate: ControllerLease,
    existing: Iterable[ControllerLease],
    *,
    at: datetime,
) -> ControllerLease:
    """Accept one active controller per desktop with monotonic fencing."""

    _require_aware(at)
    if candidate.state is not LeaseState.ACTIVE:
        _reject("controller_lease_not_active", "a new controller lease must be active")
    _require_active_controller(candidate, at)
    previous = tuple(existing)
    if any(item.controller_lease_id == candidate.controller_lease_id for item in previous):
        _reject("controller_lease_id_reused", "controllerLeaseId already exists")
    same_desktop = tuple(item for item in previous if item.desktop_id == candidate.desktop_id)
    if any(item.state is LeaseState.ACTIVE for item in same_desktop):
        _reject(
            "desktop_controller_busy",
            "an active controller lease must be explicitly ended before replacement",
        )
    latest_end = max(
        (item.ended_at for item in same_desktop if item.ended_at is not None),
        default=None,
    )
    if latest_end is not None and candidate.acquired_at < latest_end:
        _reject(
            "controller_lease_time_regressed",
            "new controller lease cannot precede the prior terminal transition",
        )
    highest_generation = max((item.generation for item in same_desktop), default=0)
    if candidate.generation <= highest_generation:
        _reject("stale_controller_generation", "controller generation must increase")
    if any(item.fencing.token_hash == candidate.fencing.token_hash for item in same_desktop):
        _reject("fencing_identity_reused", "controller fencing identity must not be reused")
    return candidate


def transition_controller_lease(
    current: ControllerLease,
    candidate: ControllerLease,
) -> ControllerLease:
    """End an active controller lease; reactivation is never implicit."""

    if current.state is not LeaseState.ACTIVE:
        _reject("controller_lease_already_terminal", "controller lease is already terminal")
    if candidate.state is LeaseState.ACTIVE:
        _reject("controller_lease_transition_missing", "terminal state is required")
    stable = (
        "controller_lease_id",
        "desktop_id",
        "scope",
        "holder_principal_id",
        "generation",
        "fencing",
        "acquired_at",
        "expires_at",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in stable):
        _reject("controller_lease_identity_changed", "controller lease identity is immutable")
    return candidate


def register_window_binding(
    controller: ControllerLease,
    candidate: WindowBinding,
    existing: Iterable[WindowBinding] = (),
) -> WindowBinding:
    """Bind one live HWND/PID/process-creation identity to a controller."""

    _require_active_controller(controller, candidate.bound_at)
    if candidate.state is not WindowBindingState.ACTIVE:
        _reject("window_binding_not_active", "a new window binding must be active")
    _require_scope(candidate.scope, controller.scope)
    if candidate.controller_lease_id != controller.controller_lease_id:
        _reject("window_controller_mismatch", "window binding references another controller")
    if candidate.desktop_id != controller.desktop_id:
        _reject("window_desktop_mismatch", "window binding references another desktop")
    previous = tuple(existing)
    if any(item.window_binding_id == candidate.window_binding_id for item in previous):
        _reject("window_binding_id_reused", "windowBindingId already exists")
    if any(
        item.state is WindowBindingState.ACTIVE
        and item.controller_lease_id == controller.controller_lease_id
        for item in previous
    ):
        _reject(
            "controller_window_already_bound",
            "active window binding must be explicitly ended before replacement",
        )
    same_controller = tuple(
        item
        for item in previous
        if item.controller_lease_id == controller.controller_lease_id
    )
    latest_end = max(
        (item.ended_at for item in same_controller if item.ended_at is not None),
        default=None,
    )
    if latest_end is not None and candidate.bound_at < latest_end:
        _reject(
            "window_binding_time_regressed",
            "new window binding cannot precede the prior terminal transition",
        )
    return candidate


def transition_window_binding(
    current: WindowBinding,
    candidate: WindowBinding,
) -> WindowBinding:
    if current.state is not WindowBindingState.ACTIVE:
        _reject("window_binding_already_terminal", "window binding is already terminal")
    if candidate.state is WindowBindingState.ACTIVE:
        _reject("window_binding_transition_missing", "terminal state is required")
    stable = (
        "window_binding_id",
        "controller_lease_id",
        "desktop_id",
        "scope",
        "hwnd",
        "pid",
        "process_created_at",
        "bound_at",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in stable):
        _reject("window_process_identity_changed", "HWND/PID/creation identity is immutable")
    return candidate


def grant_focus_lease(
    controller: ControllerLease,
    binding: WindowBinding,
    candidate: FocusLease,
    existing: Iterable[FocusLease] = (),
    *,
    at: datetime,
) -> FocusLease:
    """Grant one short focus lease on the desktop and bound process identity."""

    _require_active_controller(controller, at)
    _require_active_binding(binding)
    if candidate.state is not LeaseState.ACTIVE:
        _reject("focus_lease_not_active", "a new focus lease must be active")
    _require_active_focus(candidate, at)
    _require_scope(binding.scope, controller.scope)
    _require_scope(candidate.scope, controller.scope)
    if binding.controller_lease_id != controller.controller_lease_id:
        _reject("window_controller_mismatch", "window binding references another controller")
    if candidate.controller_lease_id != controller.controller_lease_id:
        _reject("focus_controller_mismatch", "focus lease references another controller")
    if candidate.window_binding_id != binding.window_binding_id:
        _reject("focus_window_mismatch", "focus lease references another window binding")
    if candidate.desktop_id != controller.desktop_id or binding.desktop_id != controller.desktop_id:
        _reject("focus_desktop_mismatch", "focus lease references another desktop")
    if candidate.controller_generation != controller.generation:
        _reject("stale_controller_generation", "focus lease has stale controller generation")
    if candidate.fencing_token_fingerprint != controller.fencing.public_fingerprint:
        _reject("fencing_fingerprint_mismatch", "focus lease has another fencing fingerprint")
    if candidate.acquired_at < binding.bound_at:
        _reject("focus_before_window_binding", "focus lease precedes window binding")
    if candidate.expires_at > controller.expires_at:
        _reject("focus_outlives_controller", "focus lease cannot outlive controller lease")
    previous = tuple(existing)
    if any(item.focus_lease_id == candidate.focus_lease_id for item in previous):
        _reject("focus_lease_id_reused", "focusLeaseId already exists")
    same_desktop = tuple(item for item in previous if item.desktop_id == candidate.desktop_id)
    if any(item.state is LeaseState.ACTIVE for item in same_desktop):
        _reject(
            "desktop_focus_busy",
            "active focus lease must be explicitly ended before replacement",
        )
    latest_end = max(
        (item.ended_at for item in same_desktop if item.ended_at is not None),
        default=None,
    )
    if latest_end is not None and candidate.acquired_at < latest_end:
        _reject(
            "focus_lease_time_regressed",
            "new focus lease cannot precede the prior terminal transition",
        )
    highest_generation = max((item.generation for item in same_desktop), default=0)
    if candidate.generation <= highest_generation:
        _reject("stale_focus_generation", "focus generation must increase")
    return candidate


def transition_focus_lease(current: FocusLease, candidate: FocusLease) -> FocusLease:
    if current.state is not LeaseState.ACTIVE:
        _reject("focus_lease_already_terminal", "focus lease is already terminal")
    if candidate.state is LeaseState.ACTIVE:
        _reject("focus_lease_transition_missing", "terminal state is required")
    stable = (
        "focus_lease_id",
        "controller_lease_id",
        "window_binding_id",
        "desktop_id",
        "scope",
        "generation",
        "controller_generation",
        "fencing_token_fingerprint",
        "acquired_at",
        "expires_at",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in stable):
        _reject("focus_lease_identity_changed", "focus lease identity is immutable")
    return candidate


def _validate_observation_context(
    controller: ControllerLease,
    binding: WindowBinding,
    focus: FocusLease,
    observation: Observation,
) -> None:
    _require_active_controller(controller, observation.captured_at)
    _require_active_binding(binding)
    _require_active_focus(focus, observation.captured_at)
    _require_scope(binding.scope, controller.scope)
    _require_scope(focus.scope, controller.scope)
    _require_scope(observation.scope, controller.scope)
    if observation.controller_lease_id != controller.controller_lease_id:
        _reject("observation_controller_mismatch", "observation references another controller")
    if observation.focus_lease_id != focus.focus_lease_id:
        _reject("observation_focus_mismatch", "observation references another focus lease")
    if observation.window_binding_id != binding.window_binding_id:
        _reject("observation_window_mismatch", "observation references another window")
    if focus.controller_lease_id != controller.controller_lease_id:
        _reject("focus_controller_mismatch", "focus lease references another controller")
    if focus.window_binding_id != binding.window_binding_id:
        _reject("focus_window_mismatch", "focus lease references another window")
    if focus.controller_generation != controller.generation:
        _reject("stale_controller_generation", "focus lease has stale controller generation")
    if focus.fencing_token_fingerprint != controller.fencing.public_fingerprint:
        _reject("fencing_fingerprint_mismatch", "focus lease has another fencing fingerprint")
    process_identity = (binding.hwnd, binding.pid, binding.process_created_at)
    observed_identity = (
        observation.hwnd,
        observation.pid,
        observation.process_created_at,
    )
    if observed_identity != process_identity:
        _reject(
            "window_process_identity_mismatch",
            "observation HWND/PID/process-created identity does not match binding",
        )


def accept_observation(
    controller: ControllerLease,
    binding: WindowBinding,
    focus: FocusLease,
    candidate: Observation,
    existing: Iterable[Observation] = (),
) -> Observation:
    """Validate a screenshot observation without opening its opaque artifact."""

    _validate_observation_context(controller, binding, focus, candidate)
    previous = tuple(existing)
    if any(item.observation_id == candidate.observation_id for item in previous):
        _reject("observation_id_reused", "observationId already exists")
    if any(item.artifact_ref == candidate.artifact_ref for item in previous):
        _reject("observation_artifact_reused", "screenshot artifact already has an observation")
    return candidate


def record_action_receipt(
    controller: ControllerLease,
    binding: WindowBinding,
    focus: FocusLease,
    before: Observation,
    after: Observation,
    candidate: ActionReceipt,
    existing: Iterable[ActionReceipt] = (),
) -> ActionReceipt:
    """Accept an action only with same-scope, distinct before and after frames."""

    _validate_observation_context(controller, binding, focus, before)
    _validate_observation_context(controller, binding, focus, after)
    _require_scope(candidate.scope, controller.scope)
    if candidate.controller_lease_id != controller.controller_lease_id:
        _reject("action_controller_mismatch", "action references another controller")
    if candidate.focus_lease_id != focus.focus_lease_id:
        _reject("action_focus_mismatch", "action references another focus lease")
    if candidate.window_binding_id != binding.window_binding_id:
        _reject("action_window_mismatch", "action references another window binding")
    if candidate.controller_generation != controller.generation:
        _reject("stale_controller_generation", "action has stale controller generation")
    if candidate.fencing_token_fingerprint != controller.fencing.public_fingerprint:
        _reject("fencing_fingerprint_mismatch", "action has another fencing fingerprint")
    expected_process = (binding.hwnd, binding.pid, binding.process_created_at)
    receipt_process = (candidate.hwnd, candidate.pid, candidate.process_created_at)
    if receipt_process != expected_process:
        _reject("window_process_identity_mismatch", "action process identity changed")
    if before.phase is not ObservationPhase.BEFORE_ACTION:
        _reject("before_frame_phase_invalid", "before observation has the wrong phase")
    if after.phase is not ObservationPhase.AFTER_ACTION:
        _reject("after_frame_phase_invalid", "after observation has the wrong phase")
    if before.action_request_id != candidate.action_request_id:
        _reject("before_frame_action_mismatch", "before frame belongs to another action")
    if after.action_request_id != candidate.action_request_id:
        _reject("after_frame_action_mismatch", "after frame belongs to another action")
    if candidate.before_observation_id != before.observation_id:
        _reject("before_frame_reference_mismatch", "receipt references another before frame")
    if candidate.after_observation_id != after.observation_id:
        _reject("after_frame_reference_mismatch", "receipt references another after frame")
    if before.artifact_ref == after.artifact_ref:
        _reject("action_frames_not_distinct", "before and after frames need distinct artifacts")
    if before.captured_at > candidate.started_at:
        _reject("before_frame_too_late", "before frame was captured after action start")
    if after.captured_at <= before.captured_at:
        _reject("after_frame_not_newer", "after frame must be newer than before frame")
    if after.captured_at < candidate.started_at:
        _reject("after_frame_too_early", "after frame predates action start")
    if after.captured_at > candidate.completed_at:
        _reject("receipt_completed_before_after_frame", "receipt predates after frame")
    previous = tuple(existing)
    if any(item.action_receipt_id == candidate.action_receipt_id for item in previous):
        _reject("action_receipt_id_reused", "actionReceiptId already exists")
    if any(item.action_request_id == candidate.action_request_id for item in previous):
        _reject("action_request_already_recorded", "action request already has a receipt")
    return candidate


def authorize_action_replay(
    receipt: ActionReceipt,
    controller: ControllerLease,
    binding: WindowBinding,
    focus: FocusLease,
    *,
    at: datetime,
) -> None:
    """Fail closed unless a confirmed same-context result explicitly allows retry."""

    if receipt.result is ActionResult.UNKNOWN:
        _reject(
            "unknown_action_not_replayable",
            "an action with unknown effect must be blocked for review",
        )
    if receipt.replay_disposition is not ReplayDisposition.SAFE_RETRY:
        _reject("action_replay_forbidden", "action receipt does not allow replay")
    if receipt.result not in {ActionResult.NO_EFFECT, ActionResult.FAILED}:
        _reject("action_replay_forbidden", "only confirmed no-effect or failed action may retry")
    _require_active_controller(controller, at)
    _require_active_binding(binding)
    _require_active_focus(focus, at)
    _require_scope(binding.scope, controller.scope)
    _require_scope(focus.scope, controller.scope)
    _require_scope(receipt.scope, controller.scope)
    if binding.controller_lease_id != controller.controller_lease_id:
        _reject("window_controller_mismatch", "window binding references another controller")
    if focus.controller_lease_id != controller.controller_lease_id:
        _reject("focus_controller_mismatch", "focus lease references another controller")
    if focus.window_binding_id != binding.window_binding_id:
        _reject("focus_window_mismatch", "focus lease references another window")
    if focus.controller_generation != controller.generation:
        _reject("stale_controller_generation", "focus lease has stale controller generation")
    if focus.fencing_token_fingerprint != controller.fencing.public_fingerprint:
        _reject("fencing_fingerprint_mismatch", "focus lease has another fencing fingerprint")
    if receipt.controller_lease_id != controller.controller_lease_id:
        _reject("action_controller_mismatch", "receipt belongs to another controller")
    if receipt.focus_lease_id != focus.focus_lease_id:
        _reject("action_focus_mismatch", "receipt belongs to another focus lease")
    if receipt.window_binding_id != binding.window_binding_id:
        _reject("action_window_mismatch", "receipt belongs to another window")
    if receipt.controller_generation != controller.generation:
        _reject("stale_controller_generation", "receipt has stale controller generation")
    if receipt.fencing_token_fingerprint != controller.fencing.public_fingerprint:
        _reject("fencing_fingerprint_mismatch", "receipt has another fencing fingerprint")
    if (receipt.hwnd, receipt.pid, receipt.process_created_at) != (
        binding.hwnd,
        binding.pid,
        binding.process_created_at,
    ):
        _reject("window_process_identity_mismatch", "receipt process identity is stale")


def _validate_checkpoint_evidence(
    candidate: Checkpoint,
    observations: Iterable[Observation],
    receipt: ActionReceipt | None,
) -> None:
    supplied = tuple(observations)
    if len({item.observation_id for item in supplied}) != len(supplied):
        _reject("checkpoint_observation_duplicate", "checkpoint observations contain duplicates")
    if {item.observation_id for item in supplied} != set(candidate.observation_ids):
        _reject("checkpoint_observation_mismatch", "checkpoint observation set is incomplete")
    for item in supplied:
        _require_scope(item.scope, candidate.scope)
        if item.phase is ObservationPhase.BEFORE_ACTION:
            _reject("checkpoint_before_frame_only", "before-action frame cannot prove checkpoint")
        if item.captured_at > candidate.recorded_at:
            _reject("checkpoint_before_observation", "checkpoint predates its observation")
    if candidate.action_receipt_id is None:
        if receipt is not None:
            _reject("checkpoint_unexpected_receipt", "checkpoint does not reference this receipt")
        return
    if receipt is None or receipt.action_receipt_id != candidate.action_receipt_id:
        _reject("checkpoint_receipt_mismatch", "checkpoint receipt is missing or different")
    _require_scope(receipt.scope, candidate.scope)
    if receipt.completed_at > candidate.recorded_at:
        _reject("checkpoint_before_action_receipt", "checkpoint predates its action receipt")


def record_checkpoint(
    current: Checkpoint | None,
    candidate: Checkpoint,
    *,
    observations: Iterable[Observation],
    receipt: ActionReceipt | None = None,
) -> Checkpoint:
    """Advance a checkpoint chain with strictly increasing sequence and progress."""

    if candidate.state is not CheckpointState.VALID:
        _reject(
            "checkpoint_invalidation_requires_explicit_transition",
            "use record_checkpoint_invalidation for invalidation",
        )
    if current is None:
        if candidate.sequence != 1 or candidate.previous_checkpoint_id is not None:
            _reject("checkpoint_chain_invalid", "first checkpoint must start at sequence 1")
    else:
        _require_scope(candidate.scope, current.scope)
        if candidate.todo_instance_id != current.todo_instance_id:
            _reject("checkpoint_todo_mismatch", "checkpoint belongs to another Todo")
        if candidate.checkpoint_id == current.checkpoint_id:
            _reject("checkpoint_id_reused", "each checkpoint transition needs a new ID")
        if candidate.sequence != current.sequence + 1:
            _reject("checkpoint_sequence_not_monotonic", "checkpoint sequence must increase by one")
        if candidate.previous_checkpoint_id != current.checkpoint_id:
            _reject("checkpoint_predecessor_mismatch", "checkpoint predecessor is not current")
        if candidate.progress_index <= current.progress_index:
            _reject("checkpoint_progress_not_monotonic", "checkpoint progress must increase")
        if candidate.recorded_at <= current.recorded_at:
            _reject("checkpoint_time_not_monotonic", "checkpoint time must increase")
    _validate_checkpoint_evidence(candidate, observations, receipt)
    return candidate


def record_checkpoint_invalidation(
    current: Checkpoint,
    candidate: Checkpoint,
    *,
    observations: Iterable[Observation],
    receipt: ActionReceipt | None = None,
) -> Checkpoint:
    """Append an explicit invalidation of the current valid checkpoint."""

    if current.state is not CheckpointState.VALID:
        _reject("checkpoint_already_invalidated", "only a valid checkpoint can be invalidated")
    if candidate.state is not CheckpointState.INVALIDATED:
        _reject("checkpoint_invalidation_missing", "candidate is not an invalidation")
    _require_scope(candidate.scope, current.scope)
    if candidate.todo_instance_id != current.todo_instance_id:
        _reject("checkpoint_todo_mismatch", "checkpoint belongs to another Todo")
    if candidate.sequence != current.sequence + 1:
        _reject("checkpoint_sequence_not_monotonic", "invalidation sequence must increase by one")
    if candidate.previous_checkpoint_id != current.checkpoint_id:
        _reject("checkpoint_predecessor_mismatch", "invalidation predecessor is not current")
    if candidate.invalidates_checkpoint_id != current.checkpoint_id:
        _reject("checkpoint_invalidation_target_mismatch", "invalidation target is not current")
    if candidate.progress_index != current.progress_index:
        _reject("checkpoint_invalidation_changes_progress", "invalidation cannot change progress")
    if candidate.recorded_at <= current.recorded_at:
        _reject("checkpoint_time_not_monotonic", "invalidation time must increase")
    _validate_checkpoint_evidence(candidate, observations, receipt)
    return candidate


def raise_todo_blocker(
    candidate: TodoBlocker,
    existing: Iterable[TodoBlocker] = (),
    *,
    receipt: ActionReceipt | None = None,
) -> TodoBlocker:
    """Raise revision 1 and reject duplicate active blockers for a Todo/code."""

    if candidate.state is not TodoBlockerState.ACTIVE or candidate.revision != 1:
        _reject("blocker_initial_state_invalid", "new blocker must be active revision 1")
    if candidate.kind is TodoBlockerKind.AMBIGUOUS_ACTION:
        if receipt is None or receipt.result is not ActionResult.UNKNOWN:
            _reject(
                "ambiguous_action_receipt_required",
                "ambiguous action blocker requires its unknown ActionReceipt",
            )
    _validate_blocker_action(candidate, receipt)
    previous = tuple(existing)
    if any(item.blocker_id == candidate.blocker_id for item in previous):
        _reject("blocker_id_reused", "blockerId already exists")
    if any(
        item.state is TodoBlockerState.ACTIVE
        and item.scope == candidate.scope
        and item.todo_instance_id == candidate.todo_instance_id
        and item.code == candidate.code
        for item in previous
    ):
        _reject("blocker_already_active", "Todo already has this active blocker")
    return candidate


def _require_same_blocker_identity(current: TodoBlocker, candidate: TodoBlocker) -> None:
    stable = (
        "blocker_id",
        "scope",
        "todo_instance_id",
        "kind",
        "code",
        "raised_at",
    )
    if any(getattr(current, name) != getattr(candidate, name) for name in stable):
        _reject("blocker_identity_changed", "blocker identity is immutable")
    if candidate.revision != current.revision + 1:
        _reject("blocker_revision_not_monotonic", "blocker revision must increase by one")
    if candidate.transitioned_at <= current.transitioned_at:
        _reject("blocker_time_not_monotonic", "blocker transition time must increase")


def _validate_blocker_action(
    candidate: TodoBlocker,
    receipt: ActionReceipt | None,
) -> None:
    if candidate.action_receipt_id is None:
        if receipt is not None:
            _reject("blocker_unexpected_receipt", "blocker does not reference this receipt")
        return
    if receipt is None or receipt.action_receipt_id != candidate.action_receipt_id:
        _reject("blocker_receipt_mismatch", "blocker receipt is missing or different")
    _require_scope(receipt.scope, candidate.scope)
    if receipt.completed_at > candidate.transitioned_at:
        _reject("blocker_before_action_receipt", "blocker transition predates receipt")


def resolve_todo_blocker(
    current: TodoBlocker,
    candidate: TodoBlocker,
    *,
    receipt: ActionReceipt | None = None,
) -> TodoBlocker:
    if current.state is not TodoBlockerState.ACTIVE:
        _reject("blocker_not_active", "only an active blocker can be resolved")
    if candidate.state is not TodoBlockerState.RESOLVED:
        _reject("blocker_resolution_missing", "candidate is not resolved")
    _require_same_blocker_identity(current, candidate)
    if candidate.reason != current.reason or candidate.artifact_refs != current.artifact_refs:
        _reject("blocker_origin_changed", "resolution must preserve blocker origin evidence")
    _validate_blocker_action(candidate, receipt)
    return candidate


def reopen_todo_blocker(
    current: TodoBlocker,
    candidate: TodoBlocker,
    *,
    receipt: ActionReceipt | None = None,
) -> TodoBlocker:
    if current.state is not TodoBlockerState.RESOLVED:
        _reject("blocker_not_resolved", "only a resolved blocker can be reopened")
    if candidate.state is not TodoBlockerState.ACTIVE:
        _reject("blocker_reopen_missing", "candidate is not active")
    _require_same_blocker_identity(current, candidate)
    _validate_blocker_action(candidate, receipt)
    return candidate


def active_todo_blockers(items: Iterable[TodoBlocker]) -> tuple[TodoBlocker, ...]:
    """Return the latest active revision of every blocker aggregate."""

    latest: dict[str, TodoBlocker] = {}
    for item in items:
        previous = latest.get(item.blocker_id)
        if previous is None or item.revision > previous.revision:
            latest[item.blocker_id] = item
    return tuple(
        sorted(
            (item for item in latest.values() if item.state is TodoBlockerState.ACTIVE),
            key=lambda item: (item.transitioned_at, item.blocker_id),
        )
    )
