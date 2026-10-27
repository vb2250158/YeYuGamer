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
    attempts = manager.store.list_run_attempts(run_id=run_id, limit=1)
    if not attempts:
        return False
    attempt = attempts[0]
    attempt_id = attempt["run_attempt_id"]
    budget = load_queue_timeout_budget(manager.store.get_config()["values"])
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
                "gameId": run["game_id"], "reasonCode": "human_takeover_budget",
                "reason": attempt.get("result", {}).get("message") or run.get("message"),
                "startedAt": started.isoformat(),
                "expiresAt": (started + timedelta(seconds=budget.human_takeover_seconds)).isoformat(),
                "budgetSeconds": budget.human_takeover_seconds,
                "evidenceRefs": evidence_refs,
                "evidenceStatus": "recorded" if evidence_refs else "missing",
                "logScope": {"runId": run_id, "runAttemptId": attempt_id},
                "businessCompleted": False,
            },
        )
        manager.store.append_event("queue.parking.awaiting", "run-attempt", attempt_id, resource["document"])
    if resource["document"]["runId"] != run_id or resource["document"]["batchId"] != batch_id:
        raise ValueError("queue parking owner mismatch")
    remaining = max(0, (manager._contract_datetime(resource["document"]["expiresAt"]) - clock()).total_seconds())
    deadline = time.monotonic() + min(remaining, resource["document"]["budgetSeconds"])
    while clock() < manager._contract_datetime(resource["document"]["expiresAt"]) and time.monotonic() < deadline:
        if manager.store.get_active_batch_cancel_request(batch_id) is not None:
            return False
        if manager.store.get_game_run(run_id)["state"] != EntityState.HUMAN_REQUIRED:
            return False
        threading.Event().wait(min(budget.poll_seconds, max(0, deadline - time.monotonic())))
    with manager.store.atomic():
        current = manager.store.get_game_run(run_id)
        batch = manager.store.get_batch(batch_id)
        member = manager.store.get_batch_run_membership(batch_id, run_id)
        latest = manager.store.list_run_attempts(run_id=run_id, limit=1)
        if (manager.store.get_active_batch_cancel_request(batch_id) is not None
                or current["state"] != EntityState.HUMAN_REQUIRED
                or not latest or latest[0]["run_attempt_id"] != attempt_id
                or batch["result"].get("sealVersion") is not None
                or member.get("latest_run_attempt_id") != attempt_id
                or member["state"] not in {"active", "terminal"}):
            return False
        if latest[0]["state"] != "human_required":
            return False
        if not manager.adapter_host.release_terminal_owner(attempt_id):
            return False
        manager._end_controller_lease(
            attempt_id, state=LeaseState.REVOKED, reason_code="queue_human_budget_expired",
            reason="Terminal human-gated attempt yielded queue ownership without completing its work.",
        )
        manager.store.update_game_run(
            run_id, state=EntityState.REVIEW_REQUIRED,
            message="Human takeover budget expired; work remains incomplete on this GameRun.",
        )
        manager.store.update_batch_run_membership(batch_id, run_id, state="terminal",
                                                  latest_run_attempt_id=attempt_id,
                                                  terminal_outcome="review_required")
        document = {**resource["document"], "parkedAt": clock().isoformat(),
                    "ownershipReleased": True, "businessCompleted": False,
                    "resumeRequirements": ["explicit_human_release", "fresh_observation", "binding_revalidation"]}
        manager.store.update_resource(RESOURCE_TYPE, attempt_id, state="parked", document=document)
        manager.store.append_event("queue.parking.expired", "run-attempt", attempt_id, document)
    return True
