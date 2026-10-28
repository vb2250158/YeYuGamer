from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from ..store.sqlite_store import RecordNotFound
from . import dispatch_reservation
from .queue_timeout_budget import load_queue_timeout_budget


RECOVERY_RESOURCE = "input-recovery"
QUARANTINE_RESOURCE = "quarantine"
DESKTOP_ID = "windows-interactive-desktop"


def _pending_for(manager, create=False):
    pending = getattr(manager, "_owned_input_recovery_pending", None)
    if pending is None and create:
        pending = {}
        manager._owned_input_recovery_pending = pending
    if pending is not None and type(pending) is not dict:
        raise ValueError("Owned recovery capability cache changed")
    return pending


@dataclass(frozen=True, eq=False)
class _CommittedRecovery:
    store: object
    plan: object
    ctx: object
    receipt: object
    scope: dict
    lease: dict
    dispatch: dict
    document: dict
    attempt_document: dict
    owner_released: bool = False


def attempt_recovery_record(manager, attempt_id):
    try:
        return manager.store.get_resource(RECOVERY_RESOURCE, attempt_id)
    except RecordNotFound:
        return None


def desktop_quarantine(manager, desktop_id=DESKTOP_ID):
    try:
        record = manager.store.get_resource(QUARANTINE_RESOURCE, desktop_id)
    except RecordNotFound:
        return None
    return record if record["state"] == "quarantined" else None


def require_desktop_available(manager):
    if (desktop_quarantine(manager) is not None
            and not (manager.store.connection.in_transaction and manager.store._lock._is_owned())):
        retry_committed_recovery(manager)
    if desktop_quarantine(manager) is not None:
        raise dispatch_reservation.DispatchReservationRejected("Desktop is durably quarantined; owner is not proven drained")


def _committed_snapshot(manager, capability):
    store = manager.store
    scope = capability.scope
    plan = capability.plan
    record = store.get_resource(RECOVERY_RESOURCE, plan.run_attempt_id)
    quarantine = store.get_resource(QUARANTINE_RESOURCE, scope["desktopId"])
    attempt = store.get_run_attempt(plan.run_attempt_id)
    run = store.get_game_run(plan.run_id)
    latest = store.list_run_attempts(run_id=plan.run_id, limit=1)
    member = store.get_batch_run_membership(scope["memberBatchId"], plan.run_id)
    batch = store.get_batch(scope["memberBatchId"])
    lease = store.get_controller_lease_for_attempt(plan.run_attempt_id)
    dispatch = store.get_resource("attempt-dispatch", f"dispatch:{plan.run_attempt_id}")
    leases = store.list_controller_leases_private(desktop_id=scope["desktopId"])
    if (store is not capability.store or record["state"] not in {"blocking", "expired"}
            or record["document"] != capability.document or quarantine["state"] != "quarantined"
            or quarantine["document"] != capability.document
            or attempt["state"] != "review_required" or not attempt["completed_at"]
            or attempt["run_id"] != plan.run_id or attempt["game_id"] != plan.game_id
            or attempt["result"].get("inputRecovery") != capability.attempt_document
            or attempt["result"].get("ownedInputScope") != scope
            or attempt["result"].get("packageDigest") != scope["packageDigest"]
            or run["state"] != "review_required" or run["game_id"] != scope["gameId"]
            or run.get("account_id", "default") != scope["accountId"]
            or not latest or latest[0]["run_attempt_id"] != plan.run_attempt_id
            or member["state"] != "terminal" or member["terminal_outcome"] != "review_required"
            or member.get("latest_run_attempt_id") != plan.run_attempt_id
            or batch["result"].get("sealVersion") is not None
            or batch["state"] not in {"queued", "running", "review_required", "blocked"}
            or store.get_active_batch_cancel_request(scope["memberBatchId"]) is not None
            or lease is None or lease["state"] != "revoked"
            or any(lease[key] != capability.lease[key] for key in (
                "controller_lease_id", "generation", "desktop_id", "manager_id", "game_id", "run_id",
                "run_attempt_id", "game_day_key", "fencing_hash"))
            or lease["manager_id"] != manager.manager_id or dispatch != capability.dispatch
            or dispatch["document"]["scope"] != dispatch_reservation._scope(manager, plan, run)
            or len(leases) >= 1000 or any(item["generation"] > scope["controllerLeaseGeneration"]
                or item["state"] == "active" for item in leases)):
        raise dispatch_reservation.DispatchReservationRejected("Committed recovery scope or terminal ownership changed")
    return record, quarantine, attempt, run, member, batch, lease, dispatch


