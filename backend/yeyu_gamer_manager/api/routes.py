from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from ..domain.models import (
    AdapterEventPage,
    AgentWorkItemCreateRequest,
    AgentWorkItemRecord,
    AutomationAssessmentRecord,
    AdapterDiagnosticCanaryRequest,
    AdapterGovernanceRequest,
    BatchCreateRequest,
    BatchRecord,
    BatchResumeRequest,
    BatchRunMembershipRecord,
    Cadence,
    CancelRequest,
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
    DiagnosticBundleCreateRequest,
    EventPage,
    EvidenceReviewRequest,
    GameRunCreateRequest,
    GameRunAliasRequest,
    GameRunRecord,
    GameIntegrationPage,
    GameSummary,
    HealthResponse,
    ManagerLifecycleRequest,
    MetaResponse,
    NotificationAttemptRecord,
    NotificationAttemptPage,
    NotificationDeliveryRecord,
    NotificationPolicyPatchRequest,
    NotificationPolicyRecord,
    NotificationPreviewResponse,
    NotificationRetryRequest,
    NotificationSendRequest,
    RepairSessionCreateRequest,
    RepairVerificationRequest,
    RunAttemptPage,
    RunAttemptRecord,
    RunControlRequest,
    SnapshotResponse,
    TodoDefinitionPage,
    TodoDefinitionRecord,
    TodoInstancePage,
    TodoInstanceRecord,
    TodoAttemptPage,
    TodoReconcileRequest,
    TodoResetPreviewResponse,
    TodoStatus,
    TodoTransitionRequest,
    WorkItemClaimRequest,
)
from ..services.manager import ManagerService
from ..store.sqlite_store import StateVersionRequired


api = APIRouter(prefix="/api/v1", tags=["Manager API v1"])
compat = APIRouter(tags=["Legacy read-only compatibility"])


def get_manager(request: Request) -> ManagerService:
    return request.app.state.manager


Manager = Annotated[ManagerService, Depends(get_manager)]


@dataclass(frozen=True, slots=True)
class MutationContext:
    idempotency_key: str
    request_id: str | None
    expected_state_version: int | None
    actor_id: str


@dataclass(frozen=True, slots=True)
class ArtifactReadContext:
    actor_id: str
    principal_id: str
    work_item_id: str | None = None
    claim_id: str | None = None
    fencing_token: str | None = None


def artifact_read_context(
    request: Request,
    work_item_id: Annotated[
        str | None,
        Header(alias="X-YeYu-Gamer-Work-Item-Id", min_length=1, max_length=120),
    ] = None,
    claim_id: Annotated[
        str | None, Header(alias="Claim-Id", min_length=1, max_length=120)
    ] = None,
    fencing_token: Annotated[
        str | None, Header(alias="Fencing-Token", min_length=16, max_length=160)
    ] = None,
) -> ArtifactReadContext:
    actor_id = str(getattr(request.state, "actor_id", "anonymous"))
    principal_id = str(getattr(request.state, "principal_id", actor_id))
    if actor_id in {"webgui", "cli"}:
        return ArtifactReadContext(actor_id=actor_id, principal_id=principal_id)
    if actor_id == "anonymous":
        raise HTTPException(
            status_code=401,
            detail="artifact reads require an authenticated, scoped local actor",
        )
    if actor_id != "agent":
        raise HTTPException(
            status_code=403,
            detail="this actor is not allowed to read Manager artifacts",
        )
    if not work_item_id or not claim_id or not fencing_token:
        raise HTTPException(
            status_code=403,
            detail=(
                "Agent artifact reads require work-item, claim, and fencing headers"
            ),
        )
    return ArtifactReadContext(
        actor_id=actor_id,
        principal_id=principal_id,
        work_item_id=work_item_id,
        claim_id=claim_id,
        fencing_token=fencing_token,
    )


ArtifactAccess = Annotated[ArtifactReadContext, Depends(artifact_read_context)]


@dataclass(frozen=True, slots=True)
class ReadContext:
    actor_id: str
    principal_id: str


def read_context(request: Request) -> ReadContext:
    actor_id = str(getattr(request.state, "actor_id", "anonymous"))
    return ReadContext(
        actor_id=actor_id,
        principal_id=str(getattr(request.state, "principal_id", actor_id)),
    )


ReadAccess = Annotated[ReadContext, Depends(read_context)]


