"""Pure public projection of Manager-owned Batch action availability."""

from __future__ import annotations

from typing import Any

from ..domain.models import BatchRecord, EntityState, RequestMode


def project_batch_record(
    item: dict[str, Any],
    *,
    execution_readiness: tuple[bool, str, str] | None,
    terminal_retry_run_ids: list[str],
    pending_with_attempt_ids: list[str],
    run_resume_targets: list[dict[str, Any]],
    run_reconcile_targets: list[dict[str, Any]],
    human_takeover_targets: list[dict[str, Any]] | None = None,
) -> BatchRecord:
    """Add action availability to a Batch after Manager preflight facts are known."""

    execution_ready, execution_reason_code, execution_reason = (
        execution_readiness
        or (
            False,
            "refresh_batch_projection",
            "Refresh the Manager Batch projection before requesting resume.",
        )
    )
    result = dict(item.get("result", {}))
    memberships = list(item.get("run_memberships", []))
    sealed = result.get("sealVersion") is not None
    cancel_request = item.get("cancel_request")
    human_gate_active = bool(not sealed and cancel_request is None and human_takeover_targets)
    recovery = result.get("recoveryPhase")
    completion_review = result.get("completionReviewPhase")
    pending = [
        membership
        for membership in memberships
        if membership["state"] in {"queued", "resume_pending"}
    ]
    recovery_status = (
        str(recovery.get("status")) if isinstance(recovery, dict) else None
    )
    review_available = not human_gate_active and (bool(
        isinstance(completion_review, dict)
        and completion_review.get("status") == "awaiting"
    ) or recovery_status in {
        "awaiting_reconciliation",
        "reconciliation_required",
    })
    cancel_available = bool(
        not sealed
        and str(item.get("mode")) == str(RequestMode.EXECUTE)
        and cancel_request is None
    )
    resume_available = bool(
        not sealed
        and not human_gate_active
        and recovery_status == "ready_for_resume"
        and pending
        and not terminal_retry_run_ids
        and not pending_with_attempt_ids
        and cancel_request is None
        and not any(
            membership["state"] in {"active", "reconciliation_required"}
            for membership in memberships
        )
        and execution_ready
    )
    run_resume_available = bool(
        not sealed
        and not human_gate_active
        and run_resume_targets
        and cancel_request is None
        and not review_available
        and execution_ready
    )
    # A review work item is not an ActionReceipt/Checkpoint provider. Until a
    # Manager-owned reconciliation provider can produce those fenced facts,
    # never expose a button which only creates another work item and leaves the
    # run in the same state. Unknown prior actions must be ended and fresh-started.
    run_reconcile_available = False

    if sealed:
        reason_code = "batch_sealed"
        reason = "Batch has an immutable seal; execution actions are closed."
        next_action = "view_sealed_result"
    elif cancel_request is not None:
        reason_code = "cancellation_in_progress"
        reason = "A durable cooperative cancellation request is active."
        next_action = "reconcile_cancellation"
    elif human_gate_active:
        reason_code = "explicit_human_release_required"
        reason = "A current GameRun requires human takeover; preserve its scene and explicitly release before same-run resume."
        next_action = "release_human_takeover"
    elif review_available:
        reason_code = "batch_review_required"
        reason = "Manager requires the current recovery or completion review."
        next_action = "review_batch_work_items"
    elif terminal_retry_run_ids:
        if run_resume_available:
            reason_code = "same_run_resume_required"
            reason = (
                "Terminal failed members have an executable typed same-GameRun "
                "successor: " + ", ".join(terminal_retry_run_ids)
            )
            next_action = "resume_terminal_game_runs"
        else:
            reason_code = "same_run_resume_unavailable_start_fresh"
            reason = (
                "Terminal failed members have an unknown prior action and no "
                "Manager-owned reconciliation provider. Safely stop this old "
                "batch, then start a fresh daily run: "
                + ", ".join(terminal_retry_run_ids)
            )
            next_action = "cancel_old_batch_then_start_fresh"
    elif pending_with_attempt_ids:
        if run_resume_available:
            reason_code = "same_run_resume_required"
            reason = (
                "Pending members already have an attempt lineage and require an "
                "executable typed successor: " + ", ".join(pending_with_attempt_ids)
            )
            next_action = "resume_terminal_game_runs"
        else:
            reason_code = "same_run_resume_unavailable_start_fresh"
            reason = (
                "Pending members have prior attempt lineage but no authoritative "
                "reconciliation provider. Safely stop this old batch, then start "
                "a fresh daily run: "
                + ", ".join(pending_with_attempt_ids)
            )
            next_action = "cancel_old_batch_then_start_fresh"
    elif resume_available:
        reason_code = "batch_resume_ready"
        reason = "Only never-started members of this Batch will be dispatched."
        next_action = "resume_batch"
    elif recovery_status in {"resume_dispatch_pending", "resuming"}:
        reason_code = "batch_resume_in_progress"
        reason = "The typed Batch resume coordinator is already active."
        next_action = "wait_for_batch_progress"
    elif str(item.get("state")) in {
        str(EntityState.PENDING_EXECUTION),
        str(EntityState.QUEUED),
        str(EntityState.RUNNING),
        str(EntityState.CANCELLING),
    }:
        reason_code = "batch_execution_in_progress"
        reason = "Batch execution is active; only cooperative cancellation is allowed."
        next_action = "wait_or_cancel_batch"
    elif recovery_status == "ready_for_resume" and not execution_ready:
        reason_code = execution_reason_code
        reason = execution_reason
        next_action = (
            "enable_verified_execution_policy"
            if execution_reason_code == "execution_policy_disabled"
            else "repair_adapter_runtime"
        )
    else:
        reason_code = "batch_no_available_action"
        reason = "No typed Batch mutation is available for the current facts."
        next_action = "inspect_batch"

    projected = {
        **item,
        "result": {
            **result,
            "batchActionAvailability": {
                "resume": resume_available,
                "runResume": run_resume_available,
                "runResumeTargets": [] if human_gate_active else run_resume_targets,
                "humanTakeover": human_gate_active,
                "humanTakeoverTargets": human_takeover_targets if human_gate_active else [],
                "runReconcile": run_reconcile_available,
                "runReconcileTargets": run_reconcile_targets,
                "cancel": cancel_available,
                "review": review_available,
                "nextAction": next_action,
                "reasonCode": reason_code,
                "reason": reason,
            },
        },
    }
    return BatchRecord.model_validate(projected)