def retry_committed_recovery(manager, attempt_id=None) -> bool:
    """Retry only a locally scope-validated live owner, never reconstruct proof from resources."""
    store = manager.store
    if store.connection.in_transaction and store._lock._is_owned():
        return False
    with dispatch_reservation.DispatchGate.lock:
        pending = _pending_for(manager)
        if not pending:
            return False
        quarantine = desktop_quarantine(manager)
        if quarantine is None:
            return False
        owner_attempt = quarantine["document"].get("runAttemptId")
        if attempt_id is not None and attempt_id != owner_attempt:
            return False
        capability = pending.get(owner_attempt)
        if type(capability) is not _CommittedRecovery or capability.store is not store:
            return False
        try:
            if (_scope_document(capability.ctx.scope) != capability.scope
                    or capability.ctx.matches(capability.ctx.scope) is not True):
                return False
            with store.atomic():
                before = _committed_snapshot(manager, capability)
        except (RecordNotFound, ValueError, TypeError, dispatch_reservation.DispatchReservationRejected):
            return False
        receipt = capability.receipt
        if receipt is None:
            try:
                budget = load_queue_timeout_budget(store.get_config()["values"])
                receipt = capability.ctx.stop_and_drain(budget.host_stop_grace_seconds)
            except Exception:
                return False
        try:
            if (receipt is None or isinstance(receipt, (Mapping, bool, str, int, float, bytes))
                    or _scope_document(capability.ctx.scope) != capability.scope
                    or capability.ctx.matches(capability.ctx.scope) is not True
                    or capability.ctx.verify_receipt(receipt, capability.ctx.scope) is not True):
                return False
        except Exception:
            return False
        capability = replace(capability, receipt=receipt)
        pending[owner_attempt] = capability
        if capability.document.get("proofVerified") is not True:
            with store.atomic():
                try:
                    current = _committed_snapshot(manager, capability)
                except (RecordNotFound, dispatch_reservation.DispatchReservationRejected):
                    return False
                if current != before:
                    return False
                document = {**capability.document, "proofVerified": True, "lateProofVerified": True,
                            "reasonCode": "owned_execution_drained_late", "evidenceStatus": "verified_internal_receipt",
                            "requiresPhysicalReconciliation": False}
                updated = replace(capability, document=document)
                store.update_resource(RECOVERY_RESOURCE, owner_attempt, state="expired", document=document)
                store.update_resource(QUARANTINE_RESOURCE, capability.scope["desktopId"],
                                      state="quarantined", document=document)
                store.append_event("input-recovery.late-proof", "run-attempt", owner_attempt, document)
                before = _committed_snapshot(manager, updated)
            capability = updated
            pending[owner_attempt] = capability
        if not capability.owner_released:
            try:
                released = manager.adapter_host.release_owned_execution(
                    capability.plan, capability.ctx, capability.receipt) is True
            except Exception:
                released = False
            if released:
                capability = replace(capability, owner_released=True)
                pending[owner_attempt] = capability
        with store.atomic():
            try:
                current = _committed_snapshot(manager, capability)
            except (RecordNotFound, dispatch_reservation.DispatchReservationRejected):
                return False
            if current != before:
                return False
            document = {**capability.document, "ownerRetained": not capability.owner_released,
                        "requiresPhysicalReconciliation": not capability.owner_released}
            store.update_resource(RECOVERY_RESOURCE, owner_attempt,
                                  state="expired" if capability.owner_released else "blocking", document=document)
            store.update_resource(QUARANTINE_RESOURCE, capability.scope["desktopId"],
                                  state="released" if capability.owner_released else "quarantined", document=document)
            store.append_event("input-recovery.owner-release", "run-attempt", owner_attempt,
                               {"released": capability.owner_released, "businessCompleted": False})
        if capability.owner_released:
            del pending[owner_attempt]
        else:
            pending[owner_attempt] = replace(capability, document=document)
        return capability.owner_released


