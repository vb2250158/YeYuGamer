from __future__ import annotations

from .queue_timeout_budget import load_queue_timeout_budget
from .game_launcher import GameLaunchCancelled


def validate_resume_intent(manager, intent_id, run_id, *, successor_id=None):
    intent = manager.store.latest_resume_intent(run_id)
    if intent is None or intent["resume_intent_id"] != intent_id or intent["state"] != "successor_pending":
        raise GameLaunchCancelled("Resume intent generation changed before successor dispatch")
    if successor_id is not None and intent["successor_run_attempt_id"] != successor_id:
        raise GameLaunchCancelled("Resume intent does not own this successor attempt")
    return intent


def _host_owns_run(manager, run_id):
    active = manager.adapter_host.active_execution()
    return active is not None and (
        not isinstance(active, dict) or not active.get("runId") or active["runId"] == run_id
    )


def recover_orphan_pending_executions(manager, at, *, run_id=None, failed_intent_id=None):
    recovered = []
    budget = load_queue_timeout_budget(manager.store.get_config()["values"]).orphan_pending_seconds
    with manager._execution_handoff_lock:
        for candidate in manager.store.orphan_pending_execution_candidates(run_id)[:500]:
            if _host_owns_run(manager, candidate["run_id"]):
                continue
            with manager.store.atomic():
                current = manager.store.orphan_pending_execution_candidates(candidate["run_id"])
                if not current or current[0] != candidate or _host_owns_run(manager, candidate["run_id"]):
                    continue
                immediate = failed_intent_id is not None and candidate["intent_id"] == failed_intent_id
                if failed_intent_id is not None and not immediate:
                    continue
                if not immediate and (at - manager._contract_datetime(candidate["updated_at"])).total_seconds() < budget:
                    continue
                state = candidate["attempt_state"]
                if state == "completed":
                    state = "review_required"
                reason = "resume_successor_start_failed" if immediate else "orphan_pending_execution_timeout"
                manager.store.update_game_run(candidate["run_id"], state=state,
                                              message=f"{reason}: no attempt, lease, Host or queued membership owns execution; preserved last attempt outcome.")
                manager.store.append_event("game-run.orphan-pending-execution-reconciled", "game-run", candidate["run_id"], {
                    "runId": candidate["run_id"], "runAttemptId": candidate["attempt_id"],
                    "resumeIntentId": candidate["intent_id"], "reasonCode": reason,
                    "previousState": "pending_execution", "state": state,
                    "businessCompleted": False, "budgetSeconds": budget,
                })
                recovered.append(candidate["run_id"])
    return recovered


def fail_resume_successor(manager, intent_id, at):
    with manager._execution_handoff_lock, manager.store.atomic():
        intent = manager.store.get_resume_intent(intent_id)
        if intent["state"] not in {"successor_pending", "successor_dispatched", "successor_failed"}:
            return []
        manager.store.update_resume_intent(intent_id, state="successor_failed")
        latest_intent = manager.store.latest_resume_intent(intent["run_id"])
        if (latest_intent is None or latest_intent["resume_intent_id"] != intent_id
                or _host_owns_run(manager, intent["run_id"])
                or manager.store.list_controller_leases(run_id=intent["run_id"], active_only=True, limit=1)):
            return []
        attempts = manager.store.list_run_attempts(run_id=intent["run_id"], limit=5000)
        if not attempts or len(attempts) == 5000 or any(row["state"] in {"starting", "running", "cancelling"} for row in attempts):
            return []
        latest = attempts[0]
        if latest["state"] not in {"failed", "blocked", "review_required", "human_required", "cancelled", "completed"}:
            return []
        for member in manager.store.list_batch_run_memberships(run_id=intent["run_id"], limit=5000):
            if (member.get("resume_intent_id") == intent_id
                    and member.get("latest_run_attempt_id") == latest["run_attempt_id"]
                    and member["state"] in {"resume_pending", "active"}
                    and manager.store.get_batch(member["batch_id"])["result"].get("sealVersion") is None):
                manager.store.update_batch_run_membership(member["batch_id"], intent["run_id"], state="terminal",
                                                         terminal_outcome=latest["state"])
    return recover_orphan_pending_executions(manager, at, run_id=intent["run_id"], failed_intent_id=intent_id)
