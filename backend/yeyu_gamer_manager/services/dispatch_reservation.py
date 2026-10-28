from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from .game_launcher import GameLaunchCancelled
from .queue_timeout_budget import load_queue_timeout_budget
from .orphan_pending_execution import validate_resume_intent


RESOURCE_TYPE = "attempt-dispatch"
_log = logging.getLogger(__name__)


class DispatchReservationRejected(GameLaunchCancelled):
    pass


class DispatchGate:
    lock = threading.RLock()


def _resource_id(plan):
    return f"dispatch:{plan.run_attempt_id}"


def _digest(value):
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _scope(manager, plan, run):
    return {
        "version": 2,
        "managerId": manager.manager_id,
        "gameId": plan.game_id,
        "accountId": plan.account_id,
        "accountSnapshotDigest": _digest(plan.account_snapshot),
        "runAccountId": run.get("account_id", "default"),
        "runAccountSnapshotDigest": _digest(run.get("account_snapshot", {})),
        "runId": plan.run_id,
        "runAttemptId": plan.run_attempt_id,
        "gameDayKey": manager._run_game_day_key(run, list(plan.executable_todo_instance_ids)),
        "cadence": plan.cadence,
        "planDigest": _digest(plan.to_document()),
    }


def _members(store, run_id):
    members = store.list_batch_run_memberships(run_id=run_id, limit=5000)
    if len(members) >= 5000:
        raise DispatchReservationRejected("Dispatch membership view is truncated")
    return sorted(
        ({key: member[key] for key in (
            "batch_id", "run_id", "state", "latest_run_attempt_id", "ordinal"
        )} for member in members if member["state"] not in {"terminal", "cancelled"}),
        key=lambda member: member["batch_id"]
    )


def register_prepared(manager, plan, lease_id, generation):
    store = manager.store
    if not store.connection.in_transaction or store._transaction_depth < 1:
        raise DispatchReservationRejected("Dispatch registration requires the prepare transaction")
    run = store.get_game_run(plan.run_id)
    lease = store.get_controller_lease_private(lease_id)
    if (lease["run_attempt_id"] != plan.run_attempt_id
            or lease["generation"] != generation or lease["state"] != "active"):
        raise DispatchReservationRejected("Prepared dispatch lease differs")
    budget = load_queue_timeout_budget(store.get_config()["values"])
    resume_intent_id = None
    if run["state"] == "pending_execution":
        intent = store.latest_resume_intent(plan.run_id)
        if intent is None:
            raise DispatchReservationRejected("Pending execution has no resume intent")
        validate_resume_intent(manager, intent["resume_intent_id"], plan.run_id, successor_id=plan.run_attempt_id)
        resume_intent_id = intent["resume_intent_id"]
    deadline = datetime.fromisoformat(plan.issued_at) + timedelta(
        seconds=plan.timeout_seconds + budget.callback_overhead_seconds
    )
    store.create_resource(RESOURCE_TYPE, resource_id=_resource_id(plan), state="reserved", document={
        "scope": _scope(manager, plan, run),
        "controllerLeaseId": lease_id,
        "controllerLeaseGeneration": generation,
        "desktopId": lease["desktop_id"],
        "members": _members(store, plan.run_id),
        "resumeIntentId": resume_intent_id,
        "deadlineAt": deadline.isoformat(),
        "businessCompleted": False,
    })


def _classify(manager, plan, expected_state):
    store = manager.store
    resource = store.get_resource(RESOURCE_TYPE, _resource_id(plan))
    document = resource["document"]
    run = store.get_game_run(plan.run_id)
    attempt = store.get_run_attempt(plan.run_attempt_id)
    latest = store.list_run_attempts(run_id=plan.run_id, limit=1)
    lease = store.get_controller_lease_for_attempt(plan.run_attempt_id, active_only=True)
    if (resource["state"] != expected_state or document["scope"] != _scope(manager, plan, run)
            or run["game_id"] != plan.game_id or run["mode"] != "execute"
            or run["state"] not in {"queued", "running", "pending_execution"}
            or not latest or latest[0]["run_attempt_id"] != plan.run_attempt_id
            or attempt["run_id"] != plan.run_id or attempt["game_id"] != plan.game_id
            or attempt["fencing_token_hash"] != "sha256:" + hashlib.sha256(plan.fencing_token.encode()).hexdigest()
            or attempt["cancel_authority_hash"] != "sha256:" + hashlib.sha256(plan.cancel_authority.encode()).hexdigest()
            or attempt["completed_at"] or attempt["state"] not in {"starting", "running", "cancelling"}
            or lease is None or lease["controller_lease_id"] != document["controllerLeaseId"]
            or lease["generation"] != document["controllerLeaseGeneration"]
            or lease["desktop_id"] != document["desktopId"]
            or lease["manager_id"] != manager.manager_id
            or lease["run_id"] != plan.run_id or lease["game_id"] != plan.game_id
            or lease["game_day_key"] != document["scope"]["gameDayKey"]
            or lease["fencing_hash"] != "sha256:" + hashlib.sha256(plan.fencing_token.encode()).hexdigest()
            or _members(store, plan.run_id) != document["members"]):
        raise DispatchReservationRejected("Dispatch scope, owner or membership changed")
    if document.get("resumeIntentId") is not None:
        validate_resume_intent(manager, document["resumeIntentId"], plan.run_id, successor_id=plan.run_attempt_id)
    elif run["state"] == "pending_execution":
        raise DispatchReservationRejected("Pending dispatch has no frozen resume intent")
    leases = store.list_controller_leases_private(desktop_id=lease["desktop_id"])
    if (len(leases) >= 1000 or any(item["generation"] > lease["generation"]
            or (item["state"] == "active" and item["controller_lease_id"] != lease["controller_lease_id"])
            for item in leases)):
        raise DispatchReservationRejected("Dispatch desktop lease generation changed")
    for member in document["members"]:
        batch = store.get_batch(member["batch_id"])
        if (member["state"] != "active" or member["latest_run_attempt_id"] != plan.run_attempt_id
                or (batch["state"] not in {"queued", "running"}
                    and not (batch["state"] == "pending_execution" and document.get("resumeIntentId") is not None))
                or batch["result"].get("sealVersion") is not None
                or store.get_active_batch_cancel_request(member["batch_id"]) is not None):
            raise DispatchReservationRejected("Dispatch batch is no longer authorized")
    return resource, attempt, lease