def preserve_start_failure(manager, plan):
    resolver = getattr(manager.adapter_host, "active_plan_for_run", None)
    if not callable(resolver) or resolver(plan.run_id) is not plan:
        return False
    with manager.store.atomic():
        resource = manager.store.get_resource("attempt-dispatch", f"dispatch:{plan.run_attempt_id}")
        if resource["state"] != "claimed":
            raise dispatch_reservation.DispatchReservationRejected("Failed Host start has no claimed dispatch")
        _, attempt, _ = dispatch_reservation._classify(manager, plan, "claimed")
        manager.store.update_run_attempt(plan.run_attempt_id, state=attempt["state"], completed=False,
                                         result={"inputRecoveryStartFailed": True,
                                                 "code": "input_recovery_start_failed"})
        manager.store.append_event("input-recovery.start-failed", "run-attempt", plan.run_attempt_id,
                                   {"runId": plan.run_id, "ownerRetained": True, "businessCompleted": False})
    return True


def scope_for_plan(manager, plan, package_digest) -> dict:
    if not isinstance(package_digest, str) or not package_digest:
        raise ValueError("Owned execution requires an exact package digest")
    with manager.store.atomic():
        require_desktop_available(manager)
        resource = manager.store.get_resource("attempt-dispatch", f"dispatch:{plan.run_attempt_id}")
        if resource["state"] != "claimed":
            raise dispatch_reservation.DispatchReservationRejected("Owned execution requires a claimed dispatch")
        _, attempt, lease = dispatch_reservation._classify(manager, plan, "claimed")
        members = resource["document"]["members"]
        if len(members) != 1:
            raise dispatch_reservation.DispatchReservationRejected("Owned execution requires exact single batch membership")
        persisted_digest = attempt["result"].get("packageDigest")
        if persisted_digest is not None and persisted_digest != package_digest:
            raise dispatch_reservation.DispatchReservationRejected("Owned execution package changed")
        scope = {"runId": plan.run_id, "runAttemptId": plan.run_attempt_id,
                "gameId": plan.game_id, "accountId": plan.account_id,
                "desktopId": lease["desktop_id"], "controllerLeaseId": lease["controller_lease_id"],
                "controllerLeaseGeneration": lease["generation"],
                "memberBatchId": members[0]["batch_id"], "packageDigest": package_digest}
        if "ownedInputScope" in attempt["result"] and attempt["result"]["ownedInputScope"] != scope:
            raise dispatch_reservation.DispatchReservationRejected("Persisted owned input scope changed")
        if persisted_digest != package_digest or attempt["result"].get("ownedInputScope") != scope:
            manager.store.update_run_attempt(plan.run_attempt_id, state=attempt["state"], completed=False,
                                             result={"packageDigest": package_digest, "ownedInputScope": scope})
        return scope


def ignore_recovered_callback(manager, plan, kind):
    if attempt_recovery_record(manager, plan.run_attempt_id) is None:
        return False
    manager.store.append_event(f"input-recovery.late-{kind}", "run-attempt", plan.run_attempt_id,
                              {"runId": plan.run_id, "businessCompleted": False})
    return True


def skip_quarantined_member(manager, batch_id, run_id):
    with manager.store.atomic():
        quarantine = desktop_quarantine(manager)
        if quarantine is None:
            return False
        batch = manager.store.get_batch(batch_id)
        member = manager.store.get_batch_run_membership(batch_id, run_id)
        run = manager.store.get_game_run(run_id)
        latest = manager.store.list_run_attempts(run_id=run_id, limit=1)
        if (batch["result"].get("sealVersion") is not None
                or manager.store.get_active_batch_cancel_request(batch_id) is not None
                or member["state"] not in {"queued", "resume_pending"}
                or run["state"] not in {"queued", "pending_execution"}
                or (latest and (latest[0]["state"] in {"starting", "running", "cancelling"}
                                or member.get("latest_run_attempt_id") != latest[0]["run_attempt_id"]))):
            raise dispatch_reservation.DispatchReservationRejected("Quarantined member authorization changed")
        manager.store.update_game_run(run_id, state="review_required",
                                      message="Desktop quarantined; member was not started and requires review.")
        manager.store.update_batch_run_membership(batch_id, run_id, state="terminal",
                                                  terminal_outcome="not_started_desktop_quarantine")
        manager.store.append_event("input-recovery.member-not-started", "game-run", run_id,
                                  {"batchId": batch_id, "desktopId": DESKTOP_ID,
                                   "ownerRunAttemptId": quarantine["document"]["runAttemptId"],
                                   "businessCompleted": False})
    return True