def _parse_state_version(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    normalized = normalized.strip('"')
    if not normalized.isdigit():
        raise ValueError("If-Match state version must be a non-negative integer ETag")
    return int(normalized)


def mutation_context(
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    request_id: Annotated[
        str | None, Header(alias="X-Request-Id", min_length=1, max_length=128)
    ] = None,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    expected_header: Annotated[
        str | None, Header(alias="X-Expected-State-Version")
    ] = None,
) -> MutationContext:
    if any(ord(character) < 33 or ord(character) > 126 for character in idempotency_key):
        raise ValueError("Idempotency-Key must contain visible ASCII characters only")
    etag_version = _parse_state_version(if_match)
    explicit_version = _parse_state_version(expected_header)
    if (
        etag_version is not None
        and explicit_version is not None
        and etag_version != explicit_version
    ):
        raise ValueError("If-Match and X-Expected-State-Version disagree")
    if etag_version is None and explicit_version is None:
        raise StateVersionRequired(
            "Every Manager mutation requires If-Match or X-Expected-State-Version"
        )
    path = request.url.path
    scopes = set(getattr(request.state, "actor_scopes", frozenset()))
    if path.startswith("/api/v1/manager/"):
        allowed = "lifecycle:write" in scopes
    elif path == "/api/v1/agent/work-items" and request.method == "POST":
        allowed = bool(scopes & {"agent:write", "agent:dispatch", "control:write"})
    elif path.startswith(
        (
            "/api/v1/agent/work-items/",
            "/api/v1/claims",
            "/api/v1/capability-invocations",
        )
    ):
        allowed = bool(scopes & {"agent:write", "control:write"})
    else:
        allowed = "control:write" in scopes
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="actor scope does not allow this Manager mutation",
        )
    return MutationContext(
        idempotency_key=idempotency_key,
        request_id=request_id,
        expected_state_version=(
            explicit_version if explicit_version is not None else etag_version
        ),
        actor_id=str(getattr(request.state, "principal_id", request.state.actor_id)),
    )


Mutation = Annotated[MutationContext, Depends(mutation_context)]


def _bind_actor(model: Any, mutation: MutationContext, field: str = "requested_by"):
    if hasattr(model, "model_copy"):
        return model.model_copy(update={field: mutation.actor_id})
    return model


def _model_page(items: list[Any]) -> dict[str, Any]:
    values = [
        item.model_dump(mode="json", by_alias=True)
        if hasattr(item, "model_dump")
        else item
        for item in items
    ]
    return {"items": values, "total": len(values)}


def _log_page(
    manager: ManagerService,
    *,
    after: int,
    before: int | None,
    recent: bool,
    limit: int,
) -> dict[str, Any]:
    if recent and before is not None:
        raise HTTPException(
            status_code=400,
            detail="recent and before log cursors are mutually exclusive",
        )
    if after and (recent or before is not None):
        raise HTTPException(
            status_code=400,
            detail="after cannot be combined with recent or before log cursors",
        )
    items = manager.list_logs(
        after=after,
        before=before,
        recent=recent,
        limit=limit,
    )
    page = _model_page(items)
    backward = recent or before is not None
    if items:
        cursor = items[0].sequence if backward else items[-1].sequence
    else:
        cursor = before if before is not None else after
    page["nextCursor"] = str(cursor)
    page["cursorMode"] = (
        "recent" if recent else "before" if before is not None else "after"
    )
    return page


def _receipt_response(response: Response, receipt: CommandReceipt) -> CommandReceipt:
    response.status_code = 202
    response.headers["Idempotency-Replayed"] = "true" if receipt.replayed else "false"
    if receipt.request_id:
        response.headers["X-Request-Id"] = receipt.request_id
    return receipt


@api.get("/meta", response_model=MetaResponse)
def meta(manager: Manager) -> MetaResponse:
    return manager.meta()


@api.get("/health", response_model=HealthResponse)
def health(manager: Manager) -> HealthResponse:
    return manager.health()


@api.get("/snapshot", response_model=SnapshotResponse)
def snapshot(access: ReadAccess, manager: Manager) -> SnapshotResponse:
    if access.actor_id in {"webgui", "cli"}:
        return manager.snapshot()
    return manager.state_version_snapshot()


@api.get("/games")
def games(manager: Manager) -> dict[str, Any]:
    return _model_page(manager.list_games())


@api.get("/games/{game_id}")
def game(game_id: str, manager: Manager) -> dict[str, Any]:
    return manager.get_game_detail(game_id)


