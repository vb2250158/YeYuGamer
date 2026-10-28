from __future__ import annotations

import threading
import time
from datetime import timedelta

from ..domain.execution_control import LeaseState
from ..domain.models import EntityState
from ..store.sqlite_store import RecordNotFound
from .queue_timeout_budget import load_queue_timeout_budget


RESOURCE_TYPE = "queue-parking"


def attempt_parking_record(manager, attempt_id):
    try:
        resource = manager.store.get_resource(RESOURCE_TYPE, attempt_id)
    except RecordNotFound:
        return None
    return resource if resource["state"] in {"parked", "reclaimed"} else None


def unresolved_parking_record(manager, run_id):
    attempts = manager.store.list_run_attempts(run_id=run_id, limit=1)
    if not attempts:
        return None
    return attempt_parking_record(manager, attempts[0]["run_attempt_id"])


def parking_record(manager, run_id):
    resource = unresolved_parking_record(manager, run_id)
    return resource if resource is not None and resource["state"] == "parked" else None


def park_human_gate(manager, batch_id, run_id, clock):
    return _park_terminal_gate(manager, batch_id, run_id, clock, human=True)


def park_review_gate(manager, batch_id, run_id, clock):
    return _park_terminal_gate(manager, batch_id, run_id, clock, human=False)


def review_gate_released(manager, batch_id, run_id, attempt_id, clock):
    with manager.store.atomic():
        latest = manager.store.list_run_attempts(run_id=run_id, limit=1)
        member = manager.store.get_batch_run_membership(batch_id, run_id)
        batch = manager.store.get_batch(batch_id)
        run = manager.store.get_game_run(run_id)
        if (not latest or latest[0]["run_attempt_id"] != attempt_id
                or member.get("latest_run_attempt_id") != attempt_id
                or member["state"] != "terminal" or batch["result"].get("sealVersion") is not None
                or run["state"] not in {"review_required", "done", "blocked", "failed", "cancelled"}
                or manager.store.get_controller_lease_for_attempt(attempt_id, active_only=True) is not None
                or manager.store.list_controller_leases(active_only=True, limit=1)
                or not manager.adapter_host.release_terminal_owner(attempt_id)):
            return False
        try:
            resource = manager.store.get_resource(RESOURCE_TYPE, attempt_id)
        except RecordNotFound:
            return True
        if resource["state"] == "awaiting" and resource["document"].get("batchId") == batch_id:
            document = {**resource["document"], "releasedAt": clock().isoformat(),
                        "releaseReason": "terminal_review_owner_released", "businessCompleted": False}
            manager.store.update_resource(RESOURCE_TYPE, attempt_id, state="released", document=document)
            manager.store.append_event("queue.parking.released", "run-attempt", attempt_id, document)
        return True