def _snapshot(manager, batch_id, plan, clock):
    dispatch = manager.store.get_resource("attempt-dispatch", f"dispatch:{plan.run_attempt_id}")
    if dispatch["state"] not in {"reserved", "claimed"}:
        raise dispatch_reservation.DispatchReservationRejected("Dispatch is no longer recoverable")
    resource, attempt, lease = dispatch_reservation._classify(manager, plan, dispatch["state"])
    now = clock()
    deadline = datetime.fromisoformat(resource["document"]["deadlineAt"])
    members = resource["document"]["members"]
    if (now.utcoffset() is None or deadline.utcoffset() is None
            or (now < deadline and attempt["result"].get("inputRecoveryStartFailed") is not True)
            or len(members) != 1 or members[0]["batch_id"] != batch_id):
        raise dispatch_reservation.DispatchReservationRejected("Recovery deadline or exact membership differs")
    return (resource, attempt, lease, manager.store.get_game_run(plan.run_id),
            manager.store.get_batch(batch_id), manager.store.get_batch_run_membership(batch_id, plan.run_id))


def _scope_document(scope):
    names = {
        "runId": "run_id", "runAttemptId": "run_attempt_id", "gameId": "game_id",
        "accountId": "account_id", "desktopId": "desktop_id",
        "controllerLeaseId": "controller_lease_id",
        "controllerLeaseGeneration": "controller_lease_generation",
        "memberBatchId": "member_batch_id", "packageDigest": "package_digest",
    }
    if isinstance(scope, Mapping):
        result = {name: scope.get(name) for name in names}
    else:
        result = {name: getattr(scope, attribute, None) for name, attribute in names.items()}
    if (any(not isinstance(value, str) or not value for name, value in result.items()
            if name != "controllerLeaseGeneration")
            or type(result["controllerLeaseGeneration"]) is not int):
        raise ValueError("Owned execution scope is incomplete")
    return result


