from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import stat
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable

from ..domain.models import (
    AgentWorkItemCreateRequest,
    AgentWorkItemRecord,
    AutomationAssessmentRecord,
    AdapterEventRecord,
    AdapterDiagnosticCanaryRequest,
    AdapterInfoRecord,
    AdapterGovernanceRequest,
    BatchCreateRequest,
    BatchResumeRequest,
    BatchRunMembershipRecord,
    BatchRecord,
    Cadence,
    CapabilityDefinition,
    CapabilityInvocationCreateRequest,
    CapabilityInvocationRecord,
    ClaimDecisionCreateRequest,
    ClaimDecisionRecord,
    CompletionAdjudicationRecord,
    CompletionReviewRecord,
    CommandReceipt,
    ConfigPatchRequest,
    ConfigResponse,
    CZNProfileConfig,
    DiagnosticBundleCreateRequest,
    EntityState,
    EvidenceReviewRequest,
    EventPage,
    EventRecord,
    EvidenceArtifactRecord,
    GameRunCreateRequest,
    GameRunRecord,
    GameIntegrationPage,
    GameSummary,
    HealthCheck,
    HealthResponse,
    IncidentRecord,
    LogEntry,
    ManagerLifecycleRequest,
    MetaResponse,
    NTEProfileConfig,
    OKWWProfileConfig,
    NotificationAttemptRecord,
    NotificationDeliveryRecord,
    NotificationPolicyPatchRequest,
    NotificationPolicyRecord,
    NotificationPreviewResponse,
    NotificationRetryRequest,
    NotificationSendRequest,
    RepairSessionCreateRequest,
    RepairVerificationRequest,
    RequestMode,
    RunAttemptRecord,
    RunControlRequest,
    SnapshotResponse,
    TodoDefinitionRecord,
    TodoAttemptRecord,
    TodoInstanceRecord,
    TodoReconcileRequest,
    TodoResetPreviewItem,
    TodoResetPreviewResponse,
    TodoTransitionRequest,
    WorkItemKind,
    WorkItemClaimRecord,
    WorkItemClaimRequest,
    WeeklyTaskRecord,
    utc_now,
)
from ..domain.completion_contract import (
    AgentPredicateObservation,
    AgentReviewFact,
    AgentTodoReview,
    BlockerKind,
    CompletionBlockerFact,
    CompletionContractDecision,
    CompletionContractSnapshot,
    EvidenceArtifactFact,
    GameDayWindow,
    RunAttemptCompletionFact,
    TodoCompletionFact,
    TodoReviewVerdict,
)
from ..domain.execution_control import (
    ActionReceipt,
    Checkpoint,
    ControllerLease,
    ExecutionScope,
    FencingIdentity,
    FocusLease,
    LeaseState,
    Observation,
    TodoBlocker,
    TodoBlockerKind,
    TodoBlockerState,
    WindowBinding,
)
from ..domain.resume_reconcile import (
    ActionIdempotency,
    ActionReceiptOutcome,
    ActionReceiptResumeFact,
    CheckpointResumeFact,
    CheckpointStage,
    CurrentAttemptFact,
    GameRunResumeFact,
    HumanBlockerReleaseFact,
    ReconcileRequirement,
    ResumeAttemptState,
    ResumeBindingFact,
    ResumeBlockerKind,
    ResumeReconcileConflict,
    ResumeReconcileRequest,
    ResumeReconcileSnapshot,
    ResumeRisk,
    ResumeRunState,
    ResumeTodoState,
    TodoAttemptResumeFact,
    TodoAttemptState,
    TodoBlockerResumeFact,
    TodoResumeFact,
    plan_resume_reconciliation,
)
from ..domain.todos import todo_period
from ..settings import Settings
from ..store.sqlite_store import (
    PublicFencingMaterialRejected,
    RecordNotFound,
    SqliteStore,
)
from .adapter_host import AdapterPromotionRejected, ManagerAdapterHost
from .batch_planning import BatchPlanningDecision, classify_batch_todo_plans
from .batch_projection import project_batch_record
from .capability_catalog import build_capability_registry
from .manager_errors import ExecutionUnavailable, ManagerConflict, ManagerValidation
from .manager_todos import ManagerTodosService
from .adapter_protocol import (
    AdapterEvent,
    AdapterExecutionPlan,
    AdapterRunResult,
    AdapterTodoTarget,
    ExecutionPackageManifest,
)
from .adapter_artifacts import AdapterArtifactImporter
from .artifact_integrity import (
    ArtifactIntegrityResult,
    verify_artifact_entity,
)
from .legacy_adapter import LegacyAdapter
from .legacy_import import LegacyImportReport
from .integration_catalog import registration_for
from .notifications import (
    DpapiNotificationSecretProvider,
    NotificationDispatcher,
    SmtpNotificationTransport,
)
from .notifications.artifacts import _has_reparse_point, _same_file_snapshot
from .notifications.secrets import NotificationSecretProvider
from .notifications.transport import NotificationTransport
from .completion_contract import (
    COMPLETION_REVIEW_EVIDENCE_CONTENT_TYPES,
    CompletionPolicyRegistry,
    GameCompletionPolicy,
    adjudicate_completion,
    policy_for_frozen_game_day,
    policy_review_contract,
)
from .current_completion import (
    CurrentCompletionStatus,
    project_current_game_completion,
)
from .execution_control import (
    grant_controller_lease,
    raise_todo_blocker,
    transition_controller_lease,
)
from .todo_catalog import catalog as todo_catalog
from .todo_dispatch import HUMAN_TAKEOVER_RELEASED_REOBSERVE_REASON
from .todo_reset_scheduler import TodoResetScheduler
from .game_launcher import (
    GameCloseReceipt,
    GameLaunchCancelled,
    GameLaunchError,
    GameLaunchHumanRequired,
    GameLaunchReceipt,
    GameLaunchService,
    LaunchObservation,
)
from .emulator_binding import (
    EmulatorBindingError,
    EmulatorCloseReceipt,
    EmulatorLaunchReceipt,
    LDPlayerBinding,
    LDPlayerBindingService,
)
from .window_capture import WindowCaptureError, WindowsGameWindowCapture
from ..logging_setup import (
    AttemptLogSession,
    bind_log_context,
    get_logger,
    prune_run_logs,
)

_log = get_logger("manager")


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    content: bytes
    content_type: str
    file_name: str
    inline: bool


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


class ManagerService:
    VERSION = "0.3.3"
    # A pending Agent completion review may delay the batch seal (and the round
    # mail) for at most this long.  After that the machine adjudication seals
    # the batch; unreviewed runs stay review_required.
    COMPLETION_REVIEW_BARRIER_SECONDS = 15 * 60
    COMPLETION_REVIEW_WATCHDOG_POLL_SECONDS = 60.0
    COMPLETION_REVIEW_RECOVERY_BACKOFF_SECONDS = 30 * 60
    BATCH_CANCEL_MAX_DELIVERY_ATTEMPTS = 3
    BATCH_CANCEL_RETRY_DELAYS_SECONDS = (0.25, 0.75)
    PRE_ADAPTER_LAUNCH_STATES = frozenset(
        {"starting-fixed-host", "starting-game-client"}
    )

    def __init__(
        self,
        *,
        settings: Settings,
        store: SqliteStore,
        legacy_report: LegacyImportReport,
        adapter: LegacyAdapter,
        lifecycle_callback: Callable[[str], None] | None = None,
        notification_secret_provider: NotificationSecretProvider | None = None,
        notification_transport: NotificationTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.legacy_report = legacy_report
        self.adapter = adapter
        self._watchdog_lock = threading.Lock()
        # Serializes only the short launch -> Adapter Host ownership handoff.
        # A cancellation either becomes durable before Host dispatch, or waits
        # until the Host is registered and can receive its own fenced signal.
        self._execution_handoff_lock = threading.Lock()
        self._review_watchdog_stop = threading.Event()
        self._review_watchdog_thread: threading.Thread | None = None
        self._review_recovery_retry_after: dict[str, datetime] = {}
        self.adapter_host = ManagerAdapterHost(
            adapter,
            promotion_receipt_resolver=self._resolve_adapter_promotion_receipt,
        )
        self.manager_todos = ManagerTodosService(
            store=store,
            adapter_host=self.adapter_host,
            projection_history=self._projection_history,
            automation_assessment_record=self._automation_assessment_record,
        )
        self.adapter_artifacts = AdapterArtifactImporter(
            store=store,
            staging_root=adapter.runtime_dir / "artifact-inbox",
            artifact_root=settings.data_dir / "artifacts",
        )
        self.game_launcher = GameLaunchService()
        self.emulator_launcher = LDPlayerBindingService()
        self.window_capture = WindowsGameWindowCapture()
        self.lifecycle_callback = lifecycle_callback
        self.notification_secret_provider = (
            notification_secret_provider
            or DpapiNotificationSecretProvider(settings.notification_secrets_dir)
        )
        self.notification_dispatcher = NotificationDispatcher(
            store=store,
            artifact_root=settings.data_dir / "artifacts",
            secret_provider=self.notification_secret_provider,
            transport=notification_transport or SmtpNotificationTransport(),
        )
        self._shutdown_requested = threading.Event()
        self.todo_reset_scheduler = TodoResetScheduler(
            next_boundary=self._next_todo_reset_boundary,
            reconcile=self._reconcile_todo_reset_boundary,
            on_error=self._record_todo_reset_scheduler_error,
        )
        self.started_at = utc_now()
        # managerId fences one concrete process instance.  A restart must expose
        # a new value so clients cannot mistake the draining instance for the new
        # healthy Manager merely because the port is still answering.
        self.manager_id = str(uuid.uuid4())
        store.set_metadata("manager.id", self.manager_id)
        self.revoked_stale_controller_lease_count = (
            store.revoke_stale_controller_leases(self.manager_id)
        )
        # A new process is the only authority that can move lifecycle state
        # back to running.  Stop/restart requests set it to a terminal intent
        # inside the same single-writer transaction as their receipt.
        self.store.set_metadata("manager.lifecycle_state", "running")
        self.capabilities = self._capability_registry()
        known_game_ids = {
            str(game["game_id"]) for game in self.store.list_games()
        }
        catalog_definitions = todo_catalog()
        eligible_definitions = [
            definition
            for definition in catalog_definitions
            if definition["game_id"] in known_game_ids
        ]
        self.todo_catalog_sync = {
            **self.store.sync_todo_definitions(eligible_definitions),
            "unboundGameIds": sorted(
                {
                    str(definition["game_id"])
                    for definition in catalog_definitions
                    if definition["game_id"] not in known_game_ids
                }
            ),
        }
        # Initialization is a Manager lifecycle mutation, never a GET side
        # effect. Long-running processes use the explicit reconcile/reset API
        # when a new game period begins.
        startup_items = self._todo_instance_candidates()
        self.store.reconcile_todo_instances(
            startup_items,
            intent="startup",
            requested_by="manager",
            reason="manager-startup-current-period",
        )
        self.reconciled_pending_resume_intent_count = (
            self._reconcile_pending_resume_intents()
        )
        self.recovered_active_batch_count = self._recover_active_batches()
        self._attempt_logs: dict[str, AttemptLogSession] = {}
        self._attempt_logs_lock = threading.Lock()
        _log.info(
            "manager.started managerId=%s version=%s dataDir=%s legacyExecution=%s "
            "revokedLeases=%s reconciledResumeIntents=%s recoveredActiveBatches=%s",
            self.manager_id,
            self.VERSION,
            settings.data_dir,
            settings.legacy_execution_enabled,
            self.revoked_stale_controller_lease_count,
            self.reconciled_pending_resume_intent_count,
            self.recovered_active_batch_count,
        )
        self.store.append_event(
            "manager.started",
            "manager",
            self.manager_id,
            {
                "version": self.VERSION,
                "legacyExecutionEnabled": settings.legacy_execution_enabled,
                "revokedStaleControllerLeaseCount": (
                    self.revoked_stale_controller_lease_count
                ),
                "reconciledPendingResumeIntentCount": (
                    self.reconciled_pending_resume_intent_count
                ),
                "recoveredActiveBatchCount": self.recovered_active_batch_count,
            },
        )
        self._recover_completion_review_phases()

    def _resolve_adapter_promotion_receipt(
        self, resource_id: str
    ) -> dict[str, Any]:
        """Resolve one immutable receipt and its Manager-owned canary evidence."""

        resource = self.store.get_resource("adapter-promotion-receipt", resource_id)
        expected_canary_digest = str(
            resource["document"].get("managerCanaryEvidenceSha256", "")
        )
        canary_found = False
        linked_canary_id = self.store.get_metadata(
            f"adapter.promotion.canary.{resource_id}", None
        )
        candidates: list[dict[str, Any]] = []
        if isinstance(linked_canary_id, str):
            try:
                candidates.append(
                    self.store.get_resource(
                        "adapter-diagnostic-canary", linked_canary_id
                    )
                )
            except RecordNotFound:
                pass
        if not candidates:
            # Migration fallback for a receipt written before the direct link
            # metadata existed. New receipts take the constant-time path.
            candidates = self.store.list_resources(
                "adapter-diagnostic-canary", 100000
            )
        for canary in candidates:
            public = self._public_resource(canary)
            raw = json.dumps(
                public,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if "sha256:" + hashlib.sha256(raw).hexdigest() == expected_canary_digest:
                canary_found = (
                    canary["state"] == "passed"
                    and canary["document"].get("canaryStatus") == "passed"
                    and canary["document"].get("diagnosticOnly") is True
                    and canary["document"].get("adapterProcessStarted") is False
                    and canary["document"].get("gameProcessStarted") is False
                )
                if canary_found:
                    break
        if not canary_found:
            raise RecordNotFound(
                f"Manager canary evidence for promotion receipt {resource_id}"
            )
        return resource

    def _reconcile_pending_resume_intents(self) -> int:
        """Recover the post-intent/pre-dispatch crash window without game I/O."""

        recovered = 0
        for intent in self.store.list_resume_intents(
            state="successor_pending", limit=5000
        ):
            predecessor = self.store.get_run_attempt(
                intent["predecessor_run_attempt_id"]
            )
            successor = next(
                (
                    item
                    for item in self.store.list_run_attempts(
                        run_id=intent["run_id"], limit=5000
                    )
                    if int(item["attempt_ordinal"])
                    > int(predecessor["attempt_ordinal"])
                ),
                None,
            )
            run = self.store.get_game_run(intent["run_id"])
            continuation_batch = next(
                (
                    batch
                    for batch in self.store.list_batches(5000)
                    if batch.get("continuation_resume_intent_id")
                    == intent["resume_intent_id"]
                ),
                None,
            )
            batch_recovery_scope = (
                {
                    "schemaVersion": 1,
                    "batchId": continuation_batch["batch_id"],
                    "rootBatchId": continuation_batch["root_batch_id"],
                    "predecessorBatchId": continuation_batch[
                        "predecessor_batch_id"
                    ],
                    "continuationOrdinal": continuation_batch[
                        "continuation_ordinal"
                    ],
                    "memberRunIds": [run["run_id"]],
                    "affectedRunIds": [run["run_id"]],
                    "runAttemptIds": (
                        [successor["run_attempt_id"]]
                        if successor is not None
                        else []
                    ),
                    "cancelRequestId": None,
                }
                if continuation_batch is not None
                else None
            )
            work_item = self.store.create_work_item(
                {
                    "kind": WorkItemKind.OBSERVATION,
                    "state": EntityState.REVIEW_REQUIRED,
                    "game_id": run["game_id"],
                    "cadence": run["cadence"],
                    "run_id": run["run_id"],
                    "requested_by": "manager-startup",
                    "note": (
                        "resume successor intent survived restart before dispatch; "
                        "manual reconciliation is required"
                    ),
                    "artifact_refs": [],
                    "allowed_capability_refs": self._allowed_capability_refs(
                        WorkItemKind.OBSERVATION
                    ),
                    "result": {
                        "resumeIntentId": intent["resume_intent_id"],
                        "successorRunAttemptId": (
                            successor["run_attempt_id"]
                            if successor is not None
                            else None
                        ),
                        "executionRequested": False,
                        "requirements": [
                            "intent_dispatch_reconciliation",
                            "fresh_observation",
                        ],
                        **(
                            {"batchRecoveryScope": batch_recovery_scope}
                            if batch_recovery_scope is not None
                            else {}
                        ),
                    },
                }
            )
            self.store.update_resume_intent(
                intent["resume_intent_id"],
                state="reconciliation_required",
                work_item_id=work_item["work_item_id"],
                successor_run_attempt_id=(
                    successor["run_attempt_id"] if successor is not None else None
                ),
            )
            if run["state"] not in {"done", "cancelled"}:
                self.store.update_game_run(
                    run["run_id"],
                    state=EntityState.REVIEW_REQUIRED,
                    message=(
                        "resume intent recovered after restart; Host was not started"
                    ),
                )
            if continuation_batch is not None:
                membership = self.store.get_batch_run_membership(
                    continuation_batch["batch_id"], run["run_id"]
                )
                if membership["state"] not in {"terminal", "cancelled"}:
                    self.store.update_batch_run_membership(
                        continuation_batch["batch_id"],
                        run["run_id"],
                        state="reconciliation_required",
                        latest_run_attempt_id=(
                            successor["run_attempt_id"]
                            if successor is not None
                            else None
                        ),
                    )
                self.store.update_batch(
                    continuation_batch["batch_id"],
                    state=EntityState.REVIEW_REQUIRED,
                    result={
                        **dict(continuation_batch["result"]),
                        "recoveryPhase": {
                            "schemaVersion": 1,
                            "status": "awaiting_reconciliation",
                            "workItemId": work_item["work_item_id"],
                            "affectedRunIds": [run["run_id"]],
                            "recoveredByManagerId": self.manager_id,
                        },
                    },
                )
            recovered += 1
        return recovered

    @classmethod
    def _is_pre_adapter_launch_attempt(cls, attempt: dict[str, Any]) -> bool:
        result = attempt.get("result")
        launch_state = (
            str(result.get("launchState", ""))
            if isinstance(result, dict)
            else ""
        )
        return (
            attempt.get("process_id") is None
            and launch_state in cls.PRE_ADAPTER_LAUNCH_STATES
        )

    def _launch_cancel_requested(self, run_attempt_id: str) -> bool:
        """Read the persisted attempt state from the single Manager store."""

        try:
            attempt = self.store.get_run_attempt(run_attempt_id)
        except RecordNotFound:
            return True
        return str(attempt["state"]) not in {"starting", "running"}

    def _terminalize_interrupted_pre_adapter_launch(
        self,
        attempt: dict[str, Any],
        *,
        cancel_requested: bool,
    ) -> None:
        """Close the abandoned Manager wait while preserving its visible client."""

        run_attempt_id = str(attempt["run_attempt_id"])
        run_id = str(attempt["run_id"])
        terminal_state = "cancelled" if cancel_requested else "review_required"
        code = (
            "manager_restart_cancelled_before_adapter"
            if cancel_requested
            else "manager_restart_during_game_launch"
        )
        message = (
            "Durable cancellation was recovered before Adapter Host dispatch."
            if cancel_requested
            else "Manager restarted during the pre-Adapter game launch; inspect the preserved client before resuming."
        )
        self.store.update_run_attempt(
            run_attempt_id,
            state=terminal_state,
            result={
                "code": code,
                "message": message,
                **(
                    {"cancelDeliveryTarget": "manager-game-launch"}
                    if cancel_requested
                    else {}
                ),
                "gameCleanup": {
                    "state": (
                        "preserved-cooperative-cancel"
                        if cancel_requested
                        else "preserved-restart-reconciliation"
                    ),
                    "requestedProcessIds": [],
                    "remainingProcessIds": [],
                    "zombieProcessIds": [],
                },
            },
            completed=True,
        )
        self.store.update_game_run(
            run_id,
            state=(
                EntityState.CANCELLED
                if cancel_requested
                else EntityState.REVIEW_REQUIRED
            ),
            message=message,
        )
        self._end_controller_lease(
            run_attempt_id,
            state=LeaseState.REVOKED,
            reason_code=code,
            reason=message,
        )
        if cancel_requested:
            self._terminalize_unsealed_batch_memberships(
                run_id=run_id,
                run_attempt_id=run_attempt_id,
                outcome="cancelled",
            )
        _log.warning(
            "manager.recovery.pre_adapter_launch attempt=%s state=%s code=%s",
            run_attempt_id,
            terminal_state,
            code,
        )

    def _recover_active_batches(self) -> int:
        """Fence interrupted batches and replay any durable cancellation intent."""

        recovered = 0
        active_states = {
            str(EntityState.PENDING_EXECUTION),
            str(EntityState.QUEUED),
            str(EntityState.RUNNING),
            str(EntityState.CANCELLING),
        }
        work_items = self.store.list_work_items(5000)
        for batch in self.store.list_batches(5000):
            result = dict(batch.get("result", {}))
            if (
                result.get("sealVersion") is not None
                or str(batch["state"]) not in active_states
            ):
                continue
            batch_id = str(batch["batch_id"])
            _log.warning(
                "manager.recovery.active_batch batchId=%s state=%s "
                "(Manager restarted during an active batch; memberships move to reconciliation_required)",
                batch_id,
                batch["state"],
            )
            memberships = self.store.list_batch_run_memberships(
                batch_id=batch_id, limit=5000
            )
            cancel_request = self.store.get_active_batch_cancel_request(batch_id)
            recovered_launch_attempt_ids: list[str] = []
            for membership in memberships:
                attempts = self.store.list_run_attempts(
                    run_id=str(membership["run_id"]), limit=1
                )
                if (
                    not attempts
                    or attempts[0]["state"]
                    not in {"starting", "running", "cancelling"}
                    or not self._is_pre_adapter_launch_attempt(attempts[0])
                ):
                    continue
                self._terminalize_interrupted_pre_adapter_launch(
                    attempts[0], cancel_requested=cancel_request is not None
                )
                recovered_launch_attempt_ids.append(
                    str(attempts[0]["run_attempt_id"])
                )
            if (
                cancel_request is not None
                and recovered_launch_attempt_ids
                and not self._active_run_attempts_for_batch(batch_id)
            ):
                sealed = self._seal_cancelled_batch(
                    batch_id, str(cancel_request["cancel_request_id"])
                )
                if sealed is not None:
                    self._complete_command(
                        str(cancel_request["cancel_request_id"]),
                        "succeeded",
                        "durable pre-Adapter launch cancellation was recovered and sealed",
                    )
                    self._complete_command(
                        batch_id,
                        "failed",
                        "batch cancellation was recovered before Adapter Host dispatch",
                    )
                    recovered += 1
                    continue
            durable_cancel_attempt_ids: list[str] = []
            if cancel_request is not None:
                for membership in memberships:
                    run_id = str(membership["run_id"])
                    attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
                    if not attempts or attempts[0]["state"] not in {
                        "starting",
                        "running",
                        "cancelling",
                    }:
                        continue
                    attempt = attempts[0]
                    try:
                        delivered = self.adapter_host.cancel_persisted_attempt(
                            run_id=run_id,
                            run_attempt_id=str(attempt["run_attempt_id"]),
                            cancel_authority_hash=str(
                                attempt.get("cancel_authority_hash", "")
                            ),
                            reason_code="manager_restart_cancel_recovery",
                        )
                    except Exception as error:
                        delivered = False
                        _log.warning(
                            "manager.recovery.durable_cancel_failed batchId=%s "
                            "runAttemptId=%s errorClass=%s",
                            batch_id,
                            attempt["run_attempt_id"],
                            type(error).__name__,
                        )
                    if delivered:
                        durable_cancel_attempt_ids.append(
                            str(attempt["run_attempt_id"])
                        )
            attempt_ids: list[str] = []
            affected_run_ids: list[str] = []
            with self.store.atomic():
                for membership in memberships:
                    run_id = str(membership["run_id"])
                    attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
                    if attempts:
                        attempt_ids.append(str(attempts[0]["run_attempt_id"]))
                    if membership["state"] in {
                        "active",
                        "resume_pending",
                        "reconciliation_required",
                    }:
                        affected_run_ids.append(run_id)
                        self.store.update_batch_run_membership(
                            batch_id,
                            run_id,
                            state="reconciliation_required",
                            latest_run_attempt_id=(
                                str(attempts[0]["run_attempt_id"])
                                if attempts
                                else None
                            ),
                            terminal_outcome=(
                                str(attempts[0]["state"])
                                if attempts
                                and attempts[0]["state"]
                                not in {"starting", "running", "cancelling"}
                                else None
                            ),
                        )
                existing_work_item = next(
                    (
                        item
                        for item in work_items
                        if item.get("result", {})
                        .get("batchRecoveryScope", {})
                        .get("batchId")
                        == batch_id
                        and item.get("state")
                        in {
                            EntityState.PLANNED,
                            EntityState.RUNNING,
                            EntityState.REVIEW_REQUIRED,
                        }
                    ),
                    None,
                )
                if existing_work_item is None:
                    scope = {
                        "schemaVersion": 1,
                        "batchId": batch_id,
                        "rootBatchId": batch["root_batch_id"],
                        "predecessorBatchId": batch["predecessor_batch_id"],
                        "continuationOrdinal": batch["continuation_ordinal"],
                        "memberRunIds": [
                            str(item["run_id"]) for item in memberships
                        ],
                        "affectedRunIds": affected_run_ids,
                        "runAttemptIds": list(dict.fromkeys(attempt_ids)),
                        "durableCancelRequestedRunAttemptIds": list(
                            dict.fromkeys(durable_cancel_attempt_ids)
                        ),
                        "cancelRequestId": (
                            cancel_request["cancel_request_id"]
                            if cancel_request is not None
                            else None
                        ),
                    }
                    existing_work_item = self.store.create_work_item(
                        {
                            "kind": WorkItemKind.OBSERVATION,
                            "state": EntityState.REVIEW_REQUIRED,
                            "game_id": (
                                str(batch["game_ids"][0])
                                if len(batch["game_ids"]) == 1
                                else None
                            ),
                            "cadence": batch["cadence"],
                            "run_id": (
                                affected_run_ids[0]
                                if len(affected_run_ids) == 1
                                else None
                            ),
                            "requested_by": "manager-startup",
                            "note": (
                                "Manager restarted during an active Batch; reconcile "
                                "durable attempts before any new Host execution."
                            ),
                            "artifact_refs": [],
                            "allowed_capability_refs": self._allowed_capability_refs(
                                WorkItemKind.OBSERVATION
                            ),
                            "result": {
                                "batchRecoveryScope": scope,
                                "executionRequested": False,
                                "requirements": [
                                    "attempt_outcome_reconciliation",
                                    "fresh_observation",
                                    *(
                                        ["cooperative_cancel_acknowledgement"]
                                        if cancel_request is not None
                                        else []
                                    ),
                                ],
                            },
                        }
                    )
                    work_items.append(existing_work_item)
                if cancel_request is not None:
                    self.store.update_batch_cancel_request(
                        cancel_request["cancel_request_id"],
                        state="reconciliation_required",
                        active_run_attempt_ids=list(dict.fromkeys(attempt_ids)),
                        work_item_id=existing_work_item["work_item_id"],
                    )
                self.store.update_batch(
                    batch_id,
                    state=EntityState.REVIEW_REQUIRED,
                    result={
                        **result,
                        "currentGameId": None,
                        "recoveryPhase": {
                            "schemaVersion": 1,
                            "status": "awaiting_reconciliation",
                            "workItemId": existing_work_item["work_item_id"],
                            "affectedRunIds": affected_run_ids,
                            "recoveredByManagerId": self.manager_id,
                        },
                    },
                )
            recovered += 1
        return recovered

    def _fence_batch_for_reconciliation(
        self,
        *,
        batch_id: str,
        affected_run_ids: list[str],
        run_attempt_ids: list[str],
        requested_by: str,
        note: str,
        requirements: list[str],
    ) -> dict[str, Any] | None:
        """Persist one fail-closed barrier when a fixed Host outcome is unknown."""

        batch = self.store.get_batch(batch_id)
        if batch["result"].get("sealVersion") is not None:
            return None
        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        membership_by_run = {
            str(item["run_id"]): item for item in memberships
        }
        affected = list(
            dict.fromkeys(
                run_id
                for run_id in (str(value) for value in affected_run_ids)
                if run_id in membership_by_run
            )
        )
        attempt_ids = list(dict.fromkeys(str(value) for value in run_attempt_ids))
        if not affected:
            raise ManagerConflict("batch reconciliation has no affected member GameRun")
        cancel_request = self.store.get_active_batch_cancel_request(batch_id)
        existing_work_item = next(
            (
                item
                for item in self.store.list_work_items(5000)
                if item.get("result", {})
                .get("batchRecoveryScope", {})
                .get("batchId")
                == batch_id
                and item.get("state")
                in {
                    EntityState.PLANNED,
                    EntityState.RUNNING,
                    EntityState.REVIEW_REQUIRED,
                }
            ),
            None,
        )
        scope = {
            "schemaVersion": 1,
            "batchId": batch_id,
            "rootBatchId": batch["root_batch_id"],
            "predecessorBatchId": batch["predecessor_batch_id"],
            "continuationOrdinal": batch["continuation_ordinal"],
            "memberRunIds": [str(item["run_id"]) for item in memberships],
            "affectedRunIds": affected,
            "runAttemptIds": attempt_ids,
            "cancelRequestId": (
                cancel_request["cancel_request_id"]
                if cancel_request is not None
                else None
            ),
        }
        with self.store.atomic():
            for run_id in affected:
                membership = self.store.get_batch_run_membership(batch_id, run_id)
                if membership["state"] in {
                    "queued",
                    "resume_pending",
                    "active",
                    "reconciliation_required",
                }:
                    latest_attempt = next(
                        (
                            attempt_id
                            for attempt_id in reversed(attempt_ids)
                            if self.store.get_run_attempt(attempt_id)["run_id"]
                            == run_id
                        ),
                        membership.get("latest_run_attempt_id"),
                    )
                    self.store.update_batch_run_membership(
                        batch_id,
                        run_id,
                        state="reconciliation_required",
                        latest_run_attempt_id=latest_attempt,
                    )
                run = self.store.get_game_run(run_id)
                if run["state"] not in {
                    EntityState.DONE,
                    EntityState.CANCELLED,
                }:
                    self.store.update_game_run(
                        run_id,
                        state=EntityState.REVIEW_REQUIRED,
                        message=(
                            "fixed Host outcome is unknown; fresh reconciliation is "
                            "required before any successor execution"
                        ),
                    )
            if existing_work_item is None:
                existing_work_item = self.store.create_work_item(
                    {
                        "kind": WorkItemKind.OBSERVATION,
                        "state": EntityState.REVIEW_REQUIRED,
                        "game_id": (
                            str(batch["game_ids"][0])
                            if len(batch["game_ids"]) == 1
                            else None
                        ),
                        "cadence": batch["cadence"],
                        "run_id": affected[0] if len(affected) == 1 else None,
                        "requested_by": requested_by,
                        "note": note,
                        "artifact_refs": [],
                        "allowed_capability_refs": self._allowed_capability_refs(
                            WorkItemKind.OBSERVATION
                        ),
                        "result": {
                            "batchRecoveryScope": scope,
                            "executionRequested": False,
                            "requirements": list(dict.fromkeys(requirements)),
                        },
                    }
                )
            if cancel_request is not None and cancel_request["state"] in {
                "requested",
                "signal_delivered",
                "reconciliation_required",
            }:
                self.store.update_batch_cancel_request(
                    cancel_request["cancel_request_id"],
                    state="reconciliation_required",
                    active_run_attempt_ids=attempt_ids,
                    work_item_id=existing_work_item["work_item_id"],
                )
            current = self.store.get_batch(batch_id)
            if current["result"].get("sealVersion") is None:
                self.store.update_batch(
                    batch_id,
                    state=EntityState.REVIEW_REQUIRED,
                    result={
                        **dict(current["result"]),
                        "currentGameId": None,
                        "recoveryPhase": {
                            "schemaVersion": 1,
                            "status": "awaiting_reconciliation",
                            "workItemId": existing_work_item["work_item_id"],
                            "affectedRunIds": affected,
                            "runAttemptIds": attempt_ids,
                            "recoveredByManagerId": self.manager_id,
                        },
                    },
                )
        return existing_work_item

    def _batch_run_outcome_sets(
        self, memberships: list[dict[str, Any]]
    ) -> dict[str, list[str]]:
        """Separate reviewable attempts from candidates that never started."""

        all_run_ids: list[str] = []
        attempted_run_ids: list[str] = []
        completed_run_ids: list[str] = []
        failed_run_ids: list[str] = []
        not_started_run_ids: list[str] = []
        for membership in memberships:
            run_id = str(membership["run_id"])
            all_run_ids.append(run_id)
            attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
            if not attempts:
                not_started_run_ids.append(run_id)
                continue
            attempted_run_ids.append(run_id)
            if membership.get("terminal_outcome") == "completed":
                completed_run_ids.append(run_id)
            else:
                failed_run_ids.append(run_id)
        return {
            "allRunIds": all_run_ids,
            "attemptedRunIds": attempted_run_ids,
            "completedRunIds": completed_run_ids,
            "failedRunIds": failed_run_ids,
            "notStartedRunIds": not_started_run_ids,
        }

    def _finish_resumed_batch_attempt(
        self,
        *,
        batch_id: str,
        run_id: str,
        transport_timed_out: bool = False,
    ) -> None:
        """Advance a committed resume target only after its successor is terminal."""

        batch = self.store.get_batch(batch_id)
        if batch["result"].get("sealVersion") is not None:
            return
        membership = self.store.get_batch_run_membership(batch_id, run_id)
        if membership["state"] not in {"terminal", "cancelled"}:
            attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
            if attempts and attempts[0]["state"] not in {
                "starting",
                "running",
                "cancelling",
            }:
                membership = self.store.update_batch_run_membership(
                    batch_id,
                    run_id,
                    state="terminal",
                    latest_run_attempt_id=str(attempts[0]["run_attempt_id"]),
                    terminal_outcome=str(attempts[0]["state"]),
                )
            else:
                attempt_ids = (
                    [str(attempts[0]["run_attempt_id"])] if attempts else []
                )
                self._fence_batch_for_reconciliation(
                    batch_id=batch_id,
                    affected_run_ids=[run_id],
                    run_attempt_ids=attempt_ids,
                    requested_by="manager-resume-callback",
                    note=(
                        "A same-GameRun successor callback did not produce a terminal "
                        "attempt fact; reconcile the fixed Host before continuing."
                    ),
                    requirements=[
                        "attempt_outcome_reconciliation",
                        "fresh_observation",
                    ],
                )
                if membership.get("resume_intent_id"):
                    intent = self.store.get_resume_intent(
                        str(membership["resume_intent_id"])
                    )
                    if intent["state"] not in {
                        "successor_failed",
                        "successor_terminal",
                        "reconciliation_required",
                    }:
                        self.store.update_resume_intent(
                            intent["resume_intent_id"],
                            state="reconciliation_required",
                            successor_run_attempt_id=(attempt_ids[0] if attempt_ids else None),
                        )
                return
        if membership.get("resume_intent_id"):
            intent = self.store.get_resume_intent(
                str(membership["resume_intent_id"])
            )
            if intent["state"] not in {"successor_failed", "successor_terminal"}:
                self.store.update_resume_intent(
                    intent["resume_intent_id"],
                    state="successor_terminal",
                    successor_run_attempt_id=membership.get(
                        "latest_run_attempt_id"
                    ),
                )
        cancel_request = self.store.get_active_batch_cancel_request(batch_id)
        if cancel_request is not None:
            sealed = self._seal_cancelled_batch(
                batch_id, cancel_request["cancel_request_id"]
            )
            if sealed is not None:
                self._complete_command(
                    cancel_request["cancel_request_id"],
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
                self._complete_command(
                    batch_id,
                    "failed",
                    "batch cancellation reached an immutable safe seal",
                )
            return
        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        incomplete = [
            item
            for item in memberships
            if item["state"] not in {"terminal", "cancelled"}
        ]
        if self.store.get_game_run(run_id)["state"] == EntityState.HUMAN_REQUIRED:
            self.store.update_batch(
                batch_id,
                state=EntityState.HUMAN_REQUIRED,
                result={
                    **dict(batch["result"]),
                    "currentGameId": self.store.get_game_run(run_id)["game_id"],
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "ready_for_resume",
                        "humanRequiredRunId": run_id,
                        "affectedRunIds": list(dict.fromkeys([
                            run_id,
                            *(str(item["run_id"]) for item in incomplete),
                        ])),
                    },
                },
            )
            return
        if incomplete:
            self.store.update_batch(
                batch_id,
                state=EntityState.REVIEW_REQUIRED,
                result={
                    **dict(batch["result"]),
                    "currentGameId": None,
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "ready_for_resume",
                        "affectedRunIds": [
                            str(item["run_id"]) for item in incomplete
                        ],
                    },
                },
            )
            return
        outcomes = self._batch_run_outcome_sets(memberships)
        final_run_ids = outcomes["attemptedRunIds"]
        completed_run_ids = outcomes["completedRunIds"]
        failed_run_ids = outcomes["failedRunIds"]
        initial_result = dict(batch["result"])
        initial_result.pop("completionReviewPhase", None)
        initial_result.update(
            {
                "allCandidateRunIds": outcomes["allRunIds"],
                "notStartedRunIds": outcomes["notStartedRunIds"],
            }
        )
        self._begin_completion_review_phase(
            batch_id=batch_id,
            initial_result=initial_result,
            game_ids=[
                str(value)
                for value in initial_result.get(
                    "candidateGameIds", batch["game_ids"]
                )
            ],
            cadence=str(batch["cadence"]),
            state=(
                EntityState.BLOCKED
                if failed_run_ids or outcomes["notStartedRunIds"]
                else EntityState.REVIEW_REQUIRED
            ),
            completed_run_ids=completed_run_ids,
            failed_run_ids=failed_run_ids,
            final_run_ids=final_run_ids,
            reason=(
                "Continuation successor ended with unresolved or failed Todo facts."
                if failed_run_ids or outcomes["notStartedRunIds"]
                else "Continuation successor ended; current evidence review is required."
            ),
            timed_out=transport_timed_out,
        )

    def _resolve_batch_recovery_decision(
        self, scope: dict[str, Any], decision: str
    ) -> None:
        """Advance recovery facts without treating a review as action authority."""

        batch_id = str(scope["batchId"])
        batch = self.store.get_batch(batch_id)
        if batch["result"].get("sealVersion") is not None:
            return
        cancel_request = self.store.get_active_batch_cancel_request(batch_id)
        if decision != "accepted":
            self.store.update_batch(
                batch_id,
                state=EntityState.REVIEW_REQUIRED,
                result={
                    **dict(batch["result"]),
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "reconciliation_required",
                        "workItemId": batch["result"]
                        .get("recoveryPhase", {})
                        .get("workItemId"),
                        "affectedRunIds": list(scope.get("affectedRunIds", [])),
                    },
                },
            )
            return

        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        with self.store.atomic():
            for membership in memberships:
                if membership["state"] != "reconciliation_required":
                    continue
                attempts = self.store.list_run_attempts(
                    run_id=str(membership["run_id"]), limit=1
                )
                if not attempts:
                    # The crash happened before a Host attempt was durably
                    # allocated.  This member may be resumed after review.
                    self.store.update_batch_run_membership(
                        batch_id,
                        str(membership["run_id"]),
                        state="resume_pending",
                    )
                    continue
                if attempts[0]["state"] in {
                    "starting",
                    "running",
                    "cancelling",
                }:
                    # A human review cannot manufacture a terminal Host fact.
                    # Keep the fencing barrier until a later reconciliation.
                    continue
                self.store.update_batch_run_membership(
                    batch_id,
                    str(membership["run_id"]),
                    state="terminal",
                    latest_run_attempt_id=str(attempts[0]["run_attempt_id"]),
                    terminal_outcome=str(attempts[0]["state"]),
                )
        if cancel_request is not None:
            sealed = self._seal_cancelled_batch(
                batch_id, cancel_request["cancel_request_id"]
            )
            if sealed is None:
                self.store.update_batch_cancel_request(
                    cancel_request["cancel_request_id"],
                    state="reconciliation_required",
                )
            else:
                self._complete_command(
                    cancel_request["cancel_request_id"],
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
                self._complete_command(
                    batch_id,
                    "failed",
                    "batch cancellation reached an immutable safe seal",
                )
            return

        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        unreconciled = [
            item
            for item in memberships
            if item["state"] == "reconciliation_required"
        ]
        if unreconciled:
            self.store.update_batch(
                batch_id,
                state=EntityState.REVIEW_REQUIRED,
                result={
                    **dict(batch["result"]),
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "reconciliation_required",
                        "affectedRunIds": [
                            str(item["run_id"]) for item in unreconciled
                        ],
                    },
                },
            )
            return
        if memberships and all(
            item["state"] in {"terminal", "cancelled"} for item in memberships
        ):
            outcomes = self._batch_run_outcome_sets(memberships)
            final_run_ids = outcomes["attemptedRunIds"]
            completed = outcomes["completedRunIds"]
            failed = outcomes["failedRunIds"]
            initial_result = dict(batch["result"])
            initial_result.pop("completionReviewPhase", None)
            initial_result.update(
                {
                    "allCandidateRunIds": outcomes["allRunIds"],
                    "notStartedRunIds": outcomes["notStartedRunIds"],
                }
            )
            self._begin_completion_review_phase(
                batch_id=batch_id,
                initial_result=initial_result,
                game_ids=[
                    str(value)
                    for value in initial_result.get(
                        "candidateGameIds", batch["game_ids"]
                    )
                ],
                cadence=str(batch["cadence"]),
                state=(
                    EntityState.BLOCKED
                    if failed or outcomes["notStartedRunIds"]
                    else EntityState.REVIEW_REQUIRED
                ),
                completed_run_ids=completed,
                failed_run_ids=failed,
                final_run_ids=final_run_ids,
                reason=(
                    "Recovered attempts were reconciled; completion review remains required."
                ),
                timed_out=False,
            )
            return
        self.store.update_batch(
            batch_id,
            state=EntityState.REVIEW_REQUIRED,
            result={
                **dict(batch["result"]),
                "recoveryPhase": {
                    "schemaVersion": 1,
                    "status": "ready_for_resume",
                    "affectedRunIds": [
                        str(item["run_id"])
                        for item in memberships
                        if item["state"] not in {"terminal", "cancelled"}
                    ],
                },
            },
        )

    def start_notification_worker(self) -> None:
        self.notification_dispatcher.start()

    def stop_notification_worker(self) -> None:
        self.notification_dispatcher.stop()

    def start_todo_reset_scheduler(self) -> None:
        self.todo_reset_scheduler.start()

    def stop_todo_reset_scheduler(self) -> None:
        self.todo_reset_scheduler.stop()

    def dispatch_notifications_once(self) -> int:
        """Deterministic test/maintenance entry; never exposed as an Agent capability."""

        return self.notification_dispatcher.run_once()

    def _capability_registry(self) -> dict[str, CapabilityDefinition]:
        enabled_game_ids = [
            game.game_id for game in self.list_games() if game.enabled
        ]
        execution_ready = bool(
            self._execution_readiness_for_games(enabled_game_ids)["readyGameIds"]
        )
        return build_capability_registry(execution_ready)

    def meta(self) -> MetaResponse:
        return MetaResponse(
            version=self.VERSION,
            manager_id=self.manager_id,
            started_at=self.started_at,
            web_gui_available=(self.settings.web_dist / "index.html").is_file(),
            legacy_execution_enabled=self.settings.legacy_execution_enabled,
            openapi_sha256=str(
                self.store.get_metadata("manager.openapi_sha256", "")
            ),
            web_asset_sha256=str(
                self.store.get_metadata("manager.web_asset_sha256", "")
            ),
        )

    def health(self) -> HealthResponse:
        database_check = self.store.quick_check()
        database_ok = database_check == "ok"
        legacy_status = self.legacy_report.status
        status = "ok" if database_ok and legacy_status != "error" else "degraded"
        return HealthResponse(
            status=status,
            manager="healthy",
            storage="healthy" if database_ok else "error",
            event_stream="healthy",
            checked_at=utc_now(),
            checks={
                "database": HealthCheck(
                    status="ok" if database_ok else "error", detail=database_check
                ),
                "legacyImport": HealthCheck(
                    status=("ok" if legacy_status == "ok" else "degraded"),
                    detail=self.legacy_report.detail,
                ),
                "legacyExecution": HealthCheck(
                    status="ok",
                    detail=(
                        "explicitly enabled"
                        if self.settings.legacy_execution_enabled
                        else "disabled by default"
                    ),
                ),
            },
        )

    def snapshot(self) -> SnapshotResponse:
        data = self.store.snapshot_data()
        recent_runs, sealed_batches = self._projection_history()
        games = [
            self._game_projection(
                game,
                recent_runs=recent_runs,
                sealed_batches=sealed_batches,
            )
            for game in data["games"]
        ]
        batch_execution_readiness = self._batch_action_execution_readiness()
        # The home page refreshes this projection frequently.  A complete batch
        # result can contain frozen Todo/evidence contracts for many runs; those
        # belong to ``GET /batches/{batch_id}``, which Queue already loads when a
        # user selects a batch.  Keep the shared snapshot to the fields needed
        # for status/preflight and action affordances.
        batches = [
            self._batch_snapshot_summary(
                self._batch_record(
                    item, execution_readiness=batch_execution_readiness
                )
            )
            for item in data["batches"]
        ]
        current_human_batch_ids = {
            str(item["batch_id"]) for item in data["batches"]
            if self._current_human_batch(item)
        }
        active = next(
            (
                _dump(batch)
                for batch in batches
                if batch.state
                in {"queued", "running", "pending_execution", "cancelling"}
                or batch.batch_id in current_human_batch_ids
            ),
            None,
        )
        state_version = int(data["latest_event_sequence"])
        counters = {
            "enabled": sum(1 for game in games if game.enabled),
            "running": sum(
                1
                for game in games
                if game.runtime_state in {"running", "executing", "verifying"}
            ),
            "review": sum(1 for game in games if game.review_state != "none"),
            "accepted": sum(
                1 for game in games if game.acceptance_state == "accepted_done"
            ),
        }
        todo = self.todo_overview()
        counters.update(
            {
                "todoRequired": int(todo["requiredTotal"]),
                "todoCompleted": int(todo["requiredCompleted"]),
                "todoRemaining": int(todo["requiredRemaining"]),
                "todoBlocked": int(todo["blocked"]),
                "todoReviewRequired": int(todo["reviewRequired"]),
                "todoHumanRequired": int(todo["humanRequired"]),
            }
        )
        return SnapshotResponse(
            state_version=state_version,
            generated_at=utc_now(),
            event_cursor=str(state_version),
            manager={**_dump(self.meta()), "mode": "manager"},
            health=_dump(self.health()),
            game_day=str(todo["scopeKey"]),
            active_batch=active,
            recent_batches=batches,
            games=games,
            counters=counters,
            todo=todo,
            execution_control=self.store.execution_control_summary(),
        )

    # Snapshot/game projections only need the runs and sealed batches that can
    # still describe a *current* Todo period (daily or weekly).  Reading the
    # whole history decoded every historical batch result on each refresh.
    PROJECTION_HISTORY_DAYS = 8
    PROJECTION_HISTORY_LIMIT = 2000

    def _projection_history(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Every durable change appends a ledger event, so the state version is
        # an exact cache key for this read-only projection input.
        version = self.store.latest_event_sequence()
        cached = getattr(self, "_projection_history_cache", None)
        if cached is not None and cached[0] == version:
            return cached[1], cached[2]
        created_after = (
            utc_now() - timedelta(days=self.PROJECTION_HISTORY_DAYS)
        ).isoformat()
        recent_runs = self.store.list_game_runs(
            self.PROJECTION_HISTORY_LIMIT, created_after=created_after
        )
        sealed_batches = self.store.list_batches(
            self.PROJECTION_HISTORY_LIMIT, created_after=created_after
        )
        self._projection_history_cache = (version, recent_runs, sealed_batches)
        return recent_runs, sealed_batches

    @staticmethod
    def _batch_snapshot_summary(batch: BatchRecord) -> BatchRecord:
        """Strip batch-detail-only payloads from the frequently refreshed view."""

        result_keys = {
            "candidateGameIds",
            "executableGameIds",
            "deferredGameIds",
            "skippedCompletedGameIds",
            "gameDay",
            "batchLineage",
            "batchActionAvailability",
            "currentGameId",
            "acceptedDone",
            "acceptanceReason",
            "sealVersion",
        }
        return batch.model_copy(
            update={
                "result": {
                    key: value
                    for key, value in batch.result.items()
                    if key in result_keys
                }
            }
        )

    def _current_human_batch(self, batch: dict[str, Any]) -> bool:
        """A paused batch owns Today only while its frozen member Todos are current."""
        if (
            batch.get("state") != EntityState.HUMAN_REQUIRED
            or batch.get("mode") != RequestMode.EXECUTE
            or batch.get("result", {}).get("sealVersion") is not None
        ):
            return False
        memberships = batch.get("run_memberships", [])
        if not memberships:
            return False
        now = utc_now()
        for membership in memberships:
            run = self.store.get_game_run(str(membership["run_id"]))
            if not run["todo_instance_ids"]:
                return False
            for todo_id in run["todo_instance_ids"]:
                todo = self.store.get_todo_instance(todo_id)
                if not (
                    todo["game_id"] == run["game_id"]
                    and todo["cadence"] == batch["cadence"]
                    and self._contract_datetime(todo["period_starts_at"]) <= now
                    < self._contract_datetime(todo["period_ends_at"])
                ):
                    return False
        return True

    def state_version_snapshot(self) -> SnapshotResponse:
        """Return only the CAS/liveness projection needed by scoped actors."""

        state_version = self.store.latest_event_sequence()
        health = self.health()
        return SnapshotResponse(
            state_version=state_version,
            generated_at=utc_now(),
            event_cursor=str(state_version),
            manager={"scope": "state-version-only"},
            health={"status": health.status},
            game_day="restricted",
            active_batch=None,
            recent_batches=[],
            games=[],
            counters={},
            todo={"scope": "restricted"},
            execution_control={"scope": "restricted"},
        )

    def _game_projection(
        self,
        record: dict[str, Any],
        *,
        recent_runs: list[dict[str, Any]] | None = None,
        sealed_batches: list[dict[str, Any]] | None = None,
    ) -> GameSummary:
        current_todos = self.list_todo_instances(
            game_id=str(record["game_id"]),
            cadence="daily",
            current=True,
            limit=1000,
        )
        current_todo_ids = {item.todo_instance_id for item in current_todos}
        active_run_states = {
            str(EntityState.PENDING_EXECUTION),
            str(EntityState.QUEUED),
            str(EntityState.RUNNING),
            str(EntityState.CANCELLING),
        }
        latest_run = next(
            (
                run
                for run in (
                    recent_runs
                    if recent_runs is not None
                    else self.store.list_game_runs(5000)
                )
                if run["game_id"] == record["game_id"]
                and str(run["cadence"]) == "daily"
                and (
                    str(run["state"]) in active_run_states
                    or bool(
                        current_todo_ids.intersection(
                            {
                                *run.get("todo_instance_ids", []),
                                *run.get("completed_todo_instance_ids", []),
                            }
                        )
                    )
                )
            ),
            None,
        )
        state = str(latest_run["state"]) if latest_run is not None else "unknown"
        launch_human_gate = False
        if state in {"human_required", "review_required"} and latest_run is not None:
            attempts = self.store.list_run_attempts(run_id=latest_run["run_id"], limit=1)
            launch_human_gate = bool(
                attempts and attempts[0]["result"].get("launchState") == "launch-human-required"
            )
        runtime_state = "planned" if state == "unknown" else state
        todo_summary = self._todo_summary_from_items(current_todos, "daily")
        completion_scope_items = self._selected_todo_scope_items(
            game_id=str(record["game_id"]),
            cadence="daily",
            all_items=current_todos,
        )
        completion_scope = self._todo_summary_from_items(
            completion_scope_items, "daily"
        )
        current_completion = None
        if completion_scope.get("periodKeys"):
            current_completion = project_current_game_completion(
                game_id=str(record["game_id"]),
                current_scope=completion_scope,
                sealed_batches=(
                    sealed_batches
                    if sealed_batches is not None
                    else self.store.list_batches(5000)
                ),
                invalidated_at=self.store.latest_todo_reset_at(current_todo_ids),
            )
        if (
            current_completion is not None
            and current_completion.status == CurrentCompletionStatus.ACCEPTED_DONE
        ):
            acceptance = "accepted_done"
        elif (
            current_completion is not None
            and current_completion.status == CurrentCompletionStatus.EVIDENCE_PENDING
        ):
            acceptance = "evidence_pending"
        elif launch_human_gate:
            acceptance = "not_started"
        elif state in {"running", "executing", "verifying"}:
            acceptance = "in_progress"
        elif state in {"review_required", "human_required", "blocked"}:
            acceptance = "evidence_pending"
        else:
            acceptance = "not_started"
        if acceptance == "accepted_done":
            runtime_state = "completed"
            review = "none"
        elif state == "human_required":
            review = "human_required"
        elif (
            state in {"review_required", "blocked"}
            or acceptance == "evidence_pending"
        ):
            review = "approval_required"
        else:
            review = "none"
        return GameSummary(
            game_id=record["game_id"],
            display_name=record["display_name"],
            order_index=record["order_index"],
            enabled=record["enabled"],
            runtime_state=runtime_state,
            acceptance_state=acceptance,
            review_state=review,
            reward_claimed=acceptance == "accepted_done",
            next_action=(
                str(latest_run.get("message") or "")
                if latest_run is not None
                else ""
            ),
            updated_at=(
                latest_run["updated_at"]
                if latest_run is not None
                else record["updated_at"]
            ),
            policy={
                **dict(record.get("policy", {})),
                "currentCompletion": (
                    {
                        "status": str(current_completion.status),
                        "batchId": current_completion.batch_id,
                        "sealVersion": current_completion.seal_version,
                        "gameDayKey": current_completion.game_day_key,
                        "completionAdjudicationId": current_completion.decision_id,
                        "completionReviewId": current_completion.review_id,
                        "decision": (
                            str(current_completion.decision)
                            if current_completion.decision is not None
                            else None
                        ),
                        "evidenceIds": list(current_completion.evidence_ids),
                        "screenshotEvidenceIds": list(
                            current_completion.screenshot_evidence_ids
                        ),
                        "reasonCode": current_completion.reason_code,
                    }
                    if current_completion is not None
                    else {
                        "status": "none",
                        "reasonCode": "current_todo_scope_has_no_period",
                    }
                ),
            },
            todo_summary=todo_summary,
        )

    def _next_todo_reset_boundary(self, at: datetime) -> datetime | None:
        return self.manager_todos._next_todo_reset_boundary(at)

    def _reconcile_todo_reset_boundary(self, at: datetime) -> None:
        return self.manager_todos._reconcile_todo_reset_boundary(at)

    def _record_todo_reset_scheduler_error(self, error: Exception) -> None:
        return self.manager_todos._record_todo_reset_scheduler_error(error)

    def _validated_todo_game_ids(self, game_ids: list[str] | None) -> list[str]:
        return self.manager_todos._validated_todo_game_ids(game_ids)

    def _todo_reset_policy_document(self) -> dict[str, Any]:
        return self.manager_todos._todo_reset_policy_document()

    def _configured_todo_reset_rule(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]:
        return self.manager_todos._configured_todo_reset_rule(definition)

    def _todo_instance_candidates(
        self,
        game_ids: list[str] | None = None,
        cadence: str | None = None,
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self.manager_todos._todo_instance_candidates(
            game_ids, cadence, at=at
        )

    def list_todo_definitions(
        self, game_id: str | None = None, cadence: str | None = None
    ) -> list[TodoDefinitionRecord]:
        return self.manager_todos.list_todo_definitions(game_id, cadence)

    def list_game_integrations(self) -> GameIntegrationPage:
        return self.manager_todos.list_game_integrations()

    def get_todo_definition(
        self, todo_definition_id: str
    ) -> TodoDefinitionRecord:
        return self.manager_todos.get_todo_definition(todo_definition_id)

    def list_todo_instances(
        self,
        *,
        game_id: str | None = None,
        cadence: str | None = None,
        period_key: str | None = None,
        status: str | None = None,
        current: bool = False,
        limit: int = 1000,
    ) -> list[TodoInstanceRecord]:
        return self.manager_todos.list_todo_instances(
            game_id=game_id,
            cadence=cadence,
            period_key=period_key,
            status=status,
            current=current,
            limit=limit,
        )

    def get_todo_instance(self, todo_instance_id_value: str) -> TodoInstanceRecord:
        return self.manager_todos.get_todo_instance(todo_instance_id_value)

    def _todo_instance_record(
        self,
        record: dict[str, Any],
        *,
        runtime_cache: dict[str, dict[str, Any]],
    ) -> TodoInstanceRecord:
        return self.manager_todos._todo_instance_record(
            record, runtime_cache=runtime_cache
        )

    def _latest_automation_assessments(
        self, todo_instance_ids: set[str]
    ) -> dict[str, AutomationAssessmentRecord]:
        return self.manager_todos._latest_automation_assessments(todo_instance_ids)

    @staticmethod
    def _todo_blocker_projection(item: dict[str, Any]) -> dict[str, Any]:
        return ManagerTodosService._todo_blocker_projection(item)

    def _todo_operational_context(
        self,
        item: TodoInstanceRecord,
        *,
        assessments: dict[str, AutomationAssessmentRecord],
        include_execution_facts: bool,
    ) -> dict[str, Any]:
        return self.manager_todos._todo_operational_context(
            item,
            assessments=assessments,
            include_execution_facts=include_execution_facts,
        )

    def _typed_agent_todo_snapshot(
        self,
        *,
        todo_items: list[TodoInstanceRecord],
        plans: dict[str, dict[str, Any]],
        cadence: str,
        include_execution_facts: bool,
    ) -> dict[str, Any]:
        return self.manager_todos._typed_agent_todo_snapshot(
            todo_items=todo_items,
            plans=plans,
            cadence=cadence,
            include_execution_facts=include_execution_facts,
        )

    def todo_summary(self, game_id: str, cadence: str) -> dict[str, Any]:
        return self.manager_todos.todo_summary(game_id, cadence)

    def _selected_todo_scope_items(
        self,
        *,
        game_id: str,
        cadence: str,
        all_items: list[TodoInstanceRecord],
    ) -> list[TodoInstanceRecord]:
        return self.manager_todos._selected_todo_scope_items(
            game_id=game_id,
            cadence=cadence,
            all_items=all_items,
        )

    @staticmethod
    def _todo_summary_from_items(
        items: list[TodoInstanceRecord], cadence: str
    ) -> dict[str, Any]:
        return ManagerTodosService._todo_summary_from_items(items, cadence)

    def todo_overview(self) -> dict[str, Any]:
        return self.manager_todos.todo_overview()

    def _todo_plans_for_games(
        self, game_ids: list[str], cadence: str
    ) -> dict[str, dict[str, Any]]:
        return self.manager_todos._todo_plans_for_games(game_ids, cadence)

    @staticmethod
    def _batch_todo_scope(
        todo_plans: dict[str, dict[str, Any]], game_ids: list[str]
    ) -> dict[str, Any]:
        return ManagerTodosService._batch_todo_scope(todo_plans, game_ids)


    def _completion_reconciliation_runs(
        self,
        todo_plans: dict[str, dict[str, Any]],
        game_ids: list[str],
    ) -> dict[str, str]:
        """Resolve immutable same-scope runs that need contract re-adjudication.

        This is deliberately not execution recovery.  It is used only when the
        current Todo scope is already complete, the newest sealed contract is
        evidence-pending, and that contract already owns an accepted review.
        The normal batch entry can then re-adjudicate the frozen facts without
        relaunching a game or replaying a completed action.
        """

        runs: dict[str, str] = {}
        for game_id in game_ids:
            plan = todo_plans[game_id]
            completion = plan.get("currentCompletion")
            if (
                plan.get("requiredRemaining") != 0
                or not isinstance(completion, dict)
                or completion.get("status") != "evidence_pending"
                or not completion.get("batchId")
                or not completion.get("decisionId")
                or not completion.get("reviewId")
            ):
                continue
            try:
                batch = self.store.get_batch(str(completion["batchId"]))
            except RecordNotFound:
                continue
            if batch.get("result", {}).get("sealVersion") is None:
                continue
            matches = [
                item
                for item in batch.get("result", {}).get("completionContracts", [])
                if isinstance(item, dict)
                and item.get("completionAdjudicationId")
                == completion.get("decisionId")
                and item.get("gameId") == game_id
                and item.get("reviewId") == completion.get("reviewId")
                and item.get("gameDayKey") in set(plan.get("periodKeys", []))
                and isinstance(item.get("runId"), str)
            ]
            if len(matches) != 1:
                continue
            run_id = str(matches[0]["runId"])
            try:
                run = self.store.get_game_run(run_id)
            except RecordNotFound:
                continue
            if (
                run.get("game_id") != game_id
                or run.get("cadence") != plan.get("cadence")
                or set(run.get("completion_todo_instance_ids", []))
                != set(plan.get("completionTodoInstanceIds", []))
            ):
                continue
            runs[game_id] = run_id
        return runs

    def preview_todo_reset(
        self,
        *,
        action: str,
        game_ids: list[str] | None,
        cadence: str | None,
    ) -> TodoResetPreviewResponse:
        if action not in {"reconcile", "reset"}:
            raise ManagerValidation("todo preview action must be reconcile or reset")
        candidates = self._todo_instance_candidates(game_ids, cadence)
        existing_ids = {
            item.todo_instance_id: item
            for item in self.list_todo_instances(
                cadence=cadence, current=False, limit=10000
            )
        }
        items = [
            TodoResetPreviewItem(
                todo_definition_id=item["todo_definition_id"],
                todo_instance_id=item["todo_instance_id"],
                game_id=item["game_id"],
                cadence=item["cadence"],
                period_key=item["period_key"],
                period_starts_at=item["period_starts_at"],
                period_ends_at=item["period_ends_at"],
                exists=item["todo_instance_id"] in existing_ids,
                current_status=(
                    existing_ids[item["todo_instance_id"]].status
                    if item["todo_instance_id"] in existing_ids
                    else None
                ),
                effective_reset_rule=item["effective_reset_rule"],
                next_period_reset_rule=item["next_period_reset_rule"],
                policy_change_deferred=item["policy_change_deferred"],
            )
            for item in candidates
        ]
        selected_games = self._validated_todo_game_ids(game_ids)
        return TodoResetPreviewResponse(
            state_version=self.store.latest_event_sequence(),
            generated_at=utc_now(),
            action=action,
            game_ids=selected_games,
            cadence=cadence,
            definition_count=len(items),
            existing_count=sum(1 for item in items if item.exists),
            would_create_count=sum(1 for item in items if not item.exists),
            items=items,
        )

    def reconcile_todos(
        self,
        request: TodoReconcileRequest,
        *,
        intent: str,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if intent not in {"reconcile", "reset"}:
            raise ManagerValidation("unsupported todo reconciliation intent")
        self._validated_todo_game_ids(request.game_ids)

        def operation() -> dict[str, Any]:
            candidates = self._todo_instance_candidates(
                request.game_ids, request.cadence
            )
            result = self.store.reconcile_todo_instances(
                candidates,
                intent=intent,
                requested_by=request.requested_by,
                reason=request.reason,
            )
            command_id = str(uuid.uuid4())
            return self._receipt(
                command_id=command_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/commands/{command_id}",
                message=(
                    "当前周期 Todo 已显式重置/对账；旧周期记录保持不变。"
                    if intent == "reset"
                    else "当前周期缺失 Todo 已对账补齐；现有状态未被覆盖。"
                ),
                result={
                    "intent": intent,
                    "createdTodoInstanceIds": result["created_todo_instance_ids"],
                    "existingTodoInstanceIds": result["existing_todo_instance_ids"],
                    "resetTodoInstanceIds": result.get(
                        "reset_todo_instance_ids", []
                    ),
                    "retiredTodoBlockerIds": result.get(
                        "retired_todo_blocker_ids", []
                    ),
                    "todo": self.todo_overview(),
                },
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def transition_todo(
        self,
        todo_instance_id_value: str,
        request: TodoTransitionRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        current = self.store.get_todo_instance(todo_instance_id_value)
        if request.status in {"in_progress", "completed"}:
            raise ManagerConflict(
                "Todo execution state is written only by a fenced Manager Adapter attempt"
            )
        if current["risk"] == "forbidden" and request.status not in {
            "review_required",
            "skipped",
        }:
            raise ManagerConflict(
                "forbidden Todo items can only remain review_required or skipped"
            )
        if request.status in {
            "skipped",
            "blocked",
            "review_required",
            "human_required",
        } and not request.reason.strip():
            raise ManagerValidation(f"{request.status} requires a reason")
        for artifact_id in request.evidence_refs:
            self.store.get_resource("artifact", artifact_id)
        if request.run_id:
            run = self.store.get_game_run(request.run_id)
            if run["game_id"] != current["game_id"] or run["cadence"] != current["cadence"]:
                raise ManagerValidation("runId does not match the Todo GameId/cadence")
        if request.status == "completed" and not (
            current["evidence_refs"] or request.evidence_refs
        ):
            raise ManagerValidation("completed Todo requires at least one evidenceRef")

        def operation() -> dict[str, Any]:
            record = self.store.transition_todo_instance(
                todo_instance_id_value,
                status=request.status,
                reason=request.reason,
                evidence_refs=request.evidence_refs,
                run_id=request.run_id,
                increment_attempt=request.increment_attempt,
                requested_by=request.requested_by,
            )
            command_id = str(uuid.uuid4())
            return self._receipt(
                command_id=command_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/todo-instances/{todo_instance_id_value}",
                message="Todo 状态转换已由 Manager 记账。",
                result={
                    "todoInstance": _dump(
                        self._todo_instance_record(record, runtime_cache={})
                    ),
                    "todoSummary": self.todo_summary(
                        record["game_id"], record["cadence"]
                    ),
                },
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def list_games(self) -> list[GameSummary]:
        recent_runs, sealed_batches = self._projection_history()
        return [
            self._game_projection(
                game,
                recent_runs=recent_runs,
                sealed_batches=sealed_batches,
            )
            for game in self.store.list_games()
        ]

    def get_game(self, game_id: str) -> GameSummary:
        return self._game_projection(self.store.get_game(game_id))

    def get_game_detail(self, game_id: str) -> dict[str, Any]:
        game = self.get_game(game_id)
        runs = [run for run in self.list_game_runs(200) if run.game_id == game_id]
        current = next(
            (
                run
                for run in runs
                if run.state not in {"done", "cancelled", "failed"}
            ),
            runs[0] if runs else None,
        )
        artifacts = [
            artifact
            for artifact in self.list_artifacts(200)
            if artifact.game_id == game_id
        ]
        todo_definitions = self.list_todo_definitions(game_id=game_id)
        todo_instances = self.list_todo_instances(
            game_id=game_id, current=True, limit=1000
        )
        attempt_analysis = self._attempt_analysis(
            todo_items=todo_instances,
            runs=runs,
        )
        daily_todo_summary = self.todo_summary(game_id, "daily")
        weekly_todo_summary = self.todo_summary(game_id, "weekly")
        controller_leases = self.store.list_controller_leases(
            run_id=(current.run_id if current else None), limit=20
        ) if current else []
        execution_facts = self.store.list_execution_control_facts(
            run_id=(current.run_id if current else None), limit=500
        ) if current else []
        checkpoints = [
            item["document"]
            for item in execution_facts
            if item["factType"] == "checkpoint"
        ]
        window_binding_fact = next(
            (
                item["document"]
                for item in execution_facts
                if item["factType"] == "window_binding"
            ),
            None,
        )
        return {
            **_dump(game),
            "runId": current.run_id if current else None,
            "stage": "not-attached" if current is None else current.state,
            "attempts": [_dump(run) for run in runs],
            "runAttempts": attempt_analysis["runAttempts"],
            "todoAttempts": attempt_analysis["todoAttempts"],
            "attemptAnalysis": attempt_analysis,
            "artifacts": [_dump(artifact) for artifact in artifacts],
            "evidenceCount": len(artifacts),
            "todoDefinitions": [_dump(item) for item in todo_definitions],
            "todoInstances": [_dump(item) for item in todo_instances],
            "todoSummary": {
                "daily": daily_todo_summary,
                "weekly": weekly_todo_summary,
            },
            "progress": daily_todo_summary["progress"],
            "nextResetAt": daily_todo_summary["nextResetAt"],
            "unresolvedRequiredTodoIds": daily_todo_summary[
                "unresolvedRequiredTodoIds"
            ],
            "checkpoints": checkpoints,
            "windowBinding": window_binding_fact,
            "controllerLease": controller_leases[0] if controller_leases else None,
            "managerOwnedFields": [
                "attempts",
                "runAttempts",
                "todoAttempts",
                "attemptAnalysis",
                "artifacts",
                "todoInstances",
            ],
            "unavailableFields": {
                "checkpoints": {
                    "status": "disabled-unimplemented",
                    "capability": "checkpoint.record",
                },
                "windowBinding": {
                    "status": "disabled-unimplemented",
                    "provider": "manager-input-control",
                },
            },
        }

    def _batch_action_execution_readiness(self) -> tuple[bool, str, str]:
        if self.store.get_metadata("manager.lifecycle_state", "running") != "running":
            return (
                False,
                "manager_stopping",
                "Manager is stopping and cannot dispatch a Batch resume.",
            )
        if not self.settings.legacy_execution_enabled:
            return (
                False,
                "execution_policy_disabled",
                "Manager execution policy is disabled.",
            )
        try:
            readiness = self._execution_readiness_for_games(
                [game.game_id for game in self.list_games() if game.enabled]
            )
        except Exception as error:
            return (
                False,
                "adapter_host_probe_failed",
                f"Manager Adapter Host readiness probe failed: {type(error).__name__}",
            )
        if not bool(readiness.get("hostHealthy")):
            return (
                False,
                "adapter_host_unhealthy",
                f"Manager Adapter Host is not healthy: {readiness.get('hostStatus')}",
            )
        if not readiness["readyGameIds"]:
            return (
                False,
                "no_promoted_execution_package",
                "No enabled game has a promoted per-game execution package.",
            )
        return (
            True,
            "execution_ready",
            "At least one enabled game has verified per-game execution.",
        )

    def _batch_record(
        self,
        item: dict[str, Any],
        *,
        execution_readiness: tuple[bool, str, str] | None = None,
    ) -> BatchRecord:
        """Expose Manager-owned Batch actions without UI state inference."""

        memberships = list(item.get("run_memberships", []))
        pending = [
            membership
            for membership in memberships
            if membership["state"] in {"queued", "resume_pending"}
        ]
        terminal_retry_run_ids = [
            str(membership["run_id"])
            for membership in memberships
            if membership["state"] == "terminal"
            and membership.get("terminal_outcome") != "completed"
            and self.store.list_run_attempts(
                run_id=str(membership["run_id"]), limit=1
            )
        ]
        pending_with_attempt_ids = [
            str(membership["run_id"])
            for membership in pending
            if self.store.list_run_attempts(
                run_id=str(membership["run_id"]), limit=1
            )
        ]
        run_resume_ids = list(
            dict.fromkeys(terminal_retry_run_ids + pending_with_attempt_ids)
        )
        run_resume_targets: list[dict[str, Any]] = []
        run_reconcile_targets: list[dict[str, Any]] = []
        human_takeover_targets: list[dict[str, Any]] = []
        for run_id in run_resume_ids:
            run = self.store.get_game_run(run_id)
            if run["state"] == EntityState.HUMAN_REQUIRED:
                human_takeover_targets.append({
                    "runId": run_id,
                    "gameId": str(run["game_id"]),
                })
                continue
            try:
                snapshot, _ = self._resume_reconcile_snapshot(run_id)
                decision = plan_resume_reconciliation(
                    snapshot,
                    ResumeReconcileRequest(
                        run_id=run_id,
                        game_day_key=snapshot.run.game_day_key,
                        expected_run_revision=snapshot.run.revision,
                        expected_current_attempt_id=(
                            snapshot.current_attempt.run_attempt_id
                        ),
                        expected_current_attempt_revision=(
                            snapshot.current_attempt.revision
                        ),
                    ),
                )
                requirements = list(
                    dict.fromkeys(
                        requirement.value
                        for decision_item in decision.items
                        for requirement in decision_item.requirements
                    )
                )
                provider_status = self._resume_provider_status()
                unavailable_requirements = [
                    requirement
                    for requirement in requirements
                    for provider in [self._resume_required_provider(requirement)]
                    if provider is not None and not provider_status[provider]
                ]
                deferred_ids = list(
                    dict.fromkeys(
                        [
                            *decision.reconcile_unknown,
                            *decision.deferred_review,
                            *decision.deferred_human,
                            *decision.deferred_forbidden,
                        ]
                    )
                )
                target = {
                    "runId": run_id,
                    "gameId": str(run["game_id"]),
                    "requestPath": f"/game-runs/{run_id}/resume-requests",
                    "expectedRunRevision": snapshot.run.revision,
                    "expectedCurrentAttemptId": (
                        snapshot.current_attempt.run_attempt_id
                    ),
                    "expectedCurrentAttemptRevision": (
                        snapshot.current_attempt.revision
                    ),
                }
                if (
                    decision.eligible_pending
                    and not deferred_ids
                    and not unavailable_requirements
                ):
                    run_resume_targets.append(target)
                else:
                    run_reconcile_targets.append(
                        {
                            **target,
                            "executionWillStart": False,
                            "reasonCode": (
                                "resume_provider_unavailable"
                                if unavailable_requirements
                                else "resume_reconciliation_required"
                                if deferred_ids
                                else "no_pending_resume_work"
                            ),
                            "requirements": requirements,
                            "unavailableRequirements": unavailable_requirements,
                            "deferredTodoInstanceIds": deferred_ids,
                        }
                    )
            except (ManagerConflict, ResumeReconcileConflict, ValueError) as error:
                run_reconcile_targets.append(
                    {
                        "runId": run_id,
                        "gameId": str(run["game_id"]),
                        "requestPath": f"/game-runs/{run_id}/resume-requests",
                        "executionWillStart": False,
                        "reasonCode": "resume_preflight_failed",
                        "requirements": ["current_terminal_attempt"],
                        "unavailableRequirements": [],
                        "deferredTodoInstanceIds": list(run["todo_instance_ids"]),
                        "errorClass": type(error).__name__,
                    }
                )
        return project_batch_record(
            item,
            execution_readiness=execution_readiness,
            terminal_retry_run_ids=terminal_retry_run_ids,
            pending_with_attempt_ids=pending_with_attempt_ids,
            run_resume_targets=run_resume_targets,
            run_reconcile_targets=run_reconcile_targets,
            human_takeover_targets=human_takeover_targets,
        )

    def list_batches(self, limit: int) -> list[BatchRecord]:
        readiness = self._batch_action_execution_readiness()
        return [
            self._batch_record(item, execution_readiness=readiness)
            for item in self.store.list_batches(limit)
        ]

    def get_batch(self, batch_id: str) -> BatchRecord:
        return self._batch_record(
            self.store.get_batch(batch_id),
            execution_readiness=self._batch_action_execution_readiness(),
        )

    def list_batch_run_memberships(
        self, batch_id: str
    ) -> list[BatchRunMembershipRecord]:
        self.store.get_batch(batch_id)
        return [
            BatchRunMembershipRecord.model_validate(item)
            for item in self.store.list_batch_run_memberships(
                batch_id=batch_id, limit=5000
            )
        ]

    def _latest_batch_for_run(self, run_id: str) -> dict[str, Any] | None:
        memberships = self.store.list_batch_run_memberships(
            run_id=run_id, limit=5000
        )
        candidates = [
            self.store.get_batch(str(item["batch_id"])) for item in memberships
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                int(item.get("continuation_ordinal", 0)),
                str(item.get("created_at", "")),
                str(item["batch_id"]),
            ),
        )

    def list_game_runs(self, limit: int) -> list[GameRunRecord]:
        return [
            GameRunRecord.model_validate(item) for item in self.store.list_game_runs(limit)
        ]

    def get_game_run(self, run_id: str) -> GameRunRecord:
        return GameRunRecord.model_validate(self.store.get_game_run(run_id))

    def list_controller_leases(
        self, *, run_id: str | None = None, active_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        if run_id is not None:
            self.store.get_game_run(run_id)
        return self.store.list_controller_leases(
            run_id=run_id, active_only=active_only, limit=limit
        )

    def list_todo_blockers(
        self, *, run_id: str | None = None, active_only: bool = False, limit: int = 500
    ) -> list[dict[str, Any]]:
        if run_id is not None:
            self.store.get_game_run(run_id)
        return [
            {
                "blockerId": item["blocker_id"],
                "managerId": item["manager_id"],
                "gameId": item["game_id"],
                "runId": item["run_id"],
                "runAttemptId": item["run_attempt_id"],
                "todoInstanceId": item["todo_instance_id"],
                "todoAttemptId": item["todo_attempt_id"],
                "gameDayKey": item["game_day_key"],
                "kind": item["kind"],
                "code": item["code"],
                "state": item["state"],
                "revision": item["revision"],
                "retryable": item["retryable"],
                "reason": item["reason"],
                "artifactRefs": item["artifact_refs"],
                "raisedAt": item["raised_at"],
                "transitionedAt": item["transitioned_at"],
                "resolvedAt": item["resolved_at"],
                "resolutionCode": item["resolution_code"],
                "resolutionReason": item["resolution_reason"],
                "resolutionArtifactRefs": item["resolution_artifact_refs"],
                "releaseId": item["release_id"],
                "releaseExplicit": item["release_explicit"],
                "releasedBy": item["released_by"],
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            }
            for item in self.store.list_todo_blockers(
                run_id=run_id, active_only=active_only, limit=limit
            )
        ]

    def list_execution_control_facts(
        self,
        *,
        fact_type: str | None = None,
        run_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if run_id is not None:
            self.store.get_game_run(run_id)
        return self.store.list_execution_control_facts(
            fact_type=fact_type, run_id=run_id, limit=limit
        )

    def record_execution_control_fact(
        self,
        fact: WindowBinding | FocusLease | Observation | ActionReceipt | Checkpoint,
        *,
        todo_instance_id: str | None = None,
        todo_attempt_id: str | None = None,
        action_idempotency: ActionIdempotency | None = None,
        checkpoint_stage: CheckpointStage | None = None,
    ) -> dict[str, Any]:
        """Internal typed ingress reserved for real Manager-owned providers."""

        type_map: tuple[tuple[type[Any], str, str], ...] = (
            (WindowBinding, "window_binding", "window_binding_id"),
            (FocusLease, "focus_lease", "focus_lease_id"),
            (Observation, "observation", "observation_id"),
            (ActionReceipt, "action_receipt", "action_receipt_id"),
            (Checkpoint, "checkpoint", "checkpoint_id"),
        )
        selected = next(
            (
                (fact_type, id_field)
                for fact_class, fact_type, id_field in type_map
                if isinstance(fact, fact_class)
            ),
            None,
        )
        if selected is None:
            raise ManagerValidation("unsupported execution-control fact")
        fact_type, id_field = selected
        scope = fact.scope
        if scope.manager_id != self.manager_id:
            raise ManagerConflict("execution-control fact belongs to another Manager")
        run_attempt = self.store.get_run_attempt(scope.run_attempt_id)
        if (
            run_attempt["run_id"] != scope.run_id
            or run_attempt["game_id"] != scope.game_id
            or self._run_game_day_key(
                self.store.get_game_run(scope.run_id),
                list(run_attempt["plan"].get("executableTodoInstanceIds", [])),
            )
            != scope.game_day_key
        ):
            raise ManagerConflict("execution-control fact scope is stale")
        if isinstance(fact, Checkpoint):
            if todo_instance_id is not None and todo_instance_id != fact.todo_instance_id:
                raise ManagerValidation("checkpoint Todo scope is inconsistent")
            todo_instance_id = fact.todo_instance_id
        document = fact.model_dump(mode="json", by_alias=True)
        if (
            (action_idempotency is not None or checkpoint_stage is not None)
            and todo_attempt_id is None
        ):
            raise ManagerValidation(
                "resume metadata requires a concrete current TodoAttempt"
            )
        if todo_attempt_id is not None:
            todo_attempt = self.store.get_todo_attempt(todo_attempt_id)
            if (
                todo_instance_id is None
                or todo_attempt["todo_instance_id"] != todo_instance_id
                or todo_attempt["run_attempt_id"] != scope.run_attempt_id
            ):
                raise ManagerValidation("execution-control TodoAttempt scope is stale")
        if isinstance(fact, ActionReceipt) and action_idempotency is not None:
            outcome = (
                "failed" if fact.result.value == "no_effect" else fact.result.value
            )
            document["resume"] = {
                "todoAttemptId": todo_attempt_id,
                "outcome": outcome,
                "idempotency": action_idempotency.value,
            }
        if isinstance(fact, Checkpoint) and checkpoint_stage is not None:
            document["resume"] = {
                "todoAttemptId": todo_attempt_id,
                "stage": checkpoint_stage.value,
                "valid": fact.state.value == "valid",
                "actionReceiptId": fact.action_receipt_id,
                "observationRefs": list(fact.observation_ids),
            }
        return self.store.record_execution_control_fact(
            fact_type=fact_type,
            fact_id=str(getattr(fact, id_field)),
            manager_id=scope.manager_id,
            game_id=scope.game_id,
            run_id=scope.run_id,
            run_attempt_id=scope.run_attempt_id,
            todo_instance_id=todo_instance_id,
            game_day_key=scope.game_day_key,
            document=document,
        )

    @staticmethod
    def _without_fencing_material(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ManagerService._without_fencing_material(item)
                for key, item in value.items()
                if not any(
                    sensitive
                    in "".join(
                        character.lower()
                        for character in str(key)
                        if character.isalnum()
                    )
                    for sensitive in (
                        "fencingtoken",
                        "cancelauthority",
                        "authoritymac",
                    )
                )
            }
        if isinstance(value, list):
            return [ManagerService._without_fencing_material(item) for item in value]
        return value

    @staticmethod
    def _assert_no_fencing_value(value: Any, fencing_token: str) -> None:
        """Reject copying a private fencing grant into any public field."""

        def contains(candidate: Any) -> bool:
            if isinstance(candidate, str):
                return fencing_token in candidate
            if isinstance(candidate, dict):
                return any(
                    contains(key) or contains(item)
                    for key, item in candidate.items()
                )
            if isinstance(candidate, (list, tuple, set)):
                return any(contains(item) for item in candidate)
            return False

        if contains(value):
            raise PublicFencingMaterialRejected(
                "public persistence values must not contain private claim credentials"
            )

    @classmethod
    def _run_attempt_record(cls, item: dict[str, Any]) -> RunAttemptRecord:
        return RunAttemptRecord(
            run_attempt_id=item["run_attempt_id"],
            run_id=item["run_id"],
            game_id=item["game_id"],
            cadence=item["cadence"],
            state=item["state"],
            executable_todo_instance_ids=list(
                item.get("plan", {}).get("executableTodoInstanceIds", [])
            ),
            attempt_ordinal=int(item.get("attempt_ordinal", 1)),
            process_id=item.get("process_id"),
            exit_code=item.get("exit_code"),
            result=cls._without_fencing_material(item.get("result", {})),
            started_at=item["started_at"],
            completed_at=item.get("completed_at"),
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )

    def list_run_attempts(
        self, *, run_id: str | None = None, limit: int = 100
    ) -> list[RunAttemptRecord]:
        if run_id is not None:
            self.store.get_game_run(run_id)
        return [
            self._run_attempt_record(item)
            for item in self.store.list_run_attempts(run_id=run_id, limit=limit)
        ]

    def get_run_attempt(self, run_attempt_id: str) -> RunAttemptRecord:
        return self._run_attempt_record(self.store.get_run_attempt(run_attempt_id))

    def list_todo_attempts(
        self, *, todo_instance_id: str, limit: int = 100
    ) -> list[TodoAttemptRecord]:
        self.store.get_todo_instance(todo_instance_id)
        return [
            TodoAttemptRecord.model_validate(item)
            for item in self.store.list_todo_attempts(
                todo_instance_id=todo_instance_id, limit=limit
            )
        ]

    def list_adapter_events(
        self, run_attempt_id: str
    ) -> list[AdapterEventRecord]:
        self.store.get_run_attempt(run_attempt_id)
        return [
            AdapterEventRecord.model_validate(
                {
                    **item,
                    "payload": self._without_fencing_material(item.get("payload", {})),
                }
            )
            for item in self.store.list_adapter_events(run_attempt_id)
        ]

    def _attempt_analysis(
        self,
        *,
        todo_items: list[TodoInstanceRecord],
        runs: list[GameRunRecord],
        per_todo_limit: int = 20,
        per_run_limit: int = 20,
    ) -> dict[str, Any]:
        """Build a bounded, read-only attempt history for people and Agents.

        The counters are deliberately mechanical. They expose repeated failures and
        fresh evidence without pretending that a score proves completion or diagnoses
        the root cause by itself.
        """

        run_attempts: list[RunAttemptRecord] = []
        seen_run_attempt_ids: set[str] = set()
        for run in runs[:100]:
            for attempt in self.list_run_attempts(
                run_id=run.run_id, limit=per_run_limit
            ):
                if attempt.run_attempt_id in seen_run_attempt_ids:
                    continue
                seen_run_attempt_ids.add(attempt.run_attempt_id)
                run_attempts.append(attempt)

        todo_attempts: list[TodoAttemptRecord] = []
        problem_signals: list[dict[str, Any]] = []
        for item in todo_items[:500]:
            history = self.list_todo_attempts(
                todo_instance_id=item.todo_instance_id,
                limit=per_todo_limit,
            )
            todo_attempts.extend(history)
            if not history:
                continue
            blocked_count = sum(attempt.state == "blocked" for attempt in history)
            review_count = sum(
                attempt.state == "review_required" for attempt in history
            )
            human_required_count = sum(
                attempt.state == "human_required" for attempt in history
            )
            retryable_failure_count = sum(
                attempt.retryable
                and attempt.state in {"blocked", "review_required"}
                for attempt in history
            )
            if (
                len(history) < 2
                and blocked_count == 0
                and review_count == 0
                and human_required_count == 0
            ):
                continue
            latest = history[0]
            evidence_refs = list(
                dict.fromkeys(
                    artifact_id
                    for attempt in history
                    for artifact_id in attempt.evidence_refs
                )
            )[:50]
            problem_signals.append(
                {
                    "todoInstanceId": item.todo_instance_id,
                    "todoDefinitionId": item.todo_definition_id,
                    "gameId": item.game_id,
                    "cadence": item.cadence,
                    "operation": item.operation,
                    "title": item.title,
                    "currentStatus": item.status,
                    "automationDifficulty": item.automation_difficulty,
                    "attemptCount": len(history),
                    "blockedCount": blocked_count,
                    "reviewRequiredCount": review_count,
                    "humanRequiredCount": human_required_count,
                    "retryableFailureCount": retryable_failure_count,
                    "latestState": latest.state,
                    "latestReasonCode": latest.reason_code,
                    "latestReason": latest.reason,
                    "latestAttemptAt": latest.started_at.isoformat(),
                    "evidenceRefs": evidence_refs,
                }
            )

        run_attempts.sort(key=lambda attempt: attempt.created_at, reverse=True)
        todo_attempts.sort(key=lambda attempt: attempt.created_at, reverse=True)
        problem_signals.sort(
            key=lambda item: (
                int(item["blockedCount"])
                + int(item["reviewRequiredCount"])
                + int(item["humanRequiredCount"]),
                int(item["attemptCount"]),
                str(item["latestAttemptAt"]),
            ),
            reverse=True,
        )
        bounded_run_attempts = run_attempts[:200]
        bounded_todo_attempts = todo_attempts[:500]
        return {
            "summary": {
                "runAttemptCount": len(bounded_run_attempts),
                "todoAttemptCount": len(bounded_todo_attempts),
                "blockedTodoAttemptCount": sum(
                    attempt.state == "blocked" for attempt in bounded_todo_attempts
                ),
                "reviewRequiredTodoAttemptCount": sum(
                    attempt.state == "review_required"
                    for attempt in bounded_todo_attempts
                ),
                "humanRequiredTodoAttemptCount": sum(
                    attempt.state == "human_required"
                    for attempt in bounded_todo_attempts
                ),
                "retryableFailureCount": sum(
                    attempt.retryable
                    and attempt.state in {"blocked", "review_required"}
                    for attempt in bounded_todo_attempts
                ),
                "problemSignalCount": len(problem_signals),
            },
            "runAttempts": [_dump(attempt) for attempt in bounded_run_attempts],
            "todoAttempts": [_dump(attempt) for attempt in bounded_todo_attempts],
            "problemSignals": problem_signals[:100],
            "interpretation": (
                "Repeated or blocked attempts are diagnostic signals only; Todo completion "
                "and accepted_done still require the frozen evidence contract."
            ),
        }

    def _work_item_visible_to_principal(
        self, item: dict[str, Any], *, actor_id: str, principal_id: str
    ) -> bool:
        if actor_id == "rabiroute":
            return item.get("requested_by") == principal_id
        if actor_id != "agent":
            return True
        if item.get("state") == EntityState.PLANNED:
            return True
        if item.get("requested_by") == principal_id:
            return True
        return any(
            claim.get("work_item_id") == item.get("work_item_id")
            and claim.get("claimant") == principal_id
            for claim in self.store.list_work_item_claims(500)
        )

    def list_work_items(
        self,
        limit: int,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
    ) -> list[AgentWorkItemRecord]:
        items = self.store.list_work_items(500 if actor_id else limit)
        if actor_id in {"agent", "rabiroute"}:
            if not principal_id:
                raise ManagerValidation("scoped work-item reads require a principal")
            items = [
                item
                for item in items
                if self._work_item_visible_to_principal(
                    item, actor_id=actor_id, principal_id=principal_id
                )
            ][:limit]
        return [AgentWorkItemRecord.model_validate(item) for item in items[:limit]]

    def get_work_item(
        self,
        work_item_id: str,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
    ) -> AgentWorkItemRecord:
        item = self.store.get_work_item(work_item_id)
        if actor_id in {"agent", "rabiroute"}:
            if not principal_id or not self._work_item_visible_to_principal(
                item, actor_id=actor_id, principal_id=principal_id
            ):
                raise RecordNotFound(work_item_id)
        return AgentWorkItemRecord.model_validate(item)

    @staticmethod
    def work_item_dispatch_projection(item: AgentWorkItemRecord) -> dict[str, Any]:
        return {
            "workItemId": item.work_item_id,
            "kind": item.kind,
            "state": item.state,
            "gameId": item.game_id,
            "cadence": item.cadence,
            "runId": item.run_id,
            "requestedBy": item.requested_by,
            "note": item.note,
            "createdAt": item.created_at.isoformat(),
            "updatedAt": item.updated_at.isoformat(),
        }

    def list_claim_decisions(
        self, limit: int, *, principal_id: str | None = None
    ) -> list[ClaimDecisionRecord]:
        items = self.store.list_claim_decisions(500 if principal_id else limit)
        if principal_id:
            items = [
                item
                for item in items
                if self.store.get_work_item_claim_private(item["claim_id"])[
                    "claimant"
                ]
                == principal_id
            ][:limit]
        return [ClaimDecisionRecord.model_validate(item) for item in items[:limit]]

    def get_claim_decision(
        self, decision_id: str, *, principal_id: str | None = None
    ) -> ClaimDecisionRecord:
        item = self.store.get_claim_decision(decision_id)
        if principal_id and self.store.get_work_item_claim_private(item["claim_id"])[
            "claimant"
        ] != principal_id:
            raise RecordNotFound(decision_id)
        return ClaimDecisionRecord.model_validate(item)

    def list_capabilities(self) -> list[CapabilityDefinition]:
        return sorted(self.capabilities.values(), key=lambda item: item.capability_id)

    def get_capability(self, capability_id: str) -> CapabilityDefinition:
        try:
            return self.capabilities[capability_id]
        except KeyError as error:
            raise RecordNotFound(capability_id) from error

    def list_capability_invocations(
        self, limit: int, *, principal_id: str | None = None
    ) -> list[CapabilityInvocationRecord]:
        items = self.store.list_capability_invocations(500 if principal_id else limit)
        if principal_id:
            items = [
                item for item in items if item.get("requested_by") == principal_id
            ][:limit]
        return [CapabilityInvocationRecord.model_validate(item) for item in items[:limit]]

    def get_capability_invocation(
        self, invocation_id: str, *, principal_id: str | None = None
    ) -> CapabilityInvocationRecord:
        item = self.store.get_capability_invocation(invocation_id)
        if principal_id and item.get("requested_by") != principal_id:
            raise RecordNotFound(invocation_id)
        return CapabilityInvocationRecord.model_validate(item)

    @staticmethod
    def _command_visible_to_principal(
        receipt: dict[str, Any], *, actor_id: str, principal_id: str
    ) -> bool:
        result = receipt.get("result")
        if not isinstance(result, dict):
            return False

        work_item = result.get("workItem")
        if isinstance(work_item, dict):
            requested_by = work_item.get("requestedBy", work_item.get("requested_by"))
            if requested_by == principal_id:
                return True
        if actor_id == "rabiroute":
            return False
        if actor_id != "agent":
            return True

        claim = result.get("claim")
        if isinstance(claim, dict) and claim.get("claimant") == principal_id:
            return True
        decision = result.get("decision")
        if isinstance(decision, dict):
            requested_by = decision.get("requestedBy", decision.get("requested_by"))
            if requested_by == principal_id:
                return True
        invocation = result.get("invocation")
        if isinstance(invocation, dict):
            requested_by = invocation.get("requestedBy", invocation.get("requested_by"))
            if requested_by == principal_id:
                return True
        return False

    def get_command_receipt(
        self,
        command_id: str,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
    ) -> CommandReceipt:
        document = self._without_fencing_material(
            self.store.get_command_receipt(command_id)
        )
        if actor_id in {"agent", "rabiroute"}:
            if not principal_id or not self._command_visible_to_principal(
                document, actor_id=actor_id, principal_id=principal_id
            ):
                raise RecordNotFound(command_id)
        return CommandReceipt.model_validate(document)

    def list_claims(
        self, limit: int, *, principal_id: str | None = None
    ) -> list[WorkItemClaimRecord]:
        items = self.store.list_work_item_claims(500 if principal_id else limit)
        if principal_id:
            items = [
                item for item in items if item.get("claimant") == principal_id
            ][:limit]
        return [WorkItemClaimRecord.model_validate(item) for item in items[:limit]]

    def list_incidents(self, limit: int) -> list[IncidentRecord]:
        incidents: list[IncidentRecord] = []
        for run in self.list_game_runs(limit):
            if run.state not in {
                "failed",
                "blocked",
                "review_required",
                "human_required",
            }:
                continue
            incidents.append(
                IncidentRecord(
                    incident_id=f"run-{run.run_id}",
                    fingerprint=f"{run.game_id}:{run.state}:{run.run_id}",
                    title=run.message or f"{run.game_id} requires review",
                    game_id=run.game_id,
                    state="open",
                    severity=("error" if run.state == "failed" else "warning"),
                    retry_eligibility="manual-review",
                    last_seen_at=run.updated_at,
                )
            )
        return incidents

    @staticmethod
    def _is_opaque_artifact_id(value: str) -> bool:
        return bool(
            value
            and len(value) <= 160
            and value.isascii()
            and value[0].isalnum()
            and value not in {".", ".."}
            and all(character.isalnum() or character in "_.-" for character in value)
        )

    def _artifact_read_refs(
        self,
        *,
        actor_id: str | None,
        principal_id: str | None,
        work_item_id: str | None,
        claim_id: str | None,
        fencing_token: str | None,
    ) -> tuple[str, ...] | None:
        # Calls wholly inside Manager omit actor_id and retain full access. Every
        # API route supplies it, so an Agent can never turn a bare opaque ID
        # into a cross-work-item artifact read.
        if actor_id is None or actor_id in {"webgui", "cli"}:
            return None
        if actor_id != "agent":
            raise ManagerValidation("actor is not allowed to read Manager artifacts")
        if not principal_id or not work_item_id or not claim_id or not fencing_token:
            raise ManagerValidation(
                "Agent artifact reads require work-item, claim, and fencing context"
            )
        _, work_item = self._validate_active_claim(
            claim_id=claim_id,
            work_item_id=work_item_id,
            claimant=principal_id,
            fencing_token=fencing_token,
        )
        return tuple(dict.fromkeys(str(item) for item in work_item.get("artifact_refs", [])))

    def list_artifacts(
        self,
        limit: int = 100,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
        work_item_id: str | None = None,
        claim_id: str | None = None,
        fencing_token: str | None = None,
    ) -> list[EvidenceArtifactRecord]:
        scoped_refs = self._artifact_read_refs(
            actor_id=actor_id,
            principal_id=principal_id,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )
        resources = (
            self.store.list_resources("artifact", limit)
            if scoped_refs is None
            else [
                self.store.get_resource("artifact", artifact_id)
                for artifact_id in scoped_refs[:limit]
            ]
        )
        return [self._artifact_record(item) for item in resources]

    def get_artifact(
        self,
        artifact_id: str,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
        work_item_id: str | None = None,
        claim_id: str | None = None,
        fencing_token: str | None = None,
    ) -> EvidenceArtifactRecord:
        scoped_refs = self._artifact_read_refs(
            actor_id=actor_id,
            principal_id=principal_id,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )
        if scoped_refs is not None and artifact_id not in scoped_refs:
            # Preserve the same response as an unknown opaque ID so claim scope
            # cannot be used as a cross-work-item artifact existence oracle.
            raise RecordNotFound(artifact_id)
        return self._artifact_record(
            self.store.get_resource("artifact", artifact_id)
        )

    @staticmethod
    def _artifact_record(resource: dict[str, Any]) -> EvidenceArtifactRecord:
        document = resource["document"]
        return EvidenceArtifactRecord(
            artifact_id=resource["resource_id"],
            kind=document["kind"],
            captured_at=document["capturedAt"],
            source=document["source"],
            raw=bool(document.get("raw", True)),
            content_type=document["contentType"],
            game_id=document.get("gameId"),
            run_id=document.get("runId"),
            run_attempt_id=document.get("runAttemptId"),
            todo_instance_id=document.get("todoInstanceId"),
            todo_attempt_id=document.get("todoAttemptId"),
            game_day_key=document.get("gameDayKey"),
            verdict=document.get("verdict", "unknown"),
            content_hash=document.get("hash", ""),
            size_bytes=int(document.get("sizeBytes", 0)),
            file_name=document.get("fileName", ""),
        )

    def _completion_artifact_integrity(
        self,
        artifact_id: str,
        document: dict[str, Any],
    ) -> ArtifactIntegrityResult:
        """Re-open one opaque artifact without exposing its internal path.

        This is called independently at projection, automatic-review and
        accepted human/Agent-review boundaries.  A previous successful import
        is not treated as durable proof that the entity still exists.
        """

        result = verify_artifact_entity(
            self.settings.data_dir / "artifacts",
            artifact_id,
            document,
        )
        log_fields = {
            "artifactId": artifact_id,
            "gameId": document.get("gameId"),
            "runId": document.get("runId"),
            "runAttemptId": document.get("runAttemptId"),
            "todoInstanceId": document.get("todoInstanceId"),
            "kind": document.get("kind"),
            "source": document.get("source"),
            "contentHash": result.content_hash or document.get("hash"),
            "decision": "accepted" if result.valid else "rejected",
            "reasonCode": result.reason_code,
        }
        log = _log.debug if result.valid else _log.warning
        log(
            "completion.artifact_integrity %s",
            json.dumps(log_fields, ensure_ascii=True, separators=(",", ":")),
        )
        return result

    def artifact_content(
        self,
        artifact_id: str,
        *,
        actor_id: str | None = None,
        principal_id: str | None = None,
        work_item_id: str | None = None,
        claim_id: str | None = None,
        fencing_token: str | None = None,
    ) -> ArtifactContent:
        scoped_refs = self._artifact_read_refs(
            actor_id=actor_id,
            principal_id=principal_id,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )
        if scoped_refs is not None and artifact_id not in scoped_refs:
            raise RecordNotFound(artifact_id)
        resource = self.store.get_resource("artifact", artifact_id)
        document = resource["document"]
        relative = str(document.get("relativePath", ""))
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ManagerValidation("artifact has an invalid internal path")
        configured_root = self.settings.data_dir / "artifacts"
        if str(configured_root).startswith("\\\\"):
            raise ManagerValidation("artifact content root must be local")
        if not configured_root.is_dir() or _has_reparse_point(configured_root):
            raise RecordNotFound(artifact_id)
        root = configured_root.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ManagerValidation("artifact escaped the Manager artifact root") from error
        if not candidate.is_file() or _has_reparse_point(candidate):
            raise RecordNotFound(artifact_id)
        for parent in candidate.parents:
            if parent == root:
                break
            if _has_reparse_point(parent):
                raise ManagerValidation("artifact path crosses a link")
        try:
            expected_size = int(document.get("sizeBytes", -1))
        except (TypeError, ValueError) as error:
            raise ManagerValidation("artifact size ledger is invalid") from error
        if expected_size < 0 or expected_size > 25 * 1024 * 1024:
            raise ManagerConflict("artifact exceeds the safe content-serving limit")
        try:
            path_snapshot = os.stat(candidate, follow_symlinks=False)
        except OSError as error:
            raise RecordNotFound(artifact_id) from error
        if not stat.S_ISREG(path_snapshot.st_mode):
            raise RecordNotFound(artifact_id)
        if path_snapshot.st_size != expected_size:
            raise ManagerConflict("artifact size no longer matches its ledger")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(candidate, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                opened_snapshot = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened_snapshot.st_mode)
                    or not _same_file_snapshot(path_snapshot, opened_snapshot)
                    or opened_snapshot.st_size != expected_size
                ):
                    raise ManagerConflict("artifact changed before its verified read")
                content = stream.read(expected_size + 1)
                final_snapshot = os.fstat(stream.fileno())
        except ManagerConflict:
            raise
        except OSError as error:
            raise RecordNotFound(artifact_id) from error
        if not _same_file_snapshot(opened_snapshot, final_snapshot):
            raise ManagerConflict("artifact changed during its verified read")
        if len(content) != expected_size:
            raise ManagerConflict("artifact size no longer matches its ledger")
        digest = hashlib.sha256(content).hexdigest()
        if digest != document.get("hash"):
            raise ManagerConflict("artifact hash no longer matches its ledger")
        content_type = str(document.get("contentType", "application/octet-stream"))
        inline = content_type in {"image/png", "image/jpeg", "image/webp"}
        if not inline and content_type not in {"application/json", "text/plain"}:
            content_type = "application/octet-stream"
        file_name = str(document.get("fileName", artifact_id)).replace("\\", "/")
        file_name = file_name.rsplit("/", 1)[-1]
        if (
            not file_name
            or len(file_name) > 180
            or any(ord(character) < 32 or ord(character) == 127 for character in file_name)
        ):
            file_name = artifact_id
        return ArtifactContent(
            content=content,
            content_type=content_type,
            file_name=file_name,
            inline=inline,
        )

    def list_weekly(self) -> list[WeeklyTaskRecord]:
        records: list[WeeklyTaskRecord] = []
        for game in self.list_games():
            weekly_mode = str(game.policy.get("weeklyMode", "unsupported"))
            if weekly_mode in {"unsupported", "none", "disabled"}:
                continue
            records.append(
                WeeklyTaskRecord(
                    weekly_id=f"{game.game_id}_weekly",
                    game_id=game.game_id,
                    display_name=f"{game.display_name}周常",
                    enabled=game.enabled,
                    capability_ref="game.weekly.plan@1.0",
                )
            )
        return records

    def list_adapters(self) -> list[AdapterInfoRecord]:
        records: list[AdapterInfoRecord] = []
        for game in self.list_games():
            host = self.adapter_host.probe_game(game.game_id)
            package = host["executionPackage"]
            package_status = str(package.get("status") or "unknown")
            package_version = package.get("packageVersion")
            promoted = package_status == "promoted" and isinstance(
                package_version, str
            )
            candidate = package_status == "installed-unpromoted" and isinstance(
                package_version, str
            )
            implementation_hash = package.get("payloadDigest")
            if not isinstance(implementation_hash, str):
                implementation_hash = None
            if not host["hostHealthy"]:
                health = f"host-{host['hostStatus']}-execution-disabled"
            elif package["status"] == "unsafe-entrypoint":
                health = "host-healthy-execution-package-unsafe"
            elif not host["executionReady"] and package["status"] == "missing":
                health = "host-healthy-execution-package-missing"
            elif not host["executionReady"]:
                health = "host-healthy-execution-disabled"
            else:
                health = "healthy"
            records.append(
                AdapterInfoRecord(
                    adapter_id=self.adapter_host.adapter_id(game.game_id),
                    display_name=f"{game.display_name} 兼容 Adapter",
                    game_id=game.game_id,
                    active_version=package_version if promoted else None,
                    active_version_id=package_version if promoted else None,
                    candidate_version=package_version if candidate else None,
                    candidate_version_id=package_version if candidate else None,
                    stage=(
                        "production"
                        if promoted
                        else "candidate"
                        if candidate
                        else "compatibility"
                    ),
                    health=health,
                    capability_count=2,
                    implementation_hash=implementation_hash,
                    host_id=str(host["hostId"]),
                    host_version=str(host["hostVersion"]),
                    host_healthy=bool(host["hostHealthy"]),
                    host_status=str(host["hostStatus"]),
                    execution_ready=bool(host["executionReady"]),
                    execution_package_status=package_status,
                )
            )
        return records

    def list_logs(
        self,
        after: int = 0,
        limit: int = 100,
        *,
        before: int | None = None,
        recent: bool = False,
    ) -> list[LogEntry]:
        entries: list[LogEntry] = []
        run_attempt_cache: dict[str, dict[str, Any] | None] = {}
        todo_attempt_cache: dict[str, dict[str, Any] | None] = {}
        adapter_event_cache: dict[str, dict[int, dict[str, Any]]] = {}

        def run_attempt(attempt_id: str) -> dict[str, Any] | None:
            if attempt_id not in run_attempt_cache:
                try:
                    run_attempt_cache[attempt_id] = self.store.get_run_attempt(
                        attempt_id
                    )
                except RecordNotFound:
                    run_attempt_cache[attempt_id] = None
            return run_attempt_cache[attempt_id]

        def todo_attempt(attempt_id: str) -> dict[str, Any] | None:
            if attempt_id not in todo_attempt_cache:
                try:
                    todo_attempt_cache[attempt_id] = self.store.get_todo_attempt(
                        attempt_id
                    )
                except RecordNotFound:
                    todo_attempt_cache[attempt_id] = None
            return todo_attempt_cache[attempt_id]

        log_events = [
            EventRecord.model_validate(item)
            for item in self.store.list_log_events(
                after=after,
                before=before,
                recent=recent,
                limit=limit,
            )
        ]
        for event in log_events:
            payload_layers = [event.payload]
            for key in ("document", "result", "plan", "scope", "event"):
                nested = event.payload.get(key)
                if isinstance(nested, dict):
                    payload_layers.append(nested)

            # Public events intentionally keep Adapter payloads out of the
            # generic event body.  Rejoin the immutable Adapter ledger by its
            # exact (RunAttempt, sequence) key so /logs can expose the real
            # phase/outcome/reason instead of presenting a payload hash as an
            # execution diagnosis.
            inferred_run_attempt_id = (
                event.entity_id if event.entity_type == "run-attempt" else None
            )
            if event.event_type.startswith("adapter.") and inferred_run_attempt_id:
                raw_sequence = event.payload.get("sequence")
                if isinstance(raw_sequence, int) and not isinstance(raw_sequence, bool):
                    if inferred_run_attempt_id not in adapter_event_cache:
                        adapter_event_cache[inferred_run_attempt_id] = {
                            int(item["sequence"]): dict(item.get("payload") or {})
                            for item in self.store.list_adapter_events(
                                inferred_run_attempt_id
                            )
                        }
                    adapter_payload = adapter_event_cache[
                        inferred_run_attempt_id
                    ].get(raw_sequence)
                    if adapter_payload is not None:
                        payload_layers.insert(0, adapter_payload)

            if event.entity_type == "todo-attempt":
                persisted_todo_attempt = todo_attempt(event.entity_id)
                if persisted_todo_attempt is not None:
                    payload_layers.append(persisted_todo_attempt)
                    inferred_run_attempt_id = str(
                        persisted_todo_attempt["run_attempt_id"]
                    )
            if inferred_run_attempt_id:
                persisted_run_attempt = run_attempt(inferred_run_attempt_id)
                if persisted_run_attempt is not None:
                    payload_layers.append(
                        {
                            "runAttemptId": inferred_run_attempt_id,
                            "runId": persisted_run_attempt.get("run_id"),
                            "gameId": persisted_run_attempt.get("game_id"),
                            "cadence": persisted_run_attempt.get("cadence"),
                        }
                    )

            def field(*names: str) -> str | None:
                for layer in payload_layers:
                    for name in names:
                        value = layer.get(name)
                        if isinstance(value, (str, int, float, bool)):
                            text = str(value).strip()
                            if text:
                                return text[:1000]
                return None

            batch_id = field("batchId", "batch_id") or (
                event.entity_id if event.entity_type == "batch" else None
            )
            game_id = field("gameId", "game_id")
            run_id = field("runId", "run_id")
            run_attempt_id = (
                field("runAttemptId", "run_attempt_id")
                or inferred_run_attempt_id
            )
            todo_instance_id = field("todoInstanceId", "todo_instance_id") or (
                event.entity_id if event.entity_type == "todo-instance" else None
            )
            todo_attempt_id = field("todoAttemptId", "todo_attempt_id") or (
                event.entity_id if event.entity_type == "todo-attempt" else None
            )
            adapter_event_type = field("eventType", "event_type")
            phase = field("phase", "stage") or adapter_event_type
            observed_state = field(
                "observedState",
                "observed_state",
                "uiState",
                "ui_state",
                "transportOutcome",
                "transport_outcome",
            )
            decision = field("decision", "verdict", "outcome")
            terminal_event = bool(
                adapter_event_type
                and (
                    adapter_event_type.endswith("terminal")
                    or adapter_event_type.endswith("_terminal")
                )
            )
            if decision is None and terminal_event:
                decision = field("status", "state")
            if observed_state is None and not terminal_event:
                observed_state = field("status", "state")
            reason_code = field("reasonCode", "reason_code", "code")
            detail = field("reason", "message", "detail", "note")
            severity_state = (decision or observed_state or "").lower()
            level = (
                "error"
                if (
                    event.event_type.endswith(".failed")
                    or severity_state in {"failed", "crashed", "timeout"}
                )
                else "warning"
                if (
                    "review" in event.event_type
                    or severity_state
                    in {
                        "blocked",
                        "human_required",
                        "review_required",
                        "reconciliation_required",
                    }
                )
                else "info"
            )
            summary = reason_code or decision or observed_state or phase or event.entity_id
            entries.append(
                LogEntry(
                    sequence=event.sequence,
                    log_id=event.event_id,
                    timestamp=event.created_at,
                    level=level,
                    source=(
                        f"adapter:{game_id}"
                        if game_id and event.event_type.startswith("adapter")
                        else f"{event.entity_type}:{game_id}"
                        if game_id
                        else event.entity_type
                    ),
                    message=f"{event.event_type}: {summary}",
                    event_type=event.event_type,
                    entity_type=event.entity_type,
                    entity_id=event.entity_id,
                    batch_id=batch_id,
                    game_id=game_id,
                    run_id=(
                        run_id
                        or (
                            event.entity_id
                            if event.entity_type == "game-run"
                            else None
                        )
                    ),
                    run_attempt_id=run_attempt_id,
                    todo_instance_id=todo_instance_id,
                    todo_attempt_id=todo_attempt_id,
                    phase=phase,
                    observed_state=observed_state,
                    decision=decision,
                    reason_code=reason_code,
                    detail=detail,
                )
            )
        return entries

    def diagnostics(self) -> dict[str, Any]:
        todo = self.todo_overview()
        current_todo_items = self.list_todo_instances(current=True, limit=5000)
        attempt_analysis = self._attempt_analysis(
            todo_items=current_todo_items,
            runs=self.list_game_runs(500),
        )
        difficult = [
            {
                "gameId": game_id,
                **operation,
            }
            for game_id, summary in todo["games"].items()
            for operation in summary["difficultOperations"]
        ]
        return {
            "checkedAt": utc_now().isoformat(),
            "database": {
                "status": self.store.quick_check(),
                "journalMode": "wal",
                "singleWriter": True,
            },
            "legacyImport": {
                "status": self.legacy_report.status,
                "detail": self.legacy_report.detail,
                "allowedGameIds": list(self.adapter.allowed_game_ids),
            },
            "legacyExecution": {
                "enabled": self.settings.legacy_execution_enabled,
                "arbitraryCommandApi": False,
                "arbitraryPathApi": False,
                "arbitraryClickApi": False,
            },
            "adapterHost": self.adapter_host.host_diagnostic(self.list_games()),
            "todo": {
                **todo,
                "difficultOrFailedOperations": difficult,
            },
            "attemptAnalysis": attempt_analysis,
        }

    def config(self) -> ConfigResponse:
        document = self.store.get_config()
        state_version = self.store.latest_event_sequence()
        config_values = dict(document["values"])
        config_values["todo_reset_policy"] = self._todo_reset_policy_document()
        return ConfigResponse(
            version=f"cfg-{state_version}",
            state_version=state_version,
            config=config_values,
            schema_document=ConfigPatchRequest.model_json_schema(by_alias=True),
            allowed_game_ids=document["allowed_game_ids"],
            legacy_sources=document["legacy_sources"],
            updated_at=document["updated_at"],
        )

    def policy(self) -> dict[str, Any]:
        state_version = self.store.latest_event_sequence()
        projection = self.store.get_metadata("legacy.policy_projection", {})
        return {
            "version": f"policy-{state_version}",
            "policy": projection,
            "forbiddenClasses": projection.get("forbiddenActions", []),
            "updatedAt": self.store.get_config().get("updated_at"),
        }

    def events(self, after: int, limit: int) -> EventPage:
        records = [
            EventRecord.model_validate(item)
            for item in self.store.list_events(after=after, limit=limit)
        ]
        next_sequence = records[-1].sequence if records else after
        return EventPage(events=records, next_sequence=next_sequence)

    async def event_stream(self, after: int) -> AsyncIterator[str]:
        cursor = max(0, after)
        heartbeat_deadline = asyncio.get_running_loop().time() + 15.0
        while not self._shutdown_requested.is_set():
            records = [
                EventRecord.model_validate(item)
                for item in self.store.list_events(after=cursor, limit=100)
            ]
            if records:
                state_version = self.store.latest_event_sequence()
                for record in records:
                    cursor = record.sequence
                    payload = record.as_stream_event(state_version)
                    yield (
                        f"id: {record.sequence}\n"
                        f"event: {record.event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                heartbeat_deadline = asyncio.get_running_loop().time() + 15.0
            elif asyncio.get_running_loop().time() >= heartbeat_deadline:
                yield f": keepalive {cursor}\n\n"
                heartbeat_deadline = asyncio.get_running_loop().time() + 15.0
            await asyncio.sleep(0.5)

    def _execution_readiness_for_games(
        self, game_ids: Iterable[str]
    ) -> dict[str, Any]:
        requested_game_ids = list(dict.fromkeys(str(game_id) for game_id in game_ids))
        host = self.adapter_host.probe()
        ready_game_ids: list[str] = []
        unavailable_games: dict[str, dict[str, Any]] = {}
        if host["hostHealthy"] and host["executionGateEnabled"]:
            for game_id in requested_game_ids:
                runtime = self.adapter_host.execution_bindings(game_id)
                if (
                    runtime.get("manifestVerified") is True
                    and runtime.get("status") == "promoted"
                    and bool(runtime.get("bindings"))
                ):
                    ready_game_ids.append(game_id)
                else:
                    unavailable_games[game_id] = {
                        "status": str(runtime.get("status") or "unavailable"),
                        "manifestVerified": bool(runtime.get("manifestVerified")),
                    }
        return {
            "hostHealthy": bool(host["hostHealthy"]),
            "hostStatus": str(host["hostStatus"]),
            "executionGateEnabled": bool(host["executionGateEnabled"]),
            "activeExecution": bool(host["activeExecution"]),
            "requestedGameIds": requested_game_ids,
            "readyGameIds": ready_game_ids,
            "unavailableGames": unavailable_games,
        }

    def _require_execution_ready(self, game_ids: Iterable[str]) -> None:
        if not self.settings.legacy_execution_enabled:
            raise ManagerConflict("legacy execution is disabled by Manager policy")
        readiness = self._execution_readiness_for_games(game_ids)
        if not readiness["hostHealthy"]:
            raise ManagerConflict(
                f"Manager Adapter Host is not healthy: {readiness['hostStatus']}"
            )
        if not readiness["executionGateEnabled"]:
            raise ManagerConflict("Manager execution policy is disabled")
        if readiness["activeExecution"]:
            raise ManagerConflict("Manager Adapter Host already has an active execution")
        if not readiness["readyGameIds"]:
            raise ManagerConflict(
                "no requested game has a promoted per-game execution package"
            )

    def _frozen_todo_snapshot(
        self,
        game_ids: list[str],
        cadence: str,
        *,
        final_run_ids: list[str] | None = None,
        frozen_todo_scope: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        snapshot: dict[str, Any] = {}
        unresolved_required_ids: list[str] = []
        evidence_artifact_ids: list[str] = []
        games_by_id = {game.game_id: game for game in self.list_games()}
        runs_by_game: dict[str, dict[str, Any]] = {}
        for run_id in final_run_ids or []:
            run = self.store.get_game_run(run_id)
            if run["game_id"] not in game_ids or str(run["cadence"]) != cadence:
                raise ManagerValidation(
                    "final GameRun differs from the frozen Todo snapshot scope"
                )
            if run["game_id"] in runs_by_game:
                raise ManagerConflict(
                    "batch completion scope contains duplicate game runs"
                )
            runs_by_game[str(run["game_id"])] = run
        frozen_scope_games: dict[str, dict[str, Any]] = {}
        if isinstance(frozen_todo_scope, dict):
            scope_games = frozen_todo_scope.get("games")
            if isinstance(scope_games, list):
                for value in scope_games:
                    if not isinstance(value, dict):
                        continue
                    game_id = value.get("gameId")
                    if not isinstance(game_id, str) or not game_id:
                        continue
                    if game_id in frozen_scope_games:
                        raise ManagerConflict(
                            "batch completion Todo scope contains duplicate games"
                        )
                    frozen_scope_games[game_id] = value
        for game_id in game_ids:
            run = runs_by_game.get(game_id)
            frozen_game_scope = frozen_scope_games.get(game_id)
            frozen_ids = (
                [
                    str(value)
                    for value in frozen_game_scope.get(
                        "completionTodoInstanceIds", []
                    )
                    if isinstance(value, str) and value
                ]
                if frozen_game_scope is not None
                else []
            )
            if len(frozen_ids) != len(set(frozen_ids)):
                raise ManagerConflict(
                    "batch completion Todo scope contains duplicate Todo IDs"
                )
            if frozen_ids:
                items = [self.get_todo_instance(todo_id) for todo_id in frozen_ids]
                if run is not None and set(frozen_ids) != set(
                    run.get("completion_todo_instance_ids", [])
                ):
                    raise ManagerConflict(
                        "GameRun completion scope differs from its Batch scope"
                    )
                calculated_scope = self._todo_summary_from_items(items, cadence)
                if any(
                    frozen_game_scope.get(key) != calculated_scope[key]
                    for key in ("scopeKey", "scopeFingerprint")
                ) or set(frozen_game_scope.get("periodKeys", [])) != set(
                    calculated_scope["periodKeys"]
                ):
                    raise ManagerConflict(
                        "batch completion Todo scope fingerprint is inconsistent"
                    )
                scope_integrity = "frozen_batch_scope"
            elif run is not None:
                items = self._frozen_todos_for_run(run)
                scope_integrity = (
                    "frozen_game_run_scope"
                    if int(run.get("completion_scope_version", 0)) == 1
                    else "legacy_union_fail_closed"
                )
            else:
                # Never consult current=True during seal. A legacy Batch with no
                # frozen scope remains reviewable as an empty, fail-closed fact.
                items = []
                scope_integrity = "missing_frozen_scope"
            if any(
                item.game_id != game_id or str(item.cadence) != cadence
                for item in items
            ):
                raise ManagerConflict("frozen Batch Todo scope is inconsistent")
            summary = self._todo_summary_from_items(items, cadence)
            summary = {**summary, "completionScopeIntegrity": scope_integrity}
            assessments = self._latest_automation_assessments(
                {item.todo_instance_id for item in items}
            )
            game = games_by_id.get(game_id)
            if game is not None:
                summary = {
                    **summary,
                    "displayName": game.display_name,
                    "orderIndex": game.order_index,
                }
            unresolved_required_ids.extend(summary["unresolvedRequiredTodoIds"])
            instances: list[dict[str, Any]] = []
            for item in items:
                evidence_artifact_ids.extend(item.evidence_refs)
                operational_context = self._todo_operational_context(
                    item,
                    assessments=assessments,
                    include_execution_facts=False,
                )
                instances.append(
                    {
                        "todoInstanceId": item.todo_instance_id,
                        "todoDefinitionId": item.todo_definition_id,
                        "definitionVersion": item.definition_version,
                        "catalogVersion": item.catalog_version,
                        "sourceHash": item.source_hash,
                        "operation": item.operation,
                        "title": item.title,
                        "category": item.category,
                        "orderIndex": item.order_index,
                        "required": item.required,
                        "risk": item.risk,
                        "status": item.status,
                        "attempts": item.attempts,
                        "reason": item.reason,
                        "evidenceRefs": list(item.evidence_refs),
                        "automationDifficulty": item.automation_difficulty,
                        "automationState": item.automation_state,
                        "periodKey": item.period_key,
                        "periodStartsAt": item.period_starts_at.isoformat(),
                        "periodEndsAt": item.period_ends_at.isoformat(),
                        "resetRule": dict(item.reset_rule),
                        "sourceRefs": list(item.source_refs),
                        "completedAt": (
                            item.completed_at.isoformat()
                            if item.completed_at is not None
                            else None
                        ),
                        **operational_context,
                    }
                )
            snapshot[game_id] = {"summary": summary, "instances": instances}
        return (
            snapshot,
            list(dict.fromkeys(unresolved_required_ids)),
            list(dict.fromkeys(evidence_artifact_ids)),
        )

    @staticmethod
    def _contract_datetime(value: Any) -> datetime:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ManagerValidation("completion contract timestamps must be timezone-aware")
        return parsed

    def _frozen_todos_for_run(
        self, run: dict[str, Any]
    ) -> list[TodoInstanceRecord]:
        """Load the Todo instances frozen onto a GameRun.

        Current wall-clock Todo instances are deliberately not substituted for
        a run that crossed a reset while waiting for Agent review.
        """

        if int(run.get("completion_scope_version", 0)) == 1:
            todo_ids = [
                str(value)
                for value in run.get("completion_todo_instance_ids", [])
            ]
        else:
            # Compatibility is read-only and fail-closed: recover the IDs that
            # old rows did persist, but never substitute a new wall-clock scope.
            todo_ids = list(
                dict.fromkeys(
                    str(value)
                    for value in (
                        *run.get("todo_instance_ids", []),
                        *run.get("completed_todo_instance_ids", []),
                    )
                )
            )
        if len(todo_ids) != len(set(todo_ids)):
            raise ManagerConflict("GameRun contains duplicate Todo instance IDs")
        items = [self.get_todo_instance(todo_id) for todo_id in todo_ids]
        if any(
            item.game_id != run["game_id"]
            or str(item.cadence) != str(run["cadence"])
            for item in items
        ):
            raise ManagerConflict("GameRun frozen Todo scope is inconsistent")
        return sorted(items, key=lambda item: (item.order_index, item.todo_instance_id))

    def _completion_policy_context(
        self,
        *,
        run: dict[str, Any],
        snapshot: CompletionContractSnapshot,
        todos: list[TodoInstanceRecord] | None = None,
    ) -> tuple[GameCompletionPolicy, CompletionPolicyRegistry, str, str | None]:
        """Bind completion predicates to the run's frozen reset window."""

        frozen = todos if todos is not None else self._frozen_todos_for_run(run)
        required = [item for item in frozen if item.required]
        sources = required or frozen
        unsupported_reasons: list[str] = []
        if int(run.get("completion_scope_version", 0)) != 1:
            unsupported_reasons.append("legacy_or_missing_completion_scope")
        if not required:
            unsupported_reasons.append("no_required_frozen_todos")
        period_shapes = {
            (
                item.period_key,
                item.period_starts_at,
                item.period_ends_at,
            )
            for item in sources
        }
        if len(period_shapes) != 1:
            unsupported_reasons.append("frozen_todos_do_not_share_one_period")
        reset_shapes = {
            json.dumps(item.reset_rule, ensure_ascii=False, sort_keys=True)
            for item in sources
        }
        if len(reset_shapes) != 1:
            unsupported_reasons.append("frozen_todos_do_not_share_one_reset_rule")

        reset_rule = dict(sources[0].reset_rule) if sources else {}
        timezone_name = str(reset_rule.get("timezone") or "Asia/Shanghai")
        reset_text = str(reset_rule.get("time") or "04:00")
        try:
            reset_local_time = datetime.strptime(reset_text, "%H:%M").time()
        except ValueError:
            unsupported_reasons.append("invalid_frozen_reset_time")
            reset_local_time = datetime.strptime("04:00", "%H:%M").time()
        if str(reset_rule.get("cadence") or run["cadence"]) != str(run["cadence"]):
            unsupported_reasons.append("frozen_reset_cadence_mismatch")
        offset = snapshot.game_day.starts_at.utcoffset()
        if offset is None:
            unsupported_reasons.append("frozen_period_has_no_utc_offset")
            offset_minutes = 0
        else:
            offset_minutes = int(offset.total_seconds() // 60)
        policy = policy_for_frozen_game_day(
            game_id=str(run["game_id"]),
            cadence=str(run["cadence"]),
            timezone_name=timezone_name,
            reset_utc_offset_minutes=offset_minutes,
            reset_local_time=reset_local_time,
        )
        status = "unsupported" if unsupported_reasons else "supported"
        reason = ";".join(unsupported_reasons) or None
        return policy, CompletionPolicyRegistry((policy,)), status, reason

    def _completion_contract_snapshot(
        self, run_id: str
    ) -> CompletionContractSnapshot:
        """Project only Manager-ledger facts for one GameRun adjudication."""

        run = self.store.get_game_run(run_id)
        current_todos = self._frozen_todos_for_run(run)
        window_source = next(
            (item for item in current_todos if item.required),
            current_todos[0] if current_todos else None,
        )
        if window_source is None:
            fallback = todo_period(
                {
                    "cadence": run["cadence"],
                    "timezone": "Asia/Shanghai",
                    "time": "04:00",
                    "weekStartDay": "Monday",
                }
            )
            game_day = GameDayWindow(
                period_key=fallback["periodKey"],
                starts_at=self._contract_datetime(fallback["startsAt"]),
                ends_at=self._contract_datetime(fallback["endsAt"]),
            )
        else:
            game_day = GameDayWindow(
                period_key=window_source.period_key,
                starts_at=window_source.period_starts_at,
                ends_at=window_source.period_ends_at,
            )

        attempts = self.store.list_run_attempts(run_id=run_id, limit=1000)
        current_attempt = attempts[0] if attempts else None
        completion_attempts = {
            str(attempt["run_attempt_id"]): attempt for attempt in attempts
        }

        attempts_by_todo: dict[str, dict[str, Any]] = {}
        for lineage_attempt in attempts:
            for todo_attempt in self.store.list_todo_attempts(
                run_attempt_id=lineage_attempt["run_attempt_id"], limit=5000
            ):
                attempts_by_todo.setdefault(
                    str(todo_attempt["todo_instance_id"]), todo_attempt
                )

        # A fresh batch deliberately skips immutable Todo facts already
        # completed earlier in the same GameDay.  Freeze the exact owning
        # TodoAttempt and RunAttempt into this completion lineage instead of
        # replaying a one-time claim or stamina-spending action.  Every boundary
        # below fails closed: same TodoInstance (therefore same period), same
        # game/cadence, terminal completed attempt, evidence present, and both
        # attempt timestamps inside the current GameDay.
        for todo in current_todos:
            if (
                todo.status != "completed"
                or todo.todo_instance_id in attempts_by_todo
            ):
                continue
            for candidate in self.store.list_todo_attempts(
                todo_instance_id=todo.todo_instance_id, limit=1000
            ):
                if (
                    candidate.get("state") != "completed"
                    or not candidate.get("completed_at")
                    or not candidate.get("evidence_refs")
                ):
                    continue
                try:
                    owner_attempt = self.store.get_run_attempt(
                        str(candidate["run_attempt_id"])
                    )
                    owner_started_at = self._contract_datetime(
                        owner_attempt["started_at"]
                    )
                    owner_completed_at = (
                        self._contract_datetime(owner_attempt["completed_at"])
                        if owner_attempt.get("completed_at")
                        else None
                    )
                    todo_completed_at = self._contract_datetime(
                        candidate["completed_at"]
                    )
                except (KeyError, RecordNotFound, TypeError, ValueError):
                    continue
                if (
                    owner_attempt.get("game_id") != run["game_id"]
                    or owner_attempt.get("cadence") != run["cadence"]
                    or owner_attempt.get("state")
                    in {"starting", "running", "cancelling"}
                    or not (
                        game_day.starts_at <= owner_started_at < game_day.ends_at
                    )
                    or owner_completed_at is None
                    or not (
                        game_day.starts_at
                        <= owner_completed_at
                        < game_day.ends_at
                    )
                    or not (
                        game_day.starts_at
                        <= todo_completed_at
                        < game_day.ends_at
                    )
                ):
                    continue
                attempts_by_todo[todo.todo_instance_id] = candidate
                completion_attempts.setdefault(
                    str(owner_attempt["run_attempt_id"]), owner_attempt
                )
                break

        ordered_attempts = [
            *attempts,
            *(
                attempt
                for attempt_id, attempt in completion_attempts.items()
                if attempt_id
                not in {str(current["run_attempt_id"]) for current in attempts}
            ),
        ]
        attempt_facts = tuple(
            RunAttemptCompletionFact(
                run_attempt_id=attempt["run_attempt_id"],
                run_id=attempt["run_id"],
                game_id=attempt["game_id"],
                cadence=attempt["cadence"],
                state=attempt["state"],
                started_at=self._contract_datetime(attempt["started_at"]),
                completed_at=(
                    self._contract_datetime(attempt["completed_at"])
                    if attempt.get("completed_at")
                    else None
                ),
            )
            for attempt in ordered_attempts
        )
        attempt_fact = attempt_facts[0] if attempt_facts else None

        todo_facts: list[TodoCompletionFact] = []
        for todo in current_todos:
            todo_attempt = attempts_by_todo.get(todo.todo_instance_id)
            if todo_attempt is None:
                todo_facts.append(
                    TodoCompletionFact(
                        todo_instance_id=todo.todo_instance_id,
                        game_id=todo.game_id,
                        game_day_key=todo.period_key,
                        required=todo.required,
                        status=todo.status,
                        run_id=todo.run_id,
                        run_attempt_id=None,
                        completed_at=todo.completed_at,
                        evidence_refs=tuple(todo.evidence_refs),
                        reason=todo.reason,
                    )
                )
                continue
            todo_facts.append(
                TodoCompletionFact(
                    todo_instance_id=todo.todo_instance_id,
                    game_id=todo.game_id,
                    game_day_key=todo.period_key,
                    required=todo.required,
                    status=todo_attempt["state"],
                    run_id=completion_attempts[
                        str(todo_attempt["run_attempt_id"])
                    ]["run_id"],
                    run_attempt_id=todo_attempt["run_attempt_id"],
                    completed_at=(
                        self._contract_datetime(todo_attempt["completed_at"])
                        if todo_attempt.get("completed_at")
                        else None
                    ),
                    evidence_refs=tuple(todo_attempt.get("evidence_refs", [])),
                    reason_code=str(todo_attempt.get("reason_code") or ""),
                    reason=str(todo_attempt.get("reason") or ""),
                )
            )

        review_resource = next(
            (
                item
                for item in self.store.list_resources("completion-review", 1000)
                if item["document"].get("runId") == run_id
                and current_attempt is not None
                and item["document"].get("runAttemptId")
                == current_attempt["run_attempt_id"]
                and item["document"].get("gameDayKey") == game_day.period_key
            ),
            None,
        )
        review_fact = None
        review_artifact_refs: list[str] = []
        if review_resource is not None:
            review_record = self._completion_review_record(review_resource)
            review_artifact_refs.extend(review_record.artifact_refs)
            review_fact = AgentReviewFact(
                review_id=review_record.completion_review_id,
                reviewer_principal_id=review_record.reviewer_principal_id,
                decision=review_record.decision,
                game_id=review_record.game_id,
                run_id=review_record.run_id,
                run_attempt_id=review_record.run_attempt_id,
                game_day_key=review_record.game_day_key,
                reviewed_at=review_record.reviewed_at,
                artifact_refs=tuple(review_record.artifact_refs),
                observations=tuple(
                    AgentPredicateObservation(
                        predicate_id=item.predicate_id,
                        metrics=item.metrics,
                        artifact_refs=tuple(item.artifact_refs),
                    )
                    for item in review_record.predicates
                ),
                todo_reviews=tuple(
                    AgentTodoReview(
                        todo_instance_id=item.todo_instance_id,
                        verdict=item.verdict,
                        reason_code=item.reason_code,
                        artifact_refs=tuple(item.artifact_refs),
                    )
                    for item in review_record.todo_reviews
                ),
            )

        artifact_ids = list(
            dict.fromkeys(
                [
                    artifact_id
                    for todo in todo_facts
                    for artifact_id in todo.evidence_refs
                ]
                + review_artifact_refs
            )
        )
        evidence_facts: list[EvidenceArtifactFact] = []
        for artifact_id in artifact_ids:
            try:
                resource = self.store.get_resource("artifact", artifact_id)
                document = dict(resource["document"])
                integrity = self._completion_artifact_integrity(
                    artifact_id, document
                )
                owner = self.store.get_todo_instance(
                    str(document["todoInstanceId"])
                )
                evidence_facts.append(
                    EvidenceArtifactFact(
                        artifact_id=artifact_id,
                        kind=str(document["kind"]),
                        content_type=str(document["contentType"]),
                        captured_at=self._contract_datetime(document["capturedAt"]),
                        source=str(document.get("source") or ""),
                        raw=document.get("raw") is True,
                        game_id=str(document["gameId"]),
                        run_id=str(document["runId"]),
                        run_attempt_id=str(document["runAttemptId"]),
                        todo_instance_id=str(document["todoInstanceId"]),
                        game_day_key=str(owner["period_key"]),
                        content_hash=integrity.content_hash,
                        integrity_valid=integrity.valid,
                        integrity_reason_code=integrity.reason_code,
                    )
                )
            except (KeyError, RecordNotFound, TypeError, ValueError):
                # The opaque reference remains on the Todo/review fact.  The pure
                # adjudicator then reports the evidence predicate as missing.
                continue

        blockers: list[CompletionBlockerFact] = []
        if current_attempt is not None:
            persistent_by_todo = {
                str(item["todo_instance_id"]): item
                for item in self.store.list_todo_blockers(
                    run_id=run_id, active_only=True, limit=5000
                )
                if item["game_day_key"] == game_day.period_key
            }
            blocker_kind_map = {
                "human_required": BlockerKind.HUMAN_REQUIRED,
                "safety_gate": BlockerKind.SAFETY_GATE,
                "contract_invariant": BlockerKind.CONTRACT_INVARIANT,
            }
            for item in persistent_by_todo.values():
                blockers.append(
                    CompletionBlockerFact(
                        blocker_id=item["blocker_id"],
                        kind=blocker_kind_map.get(
                            str(item["kind"]), BlockerKind.BLOCKED
                        ),
                        code=str(item["code"]),
                        message=str(item["reason"]),
                        game_id=run["game_id"],
                        run_id=run_id,
                        run_attempt_id=item["run_attempt_id"],
                        game_day_key=game_day.period_key,
                        todo_instance_id=item["todo_instance_id"],
                        artifact_refs=tuple(item["artifact_refs"]),
                    )
                )
            for todo in current_todos:
                if str(todo.status) not in {"blocked", "human_required"}:
                    continue
                if todo.todo_instance_id in persistent_by_todo:
                    continue
                blockers.append(
                    CompletionBlockerFact(
                        blocker_id=f"todo-blocker:{todo.todo_instance_id}",
                        kind=(
                            BlockerKind.HUMAN_REQUIRED
                            if str(todo.status) == "human_required"
                            else BlockerKind.BLOCKED
                        ),
                        code=f"todo_{todo.status}",
                        message=todo.reason or f"Todo is {todo.status}",
                        game_id=run["game_id"],
                        run_id=run_id,
                        run_attempt_id=current_attempt["run_attempt_id"],
                        game_day_key=game_day.period_key,
                        todo_instance_id=todo.todo_instance_id,
                        artifact_refs=tuple(todo.evidence_refs),
                    )
                )
            latest_control = next(
                (
                    item
                    for item in self.store.list_resources("run-control-request", 1000)
                    if item["document"].get("runId") == run_id
                ),
                None,
            )
            if (
                latest_control is not None
                and latest_control["state"] == "active"
                and latest_control["document"].get("action") == "takeover"
            ):
                blockers.append(
                    CompletionBlockerFact(
                        blocker_id=latest_control["resource_id"],
                        kind=BlockerKind.HUMAN_REQUIRED,
                        code="active_human_takeover",
                        message=str(
                            latest_control["document"].get("reason")
                            or "A human takeover is active."
                        ),
                        game_id=run["game_id"],
                        run_id=run_id,
                        run_attempt_id=current_attempt["run_attempt_id"],
                        game_day_key=game_day.period_key,
                    )
                )

        return CompletionContractSnapshot(
            game_id=run["game_id"],
            run_id=run_id,
            cadence=run["cadence"],
            game_day=game_day,
            current_attempt=attempt_fact,
            attempt_lineage=attempt_facts,
            todos=tuple(todo_facts),
            evidence=tuple(evidence_facts),
            agent_review=review_fact,
            blockers=tuple(blockers),
        )

    def _persist_completion_adjudication(
        self,
        *,
        batch_id: str,
        decision: CompletionContractDecision,
    ) -> CompletionAdjudicationRecord:
        contract = decision.model_dump(mode="json", by_alias=True)
        identity_hash = hashlib.sha256(
            json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        resource_id = "completion-adjudication-" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "/".join(
                    (
                        "yeyu-gamer",
                        "completion-adjudication",
                        batch_id,
                        decision.run_id,
                        decision.run_attempt_id or "no-attempt",
                        decision.game_day_key,
                        identity_hash,
                    )
                ),
            )
        )
        document = {
            "schemaVersion": 1,
            "batchId": batch_id,
            "gameId": decision.game_id,
            "runId": decision.run_id,
            "runAttemptId": decision.run_attempt_id,
            "gameDayKey": decision.game_day_key,
            "contract": contract,
        }
        try:
            resource = self.store.get_resource(
                "completion-adjudication", resource_id
            )
            if resource["document"] != document:
                raise ManagerConflict("immutable completion adjudication diverged")
        except RecordNotFound:
            resource = self.store.create_resource(
                "completion-adjudication",
                resource_id=resource_id,
                state=str(decision.outcome),
                document=document,
            )
        return self._completion_adjudication_record(resource)

    # Screenshot kinds in the order the round mail prefers them.  Reward/claim
    # frames prove completion; watermarked step frames show the last scene a
    # failed or unreviewed run reached; raw pre-step frames give the "before"
    # context.  Text logs are never mail attachments.
    MAIL_SCREENSHOT_KIND_PRIORITY: tuple[str, ...] = (
        "game-ui-claimed-reward",
        "game-ui-daily-reward-watermarked",
        "game-ui-daily-reward-raw",
        "game-ui-reward-screen",
        "game-ui-daily-training-panel",
        "game-ui-daily-task-list",
        "game-ui-step-after-watermarked",
        "game-ui-daily-reward-before",
        "game-ui-step-before-raw",
        "game-ui-main-window",
        "reward-screenshot",
        "window-screenshot",
        "screenshot",
        "raw-frame",
    )
    MAIL_SCREENSHOTS_PER_GAME = 3

    def _mail_screenshot_selection(
        self, decision: CompletionContractDecision
    ) -> list[str]:
        """Pick the few current-run frames that tell this game's story in mail.

        Accepted evidence (the completion proof) always comes first.  When the
        run did not reach ``accepted_done``, the newest frames of the current
        attempt are added so the reader sees the daily progress panel and the
        failure scene instead of a text-only line.  Nothing is fabricated: only
        artifacts the adjudication already lists as current-run screenshots
        are eligible, and the seal resolver re-validates each one.
        """

        documents: dict[str, dict[str, Any]] = {}
        for artifact_id in decision.screenshot_artifact_refs:
            try:
                documents[artifact_id] = dict(
                    self.store.get_resource("artifact", artifact_id).get("document", {})
                )
            except RecordNotFound:
                continue

        def rank(artifact_id: str) -> tuple[int, str]:
            document = documents.get(artifact_id, {})
            kind = str(document.get("kind", ""))
            priority = (
                self.MAIL_SCREENSHOT_KIND_PRIORITY.index(kind)
                if kind in self.MAIL_SCREENSHOT_KIND_PRIORITY
                else len(self.MAIL_SCREENSHOT_KIND_PRIORITY)
            )
            # Newer frames first inside the same kind.
            captured = str(document.get("capturedAt", ""))
            return (priority, "".join(chr(0x10FFFF - ord(char)) for char in captured))

        accepted = [
            artifact_id
            for artifact_id in decision.screenshot_artifact_refs
            if artifact_id in decision.accepted_evidence_refs
            and artifact_id in documents
        ]
        current_attempt = [
            artifact_id
            for artifact_id in decision.screenshot_artifact_refs
            if artifact_id in documents
            and documents[artifact_id].get("runAttemptId") == decision.run_attempt_id
        ]
        selected: list[str] = []
        if decision.accepted_done and accepted:
            pool = sorted(accepted, key=rank)
        else:
            # Failure/review story: the newest "after" frame (failure scene),
            # then the newest "before" frame (progress panel), then accepted
            # proof frames if any exist.
            after = sorted(
                (
                    artifact_id
                    for artifact_id in current_attempt
                    if str(documents[artifact_id].get("kind", "")).endswith("after-watermarked")
                ),
                key=rank,
            )
            before = sorted(
                (
                    artifact_id
                    for artifact_id in current_attempt
                    if str(documents[artifact_id].get("kind", "")).endswith("before-raw")
                ),
                key=rank,
            )
            pool = [*after[:1], *before[:1], *sorted(accepted, key=rank), *after[1:], *sorted(current_attempt, key=rank)]
        for artifact_id in pool:
            if artifact_id not in selected:
                selected.append(artifact_id)
            if len(selected) >= self.MAIL_SCREENSHOTS_PER_GAME:
                break
        return selected

    def _mail_screenshot_caption(
        self, decision: CompletionContractDecision, artifact_id: str
    ) -> dict[str, Any]:
        try:
            document = dict(
                self.store.get_resource("artifact", artifact_id).get("document", {})
            )
        except RecordNotFound:
            document = {}
        kind = str(document.get("kind", ""))
        if kind in {"game-ui-claimed-reward", "game-ui-daily-reward-watermarked", "game-ui-daily-reward-raw", "game-ui-reward-screen"}:
            role = "领取证据"
        elif kind in {"game-ui-daily-training-panel", "game-ui-daily-task-list"}:
            role = "每日面板"
        elif kind.endswith("after-watermarked"):
            role = "步骤结束画面（水印）"
        elif kind.endswith("before-raw") or kind == "game-ui-daily-reward-before":
            role = "步骤开始画面（原始）"
        else:
            role = "现场画面"
        return {
            "artifactId": artifact_id,
            "kind": kind,
            "role": role,
            "operation": document.get("operation"),
            "todoInstanceId": document.get("todoInstanceId"),
            "capturedAt": document.get("capturedAt"),
            "accepted": artifact_id in decision.accepted_evidence_refs,
        }

    def _seal_batch_from_todos(
        self,
        *,
        batch_id: str,
        initial_result: dict[str, Any],
        game_ids: list[str],
        cadence: str,
        state: str,
        completed_run_ids: list[str] | None = None,
        failed_run_ids: list[str] | None = None,
        final_run_ids: list[str] | None = None,
        reason: str,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        existing = self.store.get_batch(batch_id)
        if existing.get("result", {}).get("sealVersion") is not None:
            return existing
        frozen_candidate_ids = [
            str(value)
            for value in initial_result.get(
                "candidateGameIds",
                existing.get("result", {}).get("candidateGameIds", game_ids),
            )
            if isinstance(value, str) and value
        ]
        if not frozen_candidate_ids or len(frozen_candidate_ids) != len(
            set(frozen_candidate_ids)
        ):
            raise ManagerConflict(
                "Batch seal requires unique frozen candidateGameIds"
            )
        if set(game_ids) != set(frozen_candidate_ids):
            raise ManagerConflict(
                "Batch seal game scope differs from frozen candidateGameIds"
            )
        game_ids = frozen_candidate_ids
        frozen_scope_value = initial_result.get(
            "todoScope", existing.get("result", {}).get("todoScope")
        )
        frozen_todo_scope = (
            dict(frozen_scope_value)
            if isinstance(frozen_scope_value, dict)
            else None
        )
        todo_snapshot, unresolved_ids, evidence_ids = self._frozen_todo_snapshot(
            game_ids,
            cadence,
            final_run_ids=list(final_run_ids or []),
            frozen_todo_scope=frozen_todo_scope,
        )
        completion_contracts: list[dict[str, Any]] = []
        decisions: list[CompletionContractDecision] = []
        accepted_screenshot_candidates: list[tuple[str, str]] = []
        mail_screenshots_by_game: dict[str, list[dict[str, Any]]] = {}
        for run_id in final_run_ids or []:
            run = self.store.get_game_run(run_id)
            if run["game_id"] not in game_ids or run["cadence"] != cadence:
                raise ManagerValidation(
                    "final GameRun differs from the batch completion scope"
                )
            contract_snapshot = self._completion_contract_snapshot(run_id)
            _, run_policies, policy_status, _ = self._completion_policy_context(
                run=run,
                snapshot=contract_snapshot,
                todos=self._frozen_todos_for_run(run),
            )
            decision = adjudicate_completion(
                contract_snapshot,
                policies=(
                    run_policies
                    if policy_status == "supported"
                    else CompletionPolicyRegistry(())
                ),
            )
            decisions.append(decision)
            adjudication = self._persist_completion_adjudication(
                batch_id=batch_id, decision=decision
            )
            completion_contracts.append(
                {
                    "completionAdjudicationId": (
                        adjudication.completion_adjudication_id
                    ),
                    **decision.model_dump(mode="json", by_alias=True),
                }
            )
            accepted_screenshot_candidates.extend(
                (decision.game_id, artifact_id)
                for artifact_id in self._mail_screenshot_selection(decision)
            )
            mail_screenshots_by_game[decision.game_id] = [
                self._mail_screenshot_caption(decision, artifact_id)
                for artifact_id in self._mail_screenshot_selection(decision)
            ]
        if len({decision.game_id for decision in decisions}) != len(decisions):
            raise ManagerConflict(
                "Batch seal contains duplicate per-game completion contracts"
            )
        decisions_by_game = {decision.game_id: decision for decision in decisions}
        unresolved_by_game = {
            game_id: list(
                todo_snapshot.get(game_id, {})
                .get("summary", {})
                .get("unresolvedRequiredTodoIds", [])
            )
            for game_id in game_ids
        }
        scope_integrity_by_game = {
            game_id: str(
                todo_snapshot.get(game_id, {})
                .get("summary", {})
                .get("completionScopeIntegrity", "missing_frozen_scope")
            )
            for game_id in game_ids
        }
        deferred_game_ids = {
            str(value)
            for value in initial_result.get("deferredGameIds", [])
            if isinstance(value, str)
        }
        failed_ids = set(failed_run_ids or [])
        not_started_ids = {
            str(value)
            for value in initial_result.get("notStartedRunIds", [])
            if isinstance(value, str)
        }
        not_started_games = {
            str(member.get("gameId"))
            for member in initial_result.get("notStartedMembers", [])
            if isinstance(member, dict)
            and isinstance(member.get("gameId"), str)
            and member.get("gameId")
            and isinstance(member.get("runId"), str)
            and member.get("runId") in not_started_ids
        }
        completed_ids = set(completed_run_ids or [])
        contract_game_ids = set(decisions_by_game)
        candidate_game_ids = set(game_ids)
        full_contract_coverage = contract_game_ids == candidate_game_ids
        run_outcomes_complete = all(
            decision.run_id in completed_ids
            and decision.run_id not in failed_ids
            and decision.run_id not in not_started_ids
            for decision in decisions
        )
        scopes_complete = all(
            scope_integrity_by_game[game_id]
            in {"frozen_batch_scope", "frozen_game_run_scope"}
            for game_id in game_ids
        )
        accepted_done = (
            state != str(EntityState.CANCELLED)
            and full_contract_coverage
            and bool(decisions)
            and all(decision.accepted_done for decision in decisions)
            and not unresolved_ids
            and not deferred_game_ids
            and not failed_ids
            and not not_started_ids
            and run_outcomes_complete
            and scopes_complete
        )
        effective_state = str(EntityState.DONE) if accepted_done else state
        lineage_document = dict(existing["result"].get("batchLineage", {}))
        notification_blockers: list[dict[str, Any]] = []
        preflight_at = utc_now()
        allowed_runs = set(final_run_ids or [])
        notification_screenshot_decisions: list[dict[str, Any]] = []
        allowed_screenshots_by_game: dict[str, list[str]] = {
            game_id: [] for game_id in game_ids
        }
        for game_id, artifact_id in accepted_screenshot_candidates:
            artifact_decision = (
                self.notification_dispatcher.artifacts.preflight_reference(
                    artifact_id,
                    allowed_games=candidate_game_ids,
                    allowed_runs=allowed_runs,
                    started_at=self._contract_datetime(existing["created_at"]),
                    sealed_at=preflight_at,
                )
            )
            notification_screenshot_decisions.append(
                {
                    "artifactId": artifact_id,
                    "gameId": game_id,
                    "accepted": artifact_decision.accepted,
                    "reasonCode": artifact_decision.reason,
                }
            )
            if artifact_decision.accepted:
                allowed_screenshots_by_game.setdefault(game_id, []).append(
                    artifact_id
                )
        accepted_screenshots = list(
            dict.fromkeys(
                artifact_id
                for game_id in game_ids
                for artifact_id in allowed_screenshots_by_game.get(game_id, [])
            )
        )
        completion_coverage: list[dict[str, Any]] = []
        for game_id in game_ids:
            decision = decisions_by_game.get(game_id)
            game_failed = bool(decision and decision.run_id in failed_ids)
            game_not_started = game_id in not_started_games or bool(
                decision and decision.run_id in not_started_ids
            )
            game_completed = bool(decision and decision.run_id in completed_ids)
            issues = [
                *(["missing_completion_contract"] if decision is None else []),
                *(
                    ["unresolved_required_todos"]
                    if unresolved_by_game[game_id]
                    else []
                ),
                *(["deferred_execution"] if game_id in deferred_game_ids else []),
                *(["failed_run"] if game_failed else []),
                *(["not_started_run"] if game_not_started else []),
                *(
                    ["run_not_completed"]
                    if decision is not None and not game_completed
                    else []
                ),
                *(
                    ["completion_scope_not_frozen"]
                    if scope_integrity_by_game[game_id]
                    not in {"frozen_batch_scope", "frozen_game_run_scope"}
                    else []
                ),
                *(
                    ["completion_contract_not_accepted"]
                    if decision is not None and not decision.accepted_done
                    else []
                ),
            ]
            completion_coverage.append(
                {
                    "gameId": game_id,
                    "runId": decision.run_id if decision is not None else None,
                    "completionAdjudicationId": next(
                        (
                            item["completionAdjudicationId"]
                            for item in completion_contracts
                            if item.get("gameId") == game_id
                        ),
                        None,
                    ),
                    "scopeIntegrity": scope_integrity_by_game[game_id],
                    "unresolvedRequiredTodoIds": unresolved_by_game[game_id],
                    "issues": issues,
                    "acceptedDone": not issues,
                }
            )
        if not accepted_done:
            for game_id in game_ids:
                decision = decisions_by_game.get(game_id)
                has_screenshot = bool(allowed_screenshots_by_game.get(game_id))
                rejected_reasons = sorted(
                    {
                        str(item["reasonCode"])
                        for item in notification_screenshot_decisions
                        if item["gameId"] == game_id and not item["accepted"]
                    }
                )
                has_accepted_screenshot_candidate = any(
                    candidate_game_id == game_id
                    for candidate_game_id, _ in accepted_screenshot_candidates
                )
                unavailable_reason_code = (
                    "no_completion_contract"
                    if decision is None
                    else "no_accepted_screenshot"
                    if not has_accepted_screenshot_candidate
                    else "accepted_screenshot_excluded"
                )
                unavailable_reason = (
                    "no immutable completion contract exists for this frozen candidate"
                    if decision is None
                    else "the completion contract contains no accepted screenshot"
                    if not has_accepted_screenshot_candidate
                    else "accepted screenshot was excluded from the final mail allowlist"
                    + (
                        f" ({', '.join(rejected_reasons)})"
                        if rejected_reasons
                        else ""
                    )
                )
                notification_blockers.append(
                    {
                        "gameId": game_id,
                        "kind": (
                            "completion_contract"
                            if decision is not None
                            else "execution_not_started"
                        ),
                        "reason": (
                            decision.message
                            if decision is not None
                            else reason
                        ),
                        "nextAction": (
                            "Resolve the typed Todo/evidence blocker, then request "
                            "a same-GameRun resume through Manager."
                        ),
                        **(
                            {}
                            if has_screenshot
                            else {
                                "screenshotUnavailableReasonCode": (
                                    unavailable_reason_code
                                ),
                                "screenshotUnavailableReason": unavailable_reason,
                            }
                        ),
                        "batchLineage": lineage_document,
                    }
                )
        frozen = {
            **initial_result,
            "currentGameId": None,
            "completedRunIds": list(completed_run_ids or []),
            "failedRunIds": list(failed_run_ids or []),
            "finalGameRunIds": list(final_run_ids or []),
            "todoSnapshot": todo_snapshot,
            "unresolvedRequiredTodoIds": unresolved_ids,
            "sealEvidenceArtifactIds": list(
                accepted_screenshots
            ),
            "mailScreenshotsByGame": {
                game_id: [
                    item
                    for item in mail_screenshots_by_game.get(game_id, [])
                    if item.get("artifactId") in accepted_screenshots
                ]
                for game_id in game_ids
            },
            "allTodoEvidenceArtifactIds": list(dict.fromkeys(evidence_ids)),
            "timedOutWithProcessPreserved": timed_out,
            "completionContracts": completion_contracts,
            "completionCoverage": completion_coverage,
            "notificationScreenshotDecisions": (
                notification_screenshot_decisions
            ),
            "attemptLineages": {
                decision.run_id: list(decision.attempt_lineage_ids)
                for decision in decisions
            },
            "currentAttemptEvidenceArtifactIds": list(
                dict.fromkeys(
                    artifact_id
                    for decision in decisions
                    for artifact_id in decision.current_attempt_evidence_refs
                )
            ),
            "carriedEvidenceArtifactIds": list(
                dict.fromkeys(
                    artifact_id
                    for decision in decisions
                    for artifact_id in decision.carried_evidence_refs
                )
            ),
            "acceptedDone": accepted_done,
            "notificationOutcome": (
                "completed" if accepted_done else "blocked"
            ),
            "notificationBlockers": notification_blockers,
            "acceptanceReason": (
                "; ".join(decision.message for decision in decisions)
                if decisions
                else reason
            ),
        }
        return self.store.seal_batch(
            batch_id,
            state=effective_state,
            result=frozen,
            notification_draft_factory=lambda seal_version, sealed, policy: (
                self.notification_dispatcher.build_draft(
                    game_day=str(sealed["gameDay"]),
                    batch_id=batch_id,
                    seal_version=seal_version,
                    sealed_result=sealed,
                    policy=policy,
                )
            ),
        )

    def _active_run_attempts_for_batch(
        self, batch_id: str
    ) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for membership in self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        ):
            active.extend(
                item
                for item in self.store.list_run_attempts(
                    run_id=str(membership["run_id"]), limit=100
                )
                if item["state"] in {"starting", "running", "cancelling"}
            )
        return active

    def _seal_cancelled_batch(
        self, batch_id: str, cancel_request_id: str
    ) -> dict[str, Any] | None:
        """Seal cancellation only after every fixed Host attempt is terminal."""

        batch = self.store.get_batch(batch_id)
        if batch["result"].get("sealVersion") is not None:
            return batch
        active_attempts = self._active_run_attempts_for_batch(batch_id)
        if active_attempts:
            return None
        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        with self.store.atomic():
            for membership in memberships:
                run_id = str(membership["run_id"])
                attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
                if membership["state"] == "active" and attempts:
                    latest = attempts[0]
                    if latest["state"] in {"starting", "running", "cancelling"}:
                        return None
                    self.store.update_batch_run_membership(
                        batch_id,
                        run_id,
                        state="terminal",
                        latest_run_attempt_id=str(latest["run_attempt_id"]),
                        terminal_outcome=str(latest["state"]),
                    )
                elif membership["state"] in {
                    "queued",
                    "resume_pending",
                    "active",
                    "reconciliation_required",
                }:
                    self.store.update_batch_run_membership(
                        batch_id,
                        run_id,
                        state="cancelled",
                        terminal_outcome="cancelled",
                    )
                run = self.store.get_game_run(run_id)
                if run["state"] in {
                    EntityState.PENDING_EXECUTION,
                    EntityState.QUEUED,
                }:
                    self.store.update_game_run(
                        run_id,
                        state=EntityState.CANCELLED,
                        message="cancelled before Host execution by durable Batch request",
                    )
            request = self.store.update_batch_cancel_request(
                cancel_request_id,
                state="acknowledged",
                active_run_attempt_ids=[],
            )
        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        batch = self.store.get_batch(batch_id)
        initial_result = dict(batch["result"])
        initial_result.pop("completionReviewPhase", None)
        candidate_game_ids = [
            str(value)
            for value in initial_result.get(
                "candidateGameIds", batch["game_ids"]
            )
        ]
        outcomes = self._batch_run_outcome_sets(memberships)
        final_run_ids = outcomes["attemptedRunIds"]
        completed_run_ids = outcomes["completedRunIds"]
        sealed = self._seal_batch_from_todos(
            batch_id=batch_id,
            initial_result={
                **initial_result,
                "cancelRequestId": request["cancel_request_id"],
                "cancelReason": request["reason"],
                "cancelRequestedBy": request["requested_by"],
                "allCandidateRunIds": outcomes["allRunIds"],
                "notStartedRunIds": outcomes["notStartedRunIds"],
                "notStartedMembers": [
                    {
                        "runId": str(item["run_id"]),
                        "gameId": str(
                            self.store.get_game_run(str(item["run_id"]))["game_id"]
                        ),
                        "outcome": str(
                            item.get("terminal_outcome") or "cancelled_before_start"
                        ),
                    }
                    for item in memberships
                    if str(item["run_id"])
                    in set(outcomes["notStartedRunIds"])
                ],
            },
            game_ids=candidate_game_ids,
            cadence=str(batch["cadence"]),
            state=EntityState.CANCELLED,
            completed_run_ids=completed_run_ids,
            failed_run_ids=outcomes["failedRunIds"],
            final_run_ids=final_run_ids,
            reason="Batch cancellation was cooperatively acknowledged or safely reconciled.",
            timed_out=False,
        )
        self.store.update_batch_cancel_request(
            cancel_request_id, state="sealed", active_run_attempt_ids=[]
        )
        self.notification_dispatcher.wake()
        return sealed

    def _current_completion_review_for_run(
        self, run_id: str
    ) -> CompletionReviewRecord | None:
        snapshot = self._completion_contract_snapshot(run_id)
        if snapshot.agent_review is None:
            return None
        review = self.get_completion_review(snapshot.agent_review.review_id)
        if review.decision == "accepted" and not review.todo_reviews:
            # Pre-v2 game-level reviews are diagnostic history, not authority
            # for the current per-Todo semantic completion contract.
            return None
        return review

    def _completion_review_scope(
        self, *, batch_id: str, run_id: str
    ) -> tuple[dict[str, Any], list[str], dict[str, object]]:
        run = self.store.get_game_run(run_id)
        snapshot = self._completion_contract_snapshot(run_id)
        frozen_todos = self._frozen_todos_for_run(run)
        policy, _, policy_status, unsupported_reason = (
            self._completion_policy_context(
                run=run, snapshot=snapshot, todos=frozen_todos
            )
        )
        attempt_id = (
            snapshot.current_attempt.run_attempt_id
            if snapshot.current_attempt is not None
            else None
        )
        attempt_runs = {
            item.run_attempt_id: item.run_id for item in snapshot.attempt_lineage
        }
        screenshots = sorted(
            (
                artifact
                for artifact in snapshot.evidence
                if artifact.is_screenshot
                and artifact.game_id == snapshot.game_id
                and attempt_runs.get(artifact.run_attempt_id) == artifact.run_id
                and artifact.game_day_key == snapshot.game_day.period_key
                and snapshot.game_day.starts_at
                <= artifact.captured_at
                < snapshot.game_day.ends_at
            ),
            key=lambda artifact: (
                artifact.run_attempt_id != attempt_id,
                artifact.captured_at,
                artifact.artifact_id,
            ),
        )
        contract = policy_review_contract(
            policy,
            period_starts_at=snapshot.game_day.starts_at,
            period_ends_at=snapshot.game_day.ends_at,
            policy_status=policy_status,
            unsupported_reason=unsupported_reason,
        )
        screenshot_refs_by_todo: dict[str, list[str]] = {}
        for artifact in screenshots:
            screenshot_refs_by_todo.setdefault(
                artifact.todo_instance_id, []
            ).append(artifact.artifact_id)
        required_todos = [item for item in frozen_todos if item.required]
        return (
            {
                "schemaVersion": 3,
                "batchId": batch_id,
                "gameId": run["game_id"],
                "runId": run_id,
                "runAttemptId": attempt_id,
                "attemptLineageIds": [
                    item.run_attempt_id for item in snapshot.attempt_lineage
                ],
                "gameDayKey": snapshot.game_day.period_key,
                "periodStartsAt": snapshot.game_day.starts_at.isoformat(),
                "periodEndsAt": snapshot.game_day.ends_at.isoformat(),
                "requiredTodoInstanceIds": [
                    item.todo_instance_id for item in required_todos
                ],
                "requiredTodos": [
                    {
                        "todoInstanceId": item.todo_instance_id,
                        "operation": item.operation,
                        "status": str(item.status),
                        "artifactRefs": screenshot_refs_by_todo.get(
                            item.todo_instance_id, []
                        ),
                    }
                    for item in required_todos
                ],
            },
            list(dict.fromkeys(item.artifact_id for item in screenshots)),
            contract,
        )

    def _ensure_completion_review_work_item(
        self, *, batch_id: str, run_id: str
    ) -> dict[str, Any]:
        run = self.store.get_game_run(run_id)
        scope, screenshots, contract = self._completion_review_scope(
            batch_id=batch_id, run_id=run_id
        )
        for item in self.store.list_work_items(5000):
            if (
                item["kind"] == WorkItemKind.EVIDENCE_REVIEW
                and item.get("run_id") == run_id
                and item.get("state") in {EntityState.PLANNED, EntityState.RUNNING}
                and item.get("result", {}).get("completionReviewScope") == scope
            ):
                if item.get("state") == EntityState.PLANNED:
                    for artifact_id in screenshots:
                        if artifact_id not in item.get("artifact_refs", []):
                            item = self.store.append_work_item_artifact(
                                str(item["work_item_id"]), artifact_id
                            )
                return item
        return self.store.create_work_item(
            {
                "kind": WorkItemKind.EVIDENCE_REVIEW,
                "state": EntityState.PLANNED,
                "game_id": run["game_id"],
                "cadence": run["cadence"],
                "run_id": run_id,
                "requested_by": "manager-completion-review",
                "note": (
                    "Review the current GameRun attempt against its typed "
                    "CompletionContract before the batch can be sealed."
                ),
                "artifact_refs": screenshots,
                "allowed_capability_refs": self._allowed_capability_refs(
                    WorkItemKind.EVIDENCE_REVIEW
                ),
                "result": {
                    "completionReviewScope": scope,
                    "completionReviewContract": contract,
                },
            }
        )

    def _completion_review_can_change_outcome(self, run_id: str) -> bool:
        """Return True only when an Agent completion review could still accept.

        The seal barrier exists so that a run whose Adapter attempt completed
        and whose required Todos all completed is not reported before its
        review (the Agent may still attach visual evidence and accept).  A run
        that failed, was cancelled, timed out, or left required Todos
        unresolved can never become ``accepted_done`` through a review, so
        holding the whole batch (and its round e-mail) for that review is pure
        delay.  Unexpected projection errors keep the barrier (fail-closed).
        """

        try:
            snapshot = self._completion_contract_snapshot(run_id)
        except Exception:
            return True
        attempt = snapshot.current_attempt
        if attempt is None or str(attempt.state) != "completed":
            return False
        required = [item for item in snapshot.todos if item.required]
        if not required:
            return False
        return all(str(item.status) == "completed" for item in required)

    def _begin_completion_review_phase(
        self,
        *,
        batch_id: str,
        initial_result: dict[str, Any],
        game_ids: list[str],
        cadence: str,
        state: str,
        completed_run_ids: list[str],
        failed_run_ids: list[str],
        final_run_ids: list[str],
        reason: str,
        timed_out: bool,
    ) -> dict[str, Any]:
        """Persist the review barrier; never seal while a current review is absent."""

        current = self.store.get_batch(batch_id)
        if current.get("result", {}).get("sealVersion") is not None:
            return current
        if current.get("result", {}).get("completionReviewPhase"):
            return self._resume_completion_review_batch(batch_id)

        frozen_context = {
            "schemaVersion": 1,
            "initialResult": dict(initial_result),
            "gameIds": list(game_ids),
            "cadence": cadence,
            "terminalState": state,
            "completedRunIds": list(completed_run_ids),
            "failedRunIds": list(failed_run_ids),
            "finalRunIds": list(final_run_ids),
            "reason": reason,
            "timedOut": timed_out,
        }
        with self.store.atomic():
            work_item_ids: dict[str, str] = {}
            awaiting: list[str] = []
            for run_id in final_run_ids:
                if self._current_completion_review_for_run(run_id) is not None:
                    continue
                # The Agent work item is still created so a failed run can be
                # diagnosed and repaired, but only a run that a review could
                # actually accept keeps the seal (and the round mail) waiting.
                work_item = self._ensure_completion_review_work_item(
                    batch_id=batch_id, run_id=run_id
                )
                work_item_ids[run_id] = str(work_item["work_item_id"])
                if self._completion_review_can_change_outcome(run_id):
                    awaiting.append(run_id)
            record = self.store.update_batch(
                batch_id,
                state=EntityState.REVIEW_REQUIRED,
                result={
                    **initial_result,
                    "currentGameId": None,
                    "completedRunIds": list(completed_run_ids),
                    "failedRunIds": list(failed_run_ids),
                    "finalGameRunIds": list(final_run_ids),
                    "timedOutWithProcessPreserved": timed_out,
                    "acceptedDone": False,
                    "awaitingCompletionReviewRunIds": awaiting,
                    "completionReviewWorkItemIds": work_item_ids,
                    "completionReviewPhase": {
                        "schemaVersion": 1,
                        "status": "awaiting",
                        "barrierStartedAt": utc_now().isoformat(),
                        "barrierSeconds": self.COMPLETION_REVIEW_BARRIER_SECONDS,
                        "frozenFinalContext": frozen_context,
                    },
                },
            )
        if awaiting:
            return record
        try:
            return self._resume_completion_review_batch(batch_id)
        except (ManagerConflict, ManagerValidation) as error:
            # The review phase is persisted; a seal-scope conflict must not
            # unwind the batch execution that already finished.  The watchdog
            # retries the seal from persisted state.
            self.store.append_event(
                "completion-review-phase.seal-deferred",
                "batch",
                batch_id,
                {"errorClass": type(error).__name__, "detail": str(error)[:400]},
            )
            return self.store.get_batch(batch_id)

    def _resume_completion_review_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        result = dict(batch.get("result", {}))
        if result.get("sealVersion") is not None:
            return batch
        phase = result.get("completionReviewPhase")
        if not isinstance(phase, dict):
            return batch
        context = phase.get("frozenFinalContext")
        if not isinstance(context, dict):
            raise ManagerConflict(
                "completion review phase has no frozen final context"
            )
        final_run_ids = [str(value) for value in context.get("finalRunIds", [])]
        work_item_ids = dict(result.get("completionReviewWorkItemIds", {}))
        reviews: dict[str, CompletionReviewRecord] = {}
        awaiting: list[str] = []
        barrier_expired = self._completion_review_barrier_expired(phase)
        with self.store.atomic():
            for run_id in final_run_ids:
                review = self._current_completion_review_for_run(run_id)
                if review is not None:
                    reviews[run_id] = review
                    continue
                work_item = self._ensure_completion_review_work_item(
                    batch_id=batch_id, run_id=run_id
                )
                work_item_ids[run_id] = str(work_item["work_item_id"])
                if (
                    not barrier_expired
                    and self._completion_review_can_change_outcome(run_id)
                ):
                    awaiting.append(run_id)
            result = {
                **result,
                "awaitingCompletionReviewRunIds": awaiting,
                "completionReviewWorkItemIds": work_item_ids,
            }
            if awaiting:
                batch = self.store.update_batch(
                    batch_id,
                    state=EntityState.REVIEW_REQUIRED,
                    result=result,
                )
        if awaiting:
            return batch

        review_blocked = any(
            review.decision in {"rejected", "review_required"}
            for review in reviews.values()
        )
        terminal_state = (
            str(EntityState.BLOCKED)
            if review_blocked
            else str(context.get("terminalState") or EntityState.REVIEW_REQUIRED)
        )
        seal_initial_result = {
            **dict(context.get("initialResult", {})),
            "awaitingCompletionReviewRunIds": [],
            "completionReviewWorkItemIds": work_item_ids,
            "completionReviewPhase": {
                "schemaVersion": 1,
                "status": "completed",
                "barrierExpired": barrier_expired,
            },
        }
        sealed = self._seal_batch_from_todos(
            batch_id=batch_id,
            initial_result=seal_initial_result,
            game_ids=[str(value) for value in context.get("gameIds", [])],
            cadence=str(context["cadence"]),
            state=terminal_state,
            completed_run_ids=[
                str(value) for value in context.get("completedRunIds", [])
            ],
            failed_run_ids=[
                str(value) for value in context.get("failedRunIds", [])
            ],
            final_run_ids=final_run_ids,
            reason=(
                "One or more current completion reviews rejected acceptance."
                if review_blocked
                else str(context.get("reason") or "Completion review finished.")
            ),
            timed_out=bool(context.get("timedOut", False)),
        )
        self.notification_dispatcher.wake()
        return sealed

    def _resume_completion_review_batches_for_run(self, run_id: str) -> None:
        for batch in self.store.list_batches(1000):
            result = dict(batch.get("result", {}))
            phase = result.get("completionReviewPhase")
            if result.get("sealVersion") is not None or not isinstance(phase, dict):
                continue
            context = phase.get("frozenFinalContext")
            if not isinstance(context, dict) or run_id not in {
                str(value) for value in context.get("finalRunIds", [])
            }:
                continue
            self._resume_completion_review_batch(str(batch["batch_id"]))

    def _completion_review_barrier_expired(self, phase: dict[str, Any]) -> bool:
        """A pending Agent review may hold a seal only for a bounded time.

        After the bound the batch is sealed from the machine adjudication alone
        (un-reviewed runs stay ``review_required``); the round mail must not
        wait indefinitely for an Agent that may be offline.  Legacy phases
        without a recorded barrier start are sealed immediately.
        """

        started_raw = phase.get("barrierStartedAt")
        if not isinstance(started_raw, str) or not started_raw:
            return True
        try:
            started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        bound = phase.get("barrierSeconds")
        seconds = (
            float(bound)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool)
            else float(self.COMPLETION_REVIEW_BARRIER_SECONDS)
        )
        return (utc_now() - started).total_seconds() >= seconds

    def start_completion_review_watchdog(self) -> None:
        """Seal batches whose review barrier expired while no event arrived."""

        with self._watchdog_lock:
            thread = self._review_watchdog_thread
            if thread is not None and thread.is_alive():
                return
            self._review_watchdog_stop.clear()
            self._review_watchdog_thread = threading.Thread(
                target=self._completion_review_watchdog_loop,
                name="YeYuGamer.CompletionReviewWatchdog",
                daemon=True,
            )
            self._review_watchdog_thread.start()

    def stop_completion_review_watchdog(self, timeout: float = 5.0) -> None:
        self._review_watchdog_stop.set()
        thread = self._review_watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._review_watchdog_thread = None

    def _completion_review_watchdog_loop(self) -> None:
        while not self._review_watchdog_stop.wait(
            self.COMPLETION_REVIEW_WATCHDOG_POLL_SECONDS
        ):
            try:
                self._recover_completion_review_phases()
            except Exception:
                # The loop must survive a transient store error; the next tick
                # re-evaluates every pending phase from persisted state.
                _log.exception("completion_review_watchdog.tick_failed")
                continue

    # Ledger retention runs shortly after start (so an oversized database is
    # repaired without waiting for the next window) and then a few times a day.
    LEDGER_MAINTENANCE_INITIAL_DELAY_SECONDS = 120.0
    LEDGER_MAINTENANCE_INTERVAL_SECONDS = 6 * 3600.0
    LEDGER_EVENT_RETENTION_DAYS = 14

    def start_ledger_maintenance(self) -> None:
        with self._watchdog_lock:
            thread = getattr(self, "_ledger_maintenance_thread", None)
            if thread is not None and thread.is_alive():
                return
            self._ledger_maintenance_stop = threading.Event()
            self._ledger_maintenance_thread = threading.Thread(
                target=self._ledger_maintenance_loop,
                name="YeYuGamer.LedgerMaintenance",
                daemon=True,
            )
            self._ledger_maintenance_thread.start()

    def stop_ledger_maintenance(self, timeout: float = 5.0) -> None:
        stop = getattr(self, "_ledger_maintenance_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_ledger_maintenance_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._ledger_maintenance_thread = None

    def _ledger_maintenance_loop(self) -> None:
        stop = self._ledger_maintenance_stop
        delay = self.LEDGER_MAINTENANCE_INITIAL_DELAY_SECONDS
        while not stop.wait(delay):
            delay = self.LEDGER_MAINTENANCE_INTERVAL_SECONDS
            try:
                self.maintain_ledger()
            except Exception:
                _log.exception("ledger_maintenance.failed")

    def maintain_ledger(self, *, allow_vacuum: bool = True) -> dict[str, Any]:
        """Apply retention to the SQLite ledger and per-attempt log directories."""

        before = self.store.ledger_size_report()
        compacted = self.store.compact_batch_event_payloads()
        pruned = self.store.prune_ledger(
            event_retention_days=self.LEDGER_EVENT_RETENTION_DAYS
        )
        removed_log_days = prune_run_logs(self.settings)
        vacuumed = False
        if allow_vacuum:
            active = self.store.active_execution_summary()
            if not any(active.values()):
                vacuumed = self.store.vacuum_if_fragmented()
        after = self.store.ledger_size_report()
        report = {
            "compactedBatchEvents": compacted,
            "pruned": pruned,
            "removedRunLogDays": removed_log_days,
            "vacuumed": vacuumed,
            "fileBytesBefore": before["fileBytes"],
            "fileBytesAfter": after["fileBytes"],
            "rows": after["rows"],
        }
        _log.info("ledger_maintenance.done %s", report)
        if compacted or any(pruned.values()) or vacuumed:
            self.store.append_event(
                "manager.ledger-maintained", "manager", self.manager_id, report
            )
        return report

    def _recover_completion_review_phases(self) -> None:
        now = utc_now()
        for batch in self.store.list_batches(1000):
            result = dict(batch.get("result", {}))
            if (
                result.get("sealVersion") is not None
                or not isinstance(result.get("completionReviewPhase"), dict)
            ):
                continue
            batch_id = str(batch["batch_id"])
            retry_after = self._review_recovery_retry_after.get(batch_id)
            if retry_after is not None and now < retry_after:
                continue
            try:
                self._resume_completion_review_batch(batch_id)
                self._review_recovery_retry_after.pop(batch_id, None)
            except Exception as error:
                # Back off per batch so a persistent seal conflict does not
                # write one recovery event per watchdog tick.
                self._review_recovery_retry_after[batch_id] = now + timedelta(
                    seconds=self.COMPLETION_REVIEW_RECOVERY_BACKOFF_SECONDS
                )
                self.store.append_event(
                    "completion-review-phase.recovery-failed",
                    "batch",
                    batch_id,
                    {
                        "errorClass": type(error).__name__,
                        "retryable": True,
                    },
                )

    def _create_batch_resources(
        self,
        *,
        candidate_game_ids: list[str],
        cadence: Cadence,
        mode: RequestMode,
        requested_by: str,
        planning_reason: str,
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        BatchPlanningDecision,
        dict[str, dict[str, Any]],
    ]:
        """Persist one classified batch after the execute preflight succeeds."""

        if mode == RequestMode.EXECUTE and any(
            self._current_human_batch(batch) for batch in self.store.list_batches(1000)
        ):
            raise ManagerConflict(
                "explicit_human_release_required: resolve the current human takeover and resume its same GameRun before creating another batch"
            )
        if mode == RequestMode.EXECUTE and self.store.get_metadata(
            "manager.lifecycle_state", "running"
        ) != "running":
            raise ManagerConflict("Manager is stopping and cannot accept new execution")
        self.store.reconcile_todo_instances(
            self._todo_instance_candidates(candidate_game_ids, cadence),
            intent="reconcile",
            requested_by=requested_by,
            reason=planning_reason,
        )
        todo_plans = self._todo_plans_for_games(candidate_game_ids, str(cadence))
        decision = classify_batch_todo_plans(candidate_game_ids, todo_plans)
        if (
            mode == RequestMode.EXECUTE
            and decision.has_unresolved_todos
            and not decision.has_executable_binding
        ):
            raise ExecutionUnavailable(
                decision.execution_unavailable_details(todo_plans)
            )

        unresolved_games = list(decision.unresolved_game_ids)
        executable_games = list(decision.executable_game_ids)
        deferred_games = list(decision.deferred_game_ids)
        skipped_games = list(decision.skipped_completed_game_ids)
        batch_scope = self._batch_todo_scope(todo_plans, candidate_game_ids)
        reconciliation_runs_by_game = self._completion_reconciliation_runs(
            todo_plans, skipped_games
        )
        reconciliation_run_ids = list(reconciliation_runs_by_game.values())
        if mode == RequestMode.EXECUTE and executable_games:
            self._require_execution_ready(executable_games)
        batch_state = (
            EntityState.QUEUED
            if mode == RequestMode.EXECUTE and executable_games
            else EntityState.PLANNED
        )
        record = self.store.create_batch(
            {
                "cadence": cadence,
                "mode": mode,
                "state": batch_state,
                "game_ids": unresolved_games,
                "requested_by": requested_by,
                "result": {
                    "gameDay": batch_scope["scopeKey"],
                    "todoScope": batch_scope,
                    "candidateGameIds": list(decision.candidate_game_ids),
                    "skippedCompletedGameIds": skipped_games,
                    "executableGameIds": executable_games,
                    "deferredGameIds": deferred_games,
                    "completionReconciliationRuns": reconciliation_runs_by_game,
                    "todoPlans": todo_plans,
                },
            }
        )
        runs: list[dict[str, Any]] = []
        if mode == RequestMode.EXECUTE:
            for ordinal, game_id in enumerate(executable_games):
                run = self.store.create_game_run(
                    {
                        "game_id": game_id,
                        "cadence": cadence,
                        "state": EntityState.QUEUED,
                        "mode": RequestMode.EXECUTE,
                        "requested_by": requested_by,
                        "message": f"queued by batch {record['batch_id']}",
                        "todo_instance_ids": todo_plans[game_id][
                            "executableTodoInstanceIds"
                        ],
                        "completed_todo_instance_ids": todo_plans[game_id][
                            "completedTodoInstanceIds"
                        ],
                        "completion_todo_instance_ids": todo_plans[game_id][
                            "completionTodoInstanceIds"
                        ],
                    }
                )
                self.store.add_batch_run_membership(
                    record["batch_id"],
                    run["run_id"],
                    ordinal=ordinal,
                    role="initial",
                    state="queued",
                )
                runs.append(run)
            record = self.store.get_batch(record["batch_id"])
            if not executable_games:
                record = self._seal_batch_from_todos(
                    batch_id=record["batch_id"],
                    initial_result=dict(record["result"]),
                    game_ids=candidate_game_ids,
                    cadence=str(cadence),
                    state=EntityState.REVIEW_REQUIRED,
                    completed_run_ids=reconciliation_run_ids,
                    final_run_ids=reconciliation_run_ids,
                    reason=(
                        "Completed same-GameDay Todo facts were re-adjudicated without "
                        "replaying game actions."
                        if reconciliation_run_ids
                        else "All required Todo facts were already complete, but this new "
                        "batch has no fresh acceptance contract."
                    ),
                )
        return record, runs, decision, todo_plans

    def create_batch(
        self,
        request: BatchCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        requested_game_ids = request.game_ids
        if requested_game_ids is None and request.cadence == Cadence.DAILY:
            configured_selection = self.store.get_config()["values"].get(
                "daily_todo_selection", {}
            )
            requested_game_ids = [
                game.game_id
                for game in self.list_games()
                if game.enabled
                and isinstance(configured_selection.get(game.game_id), list)
                and configured_selection[game.game_id]
            ]
        candidate_games = self._validated_games(requested_game_ids)

        def operation() -> dict[str, Any]:
            record, runs, decision, todo_plans = self._create_batch_resources(
                candidate_game_ids=candidate_games,
                cadence=request.cadence,
                mode=request.mode,
                requested_by=request.requested_by,
                planning_reason="batch-planning",
            )
            executable_games = list(decision.executable_game_ids)
            return self._receipt(
                command_id=record["batch_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/batches/{record['batch_id']}",
                message=(
                    "批次已进入 Manager 串行执行队列。"
                    if request.mode == RequestMode.EXECUTE and executable_games
                    else "required Todo 已全部完成；Manager 已从今日队列重新验收同游戏日事实，未重复启动游戏。"
                    if request.mode == RequestMode.EXECUTE
                    and record.get("result", {}).get("acceptedDone") is True
                    else "required Todo 已全部完成；本次仅记录事实，未宣称新的验收完成。"
                    if request.mode == RequestMode.EXECUTE
                    else "批次计划已写入 Manager；没有启动游戏。"
                ),
                # An execute receipt is only an acceptance/reference document.
                # Batch, membership, GameRun, and Todo details remain in their
                # authoritative resources instead of being copied into the
                # HTTP response, idempotency row, command row, and SSE ledger.
                result=(
                    {"batchId": record["batch_id"]}
                    if request.mode == RequestMode.EXECUTE
                    else {
                        "batch": _dump(self._batch_record(record)),
                        "gameRuns": [
                            _dump(GameRunRecord.model_validate(run)) for run in runs
                        ],
                        "candidateGameIds": list(decision.candidate_game_ids),
                        "skippedCompletedGameIds": list(
                            decision.skipped_completed_game_ids
                        ),
                        "executableGameIds": executable_games,
                        "deferredGameIds": list(decision.deferred_game_ids),
                        "immediateSealed": False,
                        "todoPlans": todo_plans,
                    }
                ),
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if request.mode == RequestMode.EXECUTE and not replayed:
            batch_id = str(receipt.command_id)
            batch = self.store.get_batch(batch_id)
            memberships = self.store.list_batch_run_memberships(
                batch_id=batch_id, limit=5000
            )
            if memberships:
                persisted_runs = [
                    _dump(
                        GameRunRecord.model_validate(
                            self.store.get_game_run(str(membership["run_id"]))
                        )
                    )
                    for membership in memberships
                ]
                threading.Thread(
                    target=self._run_batch,
                    args=(batch_id, persisted_runs, str(receipt.command_id)),
                    name=f"yeyu-gamer-batch-{batch_id[:8]}",
                    daemon=True,
                ).start()
            else:
                accepted_done = bool(batch.get("result", {}).get("acceptedDone"))
                self._complete_command(
                    str(receipt.command_id),
                    "succeeded" if accepted_done else "failed",
                    (
                        "batch sealed accepted_done by same-GameDay completion reconciliation"
                        if accepted_done
                        else "batch sealed without starting Host because no new Todo execution was required"
                    ),
                )
                self.notification_dispatcher.wake()
        return receipt

    def _build_execution_plan(
        self,
        run: dict[str, Any],
        *,
        todo_instance_ids: list[str] | None = None,
        run_attempt_id: str | None = None,
        fencing_token: str | None = None,
    ) -> AdapterExecutionPlan:
        requested_ids = (
            list(run["todo_instance_ids"])
            if todo_instance_ids is None
            else list(todo_instance_ids)
        )
        run_scope = list(run["todo_instance_ids"])
        todo_ids = [todo_id for todo_id in run_scope if todo_id in set(requested_ids)]
        if todo_ids != requested_ids or len(todo_ids) != len(set(todo_ids)):
            raise ManagerConflict(
                "execution Todo scope must be an order-preserving GameRun subset"
            )
        if not todo_ids:
            raise ManagerConflict("GameRun has no executable Todo scope")
        todos = [self.get_todo_instance(todo_id) for todo_id in todo_ids]
        runtime = self.adapter_host.execution_bindings(run["game_id"])
        if not bool(runtime.get("manifestVerified")):
            raise ManagerConflict("execution manifest is not verified")
        bindings = {
            str(binding["operation"]): binding
            for binding in runtime.get("bindings", [])
            if isinstance(binding, dict) and isinstance(binding.get("operation"), str)
        }
        targets: list[AdapterTodoTarget] = []
        maximum_timeout = 0
        execution_catalog_version = self._execution_catalog_version(todos)
        for item in todos:
            binding = bindings.get(item.operation)
            if binding is None:
                raise ManagerConflict(f"Todo operation is not promoted: {item.operation}")
            if item.risk not in {"routine_action", "observe_only"}:
                raise ManagerConflict("GameRun contains a non-executable Todo risk")
            if item.adapter_capability_ref is None:
                raise ManagerConflict("GameRun contains an unbound Todo capability")
            if item.todo_definition_id not in set(binding.get("todoDefinitionIds", [])):
                raise ManagerConflict("Todo definition is not in the promoted binding")
            if item.adapter_capability_ref not in set(
                binding.get("adapterCapabilityRefs", [])
            ):
                raise ManagerConflict("Todo capability is not in the promoted binding")
            if str(binding.get("risk")) != item.risk:
                raise ManagerConflict("Todo risk differs from the promoted binding")
            # A Todo left blocked by an earlier, terminal GameRun may be
            # selected into a *new* batch after dispatch has explicitly
            # classified it as eligible.  That is a fresh attempt, not a
            # resume.  Resume safety is required only if this very GameRun
            # already owns the interrupted Todo.
            same_run_interruption = (
                item.status in {"in_progress", "blocked"}
                and str(item.run_id or "") == str(run["run_id"])
            )
            if same_run_interruption and not bool(binding.get("supportsResume")):
                raise ManagerConflict("promoted binding cannot safely resume this Todo")
            maximum_timeout += max(1, int(binding.get("timeoutSeconds", 1)))
            targets.append(
                AdapterTodoTarget(
                    todo_instance_id=item.todo_instance_id,
                    todo_definition_id=item.todo_definition_id,
                    definition_version=item.definition_version,
                    operation=item.operation,
                    risk=item.risk,
                    adapter_capability_ref=item.adapter_capability_ref,
                    prior_attempts=item.attempts,
                )
            )
        configured_timeout = int(
            self.store.get_config()["values"].get("step_timeout_seconds", 1800)
        )
        timeout_seconds = max(1, min(configured_timeout, maximum_timeout))
        issued_at = utc_now()
        expires_at = issued_at + timedelta(seconds=timeout_seconds + 30)
        policy_document = {
            "gameId": run["game_id"],
            "cadence": run["cadence"],
            "packageDigest": runtime.get("packageDigest"),
            "packageVersion": runtime.get("packageVersion"),
            "todos": [target.to_document() for target in targets],
            "preserveClientOnStop": True,
        }
        policy_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                policy_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fencing_value = fencing_token or secrets.token_urlsafe(48)
        cancel_authority = secrets.token_urlsafe(48)
        while cancel_authority == fencing_value:
            cancel_authority = secrets.token_urlsafe(48)
        plan = AdapterExecutionPlan(
            run_id=run["run_id"],
            run_attempt_id=run_attempt_id or str(uuid.uuid4()),
            fencing_token=fencing_value,
            cancel_authority=cancel_authority,
            game_id=run["game_id"],
            cadence=run["cadence"],
            manager_state_version=self.store.latest_event_sequence(),
            catalog_version=execution_catalog_version,
            policy_digest=policy_digest,
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
            timeout_seconds=timeout_seconds,
            executable_todo_instance_ids=tuple(todo_ids),
            todos=tuple(targets),
            preserve_client_on_stop=True,
        )
        return self.adapter_host.validate_execution_request(plan)

    @staticmethod
    def _execution_catalog_version(todos: list[TodoInstanceRecord]) -> str:
        """Return an auditable catalog identity for one frozen execution scope.

        A current GameDay may legitimately contain unchanged Todo snapshots from
        an older catalog release and a safely refreshed pristine Todo from a
        newer release.  Catalog version is release metadata, not a semantic
        consistency boundary.  Preserve the original value for a homogeneous
        scope; for a mixed scope bind the execution request to every frozen
        definition identity, version, source hash, and catalog label.
        """

        catalog_versions = {item.catalog_version for item in todos}
        if len(catalog_versions) == 1:
            return next(iter(catalog_versions))
        snapshot = [
            {
                "todoDefinitionId": item.todo_definition_id,
                "definitionVersion": item.definition_version,
                "sourceHash": item.source_hash,
                "catalogVersion": item.catalog_version,
            }
            for item in sorted(
                todos,
                key=lambda value: (
                    value.todo_definition_id,
                    value.definition_version,
                    value.source_hash,
                    value.catalog_version,
                ),
            )
        ]
        digest = hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"snapshot-sha256.{digest}"

    @staticmethod
    def _safe_execution_plan_document(plan: AdapterExecutionPlan) -> dict[str, Any]:
        document = plan.to_document()
        document.pop("fencingToken", None)
        document.pop("cancelAuthority", None)
        return document

    @staticmethod
    def _controller_lease_fact(record: dict[str, Any]) -> ControllerLease:
        return ControllerLease(
            controller_lease_id=record["controller_lease_id"],
            desktop_id=record["desktop_id"],
            scope=ExecutionScope(
                manager_id=record["manager_id"],
                game_id=record["game_id"],
                run_id=record["run_id"],
                run_attempt_id=record["run_attempt_id"],
                game_day_key=record["game_day_key"],
            ),
            holder_principal_id=record["holder_principal_id"],
            generation=int(record["generation"]),
            fencing=FencingIdentity(
                token_hash=record["fencing_hash"],
                public_fingerprint=record["fencing_fingerprint"],
            ),
            acquired_at=ManagerService._contract_datetime(record["acquired_at"]),
            expires_at=ManagerService._contract_datetime(record["expires_at"]),
            state=LeaseState(record["state"]),
            ended_at=(
                ManagerService._contract_datetime(record["ended_at"])
                if record.get("ended_at")
                else None
            ),
            end_reason_code=record.get("end_reason_code"),
            end_reason=record.get("end_reason"),
        )

    def _run_game_day_key(
        self, run: dict[str, Any], todo_instance_ids: list[str] | None = None
    ) -> str:
        scope = todo_instance_ids or list(run["todo_instance_ids"])
        periods = {
            self.store.get_todo_instance(todo_id)["period_key"] for todo_id in scope
        }
        if len(periods) != 1:
            raise ManagerConflict("GameRun Todo scope crosses GameDay boundaries")
        return next(iter(periods))

    def _prepare_run_attempt(
        self,
        run: dict[str, Any],
        *,
        todo_instance_ids: list[str] | None = None,
        attempt_ordinal: int | None = None,
    ) -> AdapterExecutionPlan:
        plan = self._build_execution_plan(
            run, todo_instance_ids=todo_instance_ids
        )
        digest_body = hashlib.sha256(plan.fencing_token.encode("utf-8")).hexdigest()
        fencing_hash = f"sha256:{digest_body}"
        fencing_fingerprint = f"sha256:{digest_body[:16]}"
        cancel_authority_hash = "sha256:" + hashlib.sha256(
            plan.cancel_authority.encode("utf-8")
        ).hexdigest()
        game_day_key = self._run_game_day_key(
            run, list(plan.executable_todo_instance_ids)
        )
        desktop_id = "windows-interactive-desktop"
        issued_at = self._contract_datetime(plan.issued_at)
        candidate = ControllerLease(
            controller_lease_id=str(uuid.uuid4()),
            desktop_id=desktop_id,
            scope=ExecutionScope(
                manager_id=self.manager_id,
                game_id=plan.game_id,
                run_id=plan.run_id,
                run_attempt_id=plan.run_attempt_id,
                game_day_key=game_day_key,
            ),
            holder_principal_id="manager-adapter",
            generation=(
                max(
                    (
                        int(item["generation"])
                        for item in self.store.list_controller_leases_private(
                            desktop_id=desktop_id
                        )
                    ),
                    default=0,
                )
                + 1
            ),
            fencing=FencingIdentity(
                token_hash=fencing_hash,
                public_fingerprint=fencing_fingerprint,
            ),
            acquired_at=issued_at,
            expires_at=self._contract_datetime(plan.expires_at),
        )
        existing = tuple(
            self._controller_lease_fact(item)
            for item in self.store.list_controller_leases_private(
                desktop_id=desktop_id
            )
        )
        grant_controller_lease(candidate, existing, at=issued_at)
        with self.store.atomic():
            safe_plan = self._safe_execution_plan_document(plan)
            runtime_bindings = {
                str(binding["operation"]): binding
                for binding in self.adapter_host.execution_bindings(
                    plan.game_id
                ).get("bindings", [])
                if isinstance(binding, dict)
                and isinstance(binding.get("operation"), str)
            }
            session_reentry_ids = [
                target.todo_instance_id
                for target in plan.todos
                if self.store.get_todo_instance(target.todo_instance_id)["status"]
                == "completed"
                and str(
                    runtime_bindings.get(target.operation, {}).get("actionClass")
                )
                == "session"
            ]
            session_reentry_id_set = set(session_reentry_ids)
            completion_recovery_replay_ids = [
                target.todo_instance_id
                for target in plan.todos
                if self.store.get_todo_instance(target.todo_instance_id)["status"]
                == "completed"
                and target.todo_instance_id not in session_reentry_id_set
            ]
            if session_reentry_ids:
                # This Manager-only annotation is persisted beside the safe
                # plan after wire-protocol validation.  It is deliberately not
                # sent to strict external runners, so existing promoted tools
                # keep receiving the unchanged v1.1 contract.
                safe_plan["sessionReentryTodoInstanceIds"] = session_reentry_ids
            if completion_recovery_replay_ids:
                # This Manager-private annotation authorizes a fresh evidence
                # attempt for an immutable completed stage because the exact
                # current GameDay scope still lacks accepted_done.
                safe_plan["completionRecoveryReplayTodoInstanceIds"] = (
                    completion_recovery_replay_ids
                )
            self.store.create_run_attempt(
                {
                    "run_attempt_id": plan.run_attempt_id,
                    "run_id": plan.run_id,
                    "game_id": plan.game_id,
                    "cadence": plan.cadence,
                    "state": "starting",
                    "fencing_token_hash": fencing_hash,
                    "cancel_authority_hash": cancel_authority_hash,
                    "plan": safe_plan,
                    **(
                        {"attempt_ordinal": attempt_ordinal}
                        if attempt_ordinal is not None
                        else {}
                    ),
                }
            )
            self.store.create_controller_lease(
                {
                    "controller_lease_id": candidate.controller_lease_id,
                    "desktop_id": candidate.desktop_id,
                    "manager_id": candidate.scope.manager_id,
                    "game_id": candidate.scope.game_id,
                    "run_id": candidate.scope.run_id,
                    "run_attempt_id": candidate.scope.run_attempt_id,
                    "game_day_key": candidate.scope.game_day_key,
                    "holder_principal_id": candidate.holder_principal_id,
                    "generation": candidate.generation,
                    "fencing_hash": candidate.fencing.token_hash,
                    "fencing_fingerprint": candidate.fencing.public_fingerprint,
                    "acquired_at": candidate.acquired_at.isoformat(),
                    "expires_at": candidate.expires_at.isoformat(),
                }
            )
            for membership in self.store.list_batch_run_memberships(
                run_id=plan.run_id, limit=5000
            ):
                batch = self.store.get_batch(str(membership["batch_id"]))
                if (
                    batch["result"].get("sealVersion") is None
                    and membership["state"]
                    in {
                        "queued",
                        "resume_pending",
                        "reconciliation_required",
                        "active",
                    }
                ):
                    self.store.update_batch_run_membership(
                        str(membership["batch_id"]),
                        plan.run_id,
                        state="active",
                        latest_run_attempt_id=plan.run_attempt_id,
                    )
        return plan

    def _terminalize_unsealed_batch_memberships(
        self,
        *,
        run_id: str,
        run_attempt_id: str,
        outcome: str,
    ) -> None:
        for membership in self.store.list_batch_run_memberships(
            run_id=run_id, limit=5000
        ):
            batch = self.store.get_batch(str(membership["batch_id"]))
            if batch["result"].get("sealVersion") is not None:
                continue
            if membership["state"] not in {
                "active",
                "resume_pending",
                "reconciliation_required",
            }:
                continue
            self.store.update_batch_run_membership(
                str(membership["batch_id"]),
                run_id,
                state="terminal",
                latest_run_attempt_id=run_attempt_id,
                terminal_outcome=outcome,
            )

    def _end_controller_lease(
        self,
        run_attempt_id: str,
        *,
        state: LeaseState,
        reason_code: str,
        reason: str,
    ) -> None:
        current_record = self.store.get_controller_lease_for_attempt(
            run_attempt_id, active_only=True
        )
        if current_record is None:
            return
        current = self._controller_lease_fact(current_record)
        candidate = current.model_copy(
            update={
                "state": state,
                "ended_at": utc_now(),
                "end_reason_code": reason_code,
                "end_reason": reason,
            }
        )
        transition_controller_lease(current, candidate)
        self.store.transition_controller_lease(
            current.controller_lease_id,
            state=state.value,
            reason_code=reason_code,
            reason=reason,
            ended_at=candidate.ended_at.isoformat() if candidate.ended_at else None,
        )

    def _handle_adapter_event(
        self,
        plan: AdapterExecutionPlan,
        event: AdapterEvent,
        manifest: ExecutionPackageManifest | None = None,
        capture_pids: frozenset[int] | None = None,
    ) -> None:
        document = event.document if isinstance(event.document, dict) else {}
        with bind_log_context(
            run=plan.run_id,
            attempt=plan.run_attempt_id,
            game=plan.game_id,
            todo=document.get("operation") or document.get("todoInstanceId"),
            phase="tool-run",
        ):
            summary = {
                key: document[key]
                for key in (
                    "operation",
                    "status",
                    "reasonCode",
                    "reason",
                    "kind",
                    "fileName",
                    "exitCode",
                    "transportOutcome",
                    "packageVersion",
                )
                if key in document
            }
            _log.info("adapter.event seq=%s type=%s %s", event.sequence, event.event_type, summary)
            try:
                self._handle_adapter_event_inner(plan, event, manifest, capture_pids)
            except Exception as error:
                _log.error(
                    "adapter.event.rejected seq=%s type=%s error=%s: %s",
                    event.sequence,
                    event.event_type,
                    type(error).__name__,
                    error,
                )
                raise

    def _handle_adapter_event_inner(
        self,
        plan: AdapterExecutionPlan,
        event: AdapterEvent,
        manifest: ExecutionPackageManifest | None = None,
        capture_pids: frozenset[int] | None = None,
    ) -> None:
        if event.event_type == "artifact_staged" and manifest is None:
            raise ManagerValidation(
                "artifact persistence requires the verified execution manifest"
            )
        if event.event_type == "artifact_staged" and manifest is not None:
            if (
                tuple(sorted(event.allowed_artifact_mime_types))
                != tuple(sorted(manifest.allowed_mime_types))
                or event.max_artifact_bytes != manifest.max_artifact_bytes
                or event.max_artifacts_per_todo
                != manifest.max_artifacts_per_todo
            ):
                raise ManagerValidation(
                    "artifact event policy differs from the verified execution manifest"
                )
        document = dict(event.document)
        safe_document = self._without_fencing_material(document)
        self._assert_no_fencing_value(safe_document, plan.fencing_token)
        token_hash = "sha256:" + hashlib.sha256(
            plan.fencing_token.encode("utf-8")
        ).hexdigest()
        with self.store.atomic():
            _, replayed = self.store.append_adapter_event(
                run_attempt_id=plan.run_attempt_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=safe_document,
                fencing_token_hash=token_hash,
            )
            if replayed:
                return
            if event.event_type == "hello":
                self.store.update_run_attempt(
                    plan.run_attempt_id,
                    state="running",
                    result={
                        "packageId": safe_document.get("packageId"),
                        "packageVersion": safe_document.get("packageVersion"),
                        "packageDigest": safe_document.get("packageDigest"),
                    },
                    completed=False,
                )
            elif event.event_type == "todo_attempt_started":
                todo_attempt = self.store.start_todo_attempt(
                    {
                        "todo_attempt_id": str(document["todoAttemptId"]),
                        "run_attempt_id": plan.run_attempt_id,
                        "run_id": plan.run_id,
                        "todo_instance_id": str(document["todoInstanceId"]),
                        "attempt_number": int(document["attemptNo"]),
                        "operation": str(document["operation"]),
                    }
                )
                todo = self.store.get_todo_instance(
                    str(todo_attempt["todo_instance_id"])
                )
                title = str(todo.get("title") or document["operation"])
                # The home page's GameSummary is intentionally a cheap
                # projection of the GameRun. Mirror the structured Adapter
                # event there so the user sees the actual current Todo rather
                # than the stale "starting game client" launch message.
                self.store.update_game_run(
                    plan.run_id,
                    state=EntityState.RUNNING,
                    message=f"正在执行：{title}",
                )
                try:
                    self._capture_adapter_step_artifact(
                        plan,
                        document,
                        phase="before",
                        operation=str(document["operation"]),
                        capture_pids=capture_pids,
                    )
                except (WindowCaptureError, OSError, ManagerValidation) as error:
                    # Completion remains fail-closed below.  A transient frame
                    # failure must not abort the Adapter transport before it
                    # can leave its own diagnostic evidence.
                    self._record_step_capture_failure(
                        plan,
                        document,
                        phase="before",
                        operation=str(document["operation"]),
                        error=error,
                    )
            elif event.event_type == "artifact_staged":
                assert manifest is not None
                self.adapter_artifacts.import_staged(
                    plan,
                    document,
                    allowed_mime_types=manifest.allowed_mime_types,
                    max_artifacts_per_todo=manifest.max_artifacts_per_todo,
                    max_artifact_bytes=manifest.max_artifact_bytes,
                )
            elif event.event_type == "todo_terminal":
                adapter_status = str(document["status"])
                existing_step_artifacts = self._adapter_step_artifact_ids(
                    plan, str(document["todoAttemptId"])
                )
                if not any(
                    kind == "game-ui-step-after-watermarked"
                    for _artifact_id, kind in existing_step_artifacts
                ):
                    after_operation = str(
                        self.store.get_todo_attempt(str(document["todoAttemptId"]))[
                            "operation"
                        ]
                    )
                    try:
                        self._capture_adapter_step_artifact(
                            plan,
                            document,
                            phase="after",
                            operation=after_operation,
                            capture_pids=capture_pids,
                        )
                    except (WindowCaptureError, OSError, ManagerValidation) as error:
                        self._record_step_capture_failure(
                            plan,
                            document,
                            phase="after",
                            operation=after_operation,
                            error=error,
                        )
                step_artifacts = self._adapter_step_artifact_ids(
                    plan,
                    str(document["todoAttemptId"]),
                )
                # The Adapter protocol uses ``failed`` for a fixed upstream
                # operation that ran and did not succeed.  Todo attempts do
                # not expose a separate failed state: an unresolved routine
                # operation is represented as blocked and retains the exact
                # Adapter status in the immutable adapter-event ledger.
                persisted_status = (
                    "blocked" if adapter_status == "failed" else adapter_status
                )
                terminal_attempt = self.store.finish_todo_attempt(
                    str(document["todoAttemptId"]),
                    status=persisted_status,
                    reason_code=str(document["reasonCode"]),
                    reason=str(document["reason"]),
                    retryable=bool(document["retryable"]),
                    evidence_refs=list(
                        dict.fromkeys(
                            [
                                str(value)
                                for value in document["evidenceArtifactIds"]
                            ]
                            + [artifact_id for artifact_id, _kind in step_artifacts]
                        )
                    ),
                )
                self._persist_terminal_todo_blocker(terminal_attempt)
                if terminal_attempt["state"] == "human_required":
                    self._end_controller_lease(
                        plan.run_attempt_id,
                        state=LeaseState.REVOKED,
                        reason_code="adapter_human_required",
                        reason=terminal_attempt.get("reason")
                        or "Adapter requested human control",
                    )

    def _record_step_capture_failure(
        self,
        plan: AdapterExecutionPlan,
        document: dict[str, Any],
        *,
        phase: str,
        operation: str,
        error: Exception,
    ) -> None:
        """Leave a durable trace when a Todo boundary screenshot could not be taken.

        Silent loss of a frame made failed runs look like they had no visual
        evidence at all.  The reason is logged and appended to the event ledger
        (inside the caller's transaction) without changing the Todo outcome.
        """

        reason = f"{type(error).__name__}: {error}"
        _log.warning(
            "todo.step_capture.failed phase=%s operation=%s reason=%s",
            phase,
            operation,
            reason,
        )
        try:
            self.store.append_event(
                "todo-step-capture.failed",
                "run-attempt",
                plan.run_attempt_id,
                {
                    "runId": plan.run_id,
                    "gameId": plan.game_id,
                    "todoInstanceId": str(document.get("todoInstanceId", "")),
                    "todoAttemptId": str(document.get("todoAttemptId", "")),
                    "operation": operation,
                    "stepPhase": phase,
                    "reason": reason[:500],
                },
            )
        except Exception:  # the ledger note is best effort
            pass

    def _capture_adapter_step_artifact(
        self,
        plan: AdapterExecutionPlan,
        document: dict[str, Any],
        *,
        phase: str,
        operation: str,
        capture_pids: frozenset[int] | None = None,
    ) -> str:
        """Capture one Manager-owned game frame for a Todo boundary."""

        if phase not in {"before", "after"}:
            raise ManagerValidation("unknown Todo screenshot phase")
        captured_at = utc_now()
        beijing_stamp = captured_at.astimezone(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S")
        watermark = (
            f"YeYu Gamer | {beijing_stamp} Beijing (UTC+8) | {operation}"
            if phase == "after"
            else None
        )
        captured = self.window_capture.capture(
            plan.game_id,
            watermark_text=watermark,
            allowed_pids=capture_pids,
        )
        artifact_id = str(uuid.uuid4())
        artifact_root = self.settings.data_dir / "artifacts"
        if str(artifact_root).startswith("\\\\"):
            raise ManagerValidation("step screenshot storage must be local")
        artifact_root.mkdir(parents=True, exist_ok=True)
        if _has_reparse_point(artifact_root):
            raise ManagerValidation("step screenshot storage contains a link")
        kind = (
            "game-ui-step-before-raw"
            if phase == "before"
            else "game-ui-step-after-watermarked"
        )
        file_name = f"todo-step-{phase}-{artifact_id}.png"
        final_path = artifact_root / file_name
        temp_path = artifact_root / f".{artifact_id}.tmp"
        try:
            temp_path.write_bytes(captured.content)
            os.replace(temp_path, final_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        todo = self.store.get_todo_instance(str(document["todoInstanceId"]))
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="captured",
            document={
                "kind": kind,
                "capturedAt": captured_at.isoformat(),
                "source": "manager-todo-step-capture-v1",
                "raw": phase == "before",
                "contentType": "image/png",
                "gameId": plan.game_id,
                "runId": plan.run_id,
                "runAttemptId": plan.run_attempt_id,
                "todoInstanceId": str(document["todoInstanceId"]),
                "todoAttemptId": str(document["todoAttemptId"]),
                "gameDayKey": str(todo["period_key"]),
                "operation": operation,
                "stepPhase": phase,
                "verdict": "pending",
                "hash": hashlib.sha256(captured.content).hexdigest(),
                "sizeBytes": len(captured.content),
                "fileName": file_name,
                "relativePath": file_name,
                "window": {
                    "hwnd": captured.hwnd,
                    "pid": captured.pid,
                    "processName": captured.process_name,
                    "title": captured.title,
                    "width": captured.width,
                    "height": captured.height,
                    "captureMethod": captured.method,
                },
            },
        )
        return artifact_id

    def _adapter_step_artifact_ids(
        self, plan: AdapterExecutionPlan, todo_attempt_id: str
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for resource in self.store.list_resources("artifact", limit=5000):
            artifact = dict(resource.get("document") or {})
            kind = str(artifact.get("kind") or "")
            if (
                artifact.get("runId") == plan.run_id
                and artifact.get("runAttemptId") == plan.run_attempt_id
                and artifact.get("todoAttemptId") == todo_attempt_id
                and kind
                in {
                    "game-ui-step-before-raw",
                    "game-ui-step-after-watermarked",
                }
            ):
                result.append((str(resource["resource_id"]), kind))
        return result

    def _persist_terminal_todo_blocker(
        self, todo_attempt: dict[str, Any]
    ) -> dict[str, Any]:
        status = str(todo_attempt["state"])
        if status not in {"blocked", "review_required", "human_required"}:
            return {"status": "not_applicable", "blockerId": None}
        evidence_refs = list(todo_attempt.get("evidence_refs", []))
        if not evidence_refs:
            return {
                "status": "not_persisted_missing_current_attempt_evidence",
                "blockerId": None,
            }
        run_attempt = self.store.get_run_attempt(todo_attempt["run_attempt_id"])
        todo = self.store.get_todo_instance(todo_attempt["todo_instance_id"])
        kind_by_status = {
            "blocked": TodoBlockerKind.CONTRACT_INVARIANT,
            "review_required": TodoBlockerKind.REVIEW_REQUIRED,
            "human_required": TodoBlockerKind.HUMAN_REQUIRED,
        }
        blocker_id = str(uuid.uuid4())
        raised_at = self._contract_datetime(
            todo_attempt.get("completed_at") or todo_attempt["updated_at"]
        )
        if not self.store.list_todo_blockers(
            run_id=run_attempt["run_id"],
            todo_instance_id=todo_attempt["todo_instance_id"],
            active_only=True,
            limit=1,
        ):
            candidate = TodoBlocker(
                blocker_id=blocker_id,
                scope=ExecutionScope(
                    manager_id=self.manager_id,
                    game_id=run_attempt["game_id"],
                    run_id=run_attempt["run_id"],
                    run_attempt_id=run_attempt["run_attempt_id"],
                    game_day_key=todo["period_key"],
                ),
                todo_instance_id=todo_attempt["todo_instance_id"],
                kind=kind_by_status[status],
                code=todo_attempt.get("reason_code") or f"todo_{status}",
                state=TodoBlockerState.ACTIVE,
                revision=1,
                raised_at=raised_at,
                transitioned_at=raised_at,
                reason=todo_attempt.get("reason") or f"Todo is {status}",
                artifact_refs=tuple(evidence_refs),
            )
            raise_todo_blocker(candidate)
        blocker, persistence_status = self.store.create_or_reuse_todo_blocker(
            {
                "blocker_id": blocker_id,
                "manager_id": self.manager_id,
                "game_id": run_attempt["game_id"],
                "run_id": run_attempt["run_id"],
                "run_attempt_id": run_attempt["run_attempt_id"],
                "todo_instance_id": todo_attempt["todo_instance_id"],
                "todo_attempt_id": todo_attempt["todo_attempt_id"],
                "game_day_key": todo["period_key"],
                "kind": kind_by_status[status].value,
                "code": todo_attempt.get("reason_code") or f"todo_{status}",
                "retryable": (
                    bool(todo_attempt.get("retryable"))
                    if status != "human_required"
                    else False
                ),
                "reason": todo_attempt.get("reason") or f"Todo is {status}",
                "artifact_refs": evidence_refs,
                "raised_at": raised_at.isoformat(),
            }
        )
        return {
            "status": persistence_status,
            "blockerId": blocker["blocker_id"],
        }

    def _finish_adapter_result(
        self,
        plan: AdapterExecutionPlan,
        result: AdapterRunResult,
        command_id: str | None = None,
        manager_control_outcome: str | None = None,
    ) -> AdapterRunResult | None:
        if manager_control_outcome not in {None, "human_required", "cancelled"}:
            raise ValueError("Manager control may only preserve a human or cancellation boundary")
        result_document = {
            "status": result.status,
            "transportOutcome": result.transport_outcome,
            "attemptedTodoInstanceIds": list(result.attempted_todo_instance_ids),
            "completedTodoInstanceIds": list(result.completed_todo_instance_ids),
            "unresolvedTodoInstanceIds": list(result.unresolved_todo_instance_ids),
            "protocolValid": result.protocol_valid,
            "code": result.code,
            "message": result.message,
        }
        attempt_state = manager_control_outcome or (result.status if result.protocol_valid else "failed")
        if manager_control_outcome is not None:
            result_document["managerControlOutcome"] = manager_control_outcome
        _log.info(
            "attempt.result status=%s protocolValid=%s code=%s attemptState=%s",
            result.status,
            result.protocol_valid,
            result.code,
            attempt_state,
        )
        run_state_by_result = {
            "completed": EntityState.REVIEW_REQUIRED,
            "partial": EntityState.REVIEW_REQUIRED,
            "blocked": EntityState.BLOCKED,
            "review_required": EntityState.REVIEW_REQUIRED,
            "human_required": EntityState.HUMAN_REQUIRED,
            "cancelled": EntityState.CANCELLED,
            "failed": EntityState.FAILED,
        }
        run_state = (
            run_state_by_result.get(result.status, EntityState.FAILED)
            if result.protocol_valid or manager_control_outcome is not None
            else EntityState.FAILED
        )
        with self.store.atomic():
            if self.store.get_run_attempt(plan.run_attempt_id)["state"] not in {"starting", "running", "cancelling"}:
                _log.info("attempt.result.ignored_terminal attempt=%s", plan.run_attempt_id)
                return None
            # Recheck under the state writer's lock: takeover/cancel may have
            # arrived after the process closer returned its final receipt.
            try:
                self._terminal_game_cleanup_gate(plan)
                for member in self.store.list_batch_run_memberships(run_id=plan.run_id, limit=5000):
                    if member.get("latest_run_attempt_id") == plan.run_attempt_id:
                        self._terminal_game_cleanup_gate(plan, str(member["batch_id"]))
            except (GameLaunchHumanRequired, GameLaunchCancelled) as control:
                manager_control_outcome = "human_required" if isinstance(control, GameLaunchHumanRequired) else "cancelled"
                result = replace(result, status=manager_control_outcome, completed_todo_instance_ids=(),
                    code=f"game_cleanup_{manager_control_outcome}", message=str(control))
                attempt_state = manager_control_outcome
                run_state = run_state_by_result[manager_control_outcome]
                result_document.update({"status": result.status, "completedTodoInstanceIds": [],
                    "code": result.code, "message": result.message, "managerControlOutcome": manager_control_outcome})
            prior_attempt = self.store.get_run_attempt(plan.run_attempt_id)
            prior_result = prior_attempt.get("result", {})
            prior_advance = (
                prior_result.get("nteProfileAdvance")
                if isinstance(prior_result, dict)
                else None
            )
            completed_operations = {
                todo.operation
                for todo in plan.todos
                if todo.todo_instance_id in result.completed_todo_instance_ids
            }
            if prior_advance is not None:
                result_document["nteProfileAdvance"] = prior_advance
            elif (
                result.protocol_valid
                and plan.game_id == "NTE"
                and "spend-city-vitality" in completed_operations
            ):
                config_values = self.store.get_config()["values"]
                profiles = config_values.get("daily_tool_profiles", {})
                raw_profile = (
                    profiles.get("nte") if isinstance(profiles, dict) else None
                )
                profile = NTEProfileConfig.model_validate(
                    raw_profile if isinstance(raw_profile, dict) else {}
                )
                advance: dict[str, Any] = {
                    "advanced": False,
                    "reason": "auto_cycle_disabled",
                }
                if profile.auto_cycle_sub_task:
                    if profile.anomaly_task_type == "经验与甲硬币":
                        options = ("角色经验", "弧盘经验", "甲硬币")
                        current_index = options.index(profile.exp_reward_target)
                        profile.exp_reward_target = options[
                            (current_index + 1) % len(options)
                        ]
                        next_value: str | int = profile.exp_reward_target
                    else:
                        maximum = 6 if profile.anomaly_task_type == "空幕" else 5
                        profile.material_index = profile.material_index % maximum + 1
                        next_value = profile.material_index
                    self.store.update_config(
                        {
                            "daily_tool_profiles": {
                                "nte": profile.model_dump(mode="json")
                            }
                        }
                    )
                    advance = {
                        "advanced": True,
                        "route": profile.anomaly_task_type,
                        "nextValue": next_value,
                    }
                result_document["nteProfileAdvance"] = advance
            blocker_persistence: dict[str, dict[str, Any]] = {}
            for todo_attempt in self.store.list_todo_attempts(
                run_attempt_id=plan.run_attempt_id, limit=500
            ):
                if todo_attempt["state"] == "running":
                    human_gate = (result.protocol_valid or manager_control_outcome is not None) and result.status == "human_required"
                    todo_attempt = self.store.finish_todo_attempt(
                        todo_attempt["todo_attempt_id"],
                        status=("human_required" if human_gate else "blocked"),
                        reason_code=(
                            result.code
                            if human_gate and result.code
                            else "outcome_unknown_"
                            + (result.code or "adapter_interrupted")
                        ),
                        reason=result.message or "Adapter ended before Todo terminal",
                        retryable=False,
                        evidence_refs=[],
                    )
                blocker_persistence[str(todo_attempt["todo_instance_id"])] = (
                    self._persist_terminal_todo_blocker(todo_attempt)
                )
            result_document["blockerPersistenceStatus"] = blocker_persistence
            self.store.update_run_attempt(
                plan.run_attempt_id,
                state=attempt_state,
                exit_code=result.exit_code,
                result=result_document,
                completed=True,
            )
            run = self.store.get_game_run(plan.run_id)
            completed_ids = list(
                dict.fromkeys(
                    [
                        *run["completed_todo_instance_ids"],
                        *result.completed_todo_instance_ids,
                    ]
                )
            )
            self.store.update_game_run(
                plan.run_id,
                state=run_state,
                exit_code=result.exit_code,
                message=(
                    (
                        f"Adapter {result.code}: {result.message}; "
                        "the game client is preserved and automatic retry is forbidden until explicit human release"
                    )
                    if run_state == EntityState.HUMAN_REQUIRED
                    else (
                        f"Adapter {result.code}: {result.message}; "
                        "Todo evidence is recorded, but accepted_done remains a separate review contract"
                    )
                ),
                completed_todo_instance_ids=completed_ids,
            )
            lease_state = (
                LeaseState.REVOKED
                if run_state
                in {
                    EntityState.HUMAN_REQUIRED,
                    EntityState.CANCELLED,
                    EntityState.FAILED,
                }
                else LeaseState.RELEASED
            )
            self._end_controller_lease(
                plan.run_attempt_id,
                state=lease_state,
                reason_code=f"adapter_{attempt_state}",
                reason="Adapter attempt reached a terminal Manager state",
            )
            self._terminalize_unsealed_batch_memberships(
                run_id=plan.run_id,
                run_attempt_id=plan.run_attempt_id,
                outcome=(manager_control_outcome or (result.status if result.protocol_valid else "failed")),
            )
        # A completed Adapter attempt still enters the per-Todo semantic review
        # contract.  Automatic accepted reviews stay disabled until every
        # required operation owns a machine-verifiable semantic predicate.
        self._complete_command(
            command_id or plan.run_id,
            (
                "succeeded"
                if result.protocol_valid and result.status == "completed"
                else "failed"
            ),
            result.message,
        )
        return result

    def _ensure_promoted_adapter_completion_review(
        self, plan: AdapterExecutionPlan
    ) -> CompletionReviewRecord | None:
        """Accept a normal run without scheduling an Agent evidence-review job.

        The promoted Adapter protocol is already fenced to one run and one exact
        Todo list.  Manager may therefore create an immutable machine review when
        every selected Todo ended as completed and supplied current-run evidence.
        Game-specific visual predicates remain fail-closed: StarRail additionally
        needs a raw current-run game screenshot and the indivisible training +
        reward operation pair. WW additionally needs both the original and the
        Beijing-time-watermarked screenshot from its reward-claim Todo, plus a
        signed Adapter artifact proving the same run reached 100 activity.
        """

        # No current AdapterExecutionPlan declares a complete, per-operation
        # machine semantic predicate set.  Generic before/after pairs therefore
        # cannot authorize an automatic review, including for WW.
        if not getattr(plan, "machine_semantic_predicates", None):
            return None

        lineage_review = self._ensure_promoted_adapter_lineage_completion_review(
            plan.run_id, expected_run_attempt_id=plan.run_attempt_id
        )
        if lineage_review is not None:
            return lineage_review

        attempts = self.store.list_todo_attempts(
            run_attempt_id=plan.run_attempt_id, limit=5000
        )
        attempts_by_todo = {
            str(item["todo_instance_id"]): item for item in attempts
        }
        selected = [
            attempts_by_todo.get(todo_id)
            for todo_id in plan.executable_todo_instance_ids
        ]
        if (
            not selected
            or any(item is None for item in selected)
            or any(item["state"] != "completed" for item in selected if item)
        ):
            return None
        artifact_refs = list(
            dict.fromkeys(
                artifact_id
                for item in selected
                if item is not None
                for artifact_id in item.get("evidence_refs", [])
            )
        )
        if not artifact_refs or any(
            not item.get("evidence_refs", []) for item in selected if item is not None
        ):
            return None

        artifact_documents: dict[str, dict[str, Any]] = {}
        artifact_integrity: dict[str, ArtifactIntegrityResult] = {}
        for artifact_id in artifact_refs:
            try:
                resource = self.store.get_resource("artifact", artifact_id)
            except RecordNotFound:
                return None
            document = dict(resource["document"])
            artifact_kind = str(document.get("kind") or "")
            is_watermark = artifact_kind.endswith("-watermarked")
            if (
                document.get("runId") != plan.run_id
                or document.get("runAttemptId") != plan.run_attempt_id
                or document.get("gameId") != plan.game_id
                or (
                    document.get("raw") is not False
                    if is_watermark
                    else document.get("raw") is not True
                )
            ):
                return None
            integrity = self._completion_artifact_integrity(
                artifact_id, document
            )
            if not integrity.valid:
                return None
            artifact_documents[artifact_id] = document
            artifact_integrity[artifact_id] = integrity

        # Every selected step must carry its own same-attempt before/after
        # transition evidence.  This is a product-wide daily contract, not a
        # game-specific exception: a text log or a screenshot from another
        # Todo can no longer make the run look complete.
        operation_by_todo = {
            target.todo_instance_id: target.operation for target in plan.todos
        }
        for item in selected:
            if item is None:
                return None
            todo_attempt_id = item.get("todo_attempt_id")
            kinds = {
                str(artifact_documents[artifact_id].get("kind") or "")
                for artifact_id in item.get("evidence_refs", [])
                if artifact_id in artifact_documents
                and (
                    todo_attempt_id is None
                    or artifact_documents[artifact_id].get("todoAttemptId")
                    == todo_attempt_id
                )
                and (
                    artifact_documents[artifact_id].get("todoInstanceId") is None
                    or artifact_documents[artifact_id].get("todoInstanceId")
                    == item["todo_instance_id"]
                )
            }
            step_pair_present = {
                "game-ui-step-before-raw",
                "game-ui-step-after-watermarked",
            }.issubset(kinds)
            legacy_ww_claim_pair = (
                plan.game_id == "WW"
                and operation_by_todo.get(item["todo_instance_id"])
                == "claim-daily-reward"
                and {
                    "game-ui-daily-reward-before",
                    "game-ui-daily-reward-watermarked",
                }.issubset(kinds)
            )
            if not step_pair_present and not legacy_ww_claim_pair:
                return None

        predicates: list[dict[str, Any]] = []
        if plan.game_id == "WW":
            claim_target = next(
                (item for item in plan.todos if item.operation == "claim-daily-reward"),
                None,
            )
            if claim_target is None:
                return None
            claim_attempt = attempts_by_todo.get(claim_target.todo_instance_id)
            if claim_attempt is None:
                return None
            claim_artifacts = {
                artifact_id: artifact_documents[artifact_id]
                for artifact_id in claim_attempt.get("evidence_refs", [])
                if artifact_id in artifact_documents
            }
            before_screenshot_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-reward-before"
                    and document.get("raw") is True
                    and str(document.get("contentType", "")).lower()
                    in {"image/png", "image/jpeg"}
                ),
                None,
            )
            raw_screenshot_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-reward-raw"
                    and document.get("raw") is True
                    and str(document.get("contentType", "")).lower()
                    in {"image/png", "image/jpeg"}
                ),
                None,
            )
            watermarked_screenshot_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-reward-watermarked"
                    and document.get("raw") is False
                    and str(document.get("contentType", "")).lower()
                    in {"image/png", "image/jpeg"}
                ),
                None,
            )
            activity_100_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-activity-100"
                    and document.get("raw") is True
                    and str(document.get("contentType", "")).lower()
                    == "text/plain"
                ),
                None,
            )
            if (
                before_screenshot_ref is None
                or raw_screenshot_ref is None
                or watermarked_screenshot_ref is None
                or activity_100_ref is None
            ):
                return None
            if (
                artifact_integrity[activity_100_ref]
                .content.decode("utf-8", errors="strict")
                .strip()
                != "dailyActivityPoints=100"
            ):
                return None
            predicates.append(
                {
                    "predicateId": "ww-daily-activity-100-and-reward-claim-screenshots",
                    "metrics": {
                        "dailyActivityPoints": 100,
                        "beforeClaimScreenshotPresent": True,
                        "rawScreenshotPresent": True,
                        "watermarkedScreenshotPresent": True,
                    },
                    "artifactRefs": [
                        before_screenshot_ref,
                        raw_screenshot_ref,
                        watermarked_screenshot_ref,
                        activity_100_ref,
                    ],
                }
            )
        if plan.game_id == "StarRail":
            # StarRail completion is visually semantic: a screenshot must show
            # 500/500 activity and all five reward tiers claimed.  A promoted
            # Adapter can prove that its fixed command ran, but it cannot turn
            # an arbitrary non-uniform game frame into those visual facts.
            # Leave the run pending for the scoped Agent completion review.
            return None
            operations = {item.operation for item in plan.todos}
            if not {
                "daily-training-objectives",
                "claim-daily-training-rewards",
            }.issubset(operations):
                return None
            claim_target = next(
                (item for item in plan.todos if item.operation == "claim-daily-training-rewards"),
                None,
            )
            if claim_target is None:
                return None
            claim_attempt = attempts_by_todo.get(claim_target.todo_instance_id)
            if claim_attempt is None:
                return None
            claim_artifacts = {
                artifact_id: artifact_documents[artifact_id]
                for artifact_id in claim_attempt.get("evidence_refs", [])
                if artifact_id in artifact_documents
            }
            screenshot_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-reward-raw"
                    and document.get("raw") is True
                    and str(document.get("contentType", "")).lower()
                    in {"image/png", "image/jpeg"}
                ),
                None,
            )
            watermarked_screenshot_ref = next(
                (
                    artifact_id
                    for artifact_id, document in claim_artifacts.items()
                    if document.get("kind") == "game-ui-daily-reward-watermarked"
                    and document.get("raw") is False
                    and str(document.get("contentType", "")).lower()
                    in {"image/png", "image/jpeg"}
                ),
                None,
            )
            if screenshot_ref is None or watermarked_screenshot_ref is None:
                return None
            predicates.append(
                {
                    "predicateId": "starrail-reward-claim-screenshot-pair",
                    "metrics": {
                        "rawScreenshotPresent": True,
                        "watermarkedScreenshotPresent": True,
                    },
                    "artifactRefs": [
                        screenshot_ref,
                        watermarked_screenshot_ref,
                    ],
                }
            )
            # Do not invent visual metrics from an Adapter exit. The promoted
            # Adapter review below is a distinct tool-authoritative path; an
            # actual human/Agent visual review still uses the stricter metrics.

        run = self.store.get_game_run(plan.run_id)
        frozen_todos = self._frozen_todos_for_run(run)
        game_day_key = next(
            (
                item.period_key
                for item in frozen_todos
                if item.todo_instance_id in plan.executable_todo_instance_ids
            ),
            None,
        )
        if not game_day_key:
            return None
        review_id = "completion-review-" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "/".join(
                    (
                        "yeyu-gamer",
                        "promoted-adapter-review",
                        plan.run_id,
                        plan.run_attempt_id,
                        game_day_key,
                    )
                ),
            )
        )
        reviewed_at = utc_now().isoformat()
        document = {
            "schemaVersion": 1,
            "workItemId": f"automatic:{plan.run_attempt_id}",
            "claimId": f"automatic:{plan.run_attempt_id}",
            "decisionId": f"automatic:{plan.run_attempt_id}",
            "reviewerPrincipalId": "manager:promoted-adapter",
            "decision": "accepted",
            "gameId": plan.game_id,
            "runId": plan.run_id,
            "runAttemptId": plan.run_attempt_id,
            "gameDayKey": game_day_key,
            "predicates": predicates,
            "artifactRefs": artifact_refs,
            "reviewedAt": reviewed_at,
            "reviewSource": "promoted-adapter-current-run-evidence",
        }
        try:
            resource = self.store.get_resource("completion-review", review_id)
        except RecordNotFound:
            resource = self.store.create_resource(
                "completion-review",
                resource_id=review_id,
                state="accepted",
                document=document,
            )
        return self._completion_review_record(resource)

    def _ensure_promoted_adapter_lineage_completion_review(
        self,
        run_id: str,
        *,
        expected_run_attempt_id: str | None = None,
    ) -> CompletionReviewRecord | None:
        """Create the promoted review from one frozen same-GameDay lineage.

        A new top-level daily batch may execute only the Todo that became eligible
        after an explicit human release.  Completed one-time actions remain owned
        by their earlier RunAttempts.  The completion snapshot already freezes
        those exact owners into one fail-closed lineage; this path applies the
        same per-step screenshot contract to that lineage instead of requiring
        every Todo to be replayed in the newest Adapter process.
        """

        try:
            run = self.store.get_game_run(run_id)
            snapshot = self._completion_contract_snapshot(run_id)
            frozen_todos = self._frozen_todos_for_run(run)
        except (
            AttributeError,
            ManagerConflict,
            RecordNotFound,
            TypeError,
            ValueError,
        ):
            return None

        if snapshot.game_id != "WW":
            return None

        current_attempt = snapshot.current_attempt
        if (
            current_attempt is None
            or current_attempt.state != "completed"
            or (
                expected_run_attempt_id is not None
                and current_attempt.run_attempt_id != expected_run_attempt_id
            )
        ):
            return None

        required_todos = [item for item in frozen_todos if item.required]
        facts_by_todo = {
            item.todo_instance_id: item for item in snapshot.todos if item.required
        }
        selected = [facts_by_todo.get(item.todo_instance_id) for item in required_todos]
        if (
            not selected
            or any(item is None for item in selected)
            or any(item.status != "completed" for item in selected if item is not None)
        ):
            return None

        attempt_runs = {
            item.run_attempt_id: item.run_id for item in snapshot.attempt_lineage
        }
        evidence_by_id = {item.artifact_id: item for item in snapshot.evidence}
        artifact_refs = list(
            dict.fromkeys(
                artifact_id
                for item in selected
                if item is not None
                for artifact_id in item.evidence_refs
            )
        )
        if not artifact_refs:
            return None

        scoped_evidence_by_todo: dict[str, dict[str, Any]] = {}
        for item in selected:
            if item is None or item.run_attempt_id is None or not item.evidence_refs:
                return None
            scoped: dict[str, Any] = {}
            for artifact_id in item.evidence_refs:
                artifact = evidence_by_id.get(artifact_id)
                if (
                    artifact is None
                    or artifact.game_id != snapshot.game_id
                    or artifact.todo_instance_id != item.todo_instance_id
                    or artifact.run_attempt_id != item.run_attempt_id
                    or attempt_runs.get(artifact.run_attempt_id) != artifact.run_id
                    or artifact.game_day_key != snapshot.game_day.period_key
                    or not (
                        snapshot.game_day.starts_at
                        <= artifact.captured_at
                        < snapshot.game_day.ends_at
                    )
                ):
                    return None
                scoped[artifact_id] = artifact
            kinds = {artifact.kind for artifact in scoped.values()}
            step_pair_present = {
                "game-ui-step-before-raw",
                "game-ui-step-after-watermarked",
            }.issubset(kinds)
            operation = next(
                (
                    todo.operation
                    for todo in required_todos
                    if todo.todo_instance_id == item.todo_instance_id
                ),
                "",
            )
            legacy_ww_claim_pair = (
                snapshot.game_id == "WW"
                and operation == "claim-daily-reward"
                and {
                    "game-ui-daily-reward-before",
                    "game-ui-daily-reward-watermarked",
                }.issubset(kinds)
            )
            if not step_pair_present and not legacy_ww_claim_pair:
                return None
            scoped_evidence_by_todo[item.todo_instance_id] = scoped

        predicates: list[dict[str, Any]] = []
        operations = {item.operation for item in required_todos}
        todo_by_operation = {item.operation: item for item in required_todos}
        if snapshot.game_id == "WW":
            claim_todo = todo_by_operation.get("claim-daily-reward")
            if claim_todo is None:
                return None
            claim_artifacts = scoped_evidence_by_todo.get(
                claim_todo.todo_instance_id, {}
            )

            def ww_ref(kind: str, *, raw: bool, content_type: str) -> str | None:
                return next(
                    (
                        artifact_id
                        for artifact_id, artifact in claim_artifacts.items()
                        if artifact.kind == kind
                        and artifact.raw is raw
                        and artifact.content_type.lower() == content_type
                    ),
                    None,
                )

            before_ref = ww_ref(
                "game-ui-daily-reward-before", raw=True, content_type="image/png"
            ) or next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-reward-before"
                    and artifact.raw
                    and artifact.is_screenshot
                ),
                None,
            )
            raw_ref = next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-reward-raw"
                    and artifact.raw
                    and artifact.is_screenshot
                ),
                None,
            )
            watermarked_ref = next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-reward-watermarked"
                    and not artifact.raw
                    and artifact.is_screenshot
                ),
                None,
            )
            activity_ref = next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-activity-100"
                    and artifact.raw
                    and artifact.content_type.lower() == "text/plain"
                ),
                None,
            )
            if None in {before_ref, raw_ref, watermarked_ref, activity_ref}:
                return None
            assert activity_ref is not None
            try:
                activity_document = dict(
                    self.store.get_resource("artifact", activity_ref)["document"]
                )
            except (KeyError, RecordNotFound, TypeError):
                return None
            activity_integrity = self._completion_artifact_integrity(
                activity_ref, activity_document
            )
            if (
                not activity_integrity.valid
                or activity_integrity.content.decode(
                    "utf-8", errors="strict"
                ).strip()
                != "dailyActivityPoints=100"
            ):
                return None
            predicates.append(
                {
                    "predicateId": "ww-daily-activity-100-and-reward-claim-screenshots",
                    "metrics": {
                        "dailyActivityPoints": 100,
                        "beforeClaimScreenshotPresent": True,
                        "rawScreenshotPresent": True,
                        "watermarkedScreenshotPresent": True,
                    },
                    "artifactRefs": [
                        before_ref,
                        raw_ref,
                        watermarked_ref,
                        activity_ref,
                    ],
                }
            )
        elif snapshot.game_id == "StarRail":
            # Same rule as the single-attempt path above.  Same-day lineage does
            # not weaken the required visual review.
            return None
            if not {
                "daily-training-objectives",
                "claim-daily-training-rewards",
            }.issubset(operations):
                return None
            claim_todo = todo_by_operation["claim-daily-training-rewards"]
            claim_artifacts = scoped_evidence_by_todo.get(
                claim_todo.todo_instance_id, {}
            )
            raw_ref = next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-reward-raw"
                    and artifact.raw
                    and artifact.is_screenshot
                ),
                None,
            )
            watermarked_ref = next(
                (
                    artifact_id
                    for artifact_id, artifact in claim_artifacts.items()
                    if artifact.kind == "game-ui-daily-reward-watermarked"
                    and not artifact.raw
                    and artifact.is_screenshot
                ),
                None,
            )
            if raw_ref is None or watermarked_ref is None:
                return None
            predicates.append(
                {
                    "predicateId": "starrail-reward-claim-screenshot-pair",
                    "metrics": {
                        "rawScreenshotPresent": True,
                        "watermarkedScreenshotPresent": True,
                    },
                    "artifactRefs": [raw_ref, watermarked_ref],
                }
            )

        game_day_key = snapshot.game_day.period_key
        review_id = "completion-review-" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "/".join(
                    (
                        "yeyu-gamer",
                        "promoted-adapter-lineage-review",
                        snapshot.run_id,
                        current_attempt.run_attempt_id,
                        game_day_key,
                    )
                ),
            )
        )
        document = {
            "schemaVersion": 1,
            "workItemId": f"automatic:{current_attempt.run_attempt_id}",
            "claimId": f"automatic:{current_attempt.run_attempt_id}",
            "decisionId": f"automatic:{current_attempt.run_attempt_id}",
            "reviewerPrincipalId": "manager:promoted-adapter",
            "decision": "accepted",
            "gameId": snapshot.game_id,
            "runId": snapshot.run_id,
            "runAttemptId": current_attempt.run_attempt_id,
            "gameDayKey": game_day_key,
            "predicates": predicates,
            "artifactRefs": artifact_refs,
            "reviewedAt": utc_now().isoformat(),
            "reviewSource": "promoted-adapter-same-game-day-lineage-evidence",
        }
        try:
            resource = self.store.get_resource("completion-review", review_id)
        except RecordNotFound:
            resource = self.store.create_resource(
                "completion-review",
                resource_id=review_id,
                state="accepted",
                document=document,
            )
        return self._completion_review_record(resource)

    def _start_game_run(
        self,
        run_id: str,
        completed_callback: Callable[[AdapterRunResult], None] | None = None,
        command_id: str | None = None,
        todo_instance_ids: list[str] | None = None,
        attempt_ordinal: int | None = None,
    ) -> tuple[AdapterExecutionPlan, int]:
        run = self.store.get_game_run(run_id)

        # Validate and normalize all adapter inputs before a RunAttempt claims
        # the interactive desktop.  A malformed legacy profile must fail as a
        # configuration error, never leave a "running" attempt or controller
        # lease behind.
        manager_config = self.store.get_config()["values"]
        configured_paths = manager_config.get("game_paths", {})
        path_binding = configured_paths.get(str(run["game_id"]), {})
        emulator_config = path_binding.get("emulator")
        emulator_binding: LDPlayerBinding | None = None
        if isinstance(emulator_config, dict):
            emulator_binding = LDPlayerBinding(
                game_id=str(run["game_id"]),
                console_path=str(emulator_config.get("console_path", "")),
                adb_path=str(emulator_config.get("adb_path", "")),
                instance_index=int(emulator_config.get("instance_index", 0)),
                adb_serial=str(emulator_config.get("adb_serial", "")),
                instance_name=(
                    str(emulator_config["instance_name"])
                    if emulator_config.get("instance_name") is not None
                    else None
                ),
            )
        installation_binding = {
            "gamePath": path_binding.get("game_path"),
            "toolPath": path_binding.get("tool_path"),
        }
        if emulator_binding is not None:
            installation_binding["emulatorBinding"] = {
                "provider": "ldplayer",
                "consolePath": emulator_binding.console_path,
                "adbPath": emulator_binding.adb_path,
                "instanceIndex": emulator_binding.instance_index,
                "instanceName": emulator_binding.instance_name,
                "adbSerial": emulator_binding.adb_serial,
            }
        if run["game_id"] == "WW":
            raw_profiles = manager_config.get("daily_tool_profiles", {})
            raw_ok_ww_profile = (
                raw_profiles.get("ok_ww")
                if isinstance(raw_profiles, dict)
                else None
            )
            # Older Manager builds persisted upstream-only switches that this
            # orchestrator deliberately does not expose.  Keep the supported
            # routing values and discard only those obsolete keys.
            raw_profile = raw_ok_ww_profile if isinstance(raw_ok_ww_profile, dict) else {}
            supported_profile = {
                field_name: raw_profile[field_name]
                for field_name in OKWWProfileConfig.model_fields
                if field_name in raw_profile
            }
            try:
                ok_ww_profile = OKWWProfileConfig.model_validate(supported_profile)
            except ValueError as error:
                raise ManagerValidation(
                    "Stored OK-WW daily profile is invalid"
                ) from error
            installation_binding["dailyTaskProfile"] = ok_ww_profile.model_dump(
                by_alias=True
            )
        elif run["game_id"] == "NTE":
            raw_profiles = manager_config.get("daily_tool_profiles", {})
            raw_nte_profile = (
                raw_profiles.get("nte")
                if isinstance(raw_profiles, dict)
                else None
            )
            raw_profile = raw_nte_profile if isinstance(raw_nte_profile, dict) else {}
            supported_profile = {
                field_name: raw_profile[field_name]
                for field_name in NTEProfileConfig.model_fields
                if field_name in raw_profile
            }
            try:
                nte_profile = NTEProfileConfig.model_validate(supported_profile)
            except ValueError as error:
                raise ManagerValidation(
                    "Stored OK-NTE daily profile is invalid"
                ) from error
            installation_binding["dailyTaskProfile"] = nte_profile.model_dump(
                by_alias=True
            )
        elif run["game_id"] == "CZN":
            raw_profiles = manager_config.get("daily_tool_profiles", {})
            raw_czn_profile = (
                raw_profiles.get("czn") if isinstance(raw_profiles, dict) else None
            )
            raw_profile = raw_czn_profile if isinstance(raw_czn_profile, dict) else {}
            supported_profile = {
                field_name: raw_profile[field_name]
                for field_name in CZNProfileConfig.model_fields
                if field_name in raw_profile
            }
            try:
                czn_profile = CZNProfileConfig.model_validate(supported_profile)
            except ValueError as error:
                raise ManagerValidation(
                    "Stored Maa_KES daily profile is invalid"
                ) from error
            installation_binding["dailyTaskProfile"] = czn_profile.model_dump(
                by_alias=True
            )

        plan = self._prepare_run_attempt(
            run,
            todo_instance_ids=todo_instance_ids,
            attempt_ordinal=attempt_ordinal,
        )
        attempt_log = self._open_attempt_log(
            game_id=str(run["game_id"]), run_attempt_id=plan.run_attempt_id
        )
        log_fields = {
            "run": plan.run_id,
            "attempt": plan.run_attempt_id,
            "game": str(run["game_id"]),
        }
        with bind_log_context(**log_fields, phase="prepare"):
            _log.info(
                "attempt.prepared ordinal=%s todos=%s timeoutSeconds=%s expiresAt=%s logDir=%s",
                attempt_ordinal,
                [todo.operation for todo in plan.todos],
                plan.timeout_seconds,
                plan.expires_at,
                attempt_log.directory if attempt_log is not None else None,
            )
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"launchState": "starting-fixed-host"},
            completed=False,
        )
        self.store.update_game_run(
            run_id,
            state=EntityState.RUNNING,
            message="fenced Manager Adapter launch in progress",
        )

        game_launch: GameLaunchReceipt | EmulatorLaunchReceipt | None = None
        capture_pids: frozenset[int] | None = None

        def cleanup_game(preserve_status: str | None = None) -> tuple[GameCloseReceipt | EmulatorCloseReceipt, str | None]:
            try:
                self._terminal_game_cleanup_gate(plan)
                if preserve_status in {"human_required", "cancelled"}:
                    state = "preserved-human-required" if preserve_status == "human_required" else "preserved-cooperative-cancel"
                    return GameCloseReceipt(state, (), ()), None
                if game_launch is None:
                    return GameCloseReceipt("not-started", (), ()), None
                if emulator_binding is not None:
                    return self.emulator_launcher.close_started(emulator_binding, game_launch), None
                return self._close_finished_batch_game(
                    plan, game_launch, installation_binding.get("gamePath"),
                ), None
            except GameLaunchHumanRequired as error:
                _log.info("attempt.cleanup.preserved_human code=%s message=%s", error.reason_code, error)
                return GameCloseReceipt("preserved-human-required", (), ()), "human_required"
            except GameLaunchCancelled as error:
                _log.info("attempt.cleanup.preserved_cancel message=%s", error)
                return GameCloseReceipt("preserved-cooperative-cancel", (), ()), "cancelled"
            except Exception as error:
                _log.warning("attempt.cleanup.failed error=%s: %s", type(error).__name__, error)
                return GameCloseReceipt("close-failed", (), ()), None

        def finished(result: AdapterRunResult) -> None:
            with bind_log_context(**log_fields, phase="finish"):
                _finished(result)
            self._close_attempt_log(plan.run_attempt_id)

        def _finished(result: AdapterRunResult) -> None:
            effective_result = result
            manager_control_outcome: str | None = None
            _log.info(
                "attempt.adapter_finished status=%s transport=%s code=%s exit=%s "
                "completed=%s unresolved=%s message=%s",
                result.status,
                result.transport_outcome,
                result.code,
                result.exit_code,
                len(result.completed_todo_instance_ids),
                len(result.unresolved_todo_instance_ids),
                result.message,
            )
            try:
                attempt = self.store.get_run_attempt(plan.run_attempt_id)
                if attempt["state"] not in {"starting", "running", "cancelling"}:
                    effective_result = None
                    return
                cleanup, manager_control_outcome = cleanup_game(result.status)
                if manager_control_outcome is not None:
                    effective_result = replace(
                        result, status=manager_control_outcome,
                        completed_todo_instance_ids=(),
                        code=f"game_cleanup_{manager_control_outcome}",
                        message="Manager preserved the client at the durable human/cancellation boundary.",
                    )
                _log.info(
                    "attempt.game_cleanup state=%s requested=%s remaining=%s",
                    cleanup.state,
                    list(cleanup.requested_process_ids),
                    list(cleanup.remaining_process_ids),
                )
                with self.store.atomic():
                    attempt = self.store.get_run_attempt(plan.run_attempt_id)
                    if attempt["state"] in {"starting", "running", "cancelling"}:
                        self.store.update_run_attempt(
                            plan.run_attempt_id,
                            state=str(attempt["state"]),
                            result={"gameCleanup": cleanup.as_result()},
                            completed=False,
                        )
                if self._game_cleanup_failed(cleanup):
                    effective_result = replace(
                        result,
                        status="failed",
                        transport_outcome="failed",
                        protocol_valid=False,
                        code="game_cleanup_failed",
                        message=(
                            "The automation tool ended, but game process closure "
                            "could not be fully verified."
                        ),
                    )
                effective_result = self._finish_adapter_result(
                    plan, effective_result, command_id=command_id,
                    manager_control_outcome=manager_control_outcome,
                )
            except Exception as error:  # defensive persistence boundary
                failure = f"Adapter result persistence failed: {type(error).__name__}"
                _log.exception("attempt.persistence_failed %s", failure)
                try:
                    attempt = self.store.get_run_attempt(plan.run_attempt_id)
                    if attempt["state"] in {"starting", "running", "cancelling"}:
                        self.store.update_run_attempt(
                            plan.run_attempt_id,
                            state="failed",
                            exit_code=effective_result.exit_code,
                            result={"code": "manager_persistence_failed", "message": failure},
                            completed=True,
                        )
                    self.store.update_game_run(
                        plan.run_id,
                        state=EntityState.FAILED,
                        exit_code=effective_result.exit_code,
                        message=failure,
                    )
                    self._end_controller_lease(
                        plan.run_attempt_id,
                        state=LeaseState.REVOKED,
                        reason_code="manager_persistence_failed",
                        reason=failure,
                    )
                    self._terminalize_unsealed_batch_memberships(
                        run_id=plan.run_id,
                        run_attempt_id=plan.run_attempt_id,
                        outcome="failed",
                    )
                except Exception:
                    pass
            finally:
                if completed_callback is not None and effective_result is not None:
                    completed_callback(effective_result)

        launch_log_scope = contextlib.ExitStack()
        launch_log_scope.enter_context(bind_log_context(**log_fields, phase="launch"))
        try:
            self.store.update_run_attempt(
                plan.run_attempt_id,
                state="running",
                result={"launchState": "starting-game-client"},
                completed=False,
            )
            _log.info(
                "attempt.launch.begin gamePath=%s emulator=%s",
                installation_binding.get("gamePath"),
                emulator_binding is not None,
            )
            self.store.update_game_run(
                run_id,
                state=EntityState.RUNNING,
                message="Manager starting configured game client before Adapter launch",
            )
            self._close_other_batch_games(plan, configured_paths)
            if self._launch_cancel_requested(plan.run_attempt_id):
                raise GameLaunchCancelled("Manager cancellation won the queue-cleanup to game-launch handoff")
            if self.store.get_game_run(run_id)["state"] == EntityState.HUMAN_REQUIRED:
                raise GameLaunchHumanRequired(
                    "queue_cleanup_human_takeover", "Human takeover won the queue-cleanup to game-launch handoff.",
                )
            if emulator_binding is not None:
                game_launch = self.emulator_launcher.ensure_started(emulator_binding)
                emulator_state = self.emulator_launcher.inspect(emulator_binding)
                capture_pids = frozenset(
                    pid
                    for pid in (
                        emulator_state.instance.player_process_id,
                        emulator_state.instance.virtual_machine_process_id,
                    )
                    if isinstance(pid, int) and pid > 0
                )
                if not capture_pids:
                    raise EmulatorBindingError(
                        "the validated LDPlayer instance has no captureable process PID"
                    )
            else:
                game_path = installation_binding["gamePath"]
                if not isinstance(game_path, str) or not game_path:
                    raise GameLaunchError(
                        "configured game executable is required before Adapter launch"
                    )
                self._record_zombie_game_processes(plan)
                game_launch = self.game_launcher.ensure_started(
                    run["game_id"],
                    game_path,
                    observer=lambda observation: self._record_launch_observation(
                        plan, observation
                    ),
                    cancel_requested=lambda: self._launch_cancel_requested(
                        plan.run_attempt_id
                    ),
                )
            _log.info(
                "attempt.launch.game_ready %s",
                game_launch.as_result() if game_launch is not None else None,
            )
            with self._execution_handoff_lock:
                if self._launch_cancel_requested(plan.run_attempt_id):
                    raise GameLaunchCancelled(
                        "Manager cancellation won the game-launch to Adapter handoff"
                    )
                pid = self.adapter_host.execute(
                    run_id,
                    run["game_id"],
                    finished,
                    plan=plan,
                    on_event=lambda event, manifest: self._handle_adapter_event(
                        plan, event, manifest, capture_pids
                    ),
                    installation_binding=installation_binding,
                    transcript=attempt_log,
                )
                _log.info("attempt.launch.adapter_host_started pid=%s", pid)
                attempt = self.store.get_run_attempt(plan.run_attempt_id)
                if attempt["state"] in {"starting", "running", "cancelling"}:
                    self.store.update_run_attempt(
                        plan.run_attempt_id,
                        state=(
                            "cancelling"
                            if attempt["state"] == "cancelling"
                            else "running"
                        ),
                        process_id=pid,
                        result={
                            "launchState": "fixed-host-started",
                            "gameLaunch": game_launch.as_result(),
                        },
                        completed=False,
                    )
            attempt = self.store.get_run_attempt(plan.run_attempt_id)
            if attempt["state"] in {"starting", "running", "cancelling"}:
                self.store.update_game_run(
                    run_id,
                    state=(
                        EntityState.CANCELLING
                        if attempt["state"] == "cancelling"
                        else EntityState.RUNNING
                    ),
                    message=(
                        "Adapter Host started after cancellation became durable; "
                        "its fenced cancellation is being delivered"
                        if attempt["state"] == "cancelling"
                        else "自动化工具已启动，等待其回传实际每日步骤"
                    ),
                )
            return plan, pid
        except GameLaunchCancelled as error:
            _log.info(
                "attempt.launch.cancelled target=manager-game-launch message=%s",
                error,
            )
            attempt = self.store.get_run_attempt(plan.run_attempt_id)
            if attempt["state"] in {"starting", "running", "cancelling"}:
                self.store.update_run_attempt(
                    plan.run_attempt_id,
                    state="cancelled",
                    result={
                        "code": "game_launch_cancelled",
                        "message": str(error),
                        "cancelDeliveryTarget": "manager-game-launch",
                        "gameCleanup": GameCloseReceipt(
                            "preserved-cooperative-cancel", (), ()
                        ).as_result(),
                    },
                    completed=True,
                )
            self.store.update_game_run(
                run_id,
                state=EntityState.CANCELLED,
                message="Manager cancellation stopped the pre-Adapter game launch; visible clients were preserved.",
            )
            self._end_controller_lease(
                plan.run_attempt_id,
                state=LeaseState.REVOKED,
                reason_code="game_launch_cancelled",
                reason=str(error),
            )
            self._terminalize_unsealed_batch_memberships(
                run_id=run_id,
                run_attempt_id=plan.run_attempt_id,
                outcome="cancelled",
            )
            self._close_attempt_log(plan.run_attempt_id)
            raise
        except GameLaunchHumanRequired as error:
            _log.info(
                "attempt.launch.human_required code=%s message=%s",
                error.reason_code,
                error,
            )
            attempt = self.store.get_run_attempt(plan.run_attempt_id)
            if attempt["state"] in {"starting", "running", "cancelling"}:
                self.store.update_run_attempt(
                    plan.run_attempt_id,
                    state="human_required",
                    result={
                        "launchState": "launch-human-required",
                        "code": error.reason_code,
                        "message": str(error),
                        "details": dict(error.detail),
                        "gameCleanup": GameCloseReceipt(
                            "preserved-human-required",
                            (),
                            tuple(sorted(error.process_ids)),
                        ).as_result(),
                    },
                    completed=True,
                )
            self.store.update_game_run(
                run_id,
                state=EntityState.HUMAN_REQUIRED,
                message=(
                    f"Game launch {error.reason_code}: {error}; the game client "
                    "is preserved and automatic retry is forbidden until explicit human release"
                ),
            )
            self._end_controller_lease(
                plan.run_attempt_id,
                state=LeaseState.REVOKED,
                reason_code=error.reason_code,
                reason=str(error),
            )
            self._terminalize_unsealed_batch_memberships(
                run_id=run_id,
                run_attempt_id=plan.run_attempt_id,
                outcome="human_required",
            )
            self._close_attempt_log(plan.run_attempt_id)
            raise
        except Exception as error:
            failure_code = (
                "game_start_failed"
                if isinstance(error, (GameLaunchError, EmulatorBindingError))
                else "adapter_start_failed"
            )
            _log.error(
                "attempt.launch.failed code=%s error=%s: %s",
                failure_code,
                type(error).__name__,
                error,
                exc_info=not isinstance(error, (GameLaunchError, EmulatorBindingError)),
            )
            cleanup: GameCloseReceipt | EmulatorCloseReceipt | None = None
            preserved_state: str | None = None
            if game_launch is not None:
                try:
                    cleanup, preserved_state = cleanup_game()
                    if self._game_cleanup_failed(cleanup):
                        failure_code = "game_cleanup_failed"
                except Exception:
                    cleanup = None
                    failure_code = "game_cleanup_failed"
            with self.store.atomic():
                try:
                    self._terminal_game_cleanup_gate(plan)
                    for member in self.store.list_batch_run_memberships(run_id=plan.run_id, limit=5000):
                        if member.get("latest_run_attempt_id") == plan.run_attempt_id:
                            self._terminal_game_cleanup_gate(plan, str(member["batch_id"]))
                except (GameLaunchHumanRequired, GameLaunchCancelled) as control:
                    preserved_state = "human_required" if isinstance(control, GameLaunchHumanRequired) else "cancelled"
                attempt = self.store.get_run_attempt(plan.run_attempt_id)
                if preserved_state is not None:
                    failure_code = f"game_cleanup_{preserved_state}"
                if attempt["state"] in {"starting", "running", "cancelling"}:
                    self.store.update_run_attempt(
                        plan.run_attempt_id,
                        state=preserved_state or "failed",
                        result={
                            "code": failure_code,
                            "message": f"{type(error).__name__}: {error}",
                            **(
                                {"gameCleanup": cleanup.as_result()}
                                if cleanup is not None
                                else {}
                            ),
                        },
                        completed=True,
                    )
                self.store.update_game_run(
                    run_id,
                    state=EntityState(preserved_state) if preserved_state else EntityState.FAILED,
                    message=f"Manager Adapter start failed: {type(error).__name__}: {error}",
                )
                self._end_controller_lease(
                    plan.run_attempt_id,
                    state=LeaseState.REVOKED,
                    reason_code=failure_code,
                    reason=f"{type(error).__name__}: {error}",
                )
                self._terminalize_unsealed_batch_memberships(
                    run_id=run_id,
                    run_attempt_id=plan.run_attempt_id,
                    outcome=preserved_state or "failed",
                )
            self._close_attempt_log(plan.run_attempt_id)
            raise
        finally:
            launch_log_scope.close()

    @staticmethod
    def _game_cleanup_failed(cleanup: GameCloseReceipt | EmulatorCloseReceipt) -> bool:
        if isinstance(cleanup, EmulatorCloseReceipt):
            return cleanup.state == "close-failed"
        if cleanup.state.startswith("preserved-") or cleanup.state == "not-started":
            return False
        return bool(
            cleanup.state not in {"closed", "already-closed"}
            or cleanup.remaining_process_ids
            or getattr(cleanup, "zombie_process_ids", ())
            or getattr(cleanup, "unverified_process_ids", ())
        )

    def _terminal_game_cleanup_gate(self, plan: AdapterExecutionPlan, batch_id: str | None = None) -> bool:
        run = self.store.get_game_run(plan.run_id)
        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        if run["state"] == EntityState.HUMAN_REQUIRED or attempt["state"] == "human_required" or any(
            blocker["kind"] == "human_required"
            for blocker in self.store.list_todo_blockers(run_id=plan.run_id, active_only=True, limit=5000)
        ):
            raise GameLaunchHumanRequired("terminal_cleanup_human_required", "Human takeover preserves the game during terminal cleanup.")
        if self._launch_cancel_requested(plan.run_attempt_id) or run["state"] in {EntityState.CANCELLING, EntityState.CANCELLED}:
            raise GameLaunchCancelled("Durable cancellation stopped terminal game cleanup.")
        if batch_id is not None:
            batch = self.store.get_batch(batch_id)
            members = self.store.list_batch_run_memberships(run_id=plan.run_id, limit=5000)
            if (
                batch["result"].get("sealVersion") is not None
                or batch["state"] == EntityState.CANCELLED
                or self.store.get_active_batch_cancel_request(batch_id) is not None
                or not any(member["batch_id"] == batch_id and member["state"] == "active"
                    and member.get("latest_run_attempt_id") == plan.run_attempt_id for member in members)
            ):
                raise GameLaunchCancelled("The frozen queue no longer authorizes terminal game cleanup.")
        return False

    def _close_finished_batch_game(
        self, plan: AdapterExecutionPlan, launch: GameLaunchReceipt, game_path: object,
    ) -> GameCloseReceipt:
        """Close the current native game, including a preexisting final client, within its frozen queue."""
        memberships = self.store.list_batch_run_memberships(run_id=plan.run_id, limit=5000)
        if not memberships:
            return self.game_launcher.close_started(launch)
        batches = [self.store.get_batch(str(member["batch_id"])) for member in memberships
            if member["state"] == "active" and member.get("latest_run_attempt_id") == plan.run_attempt_id]
        if len(batches) != 1:
            raise GameLaunchCancelled("No unique active queue owns this terminal attempt; preserve the client.")
        batch = batches[0]
        batch_id = str(batch["batch_id"])
        self._terminal_game_cleanup_gate(plan, batch_id)
        candidates = batch["result"].get("candidateGameIds")
        if (batch["mode"] != RequestMode.EXECUTE or not isinstance(candidates, list)
            or not all(isinstance(value, str) and value for value in candidates)
            or len(set(candidates)) != len(candidates) or plan.game_id not in candidates
            or not isinstance(game_path, str) or not game_path):
            raise GameLaunchHumanRequired("terminal_cleanup_scope_unavailable", "The frozen queue or installation binding cannot authorize closing this game.")
        cleanup = self.game_launcher.close_for_queue(
            plan.game_id, game_path,
            cancel_requested=lambda: self._terminal_game_cleanup_gate(plan, batch_id),
        )
        if not isinstance(cleanup, GameCloseReceipt):
            raise GameLaunchError("Terminal queue cleanup returned an invalid receipt")
        if cleanup.state not in {"closed", "already-closed", "close-failed"}:
            raise GameLaunchError("Terminal queue cleanup returned an unsupported closure state")
        self.store.append_event("game-run.queue-cleanup", "run-attempt", plan.run_attempt_id,
            {"runId": plan.run_id, "gameId": plan.game_id, "batchId": batch_id, **cleanup.as_result()})
        self._terminal_game_cleanup_gate(plan, batch_id)
        return cleanup

    def _close_other_batch_games(
        self, plan: AdapterExecutionPlan, configured_paths: dict[str, Any]
    ) -> None:
        """Close only other games authorized by this attempt's frozen queue."""
        batches = []
        for membership in self.store.list_batch_run_memberships(run_id=plan.run_id, limit=5000):
            batch = self.store.get_batch(str(membership["batch_id"]))
            if (
                batch["result"].get("sealVersion") is None
                and batch["mode"] == RequestMode.EXECUTE
                and membership["state"] == "active"
                and membership.get("latest_run_attempt_id") == plan.run_attempt_id
            ):
                batches.append(batch)
        if not batches:
            return  # An independent GameRun has no authority over another game.
        if len(batches) != 1:
            raise GameLaunchHumanRequired(
                "queue_cleanup_scope_ambiguous",
                "The current attempt has multiple unsealed queues; preserve clients and inspect its scope.",
            )
        batch = batches[0]
        batch_id = str(batch["batch_id"])
        candidates = batch["result"].get("candidateGameIds")
        if (
            not isinstance(candidates, list)
            or not all(isinstance(value, str) and value for value in candidates)
            or len(set(candidates)) != len(candidates)
            or plan.game_id not in candidates
        ):
            raise GameLaunchHumanRequired(
                "queue_cleanup_scope_unavailable",
                "The queue has no valid frozen game scope; preserve clients and inspect its scope.",
            )
        other_game_ids = [game_id for game_id in candidates if game_id != plan.game_id]
        report: dict[str, Any] = {
            "batchId": batch_id,
            "currentGameId": plan.game_id,
            "candidateGameIds": list(candidates),
            "targetGameIds": other_game_ids,
            "state": "checking",
            "games": [],
        }

        def persist() -> None:
            attempt = self.store.get_run_attempt(plan.run_attempt_id)
            if attempt["state"] in {"starting", "running", "cancelling"}:
                self.store.update_run_attempt(
                    plan.run_attempt_id, state=attempt["state"],
                    result={"queueGameCleanup": report}, completed=False,
                )
            self.store.append_event(
                "game-launch.queue-cleanup", "run-attempt", plan.run_attempt_id,
                {"runId": plan.run_id, "gameId": plan.game_id, **report},
            )
            _log.info("launch.queue_cleanup report=%s", report)

        def check_gate() -> bool:
            current = self.store.get_batch(batch_id)
            if (
                self._launch_cancel_requested(plan.run_attempt_id)
                or current["result"].get("sealVersion") is not None
                or self.store.get_active_batch_cancel_request(batch_id) is not None
            ):
                raise GameLaunchCancelled("Queue cleanup stopped by the durable Manager cancellation boundary")
            if self.store.get_game_run(plan.run_id)["state"] == EntityState.HUMAN_REQUIRED:
                raise GameLaunchHumanRequired(
                    "queue_cleanup_human_takeover", "Human takeover interrupted queue cleanup; preserve all remaining clients.",
                )
            # Sealed/cancelled historical batches cannot permanently protect a
            # process. A still-open human gate does, including another batch.
            for owner in self.store.list_batches(5000):
                if owner["result"].get("sealVersion") is not None or owner["state"] == EntityState.CANCELLED:
                    continue
                for member in owner.get("run_memberships", []):
                    protected = self.store.get_game_run(str(member["run_id"]))
                    if protected["game_id"] not in other_game_ids:
                        continue
                    has_human_blocker = any(
                        blocker["kind"] == "human_required"
                        for blocker in self.store.list_todo_blockers(
                            run_id=protected["run_id"], active_only=True, limit=5000,
                        )
                    )
                    if protected["state"] == EntityState.HUMAN_REQUIRED or has_human_blocker:
                        raise GameLaunchHumanRequired(
                            "queue_game_human_required",
                            f"{protected['game_id']} has an unreleased human gate; preserve its scene before starting another game.",
                            detail={"protectedGameId": protected["game_id"], "protectedRunId": protected["run_id"], "protectedBatchId": owner["batch_id"]},
                        )
            for protected in self.store.list_game_runs(5000):
                if protected["game_id"] in other_game_ids and protected["state"] == EntityState.HUMAN_REQUIRED:
                    if not self.store.list_batch_run_memberships(run_id=protected["run_id"], limit=5000):
                        raise GameLaunchHumanRequired(
                            "queue_game_human_required",
                            f"{protected['game_id']} has an independent unreleased human gate; preserve its scene.",
                            detail={"protectedGameId": protected["game_id"], "protectedRunId": protected["run_id"]},
                        )
            return False

        try:
            check_gate()
            failures: list[str] = []
            persist()
            for game_id in other_game_ids:
                check_gate()
                binding = configured_paths.get(game_id)
                if not isinstance(binding, dict) or binding.get("emulator") is not None or not isinstance(binding.get("game_path"), str) or not binding["game_path"]:
                    report["games"].append({"gameId": game_id, "state": "binding-unavailable", "code": "queue_cleanup_binding_unavailable"})
                    failures.append(game_id)
                    persist()
                    continue
                try:
                    cleanup = self.game_launcher.close_for_queue(
                        game_id, binding["game_path"], cancel_requested=check_gate,
                    )
                    if not isinstance(cleanup, GameCloseReceipt):
                        raise GameLaunchError("Queue cleanup returned an invalid receipt")
                    report["games"].append({"gameId": game_id, **cleanup.as_result()})
                    if (
                        cleanup.state not in {"closed", "already-closed"}
                        or cleanup.remaining_process_ids
                        or cleanup.zombie_process_ids
                        or getattr(cleanup, "unverified_process_ids", ())
                    ):
                        failures.append(game_id)
                except (GameLaunchCancelled, GameLaunchHumanRequired):
                    raise
                except Exception as error:
                    report["games"].append({"gameId": game_id, "state": "close-failed", "errorType": type(error).__name__, "message": str(error)})
                    failures.append(game_id)
                report["state"] = "closing"
                persist()
            check_gate()
            if failures:
                raise GameLaunchHumanRequired(
                    "queue_game_cleanup_incomplete",
                    "Queue cleanup did not close or verify every other game; the next game was not started.",
                    detail={"failedGameIds": failures, "games": list(report["games"])},
                )
            check_gate()
            report["state"] = "closed" if other_game_ids else "not-needed"
            persist()
        except (GameLaunchCancelled, GameLaunchHumanRequired, GameLaunchError) as error:
            report["state"] = "cancelled" if isinstance(error, GameLaunchCancelled) else "human_required" if isinstance(error, GameLaunchHumanRequired) else "failed"
            report["code"] = getattr(error, "reason_code", "queue_cleanup_cancelled" if isinstance(error, GameLaunchCancelled) else "queue_cleanup_failed")
            report["message"] = str(error)
            persist()
            raise

    def _record_zombie_game_processes(self, plan: AdapterExecutionPlan) -> None:
        """Observe listed-but-exited clients without inferring cause or terminating them."""
        try:
            listed = self.game_launcher.list_zombies([plan.game_id])
        except Exception as error:  # optional diagnostics do not control execution
            _log.warning("launch.zombie_scan_failed error=%s", error)
            return
        if not isinstance(listed, dict) or not listed:
            return
        _log.warning(
            "launch.listed_exited_processes game=%s observed=%s cause=unverified resourceImpact=unverified",
            plan.game_id, listed,
        )
        self.store.append_event(
            "game-launch.zombie-detected", "run-attempt", plan.run_attempt_id,
            {"runId": plan.run_id, "gameId": plan.game_id, "zombieProcesses": listed,
             "readOnly": True, "recommendedAction": "inspect_system_resources_and_client_logs"},
        )

    def reap_game_zombies(self, game_ids: list[str] | None = None) -> dict[str, Any]:
        """Legacy diagnostic name retained as a read-only process enumeration."""
        targets = game_ids or [str(game["game_id"]) for game in self.store.list_games()]
        return {"gameIds": targets, "observed": self.game_launcher.list_zombies(targets), "readOnly": True}

    LAUNCH_PHASE_ARTIFACT_KINDS = {
        "launcher-waiting": "game-ui-launch-phase",
        "launcher-action": "game-ui-launch-phase",
        "ready": "game-ui-launch-ready",
        "launch-cancelled": "game-ui-launch-cancelled",
        "launch-human-required": "game-ui-launch-human-required",
        "launch-failed": "game-ui-launch-failed",
    }

    def _record_launch_observation(
        self, plan: AdapterExecutionPlan, observation: LaunchObservation
    ) -> None:
        """Persist a launch-phase checkpoint: log line, ledger event, screenshot.

        The official launcher (update download, blank web surface, login gate)
        used to fail after many minutes with no picture at all.  Every
        checkpoint now leaves a frame of the largest visible launcher/game
        window the Manager itself started.
        """

        with bind_log_context(
            run=plan.run_id, attempt=plan.run_attempt_id, game=plan.game_id, phase="launch"
        ):
            level = _log.warning if observation.phase == "launch-failed" else _log.info
            level(
                "launch.%s elapsed=%ss pids=%s %s",
                observation.phase,
                observation.elapsed_seconds,
                sorted(observation.process_ids),
                observation.detail,
            )
            artifact_id: str | None = None
            capture_error: str | None = None
            try:
                if (
                    not observation.process_ids
                    and (
                        observation.phase == "launch-human-required"
                        or (
                            observation.game_id == "WW"
                            and "launcher_main.exe" in observation.process_names
                        )
                    )
                ):
                    raise WindowCaptureError(
                        "launch gate has no verified process IDs; capture scope cannot be widened"
                    )
                captured = self.window_capture.capture_processes(
                    observation.process_names,
                    allowed_pids=observation.process_ids or None,
                    watermark_text=(
                        f"YeYu Gamer | launch {observation.phase} | "
                        f"{observation.elapsed_seconds:.0f}s | {plan.game_id}"
                    ),
                )
                artifact_id = self._store_manager_capture(
                    plan,
                    captured,
                    kind=self.LAUNCH_PHASE_ARTIFACT_KINDS.get(
                        observation.phase, "game-ui-launch-phase"
                    ),
                    source="manager-launch-capture-v1",
                    extra={
                        "launchPhase": observation.phase,
                        "elapsedSeconds": observation.elapsed_seconds,
                        "detail": observation.detail,
                    },
                )
                _log.info("launch.capture stored artifact=%s", artifact_id)
            except (WindowCaptureError, OSError, ManagerValidation) as error:
                capture_error = f"{type(error).__name__}: {error}"
                _log.info("launch.capture unavailable reason=%s", capture_error)
            try:
                self.store.append_event(
                    "game-launch.phase",
                    "run-attempt",
                    plan.run_attempt_id,
                    {
                        "runId": plan.run_id,
                        "gameId": plan.game_id,
                        "phase": observation.phase,
                        "elapsedSeconds": observation.elapsed_seconds,
                        "processIds": sorted(observation.process_ids),
                        "detail": observation.detail,
                        "artifactId": artifact_id,
                        "captureError": capture_error,
                    },
                )
            except Exception:
                pass

    def _store_manager_capture(
        self,
        plan: AdapterExecutionPlan,
        captured: Any,
        *,
        kind: str,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Write a Manager-captured PNG into the artifact store and register it."""

        artifact_id = str(uuid.uuid4())
        artifact_root = self.settings.data_dir / "artifacts"
        if str(artifact_root).startswith("\\\\"):
            raise ManagerValidation("screenshot storage must be local")
        artifact_root.mkdir(parents=True, exist_ok=True)
        if _has_reparse_point(artifact_root):
            raise ManagerValidation("screenshot storage contains a link")
        file_name = f"{kind}-{artifact_id}.png"
        final_path = artifact_root / file_name
        temp_path = artifact_root / f".{artifact_id}.tmp"
        try:
            temp_path.write_bytes(captured.content)
            os.replace(temp_path, final_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        captured_at = utc_now()
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="captured",
            document={
                "kind": kind,
                "capturedAt": captured_at.isoformat(),
                "source": source,
                "raw": False,
                "contentType": "image/png",
                "gameId": plan.game_id,
                "runId": plan.run_id,
                "runAttemptId": plan.run_attempt_id,
                "verdict": "diagnostic",
                "hash": hashlib.sha256(captured.content).hexdigest(),
                "sizeBytes": len(captured.content),
                "fileName": file_name,
                "relativePath": file_name,
                "window": {
                    "hwnd": captured.hwnd,
                    "pid": captured.pid,
                    "processName": captured.process_name,
                    "title": captured.title,
                    "width": captured.width,
                    "height": captured.height,
                    "captureMethod": captured.method,
                },
                **(extra or {}),
            },
        )
        return artifact_id

    def _open_attempt_log(
        self, *, game_id: str, run_attempt_id: str
    ) -> AttemptLogSession | None:
        """Create the per-attempt diagnostic directory; never fail the run over it."""

        try:
            session = AttemptLogSession(
                self.settings, game_id=game_id, run_attempt_id=run_attempt_id
            ).open()
        except OSError as error:
            _log.warning(
                "attempt.log.unavailable attempt=%s error=%s", run_attempt_id, error
            )
            return None
        with self._attempt_logs_lock:
            self._attempt_logs[run_attempt_id] = session
        return session

    def _close_attempt_log(self, run_attempt_id: str) -> None:
        with self._attempt_logs_lock:
            session = self._attempt_logs.pop(run_attempt_id, None)
        if session is not None:
            session.close()

    def close_attempt_logs(self) -> None:
        """Release every per-attempt log file (process shutdown / tests)."""

        with self._attempt_logs_lock:
            sessions = list(self._attempt_logs.values())
            self._attempt_logs.clear()
        for session in sessions:
            session.close()

    def _run_batch(
        self, batch_id: str, runs: list[dict[str, Any]], command_id: str
    ) -> None:
        starting_batch = self.store.get_batch(batch_id)
        if starting_batch.get("result", {}).get("sealVersion") is not None:
            return
        with bind_log_context(batch=batch_id, phase="batch"):
            _log.info(
                "batch.start batchId=%s mode=%s games=%s",
                batch_id,
                starting_batch.get("mode"),
                starting_batch.get("game_ids"),
            )
        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        membership_by_run = {
            str(item["run_id"]): item for item in memberships
        }
        requested_run_ids = [str(run["runId"]) for run in runs]
        if len(requested_run_ids) != len(set(requested_run_ids)) or any(
            run_id not in membership_by_run for run_id in requested_run_ids
        ):
            self._complete_command(
                command_id,
                "failed",
                "Batch coordinator rejected a run outside its frozen membership.",
            )
            if command_id != batch_id:
                self._complete_command(
                    batch_id,
                    "failed",
                    "Batch coordinator rejected a run outside its frozen membership.",
                )
            return
        startup_cancel = self.store.get_active_batch_cancel_request(batch_id)
        if startup_cancel is not None:
            sealed = self._seal_cancelled_batch(
                batch_id, startup_cancel["cancel_request_id"]
            )
            if sealed is not None:
                self._complete_command(
                    command_id,
                    "failed",
                    "batch cancelled before the next fixed Host attempt started",
                )
                self._complete_command(
                    startup_cancel["cancel_request_id"],
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
            return
        initial_result = dict(starting_batch.get("result", {}))
        recovery = initial_result.get("recoveryPhase")
        if isinstance(recovery, dict):
            initial_result["recoveryPhase"] = {
                **recovery,
                "status": "resuming",
                "resumeCommandId": command_id,
                "affectedRunIds": requested_run_ids,
            }
        prior_outcomes = self._batch_run_outcome_sets(memberships)
        self.store.update_batch(
            batch_id,
            state=EntityState.RUNNING,
            result={
                **initial_result,
                "currentGameId": None,
                "completedRunIds": prior_outcomes["completedRunIds"],
                "failedRunIds": prior_outcomes["failedRunIds"],
            },
        )
        completed: list[str] = list(prior_outcomes["completedRunIds"])
        failures: list[str] = list(prior_outcomes["failedRunIds"])
        not_started: list[str] = []
        timed_out = False
        strategy = str(
            self.store.get_config()["values"].get("execution_strategy", "continue")
        )
        for index, run in enumerate(runs):
            current_batch = self.store.get_batch(batch_id)
            if current_batch.get("result", {}).get("sealVersion") is not None:
                return
            if self.store.get_active_batch_cancel_request(batch_id) is not None:
                break
            run_id = str(run["runId"])
            game_id = str(run["gameId"])
            membership = self.store.get_batch_run_membership(batch_id, run_id)
            if membership["state"] not in {"queued", "resume_pending"}:
                self._fence_batch_for_reconciliation(
                    batch_id=batch_id,
                    affected_run_ids=[run_id],
                    run_attempt_ids=(
                        [str(membership["latest_run_attempt_id"])]
                        if membership.get("latest_run_attempt_id")
                        else []
                    ),
                    requested_by="manager-batch-coordinator",
                    note=(
                        "Batch coordinator found a member outside its committed "
                        "queued/resume_pending state."
                    ),
                    requirements=[
                        "membership_state_reconciliation",
                        "fresh_observation",
                    ],
                )
                self._complete_command(
                    command_id,
                    "failed",
                    "Batch membership changed before Host dispatch; reconciliation is required.",
                )
                if command_id != batch_id:
                    self._complete_command(
                        batch_id,
                        "failed",
                        "Batch membership changed before Host dispatch; reconciliation is required.",
                    )
                return
            self.store.update_batch(
                batch_id,
                state=EntityState.RUNNING,
                result={
                    **initial_result,
                    "currentGameId": game_id,
                    "completedRunIds": completed,
                    "failedRunIds": failures,
                },
            )
            finished = threading.Event()
            outcome: dict[str, AdapterRunResult] = {}

            def complete(result: AdapterRunResult) -> None:
                outcome["result"] = result
                finished.set()

            try:
                plan, _ = self._start_game_run(run_id, complete)
                wait_seconds = (
                    plan.timeout_seconds
                    + (2 * self.adapter_host.CANCEL_GRACE_SECONDS)
                    + 30
                )
                if not finished.wait(wait_seconds):
                    timed_out = True
                    attempt = self.store.get_run_attempt(plan.run_attempt_id)
                    if attempt["state"] in {"starting", "running"}:
                        self.adapter_host.cancel(
                            run_attempt_id=plan.run_attempt_id,
                            fencing_token=plan.fencing_token,
                            reason_code="manager_batch_wait_timeout",
                        )
                        self.store.update_run_attempt(
                            plan.run_attempt_id,
                            state="cancelling",
                            result={"code": "manager_batch_wait_timeout"},
                            completed=False,
                        )
                    finished.wait(self.adapter_host.CANCEL_GRACE_SECONDS + 10)
                result = outcome.get("result")
                if result is None:
                    attempt = self.store.get_run_attempt(plan.run_attempt_id)
                    if attempt["state"] in {"starting", "running", "cancelling"}:
                        requirements = [
                            "attempt_outcome_reconciliation",
                            "fresh_observation",
                        ]
                        if self.store.get_active_batch_cancel_request(batch_id) is not None:
                            requirements.append(
                                "cooperative_cancel_acknowledgement"
                            )
                        self._fence_batch_for_reconciliation(
                            batch_id=batch_id,
                            affected_run_ids=[run_id],
                            run_attempt_ids=[plan.run_attempt_id],
                            requested_by="manager-batch-timeout",
                            note=(
                                "Adapter completion callback did not arrive while the fixed "
                                "Host attempt remained active/cancelling."
                            ),
                            requirements=requirements,
                        )
                        self._complete_command(
                            command_id,
                            "failed",
                            "Adapter callback is missing; Batch is fenced for durable reconciliation.",
                        )
                        if command_id != batch_id:
                            self._complete_command(
                                batch_id,
                                "failed",
                                "Adapter callback is missing; Batch is fenced for durable reconciliation.",
                            )
                        return
                    terminal_membership = self.store.get_batch_run_membership(
                        batch_id, run_id
                    )
                    if terminal_membership.get("terminal_outcome") == "completed":
                        completed.append(run_id)
                    else:
                        failures.append(run_id)
                elif result.protocol_valid and result.status == "completed":
                    completed.append(run_id)
                else:
                    failures.append(run_id)
                    timed_out = timed_out or result.transport_outcome == "timeout"
            except Exception as error:
                attempts = self.store.list_run_attempts(run_id=run_id, limit=1)
                launch_cancelled = isinstance(error, GameLaunchCancelled)
                if attempts and not launch_cancelled:
                    failures.append(run_id)
                elif not attempts:
                    not_started.append(run_id)
                    membership = self.store.get_batch_run_membership(batch_id, run_id)
                    if membership["state"] in {"queued", "resume_pending"}:
                        self.store.update_batch_run_membership(
                            batch_id,
                            run_id,
                            state="terminal",
                            terminal_outcome="not_started_start_failed",
                        )
                current = self.store.get_game_run(run_id)
                if not launch_cancelled and current["state"] not in {
                    EntityState.FAILED,
                    EntityState.BLOCKED,
                    EntityState.REVIEW_REQUIRED,
                    EntityState.HUMAN_REQUIRED,
                    EntityState.CANCELLED,
                }:
                    self.store.update_game_run(
                        run_id,
                        state=EntityState.FAILED,
                        message=(
                            f"Manager Adapter execution failed: "
                            f"{type(error).__name__}: {error}"
                        ),
                    )
            if self.store.get_game_run(run_id)["state"] == EntityState.HUMAN_REQUIRED:
                if self.store.get_active_batch_cancel_request(batch_id) is not None:
                    break
                # A real human gate owns the desktop until explicit release.
                # Keep later members queued and this Batch unsealed so the
                # existing same-GameRun successor must resolve the gate before
                # typed Batch resume can dispatch its never-started members.
                current = self.store.get_batch(batch_id)
                self.store.update_batch(
                    batch_id,
                    state=EntityState.HUMAN_REQUIRED,
                    result={
                        **dict(current["result"]),
                        "currentGameId": game_id,
                        "completedRunIds": completed,
                        "failedRunIds": failures,
                        "recoveryPhase": {
                            "schemaVersion": 1,
                            "status": "ready_for_resume",
                            "humanRequiredRunId": run_id,
                            "affectedRunIds": [
                                str(item["runId"]) for item in runs[index:]
                            ],
                        },
                    },
                )
                _log.info(
                    "batch.human_required batchId=%s runId=%s gameId=%s pending=%s",
                    batch_id, run_id, game_id, len(runs) - index - 1,
                )
                for pending_command_id in dict.fromkeys((command_id, batch_id)):
                    self._complete_command(
                        pending_command_id,
                        "failed",
                        "Batch paused at a human gate; preserve the client and "
                        "resolve the same GameRun before resuming queued members.",
                    )
                return
            if (failures or not_started) and strategy == "stop":
                for remaining in runs[index + 1 :]:
                    remaining_id = str(remaining["runId"])
                    if remaining_id not in not_started:
                        not_started.append(remaining_id)
                    membership = self.store.get_batch_run_membership(
                        batch_id, remaining_id
                    )
                    if membership["state"] in {"queued", "resume_pending"}:
                        self.store.update_batch_run_membership(
                            batch_id,
                            remaining_id,
                            state="terminal",
                            terminal_outcome="not_started_stop_strategy",
                        )
                    self.store.update_game_run(
                        remaining_id,
                        state=EntityState.BLOCKED,
                        message="not started because the batch stop strategy fenced later games",
                    )
                break

            if self.store.get_active_batch_cancel_request(batch_id) is not None:
                break

        cancel_request = self.store.get_active_batch_cancel_request(batch_id)
        if cancel_request is not None:
            sealed = self._seal_cancelled_batch(
                batch_id, cancel_request["cancel_request_id"]
            )
            if sealed is not None:
                self._complete_command(
                    command_id,
                    "failed",
                    "batch cancellation reached an immutable safe seal",
                )
                if command_id != batch_id:
                    self._complete_command(
                        batch_id,
                        "failed",
                        "batch cancellation reached an immutable safe seal",
                    )
                self._complete_command(
                    cancel_request["cancel_request_id"],
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
            else:
                active_attempts = self._active_run_attempts_for_batch(batch_id)
                self._fence_batch_for_reconciliation(
                    batch_id=batch_id,
                    affected_run_ids=list(
                        dict.fromkeys(
                            str(item["run_id"]) for item in active_attempts
                        )
                    ),
                    run_attempt_ids=[
                        str(item["run_attempt_id"]) for item in active_attempts
                    ],
                    requested_by="manager-cancellation",
                    note=(
                        "Cooperative cancellation has no terminal Host fact; "
                        "reconcile before sealing the Batch."
                    ),
                    requirements=[
                        "cooperative_cancel_acknowledgement",
                        "attempt_outcome_reconciliation",
                        "fresh_observation",
                    ],
                )
                self._complete_command(
                    command_id,
                    "failed",
                    "Batch cancellation is awaiting durable Host reconciliation.",
                )
            return

        memberships = self.store.list_batch_run_memberships(
            batch_id=batch_id, limit=5000
        )
        incomplete = [
            item
            for item in memberships
            if item["state"] not in {"terminal", "cancelled"}
        ]
        if incomplete:
            current = self.store.get_batch(batch_id)
            self.store.update_batch(
                batch_id,
                state=EntityState.REVIEW_REQUIRED,
                result={
                    **dict(current["result"]),
                    "currentGameId": None,
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "ready_for_resume",
                        "affectedRunIds": [
                            str(item["run_id"]) for item in incomplete
                        ],
                    },
                },
            )
            self._complete_command(
                command_id,
                "failed",
                "Batch stopped before every queued member ran; a typed resume request is required.",
            )
            return
        outcomes = self._batch_run_outcome_sets(memberships)
        final_state = (
            EntityState.BLOCKED
            if outcomes["failedRunIds"] or outcomes["notStartedRunIds"]
            else EntityState.REVIEW_REQUIRED
        )
        candidate_game_ids = [
            str(value)
            for value in initial_result.get(
                "candidateGameIds",
                list(dict.fromkeys(str(run["gameId"]) for run in runs)),
            )
        ]
        not_started_records = [
            {
                "runId": str(item["run_id"]),
                "gameId": str(self.store.get_game_run(str(item["run_id"]))["game_id"]),
                "outcome": str(item.get("terminal_outcome") or "not_started"),
            }
            for item in memberships
            if str(item["run_id"]) in set(outcomes["notStartedRunIds"])
        ]
        final_initial_result = {
            **initial_result,
            "allCandidateRunIds": outcomes["allRunIds"],
            "notStartedRunIds": outcomes["notStartedRunIds"],
            "notStartedMembers": not_started_records,
        }
        reconciliation_run_ids = list(
            dict.fromkeys(
                str(value)
                for value in initial_result.get(
                    "completionReconciliationRuns", {}
                ).values()
                if isinstance(value, str) and value
            )
        )
        completed_run_ids = list(
            dict.fromkeys(
                [*outcomes["completedRunIds"], *reconciliation_run_ids]
            )
        )
        final_run_ids = list(
            dict.fromkeys(
                [*outcomes["attemptedRunIds"], *reconciliation_run_ids]
            )
        )
        phase_record = self._begin_completion_review_phase(
            batch_id=batch_id,
            initial_result=final_initial_result,
            game_ids=candidate_game_ids,
            cadence=str(self.store.get_batch(batch_id)["cadence"]),
            state=final_state,
            completed_run_ids=completed_run_ids,
            failed_run_ids=outcomes["failedRunIds"],
            final_run_ids=final_run_ids,
            reason=(
                "One or more Adapter attempts failed, blocked, or timed out."
                if outcomes["failedRunIds"]
                else "One or more Batch members were explicitly not started."
                if outcomes["notStartedRunIds"]
                else "All executable Todo attempts ended cleanly; acceptance evidence still requires review."
            ),
            timed_out=timed_out,
        )
        self._complete_command(
            command_id,
            "failed"
            if outcomes["failedRunIds"] or outcomes["notStartedRunIds"]
            else "succeeded",
            (
                "batch Adapter execution ended; Manager completion review is required before seal"
                if phase_record.get("result", {}).get("sealVersion") is None
                else "batch sealed after all current completion reviews were adjudicated"
            ),
        )
        if command_id != batch_id:
            self._complete_command(
                batch_id,
                "failed"
                if outcomes["failedRunIds"] or outcomes["notStartedRunIds"]
                else "succeeded",
                "resumed Batch execution reached Manager completion review",
            )

    def create_game_run(
        self,
        request: GameRunCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        game = self._validated_game(request.game_id, require_enabled=True)
        if request.mode == RequestMode.EXECUTE:
            if request.cadence not in {"daily", "weekly"}:
                raise ManagerValidation("Adapter execution cadence is unsupported")

        def operation() -> dict[str, Any]:
            if request.mode == RequestMode.EXECUTE and self.store.get_metadata(
                "manager.lifecycle_state", "running"
            ) != "running":
                raise ManagerConflict("Manager is stopping and cannot accept new execution")
            self.store.reconcile_todo_instances(
                self._todo_instance_candidates([game.game_id], request.cadence),
                intent="reconcile",
                requested_by=request.requested_by,
                reason="game-run-planning",
            )
            todo_plan = self._todo_plans_for_games(
                [game.game_id], str(request.cadence)
            )[game.game_id]
            todo_plans = {game.game_id: todo_plan}
            decision = classify_batch_todo_plans([game.game_id], todo_plans)
            has_pending = decision.has_unresolved_todos
            has_executable = decision.has_executable_binding
            executable_ids = list(todo_plan["executableTodoInstanceIds"])
            if (
                request.mode == RequestMode.EXECUTE
                and has_pending
                and not has_executable
            ):
                raise ExecutionUnavailable(
                    decision.execution_unavailable_details(todo_plans)
                )
            if request.mode == RequestMode.EXECUTE and has_executable:
                self._require_execution_ready([game.game_id])
            state = (
                EntityState.QUEUED
                if request.mode == RequestMode.EXECUTE and has_executable
                else EntityState.REVIEW_REQUIRED
                if request.mode == RequestMode.EXECUTE and not has_pending
                else EntityState.BLOCKED
                if request.mode == RequestMode.EXECUTE
                else EntityState.PLANNED
            )
            record = self.store.create_game_run(
                {
                    "game_id": game.game_id,
                    "cadence": request.cadence,
                    "state": state,
                    "mode": request.mode,
                    "requested_by": request.requested_by,
                    "message": (
                        "awaiting fenced Manager Adapter Todo execution"
                        if request.mode == RequestMode.EXECUTE and has_executable
                        else "no Todo matched a verified promoted execution binding; execution sealed before Host start"
                        if request.mode == RequestMode.EXECUTE and has_pending
                        else "all required Todo facts are complete; no new acceptance was inferred"
                        if request.mode == RequestMode.EXECUTE
                        else "plan only; no game was started"
                    ),
                    "todo_instance_ids": executable_ids,
                    "completed_todo_instance_ids": todo_plan[
                        "completedTodoInstanceIds"
                    ],
                    "completion_todo_instance_ids": todo_plan[
                        "completionTodoInstanceIds"
                    ],
                }
            )
            return self._receipt(
                command_id=record["run_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/game-runs/{record['run_id']}",
                message=(
                    "单游戏执行已进入固定 Adapter 队列。"
                    if request.mode == RequestMode.EXECUTE and has_executable
                    else "没有 Todo 命中已验证执行绑定；已阻塞且没有启动游戏。"
                    if request.mode == RequestMode.EXECUTE and has_pending
                    else "required Todo 已全部完成；只记录既有事实，不推导新的验收完成。"
                    if request.mode == RequestMode.EXECUTE
                    else "单游戏计划已写入 Manager；没有启动游戏。"
                ),
                result={
                    "gameRun": _dump(GameRunRecord.model_validate(record)),
                    "todoPlan": todo_plan,
                },
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if (
            request.mode == RequestMode.EXECUTE
            and not replayed
            and receipt.result["gameRun"].get("todoInstanceIds")
        ):
            run_id = str(receipt.result["gameRun"]["runId"])
            try:
                self._start_game_run(run_id)
            except Exception as error:
                failure = f"Manager Adapter start failed: {type(error).__name__}: {error}"
                self.store.update_game_run(
                    run_id,
                    state=EntityState.FAILED,
                    message=failure,
                )
                self._complete_command(run_id, "failed", failure)
        elif request.mode == RequestMode.EXECUTE and not replayed:
            self._complete_command(
                str(receipt.command_id),
                "failed",
                "no verified executable Todo binding; Host was not started",
            )
        return receipt

    def resume_batch(
        self,
        batch_id: str,
        request: BatchResumeRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        """Resume only never-started members of one recovered Batch."""

        def operation() -> dict[str, Any]:
            if self.store.get_metadata(
                "manager.lifecycle_state", "running"
            ) != "running":
                raise ManagerConflict("Manager is stopping and cannot resume execution")
            batch = self.store.get_batch(batch_id)
            result = dict(batch.get("result", {}))
            if result.get("sealVersion") is not None:
                raise ManagerConflict("sealed batches are immutable")
            if str(batch.get("mode")) != str(RequestMode.EXECUTE):
                raise ManagerConflict("only an execute Batch can be resumed")
            if isinstance(result.get("completionReviewPhase"), dict):
                raise ManagerConflict(
                    "Batch is in CompletionReview and cannot resume Host execution"
                )
            recovery = result.get("recoveryPhase")
            if not isinstance(recovery, dict) or recovery.get("status") != "ready_for_resume":
                raise ManagerConflict(
                    "Batch recovery is not ready_for_resume; reconcile current facts first"
                )
            if self.store.get_active_batch_cancel_request(batch_id) is not None:
                raise ManagerConflict(
                    "Batch has an active cooperative cancellation request"
                )
            memberships = self.store.list_batch_run_memberships(
                batch_id=batch_id, limit=5000
            )
            if any(
                item["state"] in {"active", "reconciliation_required"}
                for item in memberships
            ):
                raise ManagerConflict(
                    "Batch has an active or unreconciled member GameRun"
                )
            terminal_retry_run_ids = [
                str(item["run_id"])
                for item in memberships
                if item["state"] == "terminal"
                and item.get("terminal_outcome") != "completed"
                and self.store.list_run_attempts(
                    run_id=str(item["run_id"]), limit=1
                )
            ]
            if terminal_retry_run_ids:
                raise ManagerConflict(
                    "terminal Batch members require same-GameRun resume first: "
                    + ", ".join(terminal_retry_run_ids)
                )
            pending = [
                item
                for item in memberships
                if item["state"] in {"queued", "resume_pending"}
            ]
            if not pending:
                raise ManagerConflict("Batch has no never-started member to resume")
            prior_attempt_run_ids = [
                str(item["run_id"])
                for item in pending
                if self.store.list_run_attempts(
                    run_id=str(item["run_id"]), limit=1
                )
            ]
            if prior_attempt_run_ids:
                raise ManagerConflict(
                    "members with a prior terminal attempt require same-GameRun resume: "
                    + ", ".join(prior_attempt_run_ids)
                )
            pending_game_ids = [
                str(self.store.get_game_run(str(item["run_id"]))["game_id"])
                for item in pending
            ]
            self._require_execution_ready(pending_game_ids)
            resume_request_id = str(uuid.uuid4())
            with self.store.atomic():
                for membership in pending:
                    run_id = str(membership["run_id"])
                    self.store.update_game_run(
                        run_id,
                        state=EntityState.QUEUED,
                        message=(
                            f"queued by typed Batch resume {resume_request_id}; "
                            "GameRun identity preserved"
                        ),
                    )
                self.store.update_batch(
                    batch_id,
                    state=EntityState.QUEUED,
                    result={
                        **result,
                        "currentGameId": None,
                        "recoveryPhase": {
                            **recovery,
                            "schemaVersion": 1,
                            "status": "resume_dispatch_pending",
                            "resumeRequestId": resume_request_id,
                            "requestedBy": request.requested_by,
                            "reason": request.reason,
                            "affectedRunIds": [
                                str(item["run_id"]) for item in pending
                            ],
                        },
                    },
                )
            return self._receipt(
                command_id=resume_request_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/batches/{batch_id}",
                message=(
                    "Recovered Batch resume was committed for never-started members; "
                    "no GameRun identity was replaced."
                ),
                result={
                    "batchId": batch_id,
                    "resumeRequestId": resume_request_id,
                },
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if not replayed:
            try:
                batch = self.store.get_batch(batch_id)
                recovery = dict(batch.get("result", {}).get("recoveryPhase", {}))
                if recovery.get("resumeRequestId") != str(receipt.command_id):
                    raise RuntimeError("Batch resume command does not match persisted recovery")
                affected_run_ids = [
                    str(run_id) for run_id in recovery.get("affectedRunIds", [])
                ]
                membership_run_ids = {
                    str(item["run_id"])
                    for item in self.store.list_batch_run_memberships(
                        batch_id=batch_id, limit=5000
                    )
                }
                if not affected_run_ids or any(
                    run_id not in membership_run_ids for run_id in affected_run_ids
                ):
                    raise RuntimeError(
                        "Batch resume references a run outside persisted membership"
                    )
                launch_runs = [
                    _dump(
                        GameRunRecord.model_validate(
                            self.store.get_game_run(run_id)
                        )
                    )
                    for run_id in affected_run_ids
                ]
                threading.Thread(
                    target=self._run_batch,
                    args=(
                        batch_id,
                        launch_runs,
                        str(receipt.command_id),
                    ),
                    name=f"yeyu-gamer-batch-resume-{batch_id[:8]}",
                    daemon=True,
                ).start()
            except Exception as error:
                batch = self.store.get_batch(batch_id)
                recovery = dict(batch.get("result", {}).get("recoveryPhase", {}))
                self.store.update_batch(
                    batch_id,
                    state=EntityState.REVIEW_REQUIRED,
                    result={
                        **dict(batch["result"]),
                        "recoveryPhase": {
                            **recovery,
                            "status": "ready_for_resume",
                            "dispatchErrorClass": type(error).__name__,
                        },
                    },
                )
                self._complete_command(
                    str(receipt.command_id),
                    "failed",
                    f"Batch resume coordinator could not start: {type(error).__name__}",
                )
        return receipt

    def cancel_batch(
        self,
        batch_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        def operation() -> dict[str, Any]:
            current = self.store.get_batch(batch_id)
            if current["result"].get("sealVersion") is not None:
                raise ManagerConflict("sealed batches are immutable")
            active_attempts = self._active_run_attempts_for_batch(batch_id)
            cancel_request, _ = self.store.create_or_get_batch_cancel_request(
                {
                    "batch_id": batch_id,
                    "reason": request.get("reason", "operator-request"),
                    "requested_by": request.get(
                        "requestedBy", request.get("requested_by", "webgui")
                    ),
                    "active_run_attempt_ids": [
                        str(item["run_attempt_id"]) for item in active_attempts
                    ],
                }
            )
            if active_attempts:
                for active_attempt in active_attempts:
                    if active_attempt["state"] in {"starting", "running"}:
                        launch_phase = self._is_pre_adapter_launch_attempt(
                            active_attempt
                        )
                        self.store.update_run_attempt(
                            str(active_attempt["run_attempt_id"]),
                            state="cancelling",
                            result={
                                "cancelReason": "batch_cancel_requested",
                                "cancelPhase": (
                                    "game-launch"
                                    if launch_phase
                                    else "adapter-host"
                                ),
                            },
                            completed=False,
                        )
                    active_run_id = str(active_attempt["run_id"])
                    active_run = self.store.get_game_run(active_run_id)
                    if active_run["state"] in {
                        EntityState.PENDING_EXECUTION,
                        EntityState.QUEUED,
                        EntityState.RUNNING,
                    }:
                        self.store.update_game_run(
                            active_run_id,
                            state=EntityState.CANCELLING,
                            message=(
                                "durable Batch cancellation intent persisted; "
                                "delivery target is being resolved"
                            ),
                        )
                record = self.store.update_batch(
                    batch_id,
                    state=EntityState.CANCELLING,
                    result={
                        **dict(current["result"]),
                        "cancelRequest": {
                            "cancelRequestId": cancel_request[
                                "cancel_request_id"
                            ],
                            "state": cancel_request["state"],
                            "reason": cancel_request["reason"],
                            "requestedBy": cancel_request["requested_by"],
                            "activeRunAttemptIds": [
                                str(item["run_attempt_id"])
                                for item in active_attempts
                            ],
                        },
                    },
                )
            else:
                record = self._seal_cancelled_batch(
                    batch_id, cancel_request["cancel_request_id"]
                )
                if record is None:
                    raise ManagerConflict(
                        "Batch became active while cancellation was being committed"
                    )
            return self._receipt(
                command_id=cancel_request["cancel_request_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/batches/{batch_id}",
                message=(
                    "协作取消请求已持久化，正在中止当前受管启动或 Adapter 执行。"
                    if active_attempts
                    else "批次在没有活动 Host 后已安全封口。"
                ),
                result={
                    "batch": _dump(self._batch_record(record)),
                    "cancelRequestId": cancel_request["cancel_request_id"],
                    "cooperativeCancellationPending": bool(active_attempts),
                    "activeRunIds": list(
                        dict.fromkeys(
                            str(item["run_id"]) for item in active_attempts
                        )
                    ),
                },
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=request,
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if (
            not replayed
            and receipt.result.get("cooperativeCancellationPending") is True
        ):
            # The durable request above is the command's transaction boundary.
            # A Host may be busy inside an upstream UI wait, so delivering its
            # cooperative signal must not hold the WebGUI response open long
            # enough for the browser to report a false cancellation failure.
            threading.Thread(
                target=self._deliver_batch_cancellation,
                args=(batch_id, receipt),
                name=f"BatchCancel-{batch_id[:8]}",
                daemon=True,
            ).start()
            return receipt
        current_batch = self.store.get_batch(batch_id)
        if current_batch.get("result", {}).get("sealVersion") is not None:
            if not replayed:
                self._complete_command(
                    str(receipt.result["cancelRequestId"]),
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
                self._complete_command(
                    batch_id,
                    "failed",
                    "batch cancellation reached an immutable safe seal",
                )
            refreshed = self.store.get_command_receipt(str(receipt.command_id))
            refreshed["replayed"] = replayed
            return CommandReceipt.model_validate(refreshed)
        return receipt

    def _deliver_batch_cancellation(
        self, batch_id: str, receipt: CommandReceipt
    ) -> None:
        """Retry one durable cancellation request before requiring reconciliation."""
        try:
            cancel_request_id = str(receipt.result["cancelRequestId"])
            pending_run_ids = [
                str(value) for value in receipt.result.get("activeRunIds", [])
            ]
            signalled = not pending_run_ids
            for delivery_index in range(self.BATCH_CANCEL_MAX_DELIVERY_ATTEMPTS):
                delivered_run_ids: list[str] = []
                error_classes: list[str] = []
                for active_run_id in pending_run_ids:
                    delivery_target: str | None = None
                    run_attempt_id: str | None = None
                    try:
                        with self._execution_handoff_lock:
                            attempts = self.store.list_run_attempts(
                                run_id=active_run_id, limit=1
                            )
                            if not attempts:
                                delivered = False
                            else:
                                active_attempt = attempts[0]
                                run_attempt_id = str(
                                    active_attempt["run_attempt_id"]
                                )
                                if (
                                    active_attempt["state"] == "cancelled"
                                    and active_attempt.get("result", {}).get(
                                        "cancelDeliveryTarget"
                                    )
                                    == "manager-game-launch"
                                ):
                                    delivered = True
                                    delivery_target = "manager-game-launch"
                                elif self._is_pre_adapter_launch_attempt(
                                    active_attempt
                                ):
                                    # The launch loop polls this attempt's
                                    # durable `cancelling` state. No Adapter
                                    # process exists yet, so do not manufacture
                                    # a Host delivery receipt.
                                    delivered = True
                                    delivery_target = "manager-game-launch"
                                else:
                                    (
                                        host_attempt_id,
                                        delivered,
                                    ) = self.adapter_host.cancel_run(
                                        run_id=active_run_id,
                                        reason_code="batch_cancel_requested",
                                    )
                                    if host_attempt_id is not None:
                                        run_attempt_id = str(host_attempt_id)
                                    if delivered:
                                        delivery_target = "adapter-host"
                    except Exception as error:
                        delivered = False
                        error_classes.append(type(error).__name__)
                    if not delivered:
                        continue
                    delivered_run_ids.append(active_run_id)
                    _log.info(
                        "batch.cancel.signal run=%s attempt=%s target=%s",
                        active_run_id,
                        run_attempt_id,
                        delivery_target,
                    )
                    if run_attempt_id is not None:
                        with self.store.atomic():
                            attempt = self.store.get_run_attempt(run_attempt_id)
                            if attempt["state"] in {
                                "starting",
                                "running",
                                "cancelling",
                            }:
                                self.store.update_run_attempt(
                                    run_attempt_id,
                                    state="cancelling",
                                    result={
                                        "cancelReason": "batch_cancel_requested",
                                        "cancelDeliveryTarget": delivery_target,
                                    },
                                    completed=False,
                                )
                                run = self.store.get_game_run(active_run_id)
                                if run["state"] in {
                                    EntityState.PENDING_EXECUTION,
                                    EntityState.QUEUED,
                                    EntityState.RUNNING,
                                }:
                                    self.store.update_game_run(
                                        active_run_id,
                                        state=EntityState.CANCELLING,
                                        message=(
                                            "durable Batch cooperative cancellation "
                                            "requested"
                                        ),
                                    )
                delivered_set = set(delivered_run_ids)
                pending_run_ids = [
                    run_id for run_id in pending_run_ids if run_id not in delivered_set
                ]
                signalled = not pending_run_ids
                retry_delay = (
                    self.BATCH_CANCEL_RETRY_DELAYS_SECONDS[delivery_index]
                    if not signalled
                    and delivery_index < len(self.BATCH_CANCEL_RETRY_DELAYS_SECONDS)
                    else None
                )
                retry_at = (
                    (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=retry_delay)
                    ).isoformat()
                    if retry_delay is not None
                    else None
                )
                self.store.record_batch_cancel_delivery_attempt(
                    cancel_request_id,
                    error_class=(
                        ",".join(sorted(set(error_classes)))
                        if error_classes
                        else "host_signal_not_delivered"
                        if not signalled
                        else ""
                    ),
                    retry_at=retry_at,
                )
                if signalled:
                    break
                if retry_delay is not None:
                    threading.Event().wait(retry_delay)
            cancel_request = self.store.get_batch_cancel_request(
                cancel_request_id
            )
            if cancel_request["state"] == "requested":
                cancel_request = self.store.update_batch_cancel_request(
                    cancel_request["cancel_request_id"],
                    state=(
                        "signal_delivered"
                        if signalled
                        else "reconciliation_required"
                    ),
                )
                if not signalled:
                    batch = self.store.get_batch(batch_id)
                    memberships = self.store.list_batch_run_memberships(
                        batch_id=batch_id, limit=5000
                    )
                    scope = {
                        "schemaVersion": 1,
                        "batchId": batch_id,
                        "rootBatchId": batch["root_batch_id"],
                        "predecessorBatchId": batch["predecessor_batch_id"],
                        "continuationOrdinal": batch["continuation_ordinal"],
                        "memberRunIds": [
                            str(item["run_id"]) for item in memberships
                        ],
                        "affectedRunIds": list(
                            receipt.result.get("activeRunIds", [])
                        ),
                        "runAttemptIds": list(
                            cancel_request["active_run_attempt_ids"]
                        ),
                        "cancelRequestId": cancel_request[
                            "cancel_request_id"
                        ],
                    }
                    work_item = self.store.create_work_item(
                        {
                            "kind": WorkItemKind.OBSERVATION,
                            "state": EntityState.REVIEW_REQUIRED,
                            "game_id": (
                                str(batch["game_ids"][0])
                                if len(batch["game_ids"]) == 1
                                else None
                            ),
                            "cadence": batch["cadence"],
                            "run_id": (
                                str(scope["affectedRunIds"][0])
                                if len(scope["affectedRunIds"]) == 1
                                else None
                            ),
                            "requested_by": "manager-cancellation",
                            "note": (
                                "Cooperative cancellation could not reach the fixed "
                                "Host; reconcile process and game outcome."
                            ),
                            "artifact_refs": [],
                            "allowed_capability_refs": self._allowed_capability_refs(
                                WorkItemKind.OBSERVATION
                            ),
                            "result": {
                                "batchRecoveryScope": scope,
                                "executionRequested": False,
                                "requirements": [
                                    "cooperative_cancel_acknowledgement",
                                    "fresh_observation",
                                ],
                            },
                        }
                    )
                    self.store.update_batch_cancel_request(
                        cancel_request["cancel_request_id"],
                        state="reconciliation_required",
                        work_item_id=work_item["work_item_id"],
                    )
                    current_batch = self.store.get_batch(batch_id)
                    if current_batch["result"].get("sealVersion") is None:
                        self.store.update_batch(
                            batch_id,
                            state=EntityState.REVIEW_REQUIRED,
                            result={
                                **dict(current_batch["result"]),
                                "recoveryPhase": {
                                    "schemaVersion": 1,
                                    "status": "awaiting_reconciliation",
                                    "workItemId": work_item["work_item_id"],
                                    "affectedRunIds": scope[
                                        "affectedRunIds"
                                    ],
                                },
                            },
                        )
            current_batch = self.store.get_batch(batch_id)
            if current_batch.get("result", {}).get("sealVersion") is not None:
                self._complete_command(
                    str(receipt.result["cancelRequestId"]),
                    "succeeded",
                    "cooperative Batch cancellation reached its immutable seal",
                )
                self._complete_command(
                    batch_id,
                    "failed",
                    "batch cancellation reached an immutable safe seal",
                )
        except Exception as error:
            self._complete_command(
                str(receipt.result["cancelRequestId"]),
                "failed",
                f"cooperative Batch cancellation delivery failed: {type(error).__name__}",
            )

    def create_work_item(
        self,
        request: AgentWorkItemCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        resolved_game_id = request.game_id
        resolved_cadence = str(request.cadence or "daily")
        if request.run_id:
            referenced_run = self.store.get_game_run(request.run_id)
            if resolved_game_id and resolved_game_id != referenced_run["game_id"]:
                raise ManagerValidation("gameId does not match the referenced run")
            resolved_game_id = resolved_game_id or referenced_run["game_id"]
            resolved_cadence = str(request.cadence or referenced_run["cadence"])
        if resolved_game_id:
            self._validated_game(resolved_game_id, require_enabled=False)
        if request.kind in {WorkItemKind.RUN_GAME, WorkItemKind.DIAGNOSE_GAME} and not request.game_id:
            raise ManagerValidation(f"{request.kind} requires gameId")
        if request.kind == WorkItemKind.CANCEL_RUN and not request.run_id:
            raise ManagerValidation("cancel_run requires runId")
        if request.mode == RequestMode.EXECUTE:
            raise ManagerConflict(
                "Agent work items describe authorized work; execution must use a typed capability"
            )
        for artifact_id in request.artifact_refs:
            if not self._is_opaque_artifact_id(artifact_id):
                raise ManagerValidation(
                    "artifactRefs must contain opaque IDs, not file paths"
                )
            self.store.get_resource("artifact", artifact_id)
        allowed_defaults = self._allowed_capability_refs(request.kind)
        requested_refs = list(request.allowed_capability_refs)
        if requested_refs and not set(requested_refs).issubset(set(allowed_defaults)):
            raise ManagerValidation(
                "allowedCapabilityRefs exceed the fixed policy for this work item kind"
            )
        allowed_refs = requested_refs or allowed_defaults

        def operation() -> dict[str, Any]:
            selected_game_ids = (
                [resolved_game_id]
                if resolved_game_id
                else [game.game_id for game in self.list_games() if game.enabled]
            )
            self.store.reconcile_todo_instances(
                self._todo_instance_candidates(
                    selected_game_ids, resolved_cadence
                ),
                intent="reconcile",
                requested_by=request.requested_by,
                reason="agent-work-item-planning",
            )
            plans = self._todo_plans_for_games(
                selected_game_ids, resolved_cadence
            )
            todo_items = (
                self._frozen_todos_for_run(referenced_run)
                if request.run_id
                else [
                    item
                    for game_id in selected_game_ids
                    for item in self.list_todo_instances(
                        game_id=game_id,
                        cadence=resolved_cadence,
                        current=True,
                        limit=1000,
                    )
                ]
            )
            selected_runs = [
                run
                for run in self.list_game_runs(500)
                if run.game_id in selected_game_ids
                and run.cadence == resolved_cadence
            ]
            attempt_analysis = self._attempt_analysis(
                todo_items=todo_items,
                runs=selected_runs,
            )
            work_item_artifact_refs = list(dict.fromkeys(request.artifact_refs))
            if request.kind == WorkItemKind.DIAGNOSE_GAME:
                for todo_item in todo_items:
                    for artifact_id in todo_item.evidence_refs:
                        if artifact_id in work_item_artifact_refs:
                            continue
                        if not self._is_opaque_artifact_id(artifact_id):
                            raise ManagerValidation(
                                "Todo evidenceRefs must contain opaque artifact IDs"
                            )
                        self.store.get_resource("artifact", artifact_id)
                        work_item_artifact_refs.append(artifact_id)
                        if len(work_item_artifact_refs) == 50:
                            break
                    if len(work_item_artifact_refs) == 50:
                        break
            todo_snapshot = self._typed_agent_todo_snapshot(
                todo_items=todo_items,
                plans=plans,
                cadence=resolved_cadence,
                include_execution_facts=(request.kind == WorkItemKind.DIAGNOSE_GAME),
            )
            difficult_items = [
                item
                for item in todo_items
                if item.required
                and item.status != "completed"
                and (
                    item.automation_difficulty in {"high", "unknown"}
                    or item.status in {"blocked", "review_required", "human_required"}
                )
            ]
            record = self.store.create_work_item(
                {
                    **request.model_dump(mode="json", exclude={"mode"}),
                    "game_id": resolved_game_id,
                    "cadence": resolved_cadence,
                    "artifact_refs": work_item_artifact_refs,
                    "allowed_capability_refs": allowed_refs,
                    "state": EntityState.PLANNED,
                    "result": {
                        "todoSnapshot": todo_snapshot,
                        "todoDifficulty": {
                            "difficultRequiredCount": len(difficult_items),
                            "items": [
                                {
                                    "todoInstanceId": item.todo_instance_id,
                                    "gameId": item.game_id,
                                    "operation": item.operation,
                                    "status": item.status,
                                    "automationDifficulty": item.automation_difficulty,
                                    "reason": item.reason,
                                }
                                for item in difficult_items
                            ],
                        },
                        "attemptAnalysis": attempt_analysis,
                    },
                }
            )
            return self._receipt(
                command_id=record["work_item_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/agent/work-items/{record['work_item_id']}",
                message="Agent 工作项已写入 Manager；没有执行外部动作。",
                result={"workItem": _dump(AgentWorkItemRecord.model_validate(record))},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def _validate_completion_review(
        self,
        request: ClaimDecisionCreateRequest,
        *,
        claim: dict[str, Any],
        work_item: dict[str, Any],
        reviewed_at: datetime,
    ) -> dict[str, Any] | None:
        review = request.completion_review
        if review is None:
            return None
        if work_item["kind"] != WorkItemKind.EVIDENCE_REVIEW:
            raise ManagerValidation(
                "completionReview requires a claimed evidence_review work item"
            )
        if not work_item.get("run_id"):
            raise ManagerValidation("completionReview work item must bind one GameRun")
        run = self.store.get_game_run(review.run_id)
        if (
            work_item["run_id"] != review.run_id
            or work_item.get("game_id") != review.game_id
            or run["game_id"] != review.game_id
            or work_item.get("cadence") != run["cadence"]
        ):
            raise ManagerValidation(
                "completionReview differs from the claimed work item or GameRun scope"
            )
        attempts = self.store.list_run_attempts(run_id=review.run_id, limit=1000)
        if not attempts or attempts[0]["run_attempt_id"] != review.run_attempt_id:
            raise ManagerValidation(
                "completionReview must reference the current GameRun attempt"
            )
        attempt = attempts[0]
        terminal_attempt_states = {
            "completed",
            "partial",
            "blocked",
            "review_required",
            "human_required",
            "cancelled",
            "failed",
        }
        if (
            attempt["run_id"] != review.run_id
            or attempt["game_id"] != review.game_id
            or attempt["cadence"] != run["cadence"]
            or attempt["state"] not in terminal_attempt_states
            or not attempt.get("completed_at")
        ):
            raise ManagerValidation(
                "completionReview requires a terminal current RunAttempt"
            )
        if request.decision == "accepted" and attempt["state"] != "completed":
            raise ManagerValidation(
                "accepted completionReview requires a completed current RunAttempt"
            )
        completed_at = self._contract_datetime(attempt["completed_at"])
        if reviewed_at < completed_at:
            raise ManagerValidation(
                "completionReview cannot predate the RunAttempt completion"
            )

        snapshot = self._completion_contract_snapshot(review.run_id)
        current_todos = self._frozen_todos_for_run(run)
        current_by_id = {item.todo_instance_id: item for item in current_todos}
        required = [item for item in current_todos if item.required]
        if not required:
            raise ManagerValidation(
                "completionReview cannot accept a run with no frozen required Todos"
            )
        periods = {
            (item.period_key, item.period_starts_at, item.period_ends_at)
            for item in required
        }
        if len(periods) != 1:
            raise ManagerValidation(
                "completionReview frozen required Todos do not share one GameDay"
            )
        game_day_key, period_start, period_end = next(iter(periods))
        if review.game_day_key != game_day_key:
            raise ManagerValidation(
                "completionReview gameDayKey differs from the frozen Todo period"
            )
        if reviewed_at < period_start:
            raise ManagerValidation(
                "completionReview cannot predate the frozen GameDay"
            )
        if request.decision == "accepted" and reviewed_at >= period_end:
            raise ManagerValidation(
                "accepted completionReview must be recorded before GameDay reset"
            )

        policy, _, policy_status, unsupported_reason = (
            self._completion_policy_context(
                run=run, snapshot=snapshot, todos=current_todos
            )
        )
        expected_contract = policy_review_contract(
            policy,
            period_starts_at=snapshot.game_day.starts_at,
            period_ends_at=snapshot.game_day.ends_at,
            policy_status=policy_status,
            unsupported_reason=unsupported_reason,
        )
        work_result = dict(work_item.get("result", {}))
        if work_result.get("completionReviewContract") != expected_contract:
            raise ManagerConflict(
                "completionReview work item contract is stale or missing"
            )
        work_scope = work_result.get("completionReviewScope")
        if not isinstance(work_scope, dict) or any(
            work_scope.get(key) != value
            for key, value in {
                "gameId": review.game_id,
                "runId": review.run_id,
                "runAttemptId": review.run_attempt_id,
                "gameDayKey": review.game_day_key,
            }.items()
        ):
            raise ManagerConflict("completionReview work item scope is stale")
        lineage_ids = {
            item.run_attempt_id for item in snapshot.attempt_lineage
        }
        if set(work_scope.get("attemptLineageIds", [])) != lineage_ids:
            raise ManagerConflict("completionReview attempt lineage changed")
        required_ids = {item.todo_instance_id for item in required}
        if (
            work_scope.get("schemaVersion") != 3
            or set(work_scope.get("requiredTodoInstanceIds", []))
            != required_ids
        ):
            raise ManagerConflict(
                "completionReview required Todo scope is stale or incomplete"
            )
        submitted_todo_review_ids = [
            item.todo_instance_id for item in review.todo_reviews
        ]
        if not set(submitted_todo_review_ids).issubset(required_ids):
            raise ManagerValidation(
                "completionReview Todo verdict crosses the frozen required scope"
            )
        if request.decision == "accepted" and set(
            submitted_todo_review_ids
        ) != required_ids:
            raise ManagerValidation(
                "accepted completionReview must cover every frozen required Todo"
            )
        if request.decision == "accepted" and any(
            item.verdict != TodoReviewVerdict.CONFIRMED
            for item in review.todo_reviews
        ):
            raise ManagerValidation(
                "accepted completionReview requires every Todo verdict confirmed"
            )
        if policy_status != "supported" and request.decision == "accepted":
            raise ManagerValidation(
                "unsupported completion policy cannot accept completion"
            )
        rules = {item.observation_id: item for item in policy.predicate_rules}
        submitted_ids = {item.predicate_id for item in review.predicates}
        if not submitted_ids.issubset(rules):
            raise ManagerValidation(
                "completionReview predicateId is not registered for this game"
            )
        if request.decision == "accepted" and submitted_ids != set(rules):
            raise ManagerValidation(
                "accepted completionReview must cover every game predicate"
            )
        if request.decision == "accepted" and any(
            item.status != "completed" for item in required
        ):
            raise ManagerValidation(
                "accepted completionReview requires every frozen required Todo completed"
            )

        work_item_artifacts = set(work_item.get("artifact_refs", []))
        decision_artifacts = set(request.evidence_ids)
        if request.decision == "accepted" and not decision_artifacts:
            raise ManagerValidation(
                "accepted completionReview requires current-attempt screenshot evidence"
            )
        attempt_started_at = {
            str(item["run_attempt_id"]): self._contract_datetime(item["started_at"])
            for item in attempts
        }
        terminal_todo_states = {
            "completed",
            "skipped",
            "blocked",
            "review_required",
            "human_required",
        }
        artifact_documents: dict[
            str, tuple[dict[str, Any], TodoInstanceRecord, dict[str, Any]]
        ] = {}
        accepted_integrity: dict[str, ArtifactIntegrityResult] = {}
        for artifact_id in decision_artifacts:
            if artifact_id not in work_item_artifacts:
                raise ManagerValidation(
                    "completionReview evidence must belong to the claimed work item"
                )
            resource = self.store.get_resource("artifact", artifact_id)
            document = dict(resource["document"])
            artifact_attempt_id = str(document.get("runAttemptId") or "")
            if (
                document.get("gameId") != review.game_id
                or document.get("runId") != review.run_id
                or artifact_attempt_id not in lineage_ids
            ):
                raise ManagerValidation(
                    "completionReview evidence differs from the frozen run lineage"
                )
            todo_instance_id = str(document.get("todoInstanceId") or "")
            todo = current_by_id.get(todo_instance_id)
            if todo is None or todo.period_key != review.game_day_key:
                raise ManagerValidation(
                    "completionReview evidence is not owned by a frozen Todo"
                )
            todo_attempt_id = str(document.get("todoAttemptId") or "")
            try:
                todo_attempt = self.store.get_todo_attempt(todo_attempt_id)
            except RecordNotFound as error:
                raise ManagerValidation(
                    "completionReview evidence has no TodoAttempt ledger owner"
                ) from error
            if (
                todo_attempt["run_attempt_id"] != artifact_attempt_id
                or todo_attempt["todo_instance_id"] != todo_instance_id
                or todo_attempt["state"] not in terminal_todo_states
                or artifact_id not in todo_attempt.get("evidence_refs", [])
            ):
                raise ManagerValidation(
                    "completionReview evidence ownership is not terminal and immutable"
                )
            captured_at = self._contract_datetime(document.get("capturedAt"))
            if not (
                period_start <= captured_at < period_end
                and captured_at <= reviewed_at
                and captured_at >= attempt_started_at[artifact_attempt_id]
            ):
                raise ManagerValidation(
                    "completionReview evidence was captured outside its frozen scope"
                )
            if (
                document.get("raw") is not True
                or str(document.get("contentType") or "").lower()
                not in COMPLETION_REVIEW_EVIDENCE_CONTENT_TYPES
            ):
                raise ManagerValidation(
                    "completionReview evidence must be a raw screenshot"
                )
            if request.decision == "accepted":
                integrity = self._completion_artifact_integrity(
                    artifact_id, document
                )
                if not integrity.valid:
                    raise ManagerValidation(
                        "accepted completionReview evidence failed entity "
                        f"revalidation ({artifact_id}={integrity.reason_code})"
                    )
                accepted_integrity[artifact_id] = integrity
            artifact_documents[artifact_id] = (document, todo, todo_attempt)

        for todo_review in review.todo_reviews:
            for artifact_id in todo_review.artifact_refs:
                if (
                    artifact_id not in decision_artifacts
                    or artifact_id not in artifact_documents
                ):
                    raise ManagerValidation(
                        "completionReview Todo artifactRefs must be claimed "
                        "work-item evidence"
                    )
                document, todo, todo_attempt = artifact_documents[artifact_id]
                if (
                    todo.todo_instance_id != todo_review.todo_instance_id
                    or not todo.required
                ):
                    raise ManagerValidation(
                        "completionReview Todo evidence has a different Todo owner"
                    )
                if (
                    request.decision == "accepted"
                    and todo_attempt["state"] != "completed"
                ):
                    raise ManagerValidation(
                        "accepted Todo verdict requires completed TodoAttempt evidence"
                    )

        if request.decision == "accepted":
            semantics_by_hash: dict[str, set[tuple[str, str]]] = {}
            refs_by_hash: dict[str, list[str]] = {}
            for artifact_id, integrity in accepted_integrity.items():
                document, _, _ = artifact_documents[artifact_id]
                semantics_by_hash.setdefault(integrity.content_hash, set()).add(
                    (
                        str(document.get("todoInstanceId") or ""),
                        str(document.get("kind") or ""),
                    )
                )
                refs_by_hash.setdefault(integrity.content_hash, []).append(
                    artifact_id
                )
            ambiguous_refs = sorted(
                artifact_id
                for digest, semantics in semantics_by_hash.items()
                if len(semantics) > 1
                for artifact_id in refs_by_hash[digest]
            )
            if ambiguous_refs:
                raise ManagerValidation(
                    "accepted completionReview reuses byte-identical screenshots "
                    "for conflicting Todo or evidence-kind semantics ("
                    + ",".join(ambiguous_refs)
                    + ")"
                )

        if request.decision == "accepted" and not any(
            document.get("runAttemptId") == review.run_attempt_id
            for document, _, _ in artifact_documents.values()
        ):
            raise ManagerValidation(
                "accepted completionReview needs a screenshot from the current attempt"
            )

        for predicate in review.predicates:
            rule = rules[predicate.predicate_id]
            expected_metric_names = {
                constraint.metric for constraint in rule.constraints
            }
            if set(predicate.metrics) != expected_metric_names:
                raise ManagerValidation(
                    "completionReview metrics differ from the predicate contract"
                )
            if request.decision == "accepted" and not all(
                constraint.matches(predicate.metrics)
                for constraint in rule.constraints
            ):
                raise ManagerValidation(
                    "accepted completionReview metrics do not satisfy the predicate"
                )
            for artifact_id in predicate.artifact_refs:
                if (
                    artifact_id not in decision_artifacts
                    or artifact_id not in artifact_documents
                ):
                    raise ManagerValidation(
                        "completionReview artifactRefs must be claimed work-item evidence"
                    )
                document, todo, todo_attempt = artifact_documents[artifact_id]
                if not todo.required:
                    raise ManagerValidation(
                        "completionReview predicate evidence must belong to a required Todo"
                    )
                if request.decision == "accepted" and todo_attempt["state"] != "completed":
                    raise ManagerValidation(
                        "accepted predicate evidence must belong to a completed TodoAttempt"
                    )
                if document.get("kind") not in rule.allowed_artifact_kinds:
                    raise ManagerValidation(
                        "completionReview predicate requires a registered raw screenshot"
                    )

        return {
            "schemaVersion": 2,
            "workItemId": claim["work_item_id"],
            "claimId": request.claim_id,
            "reviewerPrincipalId": request.requested_by,
            "decision": request.decision,
            "gameId": review.game_id,
            "runId": review.run_id,
            "runAttemptId": review.run_attempt_id,
            "gameDayKey": review.game_day_key,
            "predicates": [
                item.model_dump(mode="json", by_alias=True)
                for item in review.predicates
            ],
            "todoReviews": [
                item.model_dump(mode="json", by_alias=True)
                for item in review.todo_reviews
            ],
            "completionPolicyId": expected_contract["policyId"],
            "completionPolicyVersion": expected_contract["policyVersion"],
            "attemptLineageIds": list(work_scope["attemptLineageIds"]),
            "artifactRefs": list(dict.fromkeys(request.evidence_ids)),
            "reviewedAt": reviewed_at.isoformat(),
        }

    def create_claim_decision(
        self,
        request: ClaimDecisionCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        self._assert_no_fencing_value(
            {
                "idempotencyKey": idempotency_key,
                "requestId": request_id,
                "reason": request.reason,
                "evidenceIds": request.evidence_ids,
                "todoDiagnosis": (
                    request.todo_diagnosis.model_dump(mode="json", by_alias=True)
                    if request.todo_diagnosis is not None
                    else None
                ),
                "todoDiagnoses": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in request.todo_diagnoses
                ],
                "completionReview": (
                    request.completion_review.model_dump(
                        mode="json", by_alias=True
                    )
                    if request.completion_review is not None
                    else None
                ),
            },
            request.fencing_token,
        )

        def operation() -> dict[str, Any]:
            claim, work_item = self._validate_active_claim(
                claim_id=request.claim_id,
                work_item_id=None,
                claimant=request.requested_by,
                fencing_token=request.fencing_token,
            )
            if not set(request.evidence_ids).issubset(
                set(work_item.get("artifact_refs", []))
            ):
                raise ManagerValidation(
                    "decision evidenceIds must belong to the claimed work item"
                )
            reviewed_at = utc_now()
            completion_review_document = self._validate_completion_review(
                request,
                claim=claim,
                work_item=work_item,
                reviewed_at=reviewed_at,
            )
            batch_recovery_scope = work_item.get("result", {}).get(
                "batchRecoveryScope"
            )
            diagnoses = (
                list(request.todo_diagnoses)
                if request.todo_diagnoses
                else (
                    [request.todo_diagnosis]
                    if request.todo_diagnosis is not None
                    else []
                )
            )
            diagnosis_todos: dict[str, dict[str, Any]] = {}
            work_result = work_item.get("result", {})
            raw_todo_snapshot = work_result.get("todoSnapshot")
            snapshot_items = (
                raw_todo_snapshot.get("items", [])
                if isinstance(raw_todo_snapshot, dict)
                else []
            )
            if not isinstance(snapshot_items, list):
                raise ManagerConflict("Agent Todo snapshot items are malformed")
            snapshot_by_id = {
                str(item.get("todoInstanceId")): item
                for item in snapshot_items
                if isinstance(item, dict)
                and isinstance(item.get("todoInstanceId"), str)
                and item.get("todoInstanceId")
            }
            frozen_target_ids = (
                [
                    str(value)
                    for value in raw_todo_snapshot.get(
                        "targetTodoInstanceIds", []
                    )
                    if isinstance(value, str) and value
                ]
                if isinstance(raw_todo_snapshot, dict)
                else []
            )
            if work_item["kind"] == WorkItemKind.DIAGNOSE_GAME:
                if (
                    not isinstance(raw_todo_snapshot, dict)
                    or raw_todo_snapshot.get("schemaVersion") != 1
                    or not frozen_target_ids
                    or len(frozen_target_ids) != len(set(frozen_target_ids))
                    or len(snapshot_by_id) != len(snapshot_items)
                    or set(frozen_target_ids) != set(snapshot_by_id)
                ):
                    raise ManagerConflict(
                        "diagnose_game requires a complete typed frozen todoSnapshot"
                    )
                if not diagnoses:
                    raise ManagerValidation(
                        "diagnose_game decision requires Todo diagnoses"
                    )
                submitted_todo_id_list = [
                    diagnosis.todo_instance_id for diagnosis in diagnoses
                ]
                if len(submitted_todo_id_list) != len(set(submitted_todo_id_list)):
                    raise ManagerValidation(
                        "todoDiagnoses must contain one decision per frozen target Todo"
                    )
                submitted_todo_ids = set(submitted_todo_id_list)
                if request.decision == "accepted" and submitted_todo_ids != set(
                    frozen_target_ids
                ):
                    raise ManagerValidation(
                        "accepted diagnose_game must cover every frozen target Todo"
                    )
            if diagnoses:
                if work_item["kind"] not in {
                    WorkItemKind.DIAGNOSE_GAME,
                    WorkItemKind.OBSERVATION,
                    WorkItemKind.INCIDENT_REVIEW,
                    WorkItemKind.EVIDENCE_REVIEW,
                    WorkItemKind.REPAIR_VALIDATION,
                }:
                    raise ManagerValidation(
                        "this work item kind cannot record a Todo diagnosis"
                    )
                scoped_todo_ids = set(frozen_target_ids)
                if not scoped_todo_ids:
                    raise ManagerConflict(
                        "Todo diagnoses require a typed frozen work-item scope"
                    )
                if work_item.get("run_id"):
                    scoped_run = self.store.get_game_run(str(work_item["run_id"]))
                    if int(scoped_run.get("completion_scope_version", 0)) != 1:
                        raise ManagerConflict(
                            "Todo diagnosis run has no complete frozen scope"
                        )
                    if scoped_todo_ids != set(
                        scoped_run.get("completion_todo_instance_ids", [])
                    ):
                        raise ManagerConflict(
                            "Todo diagnosis work-item scope differs from its GameRun"
                        )
                for diagnosis in diagnoses:
                    frozen_item = snapshot_by_id.get(diagnosis.todo_instance_id)
                    if frozen_item is None or diagnosis.todo_instance_id not in scoped_todo_ids:
                        raise ManagerValidation(
                            "todoDiagnoses must reference Todos in the claimed work item scope"
                        )
                    todo = self.store.get_todo_instance(
                        diagnosis.todo_instance_id
                    )
                    if any(
                        str(frozen_item.get(snapshot_key) or "")
                        != str(todo.get(store_key) or "")
                        for snapshot_key, store_key in (
                            ("gameId", "game_id"),
                            ("periodKey", "period_key"),
                            ("periodStartsAt", "period_starts_at"),
                            ("periodEndsAt", "period_ends_at"),
                            ("sourceHash", "source_hash"),
                        )
                    ) or str(raw_todo_snapshot.get("cadence")) != str(
                        todo.get("cadence")
                    ):
                        raise ManagerConflict(
                            "Todo diagnosis target differs from the frozen period scope"
                        )
                    if work_item.get("game_id") and todo["game_id"] != work_item["game_id"]:
                        raise ManagerValidation(
                            "Todo diagnosis target crosses the work-item game scope"
                        )
                    diagnosis_evidence_ids = set(diagnosis.evidence_ids)
                    if not diagnosis_evidence_ids.issubset(
                        set(request.evidence_ids)
                    ):
                        raise ManagerValidation(
                            "Todo diagnosis evidenceIds must also be decision evidenceIds"
                        )
                    for artifact_id in diagnosis_evidence_ids:
                        resource = self.store.get_resource("artifact", artifact_id)
                        document = dict(resource["document"])
                        if (
                            document.get("todoInstanceId")
                            != diagnosis.todo_instance_id
                            or document.get("gameId") != todo["game_id"]
                            or document.get("gameDayKey") != todo["period_key"]
                            or (
                                work_item.get("run_id")
                                and document.get("runId") != work_item["run_id"]
                            )
                        ):
                            raise ManagerValidation(
                                "Todo diagnosis evidence differs from its Todo lineage"
                            )
                        todo_attempt_id = document.get("todoAttemptId")
                        if not isinstance(todo_attempt_id, str) or not todo_attempt_id:
                            raise ManagerValidation(
                                "Todo diagnosis evidence has no TodoAttempt lineage"
                            )
                        try:
                            todo_attempt = self.store.get_todo_attempt(todo_attempt_id)
                        except RecordNotFound as error:
                            raise ManagerValidation(
                                "Todo diagnosis evidence TodoAttempt is missing"
                            ) from error
                        owning_run_attempt = self.store.get_run_attempt(
                            str(todo_attempt["run_attempt_id"])
                        )
                        if (
                            todo_attempt["todo_instance_id"]
                            != diagnosis.todo_instance_id
                            or owning_run_attempt["run_id"]
                            != document.get("runId")
                            or todo_attempt["run_attempt_id"]
                            != document.get("runAttemptId")
                            or artifact_id
                            not in set(todo_attempt.get("evidence_refs", []))
                        ):
                            raise ManagerValidation(
                                "Todo diagnosis evidence ownership is inconsistent"
                            )
                    diagnosis_todos[diagnosis.todo_instance_id] = todo
            record = self.store.create_claim_decision(request.model_dump(mode="json"))
            completion_review = None
            if completion_review_document is not None:
                completion_review_resource = self.store.create_resource(
                    "completion-review",
                    state=request.decision,
                    document={
                        **completion_review_document,
                        "decisionId": record["decision_id"],
                    },
                )
                completion_review = self._completion_review_record(
                    completion_review_resource
                )
            assessments: list[AutomationAssessmentRecord] = []
            for diagnosis in diagnoses:
                todo = diagnosis_todos[diagnosis.todo_instance_id]
                assessment_resource = self.store.create_resource(
                    "automation-assessment",
                    state="recorded",
                    document={
                        "todoInstanceId": diagnosis.todo_instance_id,
                        "workItemId": claim["work_item_id"],
                        "claimId": request.claim_id,
                        "decisionId": record["decision_id"],
                        "gameId": todo["game_id"],
                        "runId": work_item.get("run_id"),
                        "difficulty": diagnosis.difficulty,
                        "automatable": diagnosis.automatable,
                        "confidence": diagnosis.confidence,
                        "basis": list(diagnosis.basis),
                        "failureStage": diagnosis.failure_stage,
                        "issue": diagnosis.issue,
                        "recommendation": diagnosis.recommendation,
                        "evidenceIds": list(diagnosis.evidence_ids),
                        "requestedBy": request.requested_by,
                    },
                )
                assessments.append(
                    self._automation_assessment_record(assessment_resource)
                )
            self.store.resolve_work_item_claim(request.claim_id, record["decision_id"])
            work_item_state = (
                EntityState.DONE
                if request.decision == "accepted"
                else EntityState.REVIEW_REQUIRED
            )
            self.store.update_work_item(
                claim["work_item_id"],
                state=work_item_state,
                result={
                    "decisionId": record["decision_id"],
                    "automationAssessmentIds": [
                        item.assessment_id for item in assessments
                    ],
                    **(
                        {"automationAssessmentId": assessments[0].assessment_id}
                        if assessments
                        else {}
                    ),
                    **(
                        {
                            "completionReviewId": (
                                completion_review.completion_review_id
                            )
                        }
                        if completion_review is not None
                        else {}
                    ),
                },
            )
            return self._receipt(
                command_id=record["decision_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/claims/decisions/{record['decision_id']}",
                message="验收决定已写入事件账本。",
                result={
                    "decision": _dump(ClaimDecisionRecord.model_validate(record)),
                    "automationAssessments": [
                        _dump(item) for item in assessments
                    ],
                    **(
                        {"automationAssessment": _dump(assessments[0])}
                        if assessments
                        else {}
                    ),
                    **(
                        {"completionReview": _dump(completion_review)}
                        if completion_review is not None
                        else {}
                    ),
                    **(
                        {"batchRecoveryScope": dict(batch_recovery_scope)}
                        if isinstance(batch_recovery_scope, dict)
                        else {}
                    ),
                },
            )

        receipt = self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if request.completion_review is not None:
            self._resume_completion_review_batches_for_run(
                request.completion_review.run_id
            )
        batch_recovery_scope = receipt.result.get("batchRecoveryScope")
        if isinstance(batch_recovery_scope, dict):
            self._resolve_batch_recovery_decision(
                batch_recovery_scope, request.decision
            )
        return receipt

    @staticmethod
    def _completion_review_record(
        resource: dict[str, Any]
    ) -> CompletionReviewRecord:
        document = dict(resource["document"])
        predicates = list(document["predicates"])
        decision = document["decision"]
        # Version 0.3.0 briefly persisted the WW machine predicate with
        # screenshot references embedded as ad-hoc fields.  The resource was
        # durably committed before the API projection rejected that shape, so
        # a later batch cancellation could no longer seal and the desktop host
        # could not take a safe lifecycle request.  Preserve the opaque
        # evidence references for diagnosis, but deliberately downgrade this
        # one known legacy shape to review_required.  It must never become a
        # completion fact, especially now that WW also requires a pre-claim
        # screenshot.
        if len(predicates) == 1 and isinstance(predicates[0], dict):
            legacy = dict(predicates[0])
            if legacy.get("type") == "ww-reward-claim-screenshot-pair":
                legacy_refs = list(
                    dict.fromkeys(
                        str(value)
                        for value in (
                            legacy.get("rawArtifactRef"),
                            legacy.get("watermarkedArtifactRef"),
                        )
                        if isinstance(value, str) and value
                    )
                )
                if legacy_refs:
                    predicates = [
                        {
                            "predicateId": "legacy-invalid-ww-reward-claim-screenshot-pair",
                            "metrics": {"legacySchemaInvalid": True},
                            "artifactRefs": legacy_refs,
                        }
                    ]
                    decision = "review_required"
        return CompletionReviewRecord(
            completion_review_id=resource["resource_id"],
            work_item_id=document["workItemId"],
            claim_id=document["claimId"],
            decision_id=document["decisionId"],
            reviewer_principal_id=document["reviewerPrincipalId"],
            decision=decision,
            game_id=document["gameId"],
            run_id=document["runId"],
            run_attempt_id=document["runAttemptId"],
            game_day_key=document["gameDayKey"],
            predicates=predicates,
            todo_reviews=document.get("todoReviews", []),
            artifact_refs=document["artifactRefs"],
            reviewed_at=document["reviewedAt"],
            created_at=resource["created_at"],
        )

    def list_completion_reviews(
        self,
        *,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 100,
    ) -> list[CompletionReviewRecord]:
        if run_id is not None:
            self.store.get_game_run(run_id)
        resources = self.store.list_resources(
            "completion-review", min(1000, max(limit, limit * 10))
        )
        records = [self._completion_review_record(item) for item in resources]
        if principal_id is not None:
            records = [
                item
                for item in records
                if item.reviewer_principal_id == principal_id
            ]
        if run_id is not None:
            records = [item for item in records if item.run_id == run_id]
        return records[:limit]

    def get_completion_review(
        self, completion_review_id: str, *, principal_id: str | None = None
    ) -> CompletionReviewRecord:
        record = self._completion_review_record(
            self.store.get_resource("completion-review", completion_review_id)
        )
        if (
            principal_id is not None
            and record.reviewer_principal_id != principal_id
        ):
            raise RecordNotFound(completion_review_id)
        return record

    @staticmethod
    def _completion_adjudication_record(
        resource: dict[str, Any]
    ) -> CompletionAdjudicationRecord:
        document = dict(resource["document"])
        return CompletionAdjudicationRecord(
            completion_adjudication_id=resource["resource_id"],
            batch_id=document["batchId"],
            game_id=document["gameId"],
            run_id=document["runId"],
            run_attempt_id=document.get("runAttemptId"),
            game_day_key=document["gameDayKey"],
            contract=document["contract"],
            created_at=resource["created_at"],
        )

    def list_completion_adjudications(
        self,
        *,
        batch_id: str | None = None,
        run_id: str | None = None,
        principal_id: str | None = None,
        limit: int = 100,
    ) -> list[CompletionAdjudicationRecord]:
        resources = self.store.list_resources(
            "completion-adjudication", min(1000, max(limit, limit * 10))
        )
        records = [
            self._completion_adjudication_record(item) for item in resources
        ]
        if batch_id is not None:
            records = [item for item in records if item.batch_id == batch_id]
        if run_id is not None:
            records = [item for item in records if item.run_id == run_id]
        if principal_id is not None:
            visible_review_ids = {
                item.completion_review_id
                for item in self.list_completion_reviews(
                    principal_id=principal_id, limit=1000
                )
            }
            records = [
                item
                for item in records
                if item.contract.review_id in visible_review_ids
            ]
        return records[:limit]

    def get_completion_adjudication(
        self,
        completion_adjudication_id: str,
        *,
        principal_id: str | None = None,
    ) -> CompletionAdjudicationRecord:
        record = self._completion_adjudication_record(
            self.store.get_resource(
                "completion-adjudication", completion_adjudication_id
            )
        )
        if principal_id is not None:
            visible = {
                item.completion_review_id
                for item in self.list_completion_reviews(
                    principal_id=principal_id, limit=1000
                )
            }
            if record.contract.review_id not in visible:
                raise RecordNotFound(completion_adjudication_id)
        return record

    @staticmethod
    def _automation_assessment_record(
        resource: dict[str, Any]
    ) -> AutomationAssessmentRecord:
        document = dict(resource["document"])
        return AutomationAssessmentRecord(
            assessment_id=resource["resource_id"],
            todo_instance_id=document["todoInstanceId"],
            work_item_id=document["workItemId"],
            claim_id=document["claimId"],
            decision_id=document["decisionId"],
            game_id=document["gameId"],
            run_id=document.get("runId"),
            difficulty=document["difficulty"],
            automatable=document.get("automatable"),
            confidence=document["confidence"],
            basis=document.get("basis", []),
            failure_stage=document.get("failureStage", ""),
            issue=document.get("issue", ""),
            recommendation=document.get("recommendation", ""),
            evidence_ids=document.get("evidenceIds", []),
            requested_by=document["requestedBy"],
            created_at=resource["created_at"],
        )

    def list_automation_assessments(
        self, *, todo_instance_id: str | None = None, limit: int = 100
    ) -> list[AutomationAssessmentRecord]:
        if todo_instance_id is not None:
            self.store.get_todo_instance(todo_instance_id)
        resources = self.store.list_resources(
            "automation-assessment", min(1000, max(limit, limit * 10))
        )
        records = [self._automation_assessment_record(item) for item in resources]
        if todo_instance_id is not None:
            records = [
                item for item in records if item.todo_instance_id == todo_instance_id
            ]
        return records[:limit]

    def get_automation_assessment(
        self, assessment_id: str
    ) -> AutomationAssessmentRecord:
        return self._automation_assessment_record(
            self.store.get_resource("automation-assessment", assessment_id)
        )

    def claim_work_item(
        self,
        work_item_id: str,
        request: WorkItemClaimRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        def operation() -> dict[str, Any]:
            latest = self.store.get_work_item(work_item_id)
            if latest["state"] == EntityState.RUNNING:
                active_claim_id = str(latest.get("result", {}).get("activeClaimId", ""))
                if not active_claim_id:
                    raise ManagerConflict(
                        "Running Agent work item has no recoverable active claim"
                    )
                try:
                    active_claim = self.store.get_work_item_claim_private(
                        active_claim_id
                    )
                except RecordNotFound as error:
                    raise ManagerConflict(
                        "Running Agent work item has no recoverable active claim"
                    ) from error
                if active_claim["work_item_id"] != work_item_id:
                    raise ManagerConflict(
                        "Running Agent work item claim belongs to another work item"
                    )
                expires_at = datetime.fromisoformat(
                    str(active_claim["expires_at"]).replace("Z", "+00:00")
                )
                expired = active_claim["state"] == "expired" or expires_at <= utc_now()
                if active_claim["state"] == "active" and not expired:
                    if active_claim["claimant"] != request.claimant:
                        raise ManagerConflict(
                            "Agent work item already has an active claim"
                        )
                elif not expired:
                    raise ManagerConflict(
                        "Agent work item claim cannot be renewed or recovered"
                    )
            elif latest["state"] != EntityState.PLANNED:
                raise ManagerConflict(
                    "Agent work item was closed or claimed before this mutation committed"
                )
            claimed_at = utc_now()
            claim = self.store.create_work_item_claim(
                {
                    "work_item_id": work_item_id,
                    "claimant": request.claimant,
                    "claimed_at": claimed_at.isoformat(),
                    "expires_at": (
                        claimed_at + timedelta(seconds=request.lease_seconds)
                    ).isoformat(),
                    "fencing_token": uuid.uuid4().hex,
                }
            )
            self.store.update_work_item(
                work_item_id,
                state=EntityState.RUNNING,
                result={
                    "activeClaimId": claim["claim_id"],
                },
            )
            public_claim = self.store.get_work_item_claim(claim["claim_id"])
            return self._receipt(
                command_id=claim["claim_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=None,
                message="Agent 工作项租约已建立。",
                result={
                    "claim": _dump(
                        WorkItemClaimRecord.model_validate(public_claim)
                    )
                },
            )

        receipt = self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        private_claim = self.store.get_work_item_claim_private(receipt.command_id)
        if private_claim.get("claimant") != request.claimant:
            raise ManagerConflict("Agent claim belongs to a different worker")
        direct_receipt = receipt.model_copy(deep=True)
        direct_claim = dict(direct_receipt.result.get("claim", {}))
        direct_claim["fencingToken"] = private_claim["fencing_token"]
        direct_receipt.result = {**direct_receipt.result, "claim": direct_claim}
        return direct_receipt

    def patch_config(
        self,
        request: ConfigPatchRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        patch = request.model_dump(mode="json", exclude_none=True)
        if not patch:
            raise ManagerValidation("config patch is empty")
        allowed = set(self.store.get_config()["allowed_game_ids"])
        enabled = patch.get("enabled")
        if enabled is not None and not set(enabled).issubset(allowed):
            raise ManagerValidation("enabled contains an unknown GameId")
        order = patch.get("order")
        if order is not None:
            if len(order) != len(set(order)) or not set(order).issubset(allowed):
                raise ManagerValidation(
                    "order must contain unique, allowlisted GameIds only"
                )
            current_order = [game.game_id for game in self.list_games()]
            patch["order"] = list(order) + [
                game_id for game_id in current_order if game_id not in order
            ]
        reset_policy = patch.get("todo_reset_policy")
        if reset_policy is not None:
            unknown_reset_games = set(reset_policy.get("per_game", {})) - allowed
            if unknown_reset_games:
                raise ManagerValidation(
                    "todoResetPolicy.perGame contains an unknown GameId"
                )
            known_definitions = {
                item.todo_definition_id for item in self.list_todo_definitions()
            }
            unknown_definitions = (
                set(reset_policy.get("per_definition", {})) - known_definitions
            )
            if unknown_definitions:
                raise ManagerValidation(
                    "todoResetPolicy.perDefinition contains an unknown todoDefinitionId"
                )
            validation_rules: list[dict[str, Any]] = [
                {
                    "timezone": reset_policy["timezone"],
                    "time": reset_policy["time"],
                    "cadence": "daily",
                },
                {
                    "timezone": reset_policy["timezone"],
                    "time": reset_policy["time"],
                    "cadence": "weekly",
                    "weekStartDay": reset_policy["week_start_day"],
                },
            ]
            for override in [
                *reset_policy.get("per_game", {}).values(),
                *reset_policy.get("per_definition", {}).values(),
            ]:
                validation_rules.append(
                    {
                        "timezone": override.get(
                            "timezone", reset_policy["timezone"]
                        ),
                        "time": override.get("time", reset_policy["time"]),
                        "cadence": "weekly",
                        "weekStartDay": override.get(
                            "week_start_day", reset_policy["week_start_day"]
                        ),
                    }
                )
            try:
                for rule in validation_rules:
                    todo_period(rule)
            except ValueError as error:
                raise ManagerValidation(str(error)) from error
        game_paths = patch.get("game_paths")
        if game_paths is not None:
            unknown_path_games = set(game_paths) - allowed
            if unknown_path_games:
                raise ManagerValidation("gamePaths contains an unknown GameId")
            effective_enabled = {
                game.game_id: game.enabled for game in self.list_games()
            }
            effective_enabled.update(patch.get("enabled", {}))
            for game_id, binding in game_paths.items():
                game_path = binding.get("game_path")
                tool_path = binding.get("tool_path")
                emulator = binding.get("emulator")
                if game_path and emulator is not None:
                    raise ManagerValidation(
                        f"{game_id} must use either gamePath or emulator, not both"
                    )
                if emulator is not None:
                    try:
                        emulator_binding = LDPlayerBinding(
                            game_id=game_id,
                            console_path=str(emulator.get("console_path", "")),
                            adb_path=str(emulator.get("adb_path", "")),
                            instance_index=int(emulator.get("instance_index", 0)),
                            adb_serial=str(emulator.get("adb_serial", "")),
                            instance_name=(
                                str(emulator["instance_name"])
                                if emulator.get("instance_name") is not None
                                else None
                            ),
                        )
                        self.emulator_launcher.validate_configured_binding(
                            emulator_binding
                        )
                    except (EmulatorBindingError, TypeError, ValueError) as error:
                        raise ManagerValidation(
                            f"{game_id} emulator binding is invalid: {error}"
                        ) from error
                elif not game_path:
                    if effective_enabled.get(game_id, False):
                        raise ManagerValidation(
                            f"{game_id} gamePath or emulator binding is required while the game is enabled"
                        )
                else:
                    try:
                        self.game_launcher.validate_configured_executable(game_path)
                    except GameLaunchError as error:
                        raise ManagerValidation(
                            f"{game_id} gamePath is invalid: {error}"
                        ) from error
                if tool_path and not Path(tool_path).exists():
                    raise ManagerValidation(
                        f"{game_id} toolPath is invalid: configured automation tool was not found"
                    )
                if (
                    not tool_path
                    and effective_enabled.get(game_id, False)
                    and registration_for(game_id) is not None
                ):
                    raise ManagerValidation(
                        f"{game_id} toolPath is required while its daily integration is enabled"
                    )
        daily_todo_selection = patch.get("daily_todo_selection")
        if daily_todo_selection is not None:
            unknown_selection_games = set(daily_todo_selection) - allowed
            if unknown_selection_games:
                raise ManagerValidation(
                    "dailyTodoSelection contains an unknown GameId"
                )
            definitions = {
                item.todo_definition_id: item
                for item in self.list_todo_definitions()
            }
            for game_id, definition_ids in daily_todo_selection.items():
                if len(definition_ids) != len(set(definition_ids)):
                    raise ManagerValidation(
                        "dailyTodoSelection contains duplicate todoDefinitionIds"
                    )
                for definition_id in definition_ids:
                    definition = definitions.get(definition_id)
                    if (
                        definition is None
                        or definition.game_id != game_id
                        or str(definition.cadence) != "daily"
                    ):
                        raise ManagerValidation(
                            "dailyTodoSelection must contain daily Todos for its GameId"
                        )

        def operation() -> dict[str, Any]:
            document = self.store.update_config(patch)
            config_values = dict(document["values"])
            config_values["todo_reset_policy"] = self._todo_reset_policy_document()
            config = ConfigResponse(
                version=f"cfg-{self.store.latest_event_sequence()}",
                state_version=self.store.latest_event_sequence(),
                config=config_values,
                schema_document=ConfigPatchRequest.model_json_schema(by_alias=True),
                allowed_game_ids=document["allowed_game_ids"],
                legacy_sources=document["legacy_sources"],
                updated_at=document["updated_at"],
            )
            return self._receipt(
                command_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url="/api/v1/config",
                message="Manager 配置已更新；旧 JSON 未被改写。",
                result={"config": _dump(config)},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def invoke_capability(
        self,
        request: CapabilityInvocationCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if request.fencing_token is not None:
            self._assert_no_fencing_value(
                {
                    "idempotencyKey": idempotency_key,
                    "requestId": request_id,
                    "arguments": request.arguments,
                },
                request.fencing_token,
            )
        definition = self.get_capability(request.capability)
        if not definition.enabled:
            raise ManagerConflict(f"capability is disabled: {request.capability}")
        self._reject_unsafe_arguments(request.arguments)
        allowed_keys = set(definition.input_schema.get("properties", {}))
        if not allowed_keys and not definition.requires_idempotency_key:
            raise ManagerValidation("read capabilities are invoked with GET, not POST")
        if not set(request.arguments).issubset(allowed_keys):
            raise ManagerValidation("capability arguments do not match its typed schema")
        required_keys = set(definition.input_schema.get("required", []))
        missing_keys = [key for key in required_keys if not request.arguments.get(key)]
        if missing_keys:
            raise ManagerValidation(
                f"capability arguments are missing: {', '.join(sorted(missing_keys))}"
            )

        claimed_work_item: dict[str, Any] | None = None
        claim_fields = (request.work_item_id, request.claim_id, request.fencing_token)
        if request.requested_by.lower().startswith("agent") or any(claim_fields):
            if not all(claim_fields):
                raise ManagerValidation(
                    "Agent capability requests require workItemId, claimId and fencingToken"
                )
            _, claimed_work_item = self._validate_active_claim(
                claim_id=str(request.claim_id),
                work_item_id=str(request.work_item_id),
                claimant=request.requested_by,
                fencing_token=str(request.fencing_token),
            )
            capability_ref = f"{definition.capability_id}@{definition.version}"
            if capability_ref not in set(
                claimed_work_item.get("allowed_capability_refs", [])
            ):
                raise ManagerConflict(
                    "capability is not allowed by the claimed Agent work item"
                )
            scoped_game = claimed_work_item.get("game_id")
            if scoped_game:
                if request.capability == "adapter.canary.request":
                    expected_adapter = self.adapter_host.adapter_id(str(scoped_game))
                    if request.arguments.get("adapterId") != expected_adapter:
                        raise ManagerConflict(
                            "capability AdapterId does not match the claimed work item scope"
                        )
                elif request.arguments.get("gameId") != scoped_game:
                    raise ManagerConflict(
                        "capability GameId does not match the claimed work item scope"
                    )
            scoped_run = claimed_work_item.get("run_id")
            if (
                request.capability == "observation.capture.request"
                and scoped_run
                and request.arguments.get("runId") != scoped_run
            ):
                raise ManagerConflict(
                    "screenshot RunId does not match the claimed work item scope"
                )

        execute = request.capability in {
            "game.daily.run",
            "game.weekly.run",
            "batch.daily.run",
        }
        game_id = request.arguments.get("gameId")
        if game_id:
            self._validated_game(str(game_id), require_enabled=execute)
        batch_game_ids: list[str] | None = None
        if request.capability in {"batch.daily.plan", "batch.daily.run"}:
            batch_game_ids = self._validated_games(request.arguments.get("gameIds"))

        def operation() -> dict[str, Any]:
            result: dict[str, Any]
            state = EntityState.PLANNED
            if request.capability in {
                "game.daily.plan",
                "game.daily.run",
                "game.weekly.plan",
                "game.weekly.run",
            }:
                cadence = (
                    "weekly" if request.capability.startswith("game.weekly") else "daily"
                )
                selected_game_id = str(game_id)
                self.store.reconcile_todo_instances(
                    self._todo_instance_candidates([selected_game_id], cadence),
                    intent="reconcile",
                    requested_by=request.requested_by,
                    reason="capability-game-run-planning",
                )
                todo_plan = self._todo_plans_for_games(
                    [selected_game_id], cadence
                )[selected_game_id]
                todo_plans = {selected_game_id: todo_plan}
                decision = classify_batch_todo_plans(
                    [selected_game_id], todo_plans
                )
                executable_ids = list(todo_plan["executableTodoInstanceIds"])
                unresolved_ids = list(todo_plan["unresolvedRequiredTodoIds"])
                if (
                    execute
                    and decision.has_unresolved_todos
                    and not decision.has_executable_binding
                ):
                    raise ExecutionUnavailable(
                        decision.execution_unavailable_details(todo_plans)
                    )
                if execute and executable_ids:
                    self._require_execution_ready([selected_game_id])
                run_state = (
                    EntityState.QUEUED
                    if execute and executable_ids
                    else EntityState.BLOCKED
                    if execute and unresolved_ids
                    else EntityState.REVIEW_REQUIRED
                    if execute
                    else EntityState.PLANNED
                )
                run = self.store.create_game_run(
                    {
                        "game_id": selected_game_id,
                        "cadence": cadence,
                        "state": run_state,
                        "mode": RequestMode.EXECUTE if execute else RequestMode.PLAN,
                        "requested_by": request.requested_by,
                        "message": (
                            "typed capability queued a fenced Todo execution"
                            if execute and executable_ids
                            else "typed capability found no verified executable Todo binding"
                            if execute and unresolved_ids
                            else "typed capability recorded existing Todo facts without inferring acceptance"
                            if execute
                            else "typed capability plan only"
                        ),
                        "todo_instance_ids": (
                            executable_ids if execute else unresolved_ids
                        ),
                        "completed_todo_instance_ids": todo_plan[
                            "completedTodoInstanceIds"
                        ],
                        "completion_todo_instance_ids": todo_plan[
                            "completionTodoInstanceIds"
                        ],
                    }
                )
                result = {
                    "gameRun": _dump(GameRunRecord.model_validate(run)),
                    "todoPlan": todo_plan,
                }
                state = run_state
            elif request.capability in {"batch.daily.plan", "batch.daily.run"}:
                if batch_game_ids is None:
                    raise RuntimeError("validated batch scope is missing")
                batch, runs, decision, todo_plans = self._create_batch_resources(
                    candidate_game_ids=batch_game_ids,
                    cadence=Cadence.DAILY,
                    mode=RequestMode.EXECUTE if execute else RequestMode.PLAN,
                    requested_by=request.requested_by,
                    planning_reason="capability-batch-planning",
                )
                result = (
                    {"batchId": str(batch["batch_id"])}
                    if execute
                    else {
                        "batch": _dump(self._batch_record(batch)),
                        "gameRuns": [
                            _dump(GameRunRecord.model_validate(run)) for run in runs
                        ],
                        "candidateGameIds": list(decision.candidate_game_ids),
                        "skippedCompletedGameIds": list(
                            decision.skipped_completed_game_ids
                        ),
                        "executableGameIds": list(decision.executable_game_ids),
                        "deferredGameIds": list(decision.deferred_game_ids),
                        "immediateSealed": False,
                        "todoPlans": todo_plans,
                    }
                )
                state = EntityState(str(batch["state"]))
            elif request.capability == "agent.diagnose.request":
                diagnose_game_id = str(game_id)
                self.store.reconcile_todo_instances(
                    self._todo_instance_candidates([diagnose_game_id], "daily"),
                    intent="reconcile",
                    requested_by=request.requested_by,
                    reason="agent-diagnose-capability-planning",
                )
                diagnose_plans = self._todo_plans_for_games(
                    [diagnose_game_id], "daily"
                )
                diagnose_items = self.list_todo_instances(
                    game_id=diagnose_game_id,
                    cadence="daily",
                    current=True,
                    limit=1000,
                )
                diagnose_snapshot = self._typed_agent_todo_snapshot(
                    todo_items=diagnose_items,
                    plans=diagnose_plans,
                    cadence="daily",
                    include_execution_facts=True,
                )
                diagnose_artifact_refs: list[str] = []
                for todo_item in diagnose_items:
                    for artifact_id in todo_item.evidence_refs:
                        if artifact_id in diagnose_artifact_refs:
                            continue
                        if not self._is_opaque_artifact_id(artifact_id):
                            raise ManagerValidation(
                                "Todo evidenceRefs must contain opaque artifact IDs"
                            )
                        self.store.get_resource("artifact", artifact_id)
                        diagnose_artifact_refs.append(artifact_id)
                        if len(diagnose_artifact_refs) == 50:
                            break
                    if len(diagnose_artifact_refs) == 50:
                        break
                work_item = self.store.create_work_item(
                    {
                        "kind": WorkItemKind.DIAGNOSE_GAME,
                        "state": EntityState.PLANNED,
                        "game_id": diagnose_game_id,
                        "cadence": "daily",
                        "requested_by": request.requested_by,
                        "note": "created by typed capability",
                        "artifact_refs": diagnose_artifact_refs,
                        "allowed_capability_refs": self._allowed_capability_refs(
                            WorkItemKind.DIAGNOSE_GAME
                        ),
                        "result": {
                            "todoSnapshot": diagnose_snapshot,
                            "executionRequested": False,
                        },
                    }
                )
                result = {
                    "workItem": _dump(AgentWorkItemRecord.model_validate(work_item))
                }
            elif request.capability == "adapter.canary.request":
                canary = self._record_adapter_diagnostic_canary(
                    str(request.arguments["adapterId"]),
                    requested_by=request.requested_by,
                    note="requested through typed capability invocation",
                )
                result = {"adapterDiagnosticCanary": self._public_resource(canary)}
                state = EntityState.DONE
            elif request.capability == "observation.capture.request":
                capture_run_id = str(request.arguments["runId"])
                capture_run = self.store.get_game_run(capture_run_id)
                if capture_run["game_id"] != str(game_id):
                    raise ManagerConflict("screenshot GameId does not match its GameRun")
                attempts = self.store.list_run_attempts(run_id=capture_run_id, limit=1)
                if not attempts:
                    raise ManagerConflict("screenshot requires an existing RunAttempt")
                run_attempt = attempts[0]
                if run_attempt["game_id"] != str(game_id):
                    raise ManagerConflict("screenshot RunAttempt scope is stale")
                try:
                    captured = self.window_capture.capture(str(game_id))
                except WindowCaptureError as error:
                    raise ManagerConflict(str(error)) from error
                artifact_id = str(uuid.uuid4())
                artifact_root = self.settings.data_dir / "artifacts"
                if str(artifact_root).startswith("\\\\"):
                    raise ManagerValidation("screenshot artifact storage must be local")
                artifact_root.mkdir(parents=True, exist_ok=True)
                if _has_reparse_point(artifact_root):
                    raise ManagerValidation("screenshot artifact storage contains a link")
                file_name = f"window-screenshot-{artifact_id}.png"
                final_path = artifact_root / file_name
                temp_path = artifact_root / f".{artifact_id}.tmp"
                captured_at = utc_now()
                try:
                    temp_path.write_bytes(captured.content)
                    os.replace(temp_path, final_path)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
                digest = hashlib.sha256(captured.content).hexdigest()
                artifact = self.store.create_resource(
                    "artifact",
                    resource_id=artifact_id,
                    state="captured",
                    document={
                        "kind": "window-screenshot",
                        "capturedAt": captured_at.isoformat(),
                        "source": "manager-window-capture-v1",
                        "raw": True,
                        "contentType": "image/png",
                        "gameId": str(game_id),
                        "runId": capture_run_id,
                        "runAttemptId": run_attempt["run_attempt_id"],
                        "gameDayKey": self._run_game_day_key(
                            capture_run,
                            list(run_attempt["plan"].get("executableTodoInstanceIds", [])),
                        ),
                        "verdict": "pending",
                        "hash": digest,
                        "sizeBytes": len(captured.content),
                        "fileName": file_name,
                        "relativePath": file_name,
                        "window": {
                            "hwnd": captured.hwnd,
                            "pid": captured.pid,
                            "processName": captured.process_name,
                            "title": captured.title,
                            "width": captured.width,
                            "height": captured.height,
                            "captureMethod": captured.method,
                        },
                    },
                )
                if claimed_work_item is not None:
                    self.store.append_work_item_artifact(
                        str(claimed_work_item["work_item_id"]), artifact_id
                    )
                observation = self.store.create_resource(
                    "window-observation",
                    state="captured",
                    document={
                        "gameId": str(game_id),
                        "runId": capture_run_id,
                        "runAttemptId": run_attempt["run_attempt_id"],
                        "artifactId": artifact_id,
                        "capturedAt": captured_at.isoformat(),
                        "sceneCode": "unreviewed-game-window",
                    },
                )
                result = {
                    "artifact": _dump(self._artifact_record(artifact)),
                    "observation": self._public_resource(observation),
                }
                state = EntityState.DONE
            else:
                # Enabled capabilities must terminate in a typed domain handler.
                # Never turn a registry entry into a generic success-shaped row.
                raise ManagerConflict(
                    f"capability handler is unavailable: {request.capability}"
                )
            invocation = self.store.create_capability_invocation(
                {
                    "capability": request.capability,
                    "state": state,
                    "arguments": request.arguments,
                    "requested_by": request.requested_by,
                    "result": result,
                }
            )
            if not (execute and request.capability == "batch.daily.run"):
                result["invocation"] = _dump(
                    CapabilityInvocationRecord.model_validate(invocation)
                )
            return self._receipt(
                command_id=invocation["invocation_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/capability-invocations/{invocation['invocation_id']}",
                message="typed capability invocation accepted by Manager",
                result=result,
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if (
            execute
            and not replayed
            and request.capability == "batch.daily.run"
        ):
            invocation = self.store.get_capability_invocation(
                str(receipt.command_id)
            )
            batch_id = str(invocation.get("result", {}).get("batchId", ""))
            if not batch_id:
                raise RuntimeError(
                    "persisted batch.daily.run invocation is missing BatchId"
                )
            batch = self.store.get_batch(batch_id)
            memberships = self.store.list_batch_run_memberships(
                batch_id=batch_id, limit=5000
            )
            runs = [
                _dump(
                    GameRunRecord.model_validate(
                        self.store.get_game_run(str(item["run_id"]))
                    )
                )
                for item in memberships
            ]
            if runs:
                threading.Thread(
                    target=self._run_batch,
                    args=(batch_id, runs, str(receipt.command_id)),
                    name=f"yeyu-gamer-batch-{batch_id[:8]}",
                    daemon=True,
                ).start()
            else:
                accepted_done = bool(batch.get("result", {}).get("acceptedDone"))
                self._complete_command(
                    str(receipt.command_id),
                    "succeeded" if accepted_done else "failed",
                    (
                        "batch sealed accepted_done by same-GameDay completion "
                        "reconciliation"
                        if accepted_done
                        else "batch sealed without starting Host because no new Todo "
                        "execution was required"
                    ),
                )
                self.notification_dispatcher.wake()
        elif execute and not replayed:
            run = receipt.result["gameRun"]
            run_id = str(run["runId"])
            if run.get("todoInstanceIds"):
                try:
                    self._start_game_run(
                        run_id, command_id=str(receipt.command_id)
                    )
                except Exception as error:
                    failure = (
                        f"Manager Adapter start failed: {type(error).__name__}: {error}"
                    )
                    self._complete_command(str(receipt.command_id), "failed", failure)
            else:
                self._complete_command(
                    str(receipt.command_id),
                    "failed",
                    "no verified executable Todo binding; Host was not started",
                )
        return receipt

    def cancel_game_run(
        self,
        run_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        self.store.get_game_run(run_id)

        def operation() -> dict[str, Any]:
            latest = self.store.get_game_run(run_id)
            if latest["state"] == EntityState.RUNNING:
                active_attempt = next(
                    (
                        item
                        for item in self.store.list_run_attempts(
                            run_id=run_id, limit=20
                        )
                        if item["state"] in {"starting", "running", "cancelling"}
                    ),
                    None,
                )
                if active_attempt is not None:
                    self._end_controller_lease(
                        active_attempt["run_attempt_id"],
                        state=LeaseState.REVOKED,
                        reason_code="user_cancelled",
                        reason=str(request.get("reason", "operator-request")),
                    )
                    if active_attempt["state"] in {"starting", "running"}:
                        self.store.update_run_attempt(
                            str(active_attempt["run_attempt_id"]),
                            state="cancelling",
                            result={
                                "cancelReason": "user_cancelled",
                                "cancelPhase": (
                                    "game-launch"
                                    if self._is_pre_adapter_launch_attempt(
                                        active_attempt
                                    )
                                    else "adapter-host"
                                ),
                            },
                            completed=False,
                        )
                self.store.append_event(
                    "game-run.cancel-requested",
                    "game-run",
                    run_id,
                    {
                        "requestedBy": str(request.get("requestedBy", "operator")),
                        "reason": str(request.get("reason", "operator-request")),
                    },
                )
                record = self.store.update_game_run(
                    run_id,
                    state=EntityState.CANCELLING,
                    message=(
                        "durable cancellation intent persisted; delivery target "
                        "is being resolved"
                    ),
                )
            elif latest["state"] in {EntityState.QUEUED, EntityState.PLANNED}:
                record = self.store.update_game_run(
                    run_id,
                    state=EntityState.CANCELLED,
                    message=str(request.get("reason", "operator-request")),
                )
            else:
                raise ManagerConflict("terminal GameRun cannot be cancelled again")
            return self._receipt(
                command_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/game-runs/{run_id}",
                message=(
                    "运行中的取消意图已持久化，正在中止当前受管阶段。"
                    if latest["state"] == EntityState.RUNNING
                    else "未运行的游戏请求已取消。"
                ),
                result={
                    "gameRun": _dump(GameRunRecord.model_validate(record)),
                    "cancellationPending": latest["state"] == EntityState.RUNNING,
                    "activeRunAttemptId": (
                        str(active_attempt["run_attempt_id"])
                        if latest["state"] == EntityState.RUNNING
                        and active_attempt is not None
                        else None
                    ),
                },
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=request,
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if (
            receipt.result.get("cancellationPending") is True
            and not replayed
        ):
            delivery_target: str | None = None
            run_attempt_id = receipt.result.get("activeRunAttemptId")
            with self._execution_handoff_lock:
                attempt = (
                    self.store.get_run_attempt(str(run_attempt_id))
                    if run_attempt_id is not None
                    else None
                )
                if attempt is not None and self._is_pre_adapter_launch_attempt(
                    attempt
                ):
                    signalled = True
                    delivery_target = "manager-game-launch"
                else:
                    host_attempt_id, signalled = self.adapter_host.cancel_run(
                        run_id=run_id, reason_code="user_cancelled"
                    )
                    if host_attempt_id is not None:
                        run_attempt_id = str(host_attempt_id)
                    if signalled:
                        delivery_target = "adapter-host"
            if not signalled:
                self._complete_command(
                    str(receipt.command_id),
                    "failed",
                    "the active managed execution phase could not receive cancellation",
                )
            else:
                attempt = self.store.get_run_attempt(str(run_attempt_id))
                if attempt["state"] in {"starting", "running", "cancelling"}:
                    self.store.update_run_attempt(
                        str(run_attempt_id),
                        state="cancelling",
                        result={
                            "cancelReason": "user_cancelled",
                            "cancelDeliveryTarget": delivery_target,
                        },
                        completed=False,
                    )
                self._complete_command(
                    str(receipt.command_id),
                    "succeeded",
                    (
                        "cancellation was accepted by the Manager game-launch gate"
                        if delivery_target == "manager-game-launch"
                        else "cooperative cancellation was delivered to the fixed Adapter Host"
                    ),
                )
        return receipt

    @staticmethod
    def _resume_blocker_kind(kind: str) -> ResumeBlockerKind:
        if kind == "human_required":
            return ResumeBlockerKind.HUMAN_REQUIRED
        if kind == "review_required":
            return ResumeBlockerKind.REVIEW_REQUIRED
        if kind in {"safety_gate", "forbidden"}:
            return ResumeBlockerKind.FORBIDDEN
        return ResumeBlockerKind.TECHNICAL

    @staticmethod
    def _resume_risk(risk: str) -> ResumeRisk:
        mapping = {
            "observe_only": ResumeRisk.OBSERVE_ONLY,
            "routine_action": ResumeRisk.ROUTINE_ACTION,
            "controlled_write": ResumeRisk.CONTROLLED_WRITE,
            "approval_required": ResumeRisk.APPROVAL_REQUIRED,
            "forbidden": ResumeRisk.FORBIDDEN,
        }
        try:
            return mapping[risk]
        except KeyError as error:
            raise ManagerValidation(f"unsupported Todo risk for resume: {risk}") from error

    def _latest_resume_action_receipt(
        self,
        facts: list[dict[str, Any]],
        *,
        run_id: str,
        todo_instance_id: str,
        todo_attempt: dict[str, Any] | None,
        game_day_key: str,
    ) -> ActionReceiptResumeFact | None:
        if todo_attempt is None:
            return None
        for item in facts:
            if (
                item["factType"] != "action_receipt"
                or item.get("todoInstanceId") != todo_instance_id
            ):
                continue
            document = dict(item.get("document") or {})
            resume = dict(document.get("resume") or document)
            if resume.get("todoAttemptId") != todo_attempt["todo_attempt_id"]:
                continue
            outcome_value = resume.get("outcome", resume.get("result"))
            if outcome_value == "no_effect":
                outcome_value = "failed"
            idempotency_value = resume.get(
                "idempotency", resume.get("actionIdempotency")
            )
            if outcome_value not in {item.value for item in ActionReceiptOutcome}:
                continue
            if idempotency_value not in {item.value for item in ActionIdempotency}:
                # The execution fact itself never implies action idempotency.
                # Without an explicit real provider field it remains absent.
                continue
            return ActionReceiptResumeFact(
                action_receipt_id=item["factId"],
                run_id=run_id,
                run_attempt_id=todo_attempt["run_attempt_id"],
                todo_instance_id=todo_instance_id,
                todo_attempt_id=todo_attempt["todo_attempt_id"],
                game_day_key=game_day_key,
                outcome=ActionReceiptOutcome(outcome_value),
                idempotency=ActionIdempotency(idempotency_value),
                revision=self.store.entity_revision(
                    "execution-control-fact", item["factId"]
                ),
            )
        return None

    def _latest_resume_checkpoint(
        self,
        facts: list[dict[str, Any]],
        *,
        run_id: str,
        todo_instance_id: str,
        todo_attempt: dict[str, Any] | None,
        game_day_key: str,
    ) -> CheckpointResumeFact | None:
        if todo_attempt is None:
            return None
        for item in facts:
            if (
                item["factType"] != "checkpoint"
                or item.get("todoInstanceId") != todo_instance_id
            ):
                continue
            document = dict(item.get("document") or {})
            resume = dict(document.get("resume") or document)
            if resume.get("todoAttemptId") != todo_attempt["todo_attempt_id"]:
                continue
            stage = resume.get("stage")
            if stage not in {item.value for item in CheckpointStage}:
                continue
            return CheckpointResumeFact(
                checkpoint_id=item["factId"],
                run_id=run_id,
                run_attempt_id=todo_attempt["run_attempt_id"],
                todo_instance_id=todo_instance_id,
                todo_attempt_id=todo_attempt["todo_attempt_id"],
                game_day_key=game_day_key,
                stage=CheckpointStage(stage),
                revision=self.store.entity_revision(
                    "execution-control-fact", item["factId"]
                ),
                valid=bool(resume.get("valid", resume.get("state") == "valid")),
                action_receipt_id=resume.get("actionReceiptId"),
                observation_refs=tuple(resume.get("observationRefs", [])),
            )
        return None

    def _resume_reconcile_snapshot(
        self, run_id: str
    ) -> tuple[ResumeReconcileSnapshot, dict[str, Any]]:
        """Project one consistent Manager ledger snapshot for pure planning."""

        with self.store.atomic():
            run = self.store.get_game_run(run_id)
            attempts = self.store.list_run_attempts(run_id=run_id, limit=1000)
            if not attempts:
                raise ManagerConflict(
                    "same-run resume requires a durable predecessor RunAttempt"
                )
            current_attempt = attempts[0]
            game_day_key = self._run_game_day_key(run)
            runtime = self.adapter_host.execution_bindings(run["game_id"])
            binding_by_operation = {
                str(item["operation"]): item
                for item in runtime.get("bindings", [])
                if isinstance(item, dict) and isinstance(item.get("operation"), str)
            }
            facts = self.store.list_execution_control_facts(
                run_id=run_id, limit=5000
            )
            todos: list[TodoResumeFact] = []
            for todo_id in run["todo_instance_ids"]:
                todo = self.store.get_todo_instance(todo_id)
                if todo["period_key"] != game_day_key:
                    raise ManagerConflict("resume snapshot crosses GameDay boundaries")
                attempts_for_todo = []
                for candidate in self.store.list_todo_attempts(
                    todo_instance_id=todo_id, limit=1000
                ):
                    candidate_run_attempt = self.store.get_run_attempt(
                        candidate["run_attempt_id"]
                    )
                    if candidate_run_attempt["run_id"] == run_id:
                        attempts_for_todo.append(candidate)
                latest_todo_attempt = attempts_for_todo[0] if attempts_for_todo else None
                attempt_fact = (
                    TodoAttemptResumeFact(
                        todo_attempt_id=latest_todo_attempt["todo_attempt_id"],
                        run_attempt_id=latest_todo_attempt["run_attempt_id"],
                        run_id=run_id,
                        todo_instance_id=todo_id,
                        game_day_key=game_day_key,
                        attempt_number=int(latest_todo_attempt["attempt_number"]),
                        state=TodoAttemptState(latest_todo_attempt["state"]),
                        revision=self.store.entity_revision(
                            "todo-attempt", latest_todo_attempt["todo_attempt_id"]
                        ),
                        retryable=bool(latest_todo_attempt["retryable"]),
                    )
                    if latest_todo_attempt is not None
                    else None
                )
                blocker_rows = self.store.list_todo_blockers(
                    run_id=run_id, todo_instance_id=todo_id, limit=1000
                )
                blocker_row = blocker_rows[0] if blocker_rows else None
                blocker_fact = None
                if blocker_row is not None:
                    blocker_entity_revision = self.store.entity_revision(
                        "todo-blocker", blocker_row["blocker_id"]
                    )
                    release = (
                        HumanBlockerReleaseFact(
                            release_id=blocker_row["release_id"],
                            blocker_id=blocker_row["blocker_id"],
                            blocker_revision=blocker_entity_revision,
                            run_id=run_id,
                            todo_instance_id=todo_id,
                            game_day_key=game_day_key,
                        )
                        if blocker_row["kind"] == "human_required"
                        and blocker_row["state"] == "resolved"
                        and blocker_row["release_explicit"]
                        and blocker_row.get("release_id")
                        else None
                    )
                    blocker_fact = TodoBlockerResumeFact(
                        blocker_id=blocker_row["blocker_id"],
                        run_id=run_id,
                        todo_instance_id=todo_id,
                        game_day_key=game_day_key,
                        kind=self._resume_blocker_kind(blocker_row["kind"]),
                        revision=blocker_entity_revision,
                        active=blocker_row["state"] == "active",
                        retryable=bool(blocker_row["retryable"]),
                        release=release,
                    )
                binding = binding_by_operation.get(todo["operation"])
                binding_fact = (
                    ResumeBindingFact(
                        operation=todo["operation"],
                        supports_resume=bool(binding.get("supportsResume")),
                    )
                    if bool(runtime.get("manifestVerified")) and binding is not None
                    else None
                )
                receipt_fact = self._latest_resume_action_receipt(
                    facts,
                    run_id=run_id,
                    todo_instance_id=todo_id,
                    todo_attempt=latest_todo_attempt,
                    game_day_key=game_day_key,
                )
                checkpoint_fact = self._latest_resume_checkpoint(
                    facts,
                    run_id=run_id,
                    todo_instance_id=todo_id,
                    todo_attempt=latest_todo_attempt,
                    game_day_key=game_day_key,
                )
                resume_todo_status = ResumeTodoState(todo["status"])
                if (
                    latest_todo_attempt is not None
                    and str(latest_todo_attempt.get("reason_code") or "").startswith(
                        ("outcome_unknown_", "manager_process_restarted")
                    )
                    and receipt_fact is None
                    and checkpoint_fact is None
                ):
                    resume_todo_status = ResumeTodoState.RECONCILING
                todos.append(
                    TodoResumeFact(
                        todo_instance_id=todo_id,
                        run_id=run_id,
                        game_id=run["game_id"],
                        game_day_key=game_day_key,
                        revision=max(
                            1, self.store.entity_revision("todo-instance", todo_id)
                        ),
                        status=resume_todo_status,
                        operation=todo["operation"],
                        risk=self._resume_risk(todo["risk"]),
                        latest_attempt=attempt_fact,
                        action_receipt=receipt_fact,
                        checkpoint=checkpoint_fact,
                        blocker=blocker_fact,
                        binding=binding_fact,
                    )
                )
            snapshot = ResumeReconcileSnapshot(
                run=GameRunResumeFact(
                    run_id=run_id,
                    game_id=run["game_id"],
                    game_day_key=game_day_key,
                    state=ResumeRunState(run["state"]),
                    revision=self.store.entity_revision("game-run", run_id),
                ),
                current_attempt=CurrentAttemptFact(
                    run_attempt_id=current_attempt["run_attempt_id"],
                    run_id=run_id,
                    game_id=run["game_id"],
                    game_day_key=game_day_key,
                    state=ResumeAttemptState(current_attempt["state"]),
                    attempt_ordinal=int(current_attempt["attempt_ordinal"]),
                    revision=self.store.entity_revision(
                        "run-attempt", current_attempt["run_attempt_id"]
                    ),
                ),
                todos=tuple(todos),
            )
        return snapshot, {"run": run, "runtimeBinding": runtime}

    @staticmethod
    def _resume_provider_status() -> dict[str, bool]:
        # These flip only when real Manager-owned providers are installed.
        return {
            "windowBinding": False,
            "focusLease": False,
            "observation": False,
            "actionReceipt": False,
            "checkpoint": False,
        }

    @staticmethod
    def _resume_required_provider(requirement: str) -> str | None:
        return {
            ReconcileRequirement.ACTION_RECEIPT_RECONCILIATION.value: "actionReceipt",
            ReconcileRequirement.FRESH_OBSERVATION.value: "observation",
            ReconcileRequirement.CHECKPOINT_VALIDATION.value: "checkpoint",
            ReconcileRequirement.BINDING_REVALIDATION.value: "windowBinding",
            ReconcileRequirement.NON_IDEMPOTENT_GUARD.value: "actionReceipt",
        }.get(requirement)

    def _release_human_takeover(
        self, run: dict[str, Any], request: RunControlRequest
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        release_id = str(uuid.uuid4())
        resolved: list[dict[str, Any]] = []
        for blocker in self.store.list_todo_blockers(
            run_id=run["run_id"], active_only=True, limit=5000
        ):
            if blocker["kind"] != "human_required":
                continue
            resolved_blocker = self.store.resolve_todo_blocker(
                blocker["blocker_id"],
                resolution_code="explicit_human_takeover_release",
                resolution_reason=request.reason,
                release_id=release_id,
                explicit_release=True,
                released_by=request.requested_by,
            )
            resolved.append(resolved_blocker)
            todo = self.store.get_todo_instance(blocker["todo_instance_id"])
            if todo["status"] == "human_required":
                self.store.transition_todo_instance(
                    todo["todo_instance_id"],
                    status="review_required",
                    reason=HUMAN_TAKEOVER_RELEASED_REOBSERVE_REASON,
                    evidence_refs=list(todo["evidence_refs"]),
                    run_id=run["run_id"],
                    increment_attempt=False,
                    requested_by=request.requested_by,
                )
        # An Adapter may have entered human_required without current-attempt
        # evidence, in which case policy deliberately refused to fabricate a
        # persistent blocker.  The explicit release still moves that Todo only
        # to review_required; it never makes it executable.
        for todo_id in run["todo_instance_ids"]:
            todo = self.store.get_todo_instance(todo_id)
            if todo["status"] == "human_required":
                self.store.transition_todo_instance(
                    todo_id,
                    status="review_required",
                    reason=HUMAN_TAKEOVER_RELEASED_REOBSERVE_REASON,
                    evidence_refs=list(todo["evidence_refs"]),
                    run_id=run["run_id"],
                    increment_attempt=False,
                    requested_by=request.requested_by,
                )
        for resource in self.store.list_resources("run-control-request", 1000):
            if (
                resource["state"] == "active"
                and resource["document"].get("runId") == run["run_id"]
                and resource["document"].get("action") == "takeover"
            ):
                self.store.update_resource(
                    "run-control-request",
                    resource["resource_id"],
                    state="released",
                    document={
                        **resource["document"],
                        "releaseId": release_id,
                        "releaseReason": request.reason,
                        "releasedBy": request.requested_by,
                        "authorizesExecution": False,
                    },
                )
        release = self.store.create_resource(
            "run-control-request",
            resource_id=release_id,
            state="released",
            document={
                "runId": run["run_id"],
                "action": "release-takeover",
                "reason": request.reason,
                "requestedBy": request.requested_by,
                "resolvedBlockerIds": [item["blocker_id"] for item in resolved],
                "executionRequested": False,
                "authorizesExecution": False,
                "requirements": [
                    "fresh_observation",
                    "binding_revalidation",
                    "checkpoint_validation",
                ],
            },
        )
        return release, resolved

    def create_run_control_request(
        self,
        run_id: str,
        action: str,
        request: RunControlRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if action not in {"resume", "takeover", "release-takeover"}:
            raise ManagerValidation("unsupported run control request")
        current = self.store.get_game_run(run_id)
        if current["state"] in {"done", "cancelled"}:
            raise ManagerConflict("terminal runs cannot accept control requests")

        if action == "release-takeover":
            active_human_blocker = any(
                item["kind"] == "human_required"
                for item in self.store.list_todo_blockers(
                    run_id=run_id, active_only=True, limit=5000
                )
            )
            active_takeover_resource = any(
                item["state"] == "active"
                and item["document"].get("runId") == run_id
                and item["document"].get("action") == "takeover"
                for item in self.store.list_resources("run-control-request", 1000)
            )
            if not (
                current["state"] == "human_required"
                or active_human_blocker
                or active_takeover_resource
            ):
                raise ManagerConflict("the run has no active human takeover to release")

        resume_launch: dict[str, Any] | None = None

        def operation() -> dict[str, Any]:
            latest = self.store.get_game_run(run_id)
            if action == "takeover":
                active_attempt = next(
                    (
                        item
                        for item in self.store.list_run_attempts(
                            run_id=run_id, limit=100
                        )
                        if item["state"] in {"starting", "running", "cancelling"}
                    ),
                    None,
                )
                if active_attempt is not None:
                    self._end_controller_lease(
                        active_attempt["run_attempt_id"],
                        state=LeaseState.REVOKED,
                        reason_code="human_takeover_requested",
                        reason=request.reason,
                    )
                resource = self.store.create_resource(
                    "run-control-request",
                    state="active",
                    document={
                        "runId": run_id,
                        "action": action,
                        "reason": request.reason,
                        "requestedBy": request.requested_by,
                        "executionRequested": False,
                        "authorizesExecution": False,
                    },
                )
                run = self.store.update_game_run(
                    run_id,
                    state=EntityState.HUMAN_REQUIRED,
                    message="human takeover requested; Manager control lease revoked",
                )
                return self._receipt(
                    command_id=resource["resource_id"],
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    status_url=f"/api/v1/game-runs/{run_id}",
                    message="Human takeover was recorded without authorizing actions.",
                    result={
                        "requestId": resource["resource_id"],
                        "gameRun": _dump(GameRunRecord.model_validate(run)),
                        "executionRequested": False,
                        "eligibleTodoInstanceIds": [],
                        "skippedTodoInstanceIds": [],
                        "deferredTodoInstanceIds": list(run["todo_instance_ids"]),
                        "requirements": ["explicit_human_release"],
                    },
                )

            if action == "release-takeover":
                release, resolved = self._release_human_takeover(latest, request)
                run = self.store.update_game_run(
                    run_id,
                    state=EntityState.REVIEW_REQUIRED,
                    message=(
                        "takeover explicitly released; fresh observation, rebind, and "
                        "checkpoint validation are required before execution"
                    ),
                )
                return self._receipt(
                    command_id=release["resource_id"],
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    status_url=f"/api/v1/game-runs/{run_id}",
                    message="Takeover release recorded; it does not authorize a click.",
                    result={
                        "requestId": release["resource_id"],
                        "gameRun": _dump(GameRunRecord.model_validate(run)),
                        "executionRequested": False,
                        "eligibleTodoInstanceIds": [],
                        "skippedTodoInstanceIds": [],
                        "deferredTodoInstanceIds": list(run["todo_instance_ids"]),
                        "requirements": [
                            "fresh_observation",
                            "binding_revalidation",
                            "checkpoint_validation",
                        ],
                        "resolvedBlockerIds": [
                            item["blocker_id"] for item in resolved
                        ],
                    },
                )

            predecessor_attempts = self.store.list_run_attempts(
                run_id=run_id, limit=1
            )
            if (
                latest["state"] == EntityState.HUMAN_REQUIRED
                and predecessor_attempts
                and predecessor_attempts[0]["result"].get("launchState")
                == "launch-human-required"
            ):
                # A pre-Adapter gate has no TodoAttempt to carry the human
                # blocker.  Pending Todos must not bypass the run-level gate.
                raise ManagerConflict(
                    "explicit_human_release: release the launch takeover before same-run resume"
                )
            if not predecessor_attempts:
                todo_plan = self._todo_plans_for_games(
                    [latest["game_id"]], latest["cadence"]
                )[latest["game_id"]]
                todo_plan = {
                    **todo_plan,
                    "executableTodoInstanceIds": [],
                    "deferredTodoInstanceIds": list(latest["todo_instance_ids"]),
                }
                work_item = self.store.create_work_item(
                    {
                        "kind": WorkItemKind.OBSERVATION,
                        "state": EntityState.REVIEW_REQUIRED,
                        "game_id": latest["game_id"],
                        "cadence": latest["cadence"],
                        "run_id": run_id,
                        "requested_by": request.requested_by,
                        "note": "same-run resume has no durable predecessor RunAttempt",
                        "artifact_refs": [],
                        "allowed_capability_refs": self._allowed_capability_refs(
                            WorkItemKind.OBSERVATION
                        ),
                        "result": {
                            "executionRequested": False,
                            "requirements": ["current_terminal_attempt"],
                            "providerStatus": self._resume_provider_status(),
                        },
                    }
                )
                resource = self.store.create_resource(
                    "run-control-request",
                    state="reconciliation_required",
                    document={
                        "runId": run_id,
                        "action": "resume",
                        "reason": request.reason,
                        "requestedBy": request.requested_by,
                        "executionRequested": False,
                        "reconcileWorkItemId": work_item["work_item_id"],
                        "requirements": ["current_terminal_attempt"],
                    },
                )
                run = self.store.update_game_run(
                    run_id,
                    state=EntityState.REVIEW_REQUIRED,
                    message="same-run resume requires a durable predecessor attempt",
                )
                return self._receipt(
                    command_id=resource["resource_id"],
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    status_url=f"/api/v1/game-runs/{run_id}",
                    message="Reconciliation work was recorded; Host was not started.",
                    result={
                        "requestId": resource["resource_id"],
                        "resumeIntentId": None,
                        "gameRun": _dump(GameRunRecord.model_validate(run)),
                        "todoPlan": todo_plan,
                        "executionRequested": False,
                        "eligibleTodoInstanceIds": [],
                        "skippedTodoInstanceIds": [],
                        "deferredTodoInstanceIds": list(
                            latest["todo_instance_ids"]
                        ),
                        "requirements": ["current_terminal_attempt"],
                        "unavailableRequirements": [],
                        "providerStatus": self._resume_provider_status(),
                        "reconcileWorkItemId": work_item["work_item_id"],
                        "successorAttemptOrdinal": None,
                    },
                )

            try:
                snapshot, projected = self._resume_reconcile_snapshot(run_id)
                pure_request = ResumeReconcileRequest(
                    run_id=run_id,
                    game_day_key=snapshot.run.game_day_key,
                    expected_run_revision=(
                        request.expected_run_revision
                        if request.expected_run_revision is not None
                        else snapshot.run.revision
                    ),
                    expected_current_attempt_id=(
                        request.expected_current_attempt_id
                        or snapshot.current_attempt.run_attempt_id
                    ),
                    expected_current_attempt_revision=(
                        request.expected_current_attempt_revision
                        if request.expected_current_attempt_revision is not None
                        else snapshot.current_attempt.revision
                    ),
                )
                decision = plan_resume_reconciliation(snapshot, pure_request)
            except ResumeReconcileConflict as error:
                raise ManagerConflict(f"{error.code}: {error}") from error

            decision_document = self._without_fencing_material(
                decision.model_dump(mode="json", by_alias=True)
            )
            successor_document = decision_document.get("successorAttemptIntent")
            if isinstance(successor_document, dict):
                successor_document["requiresNewFence"] = True
            provider_status = self._resume_provider_status()
            requirements = list(
                dict.fromkeys(
                    requirement.value
                    for item in decision.items
                    for requirement in item.requirements
                )
            )
            if decision.deferred_human:
                requirements.append("explicit_human_release")
            if decision.deferred_review:
                requirements.append("review_resolution")
            if decision.deferred_forbidden:
                requirements.append("forbidden_policy")
            requirements = list(dict.fromkeys(requirements))
            unavailable_requirements = [
                {
                    "requirement": requirement,
                    "provider": provider,
                    "status": "disabled-unimplemented",
                }
                for requirement in requirements
                for provider in [self._resume_required_provider(requirement)]
                if provider is not None and not provider_status[provider]
            ]
            has_deferred = bool(
                decision.reconcile_unknown
                or decision.deferred_review
                or decision.deferred_human
                or decision.deferred_forbidden
            )
            execution_requested = bool(decision.eligible_pending) and not has_deferred and not unavailable_requirements
            eligible_ids = list(decision.eligible_pending)
            skipped_ids = list(
                dict.fromkeys([*decision.completed_skip, *decision.terminal_skip])
            )
            deferred_ids = list(
                dict.fromkeys(
                    [
                        *decision.reconcile_unknown,
                        *decision.deferred_review,
                        *decision.deferred_human,
                        *decision.deferred_forbidden,
                    ]
                )
            )
            todo_plan = self._todo_plans_for_games(
                [latest["game_id"]], latest["cadence"]
            )[latest["game_id"]]
            latest_batch = self._latest_batch_for_run(run_id)
            target_batch: dict[str, Any] | None = None
            if execution_requested and latest_batch is not None:
                latest_batch_result = dict(latest_batch.get("result", {}))
                if latest_batch_result.get("sealVersion") is None:
                    recovery = latest_batch_result.get("recoveryPhase")
                    recovery_ready = (
                        isinstance(recovery, dict)
                        and recovery.get("status") == "ready_for_resume"
                    )
                    if (
                        isinstance(
                            latest_batch_result.get("completionReviewPhase"), dict
                        )
                        or not recovery_ready
                    ):
                        execution_requested = False
                        deferred_ids = list(
                            dict.fromkeys([*deferred_ids, *eligible_ids])
                        )
                        eligible_ids = []
                        requirements.append(
                            "owning_batch_reconciliation_or_review"
                        )
                    else:
                        target_batch = latest_batch
            intent, _ = self.store.create_or_get_resume_intent(
                {
                    "run_id": run_id,
                    "predecessor_run_attempt_id": (
                        decision.source_current_attempt_id
                    ),
                    "source_run_revision": decision.source_run_revision,
                    "source_attempt_revision": (
                        decision.source_current_attempt_revision
                    ),
                    "decision": decision_document,
                    "state": (
                        "successor_pending"
                        if execution_requested
                        else "reconciliation_required"
                        if deferred_ids
                        else "no_execution"
                    ),
                }
            )
            if execution_requested and latest_batch is not None:
                if latest_batch["result"].get("sealVersion") is not None:
                    predecessor_lineage = {
                        "rootBatchId": latest_batch["root_batch_id"],
                        "predecessorBatchId": latest_batch["batch_id"],
                        "predecessorSealVersion": latest_batch["result"][
                            "sealVersion"
                        ],
                        "continuationOrdinal": (
                            int(latest_batch["continuation_ordinal"]) + 1
                        ),
                        "resumeIntentId": intent["resume_intent_id"],
                    }
                    target_batch, _ = self.store.create_or_get_continuation_batch(
                        {
                            "resume_intent_id": intent["resume_intent_id"],
                            "predecessor_batch_id": latest_batch["batch_id"],
                            "run_id": run_id,
                            "requested_by": request.requested_by,
                            "state": EntityState.PENDING_EXECUTION,
                            "result": {
                                "schemaVersion": 2,
                                "gameDay": snapshot.run.game_day_key,
                                "candidateGameIds": [latest["game_id"]],
                                "executableGameIds": [latest["game_id"]],
                                "deferredGameIds": [],
                                "skippedCompletedGameIds": [],
                                "todoPlans": {latest["game_id"]: todo_plan},
                                "continuation": predecessor_lineage,
                            },
                        }
                    )
                elif target_batch is not None:
                    self.store.update_batch_run_membership(
                        target_batch["batch_id"],
                        run_id,
                        state="resume_pending",
                        resume_intent_id=intent["resume_intent_id"],
                        latest_run_attempt_id=decision.source_current_attempt_id,
                    )
            work_item = None
            if deferred_ids:
                work_item = self.store.create_work_item(
                    {
                        "kind": WorkItemKind.OBSERVATION,
                        "state": EntityState.REVIEW_REQUIRED,
                        "game_id": latest["game_id"],
                        "cadence": latest["cadence"],
                        "run_id": run_id,
                        "requested_by": request.requested_by,
                        "note": (
                            "same-GameRun reconciliation requires real observation/provider facts"
                        ),
                        "artifact_refs": [],
                        "allowed_capability_refs": self._allowed_capability_refs(
                            WorkItemKind.OBSERVATION
                        ),
                        "result": {
                            "resumeIntentId": intent["resume_intent_id"],
                            "decision": decision_document,
                            "providerStatus": provider_status,
                            "requirements": requirements,
                            "executionRequested": False,
                        },
                    }
                )
                intent = self.store.update_resume_intent(
                    intent["resume_intent_id"],
                    state="reconciliation_required",
                    work_item_id=work_item["work_item_id"],
                )

            todo_plan = {
                **todo_plan,
                "todoInstanceIds": list(latest["todo_instance_ids"]),
                "executableTodoInstanceIds": (
                    eligible_ids if execution_requested else []
                ),
                "completedTodoInstanceIds": list(decision.completed_skip),
                "deferredTodoInstanceIds": deferred_ids,
            }
            target_state = (
                EntityState.PENDING_EXECUTION
                if execution_requested
                else EntityState.HUMAN_REQUIRED
                if decision.deferred_human
                else EntityState.REVIEW_REQUIRED
            )
            run = self.store.update_game_run(
                run_id,
                state=target_state,
                message=(
                    "fenced same-run successor intent committed"
                    if execution_requested
                    else "same-run reconciliation deferred until required facts exist"
                ),
            )
            successor_ordinal = (
                decision.successor_attempt_intent.successor_attempt_ordinal
                if decision.successor_attempt_intent is not None
                else None
            )
            return self._receipt(
                command_id=intent["resume_intent_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/game-runs/{run_id}",
                message=(
                    "Same-run successor intent accepted."
                    if execution_requested
                    else "Same-run reconciliation recorded without starting Host."
                ),
                result={
                    "requestId": intent["resume_intent_id"],
                    "resumeIntentId": intent["resume_intent_id"],
                    "gameRun": _dump(GameRunRecord.model_validate(run)),
                    "todoPlan": todo_plan,
                    "decision": decision_document,
                    "executionRequested": execution_requested,
                    "eligibleTodoInstanceIds": eligible_ids,
                    "skippedTodoInstanceIds": skipped_ids,
                    "deferredTodoInstanceIds": deferred_ids,
                    "requirements": requirements,
                    "unavailableRequirements": unavailable_requirements,
                    "providerStatus": provider_status,
                    "reconcileWorkItemId": (
                        work_item["work_item_id"] if work_item is not None else None
                    ),
                    "successorAttemptOrdinal": successor_ordinal,
                    "sourceRunRevision": decision.source_run_revision,
                    "sourceCurrentAttemptId": decision.source_current_attempt_id,
                    "sourceCurrentAttemptRevision": (
                        decision.source_current_attempt_revision
                    ),
                    "runtimeBinding": projected["runtimeBinding"],
                    "targetBatchId": (
                        target_batch["batch_id"] if target_batch is not None else None
                    ),
                    "continuationBatchId": (
                        target_batch["batch_id"]
                        if target_batch is not None
                        and target_batch["predecessor_batch_id"] is not None
                        else None
                    ),
                    "batchLineage": (
                        target_batch["result"].get("batchLineage")
                        if target_batch is not None
                        else None
                    ),
                },
            )

        receipt, replayed = self._idempotent_with_replay(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )
        if action == "takeover" and not replayed:
            self.adapter_host.cancel_run(
                run_id=run_id, reason_code="human_takeover_requested"
            )
        if (
            action == "resume"
            and receipt.result.get("executionRequested") is True
            and not replayed
        ):
            resume_launch = {
                "intentId": receipt.result["resumeIntentId"],
                "targetBatchId": receipt.result.get("targetBatchId"),
                "eligibleTodoInstanceIds": list(
                    receipt.result["eligibleTodoInstanceIds"]
                ),
                "successorAttemptOrdinal": int(
                    receipt.result["successorAttemptOrdinal"]
                ),
            }
        if resume_launch is not None:
            try:
                target_batch_id = resume_launch.get("targetBatchId")

                def resumed(result: AdapterRunResult) -> None:
                    if target_batch_id is None:
                        return
                    self._finish_resumed_batch_attempt(
                        batch_id=str(target_batch_id),
                        run_id=run_id,
                        transport_timed_out=(
                            result.transport_outcome == "timeout"
                        ),
                    )

                plan, _ = self._start_game_run(
                    run_id,
                    completed_callback=resumed,
                    command_id=str(receipt.command_id),
                    todo_instance_ids=resume_launch["eligibleTodoInstanceIds"],
                    attempt_ordinal=resume_launch["successorAttemptOrdinal"],
                )
                current_intent = self.store.get_resume_intent(
                    resume_launch["intentId"]
                )
                if current_intent["state"] == "successor_pending":
                    self.store.update_resume_intent(
                        resume_launch["intentId"],
                        state="successor_dispatched",
                        successor_run_attempt_id=plan.run_attempt_id,
                    )
            except Exception as error:
                self.store.update_resume_intent(
                    resume_launch["intentId"], state="successor_failed"
                )
                self._complete_command(
                    str(receipt.command_id),
                    "failed",
                    f"same-run successor start failed: {type(error).__name__}: {error}",
                )
                if resume_launch.get("targetBatchId") is not None:
                    self._finish_resumed_batch_attempt(
                        batch_id=str(resume_launch["targetBatchId"]),
                        run_id=run_id,
                    )
        return receipt

    def create_repair_session(
        self,
        incident_id: str,
        request: RepairSessionCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        incident = next(
            (item for item in self.list_incidents(500) if item.incident_id == incident_id),
            None,
        )
        if incident is None:
            raise RecordNotFound(incident_id)

        def operation() -> dict[str, Any]:
            resource = self.store.create_resource(
                "repair-session",
                state="planned",
                document={
                    "incidentId": incident_id,
                    "gameId": incident.game_id,
                    "reason": request.reason,
                    "requestedBy": request.requested_by,
                    "productionMutationAllowed": False,
                },
            )
            return self._receipt(
                command_id=resource["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/repair-sessions/{resource['resource_id']}",
                message="隔离维修会话已建立；没有修改生产 Adapter。",
                result={"repairSession": self._public_resource(resource)},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def verify_repair_session(
        self,
        repair_session_id: str,
        request: RepairVerificationRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        session = self.store.get_resource("repair-session", repair_session_id)
        known_artifacts = {
            artifact.artifact_id for artifact in self.list_artifacts(500)
        }
        if not set(request.evidence_ids).issubset(known_artifacts):
            raise ManagerValidation("repair verification references unknown artifacts")

        def operation() -> dict[str, Any]:
            verification = self.store.create_resource(
                "repair-verification",
                state=request.verdict,
                document={
                    "repairSessionId": repair_session_id,
                    **_dump(request),
                },
            )
            updated = self.store.update_resource(
                "repair-session",
                repair_session_id,
                state=("verified" if request.verdict == "passed" else "needs-work"),
                document={
                    **session["document"],
                    "lastVerificationId": verification["resource_id"],
                    "lastVerdict": request.verdict,
                },
            )
            return self._receipt(
                command_id=verification["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/repair-sessions/{repair_session_id}",
                message="维修复验结果已写入 Manager 账本。",
                result={"repairSession": self._public_resource(updated)},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def review_evidence(
        self,
        request: EvidenceReviewRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        artifact = self.store.get_resource("artifact", request.artifact_id)

        def operation() -> dict[str, Any]:
            review = self.store.create_resource(
                "evidence-review",
                state=request.verdict,
                document=_dump(request),
            )
            updated = self.store.update_resource(
                "artifact",
                request.artifact_id,
                state=request.verdict,
                document={
                    **artifact["document"],
                    "verdict": request.verdict,
                    "reviewId": review["resource_id"],
                },
            )
            return self._receipt(
                command_id=review["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/artifacts/{request.artifact_id}",
                message="证据复核已记录；单个 artifact 不会直接把游戏标为完成。",
                result={"artifact": _dump(self._artifact_record(updated))},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def request_adapter_governance(
        self,
        version_id: str,
        action: str,
        request: AdapterGovernanceRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if action not in {"promotion", "rollback"}:
            raise ManagerValidation("unsupported Adapter governance action")
        adapter = next(
            (item for item in self.list_adapters() if item.adapter_id == request.adapter_id),
            None,
        )
        if adapter is None:
            raise RecordNotFound(request.adapter_id)
        if action == "rollback" and version_id != adapter.active_version:
            raise ManagerConflict("rollback version is not the active registered version")
        if action == "promotion":
            if request.target_stage != "promoted":
                raise ManagerValidation("promotion targetStage must be promoted")

        def operation() -> dict[str, Any]:
            if action == "promotion":
                active_now = self.store.active_execution_summary()
                if (
                    active_now["batches"]
                    or active_now["gameRuns"]
                    or self.adapter_host.active_execution()
                ):
                    raise ManagerConflict(
                        "Adapter promotion is refused while an execution is active"
                    )
                game = self.get_game(adapter.game_id)
                try:
                    result = self.adapter_host.promote_candidate(
                        adapter_id=adapter.adapter_id,
                        game=game,
                        version_id=version_id,
                        requested_by=request.requested_by,
                        reason=request.reason,
                        create_canary_resource=lambda document: self.store.create_resource(
                            "adapter-diagnostic-canary",
                            state="passed",
                            document=document,
                        ),
                        create_receipt_resource=lambda document: self.store.create_resource(
                            "adapter-promotion-receipt",
                            resource_id=str(document["resourceId"]),
                            state="passed",
                            document=document,
                        ),
                    )
                except AdapterPromotionRejected as error:
                    raise ManagerConflict(f"{error.code}: {error}") from error
                governance = self.store.create_resource(
                    "adapter-governance-request",
                    state="passed",
                    document={
                        **_dump(request),
                        "versionId": version_id,
                        "action": action,
                        "hotPromotion": False,
                        "promotionReceiptId": result["promotionReceipt"]["resourceId"],
                        "managerCanaryId": result["managerCanary"]["resourceId"],
                    },
                )
                self.store.set_metadata(
                    (
                        "adapter.promotion.canary."
                        + str(result["promotionReceipt"]["resourceId"])
                    ),
                    str(result["managerCanary"]["resourceId"]),
                )
                return self._receipt(
                    command_id=governance["resource_id"],
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    status_url=None,
                    message=(
                        "Adapter candidate passed Manager-owned tests and canary; "
                        "the fixed local module is now promoted."
                    ),
                    result={
                        "governanceRequest": self._public_resource(governance),
                        **result,
                    },
                )
            resource = self.store.create_resource(
                "adapter-governance-request",
                state="pending-verification",
                document={
                    **_dump(request),
                    "versionId": version_id,
                    "action": action,
                    "hotPromotion": False,
                },
            )
            return self._receipt(
                command_id=resource["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=None,
                message="Adapter 治理请求已记录；活动运行版本未被热替换。",
                result={"governanceRequest": self._public_resource(resource)},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def _record_adapter_diagnostic_canary(
        self,
        adapter_id: str,
        *,
        requested_by: str,
        note: str,
    ) -> dict[str, Any]:
        diagnostic = self.adapter_host.canary(
            adapter_id, self.list_games()
        )
        return self.store.create_resource(
            "adapter-diagnostic-canary",
            state=str(diagnostic["canaryStatus"]),
            document={
                **diagnostic,
                "requestedBy": requested_by,
                "note": note,
            },
        )

    def create_adapter_diagnostic_canary(
        self,
        adapter_id: str,
        request: AdapterDiagnosticCanaryRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        # Validate the opaque registered ID before entering the write operation.
        # Do not execute the canary here: the idempotent operation below must be
        # the only place that can start the fixed diagnostic Host process.
        self.adapter_host.validate_adapter_id(adapter_id, self.list_games())

        def operation() -> dict[str, Any]:
            canary = self._record_adapter_diagnostic_canary(
                adapter_id,
                requested_by=request.requested_by,
                note=request.note,
            )
            return self._receipt(
                command_id=canary["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=(
                    f"/api/v1/adapter-diagnostic-canaries/{canary['resource_id']}"
                ),
                message=(
                    "Manager-owned Adapter diagnostic canary completed without "
                    "starting an Adapter or game process."
                ),
                result={
                    "adapterDiagnosticCanary": self._public_resource(canary)
                },
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request={"adapterId": adapter_id, **_dump(request)},
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def list_adapter_diagnostic_canaries(self, limit: int) -> list[dict[str, Any]]:
        return [
            self._public_resource(item)
            for item in self.store.list_resources(
                "adapter-diagnostic-canary", limit
            )
        ]

    def get_adapter_diagnostic_canary(self, canary_id: str) -> dict[str, Any]:
        return self._public_resource(
            self.store.get_resource("adapter-diagnostic-canary", canary_id)
        )

    def create_diagnostic_bundle(
        self,
        request: DiagnosticBundleCreateRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if request.run_id:
            self.store.get_game_run(request.run_id)
        if request.batch_id:
            self.store.get_batch(request.batch_id)

        def operation() -> dict[str, Any]:
            artifact_id = str(uuid.uuid4())
            artifact_root = self.settings.data_dir / "artifacts"
            artifact_root.mkdir(parents=True, exist_ok=True)
            file_name = f"diagnostic-{artifact_id}.json"
            final_path = artifact_root / file_name
            temp_path = artifact_root / f".{artifact_id}.tmp"
            captured_at = utc_now()
            document = {
                "schemaVersion": 1,
                "capturedAt": captured_at.isoformat(),
                "scope": _dump(request),
                "snapshot": _dump(self.snapshot()),
                "diagnostics": self.diagnostics(),
                "events": [
                    event.model_dump(mode="json", by_alias=True)
                    for event in self.events(max(0, self.store.latest_event_sequence() - 100), 100).events
                ],
            }
            encoded = json.dumps(
                document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            temp_path.write_bytes(encoded)
            os.replace(temp_path, final_path)
            digest = hashlib.sha256(encoded).hexdigest()
            artifact = self.store.create_resource(
                "artifact",
                resource_id=artifact_id,
                state="captured",
                document={
                    "kind": "diagnostic-bundle",
                    "capturedAt": captured_at.isoformat(),
                    "source": "manager-diagnostics",
                    "raw": True,
                    "contentType": "application/json",
                    "runId": request.run_id,
                    "verdict": "not-applicable",
                    "hash": digest,
                    "sizeBytes": len(encoded),
                    "fileName": file_name,
                    "relativePath": file_name,
                },
            )
            bundle = self.store.create_resource(
                "diagnostic-bundle",
                state="ready",
                document={
                    **_dump(request),
                    "artifactId": artifact_id,
                    "hash": digest,
                },
            )
            return self._receipt(
                command_id=bundle["resource_id"],
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/diagnostic-bundles/{bundle['resource_id']}",
                message="诊断包已由 Manager 生成并登记为 opaque artifact。",
                result={
                    "diagnosticBundle": self._public_resource(bundle),
                    "artifact": _dump(self._artifact_record(artifact)),
                },
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def list_repair_sessions(self, limit: int) -> list[dict[str, Any]]:
        return [
            self._public_resource(item)
            for item in self.store.list_resources("repair-session", limit)
        ]

    def get_repair_session(self, repair_session_id: str) -> dict[str, Any]:
        return self._public_resource(
            self.store.get_resource("repair-session", repair_session_id)
        )

    def list_diagnostic_bundles(self, limit: int) -> list[dict[str, Any]]:
        return [
            self._public_resource(item)
            for item in self.store.list_resources("diagnostic-bundle", limit)
        ]

    def get_diagnostic_bundle(self, bundle_id: str) -> dict[str, Any]:
        return self._public_resource(
            self.store.get_resource("diagnostic-bundle", bundle_id)
        )

    @staticmethod
    def _notification_record(record: dict[str, Any]) -> NotificationDeliveryRecord:
        return NotificationDeliveryRecord(
            notification_id=record["notification_id"],
            batch_id=record["batch_id"],
            seal_version=record["seal_version"],
            channel=record["channel"],
            recipient_binding_id=record["recipient_binding_id"],
            message_id=record["message_id"],
            state=record["state"],
            dispatch_gate=record["dispatch_gate"],
            outcome=record["outcome"],
            subject=record["subject"],
            attachment_refs=record["attachment_refs"],
            attempt_count=record["attempt_count"],
            next_attempt_at=record["next_attempt_at"],
            last_error_class=record["last_error_class"],
            sent_at=record["sent_at"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def list_notifications(self, limit: int) -> list[NotificationDeliveryRecord]:
        return [
            self._notification_record(item)
            for item in self.store.list_notification_deliveries(limit)
        ]

    def get_notification(self, notification_id: str) -> NotificationDeliveryRecord:
        return self._notification_record(
            self.store.get_notification_delivery(notification_id)
        )

    def list_notification_attempts(
        self, notification_id: str
    ) -> list[NotificationAttemptRecord]:
        return [
            NotificationAttemptRecord.model_validate(item)
            for item in self.store.list_notification_attempts(notification_id)
        ]

    def notification_preview(
        self, notification_id: str
    ) -> NotificationPreviewResponse:
        delivery = self.store.get_notification_delivery(notification_id)
        _, decisions = self.notification_dispatcher.artifacts.resolve(delivery)
        return NotificationPreviewResponse(
            notification_id=notification_id,
            subject=delivery["subject"],
            text_body=delivery["text_body"],
            html_body=delivery["html_body"],
            attachment_decisions=[
                {
                    "artifactId": decision.artifact_id,
                    "accepted": decision.accepted,
                    "reason": decision.reason,
                }
                for decision in decisions
            ],
        )

    def notification_policy(self) -> NotificationPolicyRecord:
        policy = self.store.get_notification_policy()
        return NotificationPolicyRecord(
            **policy,
            secret_state=self.notification_secret_provider.state(
                str(policy["recipient_binding_id"])
            ),
        )

    def patch_notification_policy(
        self,
        request: NotificationPolicyPatchRequest,
        *,
        requested_by: str,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        patch = request.model_dump(mode="json", exclude_none=True)
        if not patch:
            raise ManagerValidation("notification policy patch is empty")

        def operation() -> dict[str, Any]:
            policy = self.store.update_notification_policy(
                patch, requested_by=requested_by
            )
            record = NotificationPolicyRecord(
                **policy,
                secret_state=self.notification_secret_provider.state(
                    str(policy["recipient_binding_id"])
                ),
            )
            return self._receipt(
                command_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url="/api/v1/notification-policy",
                message="通知策略已更新；收件地址与 SMTP 凭据仍只存在本机 DPAPI 密文。",
                result={"notificationPolicy": _dump(record)},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request={**_dump(request), "requestedBy": requested_by},
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def request_notification_send(
        self,
        notification_id: str,
        request: NotificationSendRequest | NotificationRetryRequest,
        *,
        retry: bool,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        current = self.store.get_notification_delivery(notification_id)
        policy = self.store.get_notification_policy()
        if not bool(policy["enabled"]):
            raise ManagerConflict(
                "notification policy is disabled; explicit send and retry are both blocked"
            )
        if retry and current["state"] not in {"failed", "draft"}:
            raise ManagerValidation("only draft or failed notifications can be retried")
        confirm_ambiguous = bool(
            getattr(request, "confirm_ambiguous", False)
        )

        def operation() -> dict[str, Any]:
            delivery = self.store.arm_notification_delivery(
                notification_id,
                requested_by=request.requested_by,
                reason=request.reason,
                secret_state=self.notification_secret_provider.state(
                    str(current["recipient_binding_id"])
                ),
                confirm_ambiguous=confirm_ambiguous,
            )
            self.notification_dispatcher.wake()
            return self._receipt(
                command_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=f"/api/v1/notifications/{notification_id}",
                message=(
                    "通知重试已进入 outbox。"
                    if retry
                    else "通知发送请求已进入 outbox。"
                ),
                result={"notification": _dump(self._notification_record(delivery))},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    @staticmethod
    def _public_resource(resource: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceId": resource["resource_id"],
            "resourceType": resource["resource_type"],
            "state": resource["state"],
            "document": resource["document"],
            "createdAt": resource["created_at"],
            "updatedAt": resource["updated_at"],
        }

    def lifecycle_request(
        self,
        action: str,
        request: ManagerLifecycleRequest,
        *,
        idempotency_key: str,
        request_id: str | None,
        path: str,
        expected_state_version: int | None,
    ) -> CommandReceipt:
        if action not in {"stop", "restart"}:
            raise ManagerValidation("unsupported lifecycle action")

        def operation() -> dict[str, Any]:
            active = self.store.active_execution_summary()
            adapter_active = self.adapter_host.active_execution()
            if active["batches"] or active["gameRuns"] or adapter_active is not None:
                raise ManagerConflict(
                    "Manager cannot stop or restart while an execution is active; "
                    "request a safe cancellation and wait for the executor acknowledgement"
                )
            lifecycle_id = str(uuid.uuid4())
            self.store.set_metadata(
                "manager.lifecycle_state",
                {"state": f"{action}-requested", "requestId": lifecycle_id},
            )
            self.store.append_event(
                f"manager.{action}-requested",
                "manager-lifecycle",
                lifecycle_id,
                {**_dump(request), "action": action},
            )
            return self._receipt(
                command_id=lifecycle_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                status_url=None,
                message=f"Manager {action} request accepted.",
                result={"action": action},
            )

        return self._idempotent(
            key=idempotency_key,
            path=path,
            request=_dump(request),
            operation=operation,
            expected_state_version=expected_state_version,
        )

    def invoke_lifecycle_callback(self, action: str) -> None:
        # Close Manager-owned long-lived streams before Uvicorn waits for open
        # connections.  This keeps safe stop/restart bounded even while WebGUI or
        # the tray has an active SSE subscription.
        self._shutdown_requested.set()
        if self.lifecycle_callback is not None:
            self.lifecycle_callback(action)

    def _validated_games(self, requested: list[str] | None) -> list[str]:
        games = self.list_games()
        by_id = {game.game_id: game for game in games}
        selected = requested if requested is not None else [
            game.game_id for game in games if game.enabled
        ]
        if not selected:
            raise ManagerValidation("batch has no enabled games")
        if len(selected) != len(set(selected)):
            raise ManagerValidation("gameIds contains duplicates")
        unknown = [game_id for game_id in selected if game_id not in by_id]
        if unknown:
            raise ManagerValidation(f"unknown GameId: {', '.join(unknown)}")
        disabled = [game_id for game_id in selected if not by_id[game_id].enabled]
        if disabled:
            raise ManagerValidation(f"disabled GameId: {', '.join(disabled)}")
        return list(selected)

    def _allowed_capability_refs(self, kind: WorkItemKind | str) -> list[str]:
        mapping: dict[str, list[str]] = {
            WorkItemKind.DIAGNOSE_GAME: [
                "system.snapshot.read@1.0",
                "game.daily.plan@1.0",
                "adapter.canary.request@1.0",
            ],
            WorkItemKind.OBSERVATION: [
                "system.snapshot.read@1.0",
                "observation.capture.request@1.0",
            ],
            WorkItemKind.INCIDENT_REVIEW: [
                "system.snapshot.read@1.0",
            ],
            WorkItemKind.EVIDENCE_REVIEW: [
                "system.snapshot.read@1.0",
            ],
            WorkItemKind.REPAIR_VALIDATION: [
                "system.snapshot.read@1.0",
                "adapter.canary.request@1.0",
            ],
            WorkItemKind.RUN_GAME: ["game.daily.plan@1.0"],
            WorkItemKind.RUN_BATCH: ["batch.daily.plan@1.0"],
            WorkItemKind.CANCEL_RUN: [],
        }
        candidates = list(mapping.get(str(kind), mapping.get(kind, [])))
        return [
            reference
            for reference in candidates
            if (
                reference.rsplit("@", 1)[0] in self.capabilities
                and self.capabilities[reference.rsplit("@", 1)[0]].enabled
            )
        ]

    def _validate_active_claim(
        self,
        *,
        claim_id: str,
        work_item_id: str | None,
        claimant: str,
        fencing_token: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        claim = self.store.get_work_item_claim_private(claim_id)
        if claim["state"] != "active":
            raise ManagerConflict("Agent claim is no longer active")
        expires_at = datetime.fromisoformat(str(claim["expires_at"]).replace("Z", "+00:00"))
        if expires_at <= utc_now():
            raise ManagerConflict("Agent claim has expired")
        if claim["claimant"] != claimant:
            raise ManagerConflict("Agent claim belongs to a different actor")
        if claim.get("fencing_token") != fencing_token:
            raise ManagerConflict("Agent claim fencing token is stale or invalid")
        if work_item_id is not None and claim["work_item_id"] != work_item_id:
            raise ManagerConflict("Agent claim does not belong to this work item")
        work_item = self.store.get_work_item(claim["work_item_id"])
        return claim, work_item

    def _validated_game(self, game_id: str, require_enabled: bool) -> GameSummary:
        game = self.get_game(game_id)
        self.adapter_host.validate_game_id(game_id)
        if require_enabled and not game.enabled:
            raise ManagerValidation(f"GameId is disabled: {game_id}")
        return game

    @staticmethod
    def _reject_unsafe_arguments(arguments: dict[str, Any]) -> None:
        forbidden = {
            "shell",
            "command",
            "script",
            "path",
            "executable",
            "click",
            "coordinate",
            "keys",
            "environment",
        }

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    lowered = str(key).replace("_", "").lower()
                    if any(term in lowered for term in forbidden):
                        raise ManagerValidation(
                            "capability arguments may not contain shell/path/click controls"
                        )
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(arguments)

    def _receipt(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        request_id: str | None,
        status_url: str | None,
        message: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        next_version = self.store.latest_event_sequence() + 1
        receipt = CommandReceipt(
            command_id=command_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            status_url=f"/api/v1/commands/{command_id}",
            accepted_state_version=next_version,
            state="accepted",
            message=message,
            result=result,
            submitted_at=utc_now(),
        )
        payload = _dump(receipt)
        event = self.store.append_event(
            "command.receipt", "command", command_id, payload
        )
        if event["sequence"] != next_version:
            raise RuntimeError("event sequence changed inside single-writer transaction")
        self.store.save_command_receipt(command_id, payload)
        return payload

    def _complete_command(self, command_id: str, state: str, message: str) -> None:
        try:
            receipt = self.store.get_command_receipt(command_id)
        except RecordNotFound:
            return
        receipt.update(
            {"state": state, "message": message, "completedAt": utc_now().isoformat()}
        )
        self.store.save_command_receipt(command_id, receipt)
        self.store.append_event("command.receipt", "command", command_id, receipt)

    def _idempotent(
        self,
        *,
        key: str,
        path: str,
        request: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
        expected_state_version: int | None = None,
    ) -> CommandReceipt:
        receipt, _ = self._idempotent_with_replay(
            key=key,
            path=path,
            request=request,
            expected_state_version=expected_state_version,
            operation=operation,
        )
        return receipt

    def _idempotent_with_replay(
        self,
        *,
        key: str,
        path: str,
        request: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
        expected_state_version: int | None = None,
    ) -> tuple[CommandReceipt, bool]:
        response, replayed = self.store.run_idempotent(
            key=key,
            method=(
                "PATCH"
                if path in {"/api/v1/config", "/api/v1/notification-policy"}
                else "POST"
            ),
            path=path,
            request=request,
            status_code=202,
            expected_state_version=expected_state_version,
            operation=operation,
        )
        response = dict(response)
        response["replayed"] = replayed
        return CommandReceipt.model_validate(response), replayed