@api.get("/execution-control/controller-leases")
def controller_leases(
    access: ReadAccess,
    manager: Manager,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    active_only: Annotated[bool, Query(alias="activeOnly")] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    return _model_page(
        manager.list_controller_leases(
            run_id=run_id, active_only=active_only, limit=limit
        )
    )


@api.get("/execution-control/todo-blockers")
def todo_blockers(
    access: ReadAccess,
    manager: Manager,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    active_only: Annotated[bool, Query(alias="activeOnly")] = False,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    # No corresponding write route exists.  Adapter terminal handling and
    # explicit takeover release are the only blocker lifecycle writers.
    return _model_page(
        manager.list_todo_blockers(
            run_id=run_id, active_only=active_only, limit=limit
        )
    )


@api.get("/execution-control/facts")
def execution_control_facts(
    access: ReadAccess,
    manager: Manager,
    fact_type: Annotated[str | None, Query(alias="factType")] = None,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict[str, Any]:
    return _model_page(
        manager.list_execution_control_facts(
            fact_type=fact_type, run_id=run_id, limit=limit
        )
    )


@api.get("/todo-definitions", response_model=TodoDefinitionPage)
def todo_definitions(
    manager: Manager,
    game_id: Annotated[str | None, Query(alias="gameId")] = None,
    cadence: Cadence | None = None,
) -> TodoDefinitionPage:
    items = manager.list_todo_definitions(game_id=game_id, cadence=cadence)
    return TodoDefinitionPage(items=items, total=len(items))


@api.get("/integrations", response_model=GameIntegrationPage)
def integrations(manager: Manager) -> GameIntegrationPage:
    return manager.list_game_integrations()


@api.get(
    "/todo-definitions/{todo_definition_id}",
    response_model=TodoDefinitionRecord,
)
def todo_definition(
    todo_definition_id: str, manager: Manager
) -> TodoDefinitionRecord:
    return manager.get_todo_definition(todo_definition_id)


@api.get("/todo-instances", response_model=TodoInstancePage)
def todo_instances(
    manager: Manager,
    game_id: Annotated[str | None, Query(alias="gameId")] = None,
    cadence: Cadence | None = None,
    period_key: Annotated[str | None, Query(alias="periodKey")] = None,
    status: TodoStatus | None = None,
    current: bool = False,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> TodoInstancePage:
    items = manager.list_todo_instances(
        game_id=game_id,
        cadence=cadence,
        period_key=period_key,
        status=status,
        current=current,
        limit=limit,
    )
    return TodoInstancePage(items=items, total=len(items))


@api.get("/todo-instances/{todo_instance_id}", response_model=TodoInstanceRecord)
def todo_instance(todo_instance_id: str, manager: Manager) -> TodoInstanceRecord:
    return manager.get_todo_instance(todo_instance_id)


@api.get(
    "/todo-instances/{todo_instance_id}/attempts",
    response_model=TodoAttemptPage,
)
def todo_attempts(
    todo_instance_id: str,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TodoAttemptPage:
    items = manager.list_todo_attempts(
        todo_instance_id=todo_instance_id,
        limit=limit,
    )
    return TodoAttemptPage(items=items, total=len(items))


@api.get("/automation-assessments")
def automation_assessments(
    manager: Manager,
    todo_instance_id: Annotated[
        str | None, Query(alias="todoInstanceId")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _model_page(
        manager.list_automation_assessments(
            todo_instance_id=todo_instance_id, limit=limit
        )
    )


@api.get(
    "/automation-assessments/{assessment_id}",
    response_model=AutomationAssessmentRecord,
)
def automation_assessment(
    assessment_id: str, manager: Manager
) -> AutomationAssessmentRecord:
    return manager.get_automation_assessment(assessment_id)


@api.get("/completion-reviews")
def completion_reviews(
    access: ReadAccess,
    manager: Manager,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _model_page(
        manager.list_completion_reviews(
            run_id=run_id,
            principal_id=(access.principal_id if access.actor_id == "agent" else None),
            limit=limit,
        )
    )


@api.get(
    "/completion-reviews/{completion_review_id}",
    response_model=CompletionReviewRecord,
)
def completion_review(
    completion_review_id: str, access: ReadAccess, manager: Manager
) -> CompletionReviewRecord:
    return manager.get_completion_review(
        completion_review_id,
        principal_id=(access.principal_id if access.actor_id == "agent" else None),
    )


@api.get("/completion-adjudications")
def completion_adjudications(
    access: ReadAccess,
    manager: Manager,
    batch_id: Annotated[str | None, Query(alias="batchId")] = None,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _model_page(
        manager.list_completion_adjudications(
            batch_id=batch_id,
            run_id=run_id,
            principal_id=(access.principal_id if access.actor_id == "agent" else None),
            limit=limit,
        )
    )


@api.get(
    "/completion-adjudications/{completion_adjudication_id}",
    response_model=CompletionAdjudicationRecord,
)
def completion_adjudication(
    completion_adjudication_id: str, access: ReadAccess, manager: Manager
) -> CompletionAdjudicationRecord:
    return manager.get_completion_adjudication(
        completion_adjudication_id,
        principal_id=(access.principal_id if access.actor_id == "agent" else None),
    )


@api.get("/todo-reset-preview", response_model=TodoResetPreviewResponse)
def todo_reset_preview(
    manager: Manager,
    action: Annotated[str, Query(pattern="^(?:reconcile|reset)$")] = "reconcile",
    game_ids: Annotated[list[str] | None, Query(alias="gameId")] = None,
    cadence: Cadence | None = None,
) -> TodoResetPreviewResponse:
    return manager.preview_todo_reset(
        action=action, game_ids=game_ids, cadence=cadence
    )


def _reconcile_todos(
    body: TodoReconcileRequest,
    intent: str,
    mutation: MutationContext,
    manager: ManagerService,
    response: Response,
    path: str,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.reconcile_todos(
        body,
        intent=intent,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=path,
    )
    return _receipt_response(response, receipt)


@api.post(
    "/todo-reconcile-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def reconcile_todos(
    body: TodoReconcileRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    return _reconcile_todos(
        body,
        "reconcile",
        mutation,
        manager,
        response,
        "/api/v1/todo-reconcile-requests",
    )


@api.post(
    "/todo-reset-requests", response_model=CommandReceipt, status_code=202
)
def reset_todos(
    body: TodoReconcileRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    return _reconcile_todos(
        body,
        "reset",
        mutation,
        manager,
        response,
        "/api/v1/todo-reset-requests",
    )


@api.post(
    "/todo-instances/{todo_instance_id}/transitions",
    response_model=CommandReceipt,
    status_code=202,
)
def transition_todo(
    todo_instance_id: str,
    body: TodoTransitionRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.transition_todo(
        todo_instance_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/todo-instances/{todo_instance_id}/transitions",
    )
    return _receipt_response(response, receipt)


@api.get("/batches")
def batches(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_batches(limit))


@api.post("/batches", response_model=CommandReceipt, status_code=202)
def create_batch(
    body: BatchCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_batch(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/batches",
    )
    return _receipt_response(response, receipt)


@api.get("/batches/{batch_id}", response_model=BatchRecord)
def batch(batch_id: str, manager: Manager) -> BatchRecord:
    return manager.get_batch(batch_id)


@api.get(
    "/batches/{batch_id}/run-memberships",
    response_model=list[BatchRunMembershipRecord],
)
def batch_run_memberships(
    batch_id: str, manager: Manager
) -> list[BatchRunMembershipRecord]:
    return manager.list_batch_run_memberships(batch_id)


@api.post(
    "/batches/{batch_id}/cancel-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def cancel_batch(
    batch_id: str,
    body: CancelRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.cancel_batch(
        batch_id,
        body.model_dump(mode="json", by_alias=True),
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/batches/{batch_id}/cancel-requests",
    )
    return _receipt_response(response, receipt)


@api.post(
    "/batches/{batch_id}/resume-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def resume_batch(
    batch_id: str,
    body: BatchResumeRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.resume_batch(
        batch_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/batches/{batch_id}/resume-requests",
    )
    return _receipt_response(response, receipt)


@api.get("/game-runs")
def game_runs(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_game_runs(limit))


@api.post(
    "/games/{game_id}/run-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def create_game_run_alias(
    game_id: str,
    body: GameRunAliasRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    request = GameRunCreateRequest(
        game_id=game_id,
        cadence=body.kind,
        mode="execute",
        requested_by=body.requested_by,
    )
    receipt = manager.create_game_run(
        request,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/games/{game_id}/run-requests",
    )
    return _receipt_response(response, receipt)


@api.post("/game-runs", response_model=CommandReceipt, status_code=202)
def create_game_run(
    body: GameRunCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_game_run(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/game-runs",
    )
    return _receipt_response(response, receipt)


@api.get("/game-runs/{run_id}", response_model=GameRunRecord)
def game_run(run_id: str, manager: Manager) -> GameRunRecord:
    return manager.get_game_run(run_id)


@api.get("/game-runs/{run_id}/attempts", response_model=RunAttemptPage)
def game_run_attempts(
    run_id: str,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> RunAttemptPage:
    items = manager.list_run_attempts(run_id=run_id, limit=limit)
    return RunAttemptPage(items=items, total=len(items))


@api.get("/run-attempts/{run_attempt_id}", response_model=RunAttemptRecord)
def run_attempt(
    run_attempt_id: str,
    manager: Manager,
) -> RunAttemptRecord:
    return manager.get_run_attempt(run_attempt_id)


@api.get(
    "/run-attempts/{run_attempt_id}/events",
    response_model=AdapterEventPage,
)
def run_attempt_events(
    run_attempt_id: str,
    manager: Manager,
) -> AdapterEventPage:
    items = manager.list_adapter_events(run_attempt_id)
    return AdapterEventPage(items=items, total=len(items))


@api.post(
    "/game-runs/{run_id}/cancel-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def cancel_game_run(
    run_id: str,
    body: CancelRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.cancel_game_run(
        run_id,
        body.model_dump(mode="json", by_alias=True),
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/game-runs/{run_id}/cancel-requests",
    )
    return _receipt_response(response, receipt)


def _run_control(
    run_id: str,
    action: str,
    body: RunControlRequest,
    mutation: MutationContext,
    manager: ManagerService,
    response: Response,
    path: str,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_run_control_request(
        run_id,
        action,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=path,
    )
    return _receipt_response(response, receipt)


@api.post(
    "/game-runs/{run_id}/resume-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def resume_game_run(
    run_id: str, body: RunControlRequest, mutation: Mutation, manager: Manager, response: Response
) -> CommandReceipt:
    return _run_control(
        run_id,
        "resume",
        body,
        mutation,
        manager,
        response,
        f"/api/v1/game-runs/{run_id}/resume-requests",
    )


@api.post(
    "/game-runs/{run_id}/takeover-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def takeover_game_run(
    run_id: str, body: RunControlRequest, mutation: Mutation, manager: Manager, response: Response
) -> CommandReceipt:
    return _run_control(
        run_id,
        "takeover",
        body,
        mutation,
        manager,
        response,
        f"/api/v1/game-runs/{run_id}/takeover-requests",
    )


@api.post(
    "/game-runs/{run_id}/takeover-release-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def release_game_run_takeover(
    run_id: str, body: RunControlRequest, mutation: Mutation, manager: Manager, response: Response
) -> CommandReceipt:
    return _run_control(
        run_id,
        "release-takeover",
        body,
        mutation,
        manager,
        response,
        f"/api/v1/game-runs/{run_id}/takeover-release-requests",
    )


@api.get("/agent/work-items")
def work_items(
    access: ReadAccess,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    items = manager.list_work_items(
        limit,
        actor_id=access.actor_id,
        principal_id=access.principal_id,
    )
    if access.actor_id == "rabiroute":
        values = [manager.work_item_dispatch_projection(item) for item in items]
        return {"items": values, "total": len(values)}
    if access.actor_id == "agent":
        values = [
            (
                manager.work_item_dispatch_projection(item)
                if item.state == "planned"
                and item.requested_by != access.principal_id
                else item.model_dump(mode="json", by_alias=True)
            )
            for item in items
        ]
        return {"items": values, "total": len(values)}
    return _model_page(items)


@api.post("/agent/work-items", response_model=CommandReceipt, status_code=202)
def create_work_item(
    body: AgentWorkItemCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_work_item(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/agent/work-items",
    )
    if mutation.actor_id == "rabiroute":
        direct = receipt.model_copy(deep=True)
        item = AgentWorkItemRecord.model_validate(direct.result["workItem"])
        direct.result = {
            "workItem": manager.work_item_dispatch_projection(item)
        }
        receipt = direct
    return _receipt_response(response, receipt)


@api.get("/agent/work-items/{work_item_id}")
def work_item(
    work_item_id: str, access: ReadAccess, manager: Manager
) -> AgentWorkItemRecord | dict[str, Any]:
    item = manager.get_work_item(
        work_item_id,
        actor_id=access.actor_id,
        principal_id=access.principal_id,
    )
    if access.actor_id == "rabiroute":
        return manager.work_item_dispatch_projection(item)
    if (
        access.actor_id == "agent"
        and item.state == "planned"
        and item.requested_by != access.principal_id
    ):
        return manager.work_item_dispatch_projection(item)
    return item


@api.post(
    "/agent/work-items/{work_item_id}/claims",
    response_model=CommandReceipt,
    status_code=202,
)
def claim_work_item(
    work_item_id: str,
    body: WorkItemClaimRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation, field="claimant")
    receipt = manager.claim_work_item(
        work_item_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/agent/work-items/{work_item_id}/claims",
    )
    return _receipt_response(response, receipt)


@api.get("/claims")
def claims(
    access: ReadAccess,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    principal_id = access.principal_id if access.actor_id == "agent" else None
    return _model_page(manager.list_claims(limit, principal_id=principal_id))


@api.get("/claims/decisions")
def claim_decisions(
    access: ReadAccess,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    principal_id = access.principal_id if access.actor_id == "agent" else None
    return _model_page(
        manager.list_claim_decisions(limit, principal_id=principal_id)
    )


@api.post("/claims/decisions", response_model=CommandReceipt, status_code=202)
def create_claim_decision(
    body: ClaimDecisionCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_claim_decision(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/claims/decisions",
    )
    return _receipt_response(response, receipt)


@api.get("/claims/decisions/{decision_id}", response_model=ClaimDecisionRecord)
def claim_decision(
    decision_id: str, access: ReadAccess, manager: Manager
) -> ClaimDecisionRecord:
    principal_id = access.principal_id if access.actor_id == "agent" else None
    return manager.get_claim_decision(decision_id, principal_id=principal_id)


@api.get("/capabilities")
def capabilities(manager: Manager) -> dict[str, Any]:
    return _model_page(manager.list_capabilities())


@api.get("/capabilities/{capability_id:path}", response_model=CapabilityDefinition)
def capability(capability_id: str, manager: Manager) -> CapabilityDefinition:
    return manager.get_capability(capability_id)


@api.get("/capability-invocations")
def capability_invocations(
    access: ReadAccess,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    principal_id = access.principal_id if access.actor_id == "agent" else None
    return _model_page(
        manager.list_capability_invocations(limit, principal_id=principal_id)
    )


@api.post(
    "/capability-invocations", response_model=CommandReceipt, status_code=202
)
def invoke_capability(
    body: CapabilityInvocationCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.invoke_capability(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/capability-invocations",
    )
    return _receipt_response(response, receipt)


@api.get(
    "/capability-invocations/{invocation_id}",
    response_model=CapabilityInvocationRecord,
)
def capability_invocation(
    invocation_id: str, access: ReadAccess, manager: Manager
) -> CapabilityInvocationRecord:
    principal_id = access.principal_id if access.actor_id == "agent" else None
    return manager.get_capability_invocation(
        invocation_id, principal_id=principal_id
    )


@api.get("/commands/{command_id}", response_model=CommandReceipt)
def command(
    command_id: str, access: ReadAccess, manager: Manager
) -> CommandReceipt:
    return manager.get_command_receipt(
        command_id,
        actor_id=access.actor_id,
        principal_id=access.principal_id,
    )


@api.get("/config", response_model=ConfigResponse)
def config(manager: Manager) -> ConfigResponse:
    return manager.config()


@api.patch("/config", response_model=CommandReceipt, status_code=202)
def patch_config(
    body: ConfigPatchRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    receipt = manager.patch_config(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/config",
    )
    return _receipt_response(response, receipt)


@api.get("/policy")
def policy(manager: Manager) -> dict[str, Any]:
    return manager.policy()


@api.get("/incidents")
def incidents(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_incidents(limit))


@api.post(
    "/incidents/{incident_id}/repair-sessions",
    response_model=CommandReceipt,
    status_code=202,
)
def create_repair_session(
    incident_id: str,
    body: RepairSessionCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_repair_session(
        incident_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/incidents/{incident_id}/repair-sessions",
    )
    return _receipt_response(response, receipt)


@api.get("/repair-sessions")
def repair_sessions(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_repair_sessions(limit))


@api.get("/repair-sessions/{repair_session_id}")
def repair_session(repair_session_id: str, manager: Manager) -> dict[str, Any]:
    return manager.get_repair_session(repair_session_id)


@api.post(
    "/repair-sessions/{repair_session_id}/verification-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def verify_repair_session(
    repair_session_id: str,
    body: RepairVerificationRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.verify_repair_session(
        repair_session_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/repair-sessions/{repair_session_id}/verification-requests",
    )
    return _receipt_response(response, receipt)


@api.get("/artifacts")
def artifacts(
    access: ArtifactAccess,
    manager: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _model_page(
        manager.list_artifacts(
            limit,
            actor_id=access.actor_id,
            principal_id=access.principal_id,
            work_item_id=access.work_item_id,
            claim_id=access.claim_id,
            fencing_token=access.fencing_token,
        )
    )


@api.get("/artifacts/{artifact_id}")
def artifact(
    artifact_id: str, access: ArtifactAccess, manager: Manager
) -> dict[str, Any]:
    return manager.get_artifact(
        artifact_id,
        actor_id=access.actor_id,
        principal_id=access.principal_id,
        work_item_id=access.work_item_id,
        claim_id=access.claim_id,
        fencing_token=access.fencing_token,
    ).model_dump(mode="json", by_alias=True)


@api.get("/artifacts/{artifact_id}/content")
def artifact_content(
    artifact_id: str, access: ArtifactAccess, manager: Manager
) -> Response:
    content = manager.artifact_content(
        artifact_id,
        actor_id=access.actor_id,
        principal_id=access.principal_id,
        work_item_id=access.work_item_id,
        claim_id=access.claim_id,
        fencing_token=access.fencing_token,
    )
    disposition = "inline" if content.inline else "attachment"
    encoded_name = quote(content.file_name, safe="")
    return Response(
        content=content.content,
        media_type=content.content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@api.post("/evidence-reviews", response_model=CommandReceipt, status_code=202)
def review_evidence(
    body: EvidenceReviewRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.review_evidence(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/evidence-reviews",
    )
    return _receipt_response(response, receipt)


@api.get("/weekly")
def weekly(manager: Manager) -> dict[str, Any]:
    return _model_page(manager.list_weekly())


@api.get("/adapters")
def adapters(manager: Manager) -> dict[str, Any]:
    return _model_page(manager.list_adapters())


@api.post(
    "/adapters/{adapter_id}/diagnostic-canary-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def create_adapter_diagnostic_canary(
    adapter_id: str,
    body: AdapterDiagnosticCanaryRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    path = f"/api/v1/adapters/{adapter_id}/diagnostic-canary-requests"
    receipt = manager.create_adapter_diagnostic_canary(
        adapter_id,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=path,
    )
    return _receipt_response(response, receipt)


@api.get("/adapter-diagnostic-canaries")
def adapter_diagnostic_canaries(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_adapter_diagnostic_canaries(limit))


@api.get("/adapter-diagnostic-canaries/{canary_id}")
def adapter_diagnostic_canary(canary_id: str, manager: Manager) -> dict[str, Any]:
    return manager.get_adapter_diagnostic_canary(canary_id)


def _adapter_governance(
    version_id: str,
    action: str,
    body: AdapterGovernanceRequest,
    mutation: MutationContext,
    manager: ManagerService,
    response: Response,
    path: str,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.request_adapter_governance(
        version_id,
        action,
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=path,
    )
    return _receipt_response(response, receipt)


@api.post(
    "/adapter-versions/{version_id}/promotion-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def promote_adapter(
    version_id: str,
    body: AdapterGovernanceRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    return _adapter_governance(
        version_id,
        "promotion",
        body,
        mutation,
        manager,
        response,
        f"/api/v1/adapter-versions/{version_id}/promotion-requests",
    )


@api.post(
    "/adapter-versions/{version_id}/rollback-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def rollback_adapter(
    version_id: str,
    body: AdapterGovernanceRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    return _adapter_governance(
        version_id,
        "rollback",
        body,
        mutation,
        manager,
        response,
        f"/api/v1/adapter-versions/{version_id}/rollback-requests",
    )


@api.get("/logs")
def logs(
    manager: Manager,
    after: Annotated[int, Query(ge=0)] = 0,
    before: Annotated[int | None, Query(ge=1)] = None,
    recent: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _log_page(
        manager,
        after=after,
        before=before,
        recent=recent,
        limit=limit,
    )


@api.get("/diagnostics")
def diagnostics(manager: Manager) -> dict[str, Any]:
    return manager.diagnostics()


@api.post("/diagnostic-bundles", response_model=CommandReceipt, status_code=202)
def create_diagnostic_bundle(
    body: DiagnosticBundleCreateRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.create_diagnostic_bundle(
        body,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/diagnostic-bundles",
    )
    return _receipt_response(response, receipt)


@api.get("/diagnostic-bundles")
def diagnostic_bundles(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_diagnostic_bundles(limit))


@api.get("/diagnostic-bundles/{bundle_id}")
def diagnostic_bundle(bundle_id: str, manager: Manager) -> dict[str, Any]:
    return manager.get_diagnostic_bundle(bundle_id)


@api.get("/notifications")
def notifications(
    manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return _model_page(manager.list_notifications(limit))


@api.get(
    "/notifications/{notification_id}", response_model=NotificationDeliveryRecord
)
def notification(
    notification_id: str, manager: Manager
) -> NotificationDeliveryRecord:
    return manager.get_notification(notification_id)


@api.get(
    "/notifications/{notification_id}/attempts",
    response_model=NotificationAttemptPage,
)
def notification_attempts(
    notification_id: str, manager: Manager
) -> NotificationAttemptPage:
    items = manager.list_notification_attempts(notification_id)
    return NotificationAttemptPage(items=items, total=len(items))


@api.get(
    "/notifications/{notification_id}/preview",
    response_model=NotificationPreviewResponse,
)
def notification_preview(
    notification_id: str, manager: Manager
) -> NotificationPreviewResponse:
    return manager.notification_preview(notification_id)


@api.post(
    "/notifications/{notification_id}/send-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def request_notification_send(
    notification_id: str,
    body: NotificationSendRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.request_notification_send(
        notification_id,
        body,
        retry=False,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/notifications/{notification_id}/send-requests",
    )
    return _receipt_response(response, receipt)


@api.post(
    "/notifications/{notification_id}/retry-requests",
    response_model=CommandReceipt,
    status_code=202,
)
def request_notification_retry(
    notification_id: str,
    body: NotificationRetryRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    body = _bind_actor(body, mutation)
    receipt = manager.request_notification_send(
        notification_id,
        body,
        retry=True,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path=f"/api/v1/notifications/{notification_id}/retry-requests",
    )
    return _receipt_response(response, receipt)


@api.get("/notification-policy", response_model=NotificationPolicyRecord)
def notification_policy(manager: Manager) -> NotificationPolicyRecord:
    return manager.notification_policy()


@api.patch(
    "/notification-policy", response_model=CommandReceipt, status_code=202
)
def patch_notification_policy(
    body: NotificationPolicyPatchRequest,
    mutation: Mutation,
    manager: Manager,
    response: Response,
) -> CommandReceipt:
    receipt = manager.patch_notification_policy(
        body,
        requested_by=mutation.actor_id,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/notification-policy",
    )
    return _receipt_response(response, receipt)


@api.get("/events", response_model=EventPage)
def events(
    manager: Manager,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    return manager.events(after, limit)


@api.get("/events/stream")
def event_stream(
    manager: Manager,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass
    return StreamingResponse(
        manager.event_stream(cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@api.post(
    "/manager/stop-requests", response_model=CommandReceipt, status_code=202
)
def stop_manager(
    mutation: Mutation,
    manager: Manager,
    response: Response,
    background_tasks: BackgroundTasks,
    body: ManagerLifecycleRequest | None = None,
) -> CommandReceipt:
    request = _bind_actor(body or ManagerLifecycleRequest(), mutation)
    receipt = manager.lifecycle_request(
        "stop",
        request,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/manager/stop-requests",
    )
    if not receipt.replayed:
        background_tasks.add_task(manager.invoke_lifecycle_callback, "stop")
    return _receipt_response(response, receipt)


@api.post(
    "/manager/restart-requests", response_model=CommandReceipt, status_code=202
)
def restart_manager(
    mutation: Mutation,
    manager: Manager,
    response: Response,
    background_tasks: BackgroundTasks,
    body: ManagerLifecycleRequest | None = None,
) -> CommandReceipt:
    request = _bind_actor(body or ManagerLifecycleRequest(), mutation)
    receipt = manager.lifecycle_request(
        "restart",
        request,
        idempotency_key=mutation.idempotency_key,
        request_id=mutation.request_id,
        expected_state_version=mutation.expected_state_version,
        path="/api/v1/manager/restart-requests",
    )
    if not receipt.replayed:
        background_tasks.add_task(manager.invoke_lifecycle_callback, "restart")
    return _receipt_response(response, receipt)


# GET-only compatibility surface. These handlers call the same pure projections;
# no compatibility endpoint can mutate Manager or legacy state.
@compat.get("/health", response_model=HealthResponse)
def compat_health(manager: Manager) -> HealthResponse:
    return manager.health()


@compat.get("/status", response_model=SnapshotResponse)
def compat_status(manager: Manager) -> SnapshotResponse:
    return manager.snapshot()


@compat.get("/logs")
def compat_logs(
    manager: Manager,
    after: Annotated[int, Query(ge=0)] = 0,
    before: Annotated[int | None, Query(ge=1)] = None,
    recent: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return _log_page(
        manager,
        after=after,
        before=before,
        recent=recent,
        limit=limit,
    )


@compat.get("/events", response_model=EventPage)
def compat_events(
    manager: Manager,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    return manager.events(after, limit)