def recover_attempt(manager, batch_id, plan, clock) -> bool:
    store = manager.store
    if store.connection.in_transaction and store._lock._is_owned():
        raise ValueError("Input recovery requires its own commit before Host release")
    with dispatch_reservation.DispatchGate.lock:
        with store.atomic():
            if attempt_recovery_record(manager, plan.run_attempt_id) is not None:
                return False
            try:
                before = _snapshot(manager, batch_id, plan, clock)
            except dispatch_reservation.DispatchReservationRejected:
                return False
        resource, attempt, lease, _, _, _ = before
        expected = {
            "runId": plan.run_id, "runAttemptId": plan.run_attempt_id,
            "gameId": plan.game_id, "accountId": plan.account_id,
            "desktopId": lease["desktop_id"], "controllerLeaseId": lease["controller_lease_id"],
            "controllerLeaseGeneration": lease["generation"], "memberBatchId": batch_id,
        }
        ctx = None
        receipt = None
        verified = False
        scope_validated = False
        scope_document = None
        reason = "unknown_owner"
        try:
            lookup = getattr(manager.adapter_host, "owned_execution_for", None)
            ctx = lookup(plan) if callable(lookup) else None
            if ctx is not None:
                scope = ctx.scope
                scope_document = _scope_document(scope)
                if any(scope_document[name] != value for name, value in expected.items()):
                    raise ValueError("Owned execution scope differs from frozen dispatch")
                if ctx.matches(scope) is not True:
                    raise ValueError("Owned execution does not match its scope")
                package_digest = attempt["result"].get("packageDigest")
                if (package_digest != scope_document["packageDigest"]
                        or attempt["result"].get("ownedInputScope") != scope_document):
                    raise ValueError("Owned execution scope or package differs from persisted attempt")
                scope_validated = True
                budget = load_queue_timeout_budget(store.get_config()["values"])
                receipt = ctx.stop_and_drain(budget.host_stop_grace_seconds)
                verified = (receipt is not None and not isinstance(receipt, (Mapping, bool, str, int, float, bytes))
                            and ctx.verify_receipt(receipt, scope) is True)
                reason = "owned_execution_drained" if verified else "drain_proof_rejected"
        except Exception as error:
            reason = f"drain_proof_failed:{type(error).__name__}"
        if scope_validated:
            try:
                scope_validated = (_scope_document(ctx.scope) == scope_document and ctx.matches(ctx.scope) is True)
            except Exception:
                scope_validated = False
            if not scope_validated:
                verified = False
                reason = "drain_scope_changed"
        with store.atomic():
            if attempt_recovery_record(manager, plan.run_attempt_id) is not None:
                return False
            try:
                current = _snapshot(manager, batch_id, plan, clock)
            except dispatch_reservation.DispatchReservationRejected:
                return False
            if current != before:
                return False
            evidence_refs = list(dict.fromkeys(
                reference for todo_id in current[3]["todo_instance_ids"]
                for reference in store.get_todo_instance(todo_id)["evidence_refs"]
            ))
            document = {**expected, "packageDigest": (scope_document or {}).get("packageDigest"),
                        "reasonCode": reason, "recoveredAt": clock().isoformat(),
                        "startedAt": plan.issued_at, "deadlineAt": resource["document"]["deadlineAt"],
                        "logScope": {"runId": plan.run_id, "runAttemptId": plan.run_attempt_id},
                        "evidenceRefs": evidence_refs,
                        "sceneEvidenceStatus": "recorded" if evidence_refs else "missing",
                        "proofVerified": verified, "businessCompleted": False,
                        "ownerRetained": True, "evidenceStatus": "verified_internal_receipt" if verified else "unproven",
                        "requiresPhysicalReconciliation": not verified}
            store.create_resource(RECOVERY_RESOURCE, resource_id=plan.run_attempt_id,
                                  state="expired" if verified else "blocking", document=document)
            prior_quarantine = None
            try:
                prior_quarantine = store.get_resource(QUARANTINE_RESOURCE, lease["desktop_id"])
            except RecordNotFound:
                pass
            if prior_quarantine is None:
                store.create_resource(QUARANTINE_RESOURCE, resource_id=lease["desktop_id"],
                                      state="quarantined", document=document)
            elif prior_quarantine["state"] == "released":
                store.update_resource(QUARANTINE_RESOURCE, lease["desktop_id"],
                                      state="quarantined", document=document)
            else:
                raise dispatch_reservation.DispatchReservationRejected("Desktop already has a quarantine owner")
            store.update_run_attempt(plan.run_attempt_id, state="review_required", completed=True,
                                     result={"code": "input_recovery_timeout", "businessCompleted": False,
                                             "inputRecovery": document})
            store.update_game_run(plan.run_id, state="review_required",
                                  message="Missing callback recovered without success; desktop "
                                          + ("drained." if verified else "quarantined with owner retained."))
            store.transition_controller_lease(lease["controller_lease_id"], state="revoked",
                                               reason_code="input_recovery_timeout",
                                               reason="Missing callback; recovery does not establish business success")
            store.update_batch_run_membership(batch_id, plan.run_id, state="terminal",
                                               latest_run_attempt_id=plan.run_attempt_id,
                                               terminal_outcome="review_required")
            store.append_event("input-recovery.committed", "run-attempt", plan.run_attempt_id, document)
        if scope_validated:
            pending = _pending_for(manager, create=True)
            pending[plan.run_attempt_id] = _CommittedRecovery(
                store=store, plan=plan, ctx=ctx, receipt=receipt if verified else None, scope=scope_document,
                lease=lease, dispatch=resource, document=document, attempt_document=document)
            if verified:
                retry_committed_recovery(manager, plan.run_attempt_id)
        return True