def _owner_view_idle(manager):
    host = manager.adapter_host
    if host.active_execution() is not None:
        return False
    if host.compatibility_adapter.active_execution() is not None:
        return False
    for name in ("_active_plan", "_active_process", "_owned_job", "_owned_jobs", "owned_job", "owned_jobs"):
        if getattr(host, name, None):
            return False
    return True


def claim_start(manager, plan):
    idle = _owner_view_idle(manager)
    with DispatchGate.lock, manager.store.atomic():
        if not idle:
            raise DispatchReservationRejected("Dispatch has an existing Host or compatibility owner")
        resource, attempt, _ = _classify(manager, plan, "reserved")
        if attempt["state"] != "starting" or attempt["process_id"] is not None:
            raise DispatchReservationRejected("Dispatch is no longer pristine")
        manager.store.update_resource(RESOURCE_TYPE, _resource_id(plan), state="claimed", document={
            **resource["document"], "claimedAt": datetime.now(timezone.utc).isoformat(),
        })
    return True


def validate_start(manager, plan):
    with DispatchGate.lock, manager.store.atomic():
        _, attempt, _ = _classify(manager, plan, "claimed")
        if attempt["state"] not in {"starting", "running"} or attempt["process_id"] is not None:
            raise DispatchReservationRejected("Dispatch no longer authorizes a Host start")
    return True


def callback_wait_seconds(manager, plan, clock):
    resource = manager.store.get_resource(RESOURCE_TYPE, _resource_id(plan))
    document = resource["document"]
    run = manager.store.get_game_run(plan.run_id)
    if document["scope"] != _scope(manager, plan, run):
        raise DispatchReservationRejected("Callback deadline belongs to another dispatch scope")
    deadline = datetime.fromisoformat(document["deadlineAt"])
    now = clock()
    if deadline.utcoffset() is None or now.utcoffset() is None:
        raise DispatchReservationRejected("Callback deadline requires timezone-aware timestamps")
    return max(0.0, (deadline - now).total_seconds())


def recover_undispatched(manager, batch_id, plan, clock):
    try:
        idle = _owner_view_idle(manager)
        with DispatchGate.lock, manager.store.atomic():
            if not idle:
                return False
            resource, attempt, lease = _classify(manager, plan, "reserved")
            document = resource["document"]
            now = clock()
            deadline = datetime.fromisoformat(document["deadlineAt"])
            if (now.utcoffset() is None or deadline.utcoffset() is None or now < deadline
                    or attempt["process_id"] is not None
                    or attempt["state"] not in {"starting", "cancelling"}
                    or (attempt["state"] == "cancelling"
                        and attempt["result"].get("code") != "manager_batch_wait_timeout")
                    or attempt["result"].get("launchState")
                    or len(document["members"]) != 1
                    or document["members"][0]["batch_id"] != batch_id
                    or manager.store.list_todo_attempts(run_attempt_id=plan.run_attempt_id, limit=1)
                    or manager.store.list_adapter_events(plan.run_attempt_id)):
                return False
            receipt = {**document, "expiredAt": now.isoformat(), "reasonCode": "undispatched_timeout"}
            manager.store.update_resource(RESOURCE_TYPE, _resource_id(plan), state="expired", document=receipt)
            manager.store.update_run_attempt(plan.run_attempt_id, state="review_required", completed=True,
                                            result={"code": "undispatched_timeout", "businessCompleted": False})
            manager.store.update_game_run(plan.run_id, state="review_required",
                                          message="Prepared attempt expired before dispatch; business outcome requires review.")
            manager.store.transition_controller_lease(lease["controller_lease_id"], state="revoked",
                reason_code="undispatched_timeout", reason="Dispatch reservation expired before claim", ended_at=now.isoformat())
            manager.store.update_batch_run_membership(batch_id, plan.run_id, state="terminal",
                latest_run_attempt_id=plan.run_attempt_id, terminal_outcome="review_required")
            manager.store.append_event("dispatch.expired", "run-attempt", plan.run_attempt_id, receipt)
        return True
    except DispatchReservationRejected:
        _log.info("dispatch.recovery.ineligible attempt=%s", plan.run_attempt_id)
        return False
    except Exception:
        _log.exception("dispatch.recovery.rejected attempt=%s", plan.run_attempt_id)
        return False