def _park_terminal_gate(manager, batch_id, run_id, clock, *, human):
    attempts = manager.store.list_run_attempts(run_id=run_id, limit=1)
    if not attempts:
        return False
    attempt = attempts[0]
    attempt_id = attempt["run_attempt_id"]
    expected_state = EntityState.HUMAN_REQUIRED if human else EntityState.REVIEW_REQUIRED
    allowed_attempt_states = {"human_required"} if human else {"failed", "blocked", "review_required", "human_required", "cancelled", "completed"}
    if attempt["state"] not in allowed_attempt_states or manager.store.get_game_run(run_id)["state"] != expected_state:
        return False
    original_lease = manager.store.get_controller_lease_for_attempt(attempt_id, active_only=True)
    if not human and original_lease is None:
        return False
    budget = load_queue_timeout_budget(manager.store.get_config()["values"])
    budget_seconds = budget.human_takeover_seconds if human else budget.completion_review_seconds
    gate_kind = "human_takeover" if human else "completion_review"
    try:
        resource = manager.store.get_resource(RESOURCE_TYPE, attempt_id)
    except RecordNotFound:
        started = clock()
        run = manager.store.get_game_run(run_id)
        evidence_refs = list(dict.fromkeys(
            reference
            for todo_id in run["todo_instance_ids"]
            for reference in manager.store.get_todo_instance(todo_id)["evidence_refs"]
        ))
        resource = manager.store.create_resource(
            RESOURCE_TYPE, resource_id=attempt_id, state="awaiting",
            document={
                "batchId": batch_id, "runId": run_id, "runAttemptId": attempt_id,
                "gameId": run["game_id"], "reasonCode": f"{gate_kind}_budget", "gateKind": gate_kind,
                "reason": attempt.get("result", {}).get("message") or run.get("message"),
                "startedAt": started.isoformat(),
                "expiresAt": (started + timedelta(seconds=budget_seconds)).isoformat(),
                "budgetSeconds": budget_seconds,
                "controllerLeaseId": original_lease["controller_lease_id"] if original_lease is not None else None,
                "controllerLeaseGeneration": original_lease["generation"] if original_lease is not None else None,
                "evidenceRefs": evidence_refs,
                "evidenceStatus": "recorded" if evidence_refs else "missing",
                "logScope": {"runId": run_id, "runAttemptId": attempt_id},
                "businessCompleted": False,
            },
        )
        manager.store.append_event("queue.parking.awaiting", "run-attempt", attempt_id, resource["document"])
    if resource["document"]["runId"] != run_id or resource["document"]["batchId"] != batch_id:
        raise ValueError("queue parking owner mismatch")
    if resource["document"].get("gateKind", "human_takeover") != gate_kind:
        return False
    if resource["state"] != "awaiting":
        return False
    remaining = max(0, (manager._contract_datetime(resource["document"]["expiresAt"]) - clock()).total_seconds())
    deadline = time.monotonic() + min(remaining, resource["document"]["budgetSeconds"])
    while clock() < manager._contract_datetime(resource["document"]["expiresAt"]) and time.monotonic() < deadline:
        if manager.store.get_active_batch_cancel_request(batch_id) is not None:
            return False
        if manager.store.get_game_run(run_id)["state"] != expected_state:
            return False
        if not human and manager.store.get_controller_lease_for_attempt(attempt_id, active_only=True) is None:
            return False
        threading.Event().wait(min(budget.poll_seconds, max(0, deadline - time.monotonic())))
    with manager.store.atomic():
        current = manager.store.get_game_run(run_id)
        batch = manager.store.get_batch(batch_id)
        member = manager.store.get_batch_run_membership(batch_id, run_id)
        latest = manager.store.list_run_attempts(run_id=run_id, limit=1)
        if (manager.store.get_active_batch_cancel_request(batch_id) is not None
                or current["state"] != expected_state
                or not latest or latest[0]["run_attempt_id"] != attempt_id
                or batch["result"].get("sealVersion") is not None
                or member.get("latest_run_attempt_id") != attempt_id
                or member["state"] not in {"active", "terminal"}):
            return False
        if latest[0]["state"] not in allowed_attempt_states:
            return False
        current_lease = manager.store.get_controller_lease_for_attempt(attempt_id, active_only=True)
        if not human and current_lease is None:
            return False
        active_leases = manager.store.list_controller_leases(active_only=True, limit=1)
        if active_leases and active_leases[0]["runAttemptId"] != attempt_id:
            return False
        if current_lease is not None and (
                current_lease["controller_lease_id"] != resource["document"].get("controllerLeaseId")
                or current_lease["generation"] != resource["document"].get("controllerLeaseGeneration")):
            return False
        if not manager.adapter_host.release_terminal_owner(attempt_id):
            return False
        manager._end_controller_lease(
            attempt_id, state=LeaseState.REVOKED, reason_code=f"queue_{gate_kind}_budget_expired",
            reason="Terminal gated attempt yielded queue ownership without completing its work.",
        )
        manager.store.update_game_run(
            run_id, state=EntityState.REVIEW_REQUIRED,
            message=f"{gate_kind} budget expired; work remains incomplete on this GameRun.",
        )
        manager.store.update_batch_run_membership(batch_id, run_id, state="terminal",
                                                  latest_run_attempt_id=attempt_id,
                                                  terminal_outcome="review_required")
        document = {**resource["document"], "parkedAt": clock().isoformat(),
                    "ownershipReleased": True, "businessCompleted": False,
                    "resumeRequirements": (["explicit_human_release"] if human else []) + ["fresh_observation", "binding_revalidation", "checkpoint_validation"]}
        manager.store.update_resource(RESOURCE_TYPE, attempt_id, state="parked", document=document)
        manager.store.append_event("queue.parking.expired", "run-attempt", attempt_id, document)
    return True
