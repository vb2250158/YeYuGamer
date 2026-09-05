from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .api.routes import api, compat
from .logging_setup import get_logger
from .services.legacy_adapter import LegacyAdapter
from .services.legacy_import import LegacyImporter
from .services.manager import ManagerService
from .services.manager_errors import (
    ExecutionUnavailable,
    ManagerConflict,
    ManagerValidation,
)
from .services.notifications import (
    DpapiNotificationSecretProvider,
    SmtpNotificationTransport,
)
from .services.notifications.secrets import NotificationSecretProvider
from .services.notifications.transport import NotificationTransport
from .security import (
    WEBGUI_SCOPES,
    WebGuiSessionBroker,
    bearer_token,
    provision_actor_credentials,
)
from .settings import Settings
from .store.sqlite_store import (
    IdempotencyConflict,
    PublicFencingMaterialRejected,
    RecordNotFound,
    SqliteStore,
    StateVersionConflict,
    StateVersionRequired,
)

_app_log = get_logger("app")


class SpaStaticFiles(StaticFiles):
    """Serve Vite assets while falling back to index.html for Vue history routes."""

    async def get_response(self, path: str, scope: dict[str, Any]):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            leaf = path.rsplit("/", 1)[-1]
            if error.status_code != 404 or "." in leaf or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


class WebGuiSessionExchangeRequest(BaseModel):
    nonce: str = Field(min_length=40, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class WebGuiBootstrapNonceResponse(BaseModel):
    nonce: str
    expiresInSeconds: int


class WebGuiSessionResponse(BaseModel):
    authenticated: bool
    actor: str


class WebGuiSessionRevocationResponse(BaseModel):
    revoked: bool


MAX_MUTATION_BODY_BYTES = 1024 * 1024
_BODY_LIMITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class MutationBodyLimitMiddleware:
    """Buffer and cap local mutation bodies, including chunked requests."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_LIMITED_METHODS:
            await self.app(scope, receive, send)
            return

        declared_length: int | None = None
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(value.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                declared_length = None
            break
        if declared_length is not None and declared_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        position = 0

        async def replay() -> Message:
            nonlocal position
            if position < len(buffered):
                message = buffered[position]
                position += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = _problem(
            413,
            "Request body too large",
            "Mutation JSON bodies are limited to 1 MiB.",
            "request_body_too_large",
        )
        await response(scope, receive, send)


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _persistent_read_allowed(actor_id: str, path: str) -> bool:
    """Return the least-privilege GET surface for a local actor role."""

    if actor_id in {"webgui", "cli"}:
        return True
    if path in {"/health", "/api/v1/health"}:
        return True
    if actor_id == "tray-lifecycle":
        # The typed lifecycle client reads a state-version-only projection from
        # snapshot before submitting its CAS-protected stop/restart request.
        return path == "/api/v1/snapshot"
    if actor_id == "tray":
        return False
    if actor_id == "agent":
        return (
            path in {"/api/v1/meta", "/api/v1/snapshot"}
            or path == "/api/v1/agent/work-items"
            or path.startswith("/api/v1/agent/work-items/")
            or path == "/api/v1/claims"
            or path.startswith("/api/v1/claims/decisions")
            or path == "/api/v1/capabilities"
            or path.startswith("/api/v1/capabilities/")
            or path == "/api/v1/capability-invocations"
            or path.startswith("/api/v1/capability-invocations/")
            or path == "/api/v1/artifacts"
            or path.startswith("/api/v1/artifacts/")
            or path == "/api/v1/completion-reviews"
            or path.startswith("/api/v1/completion-reviews/")
            or path == "/api/v1/completion-adjudications"
            or path.startswith("/api/v1/completion-adjudications/")
            or path.startswith("/api/v1/commands/")
        )
    if actor_id == "rabiroute":
        return (
            path in {"/api/v1/meta", "/api/v1/snapshot"}
            or path == "/api/v1/agent/work-items"
            or (
                path.startswith("/api/v1/agent/work-items/")
                and not path.endswith("/claims")
            )
            or path.startswith("/api/v1/commands/")
        )
    return False


def _problem(
    status: int,
    title: str,
    detail: str,
    code: str,
    request_id: str | None = None,
    errors: Any = None,
) -> JSONResponse:
    # Error correlation IDs are always server-generated.  A client-controlled
    # X-Request-Id can itself contain a copied private credential, including on
    # failures raised before the persistence boundary is reached.
    server_request_id = str(uuid.uuid4())
    body: dict[str, Any] = {
        "type": f"urn:yeyu-gamer:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "requestId": server_request_id,
    }
    if errors is not None:
        body["errors"] = errors
    response = JSONResponse(
        body, status_code=status, media_type="application/problem+json"
    )
    response.headers["X-Request-Id"] = server_request_id
    return response


def _request_problem(
    request: Request,
    status: int,
    title: str,
    detail: str,
    code: str,
    request_id: str | None = None,
    errors: Any = None,
) -> JSONResponse:
    """Build a problem response without reflecting a historical claim grant."""

    store = getattr(request.app.state, "store", None)
    if store is not None:
        try:
            store.assert_public_fencing_material_absent_from_values(detail, errors)
        except PublicFencingMaterialRejected:
            detail = "A public request value contains private claim material."
            errors = None
    return _problem(status, title, detail, code, request_id, errors)


def create_app(
    settings: Settings | None = None,
    lifecycle_callback: Callable[[str], None] | None = None,
    notification_secret_provider: NotificationSecretProvider | None = None,
    notification_transport: NotificationTransport | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _app_log.info(
            "lifespan.startup database=%s dataDir=%s webDist=%s legacyExecution=%s",
            resolved.database_path,
            resolved.data_dir,
            resolved.web_dist,
            resolved.legacy_execution_enabled,
        )
        store = SqliteStore(resolved.database_path)
        store.initialize()
        legacy_report = LegacyImporter(resolved.legacy_root, store).run()
        allowed_game_ids = [game["game_id"] for game in store.list_games()]
        adapter = LegacyAdapter(
            legacy_root=resolved.legacy_root,
            runtime_dir=resolved.data_dir,
            allowed_game_ids=allowed_game_ids,
            execution_enabled=resolved.legacy_execution_enabled,
        )
        manager = ManagerService(
            settings=resolved,
            store=store,
            legacy_report=legacy_report,
            adapter=adapter,
            lifecycle_callback=lifecycle_callback,
            notification_secret_provider=(
                notification_secret_provider
                or DpapiNotificationSecretProvider(resolved.notification_secrets_dir)
            ),
            notification_transport=(
                notification_transport or SmtpNotificationTransport()
            ),
        )
        app.state.settings = resolved
        app.state.store = store
        app.state.manager = manager
        openapi_bytes = json.dumps(
            app.openapi(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        app.state.openapi_sha256 = hashlib.sha256(openapi_bytes).hexdigest()
        web_index = resolved.web_dist / "index.html"
        app.state.web_asset_sha256 = (
            hashlib.sha256(web_index.read_bytes()).hexdigest()
            if web_index.is_file()
            else ""
        )
        store.set_metadata("manager.openapi_sha256", app.state.openapi_sha256)
        store.set_metadata("manager.web_asset_sha256", app.state.web_asset_sha256)
        app.state.actor_credentials = provision_actor_credentials(
            resolved.actor_tokens_dir,
            tray_bootstrap_secret=resolved.tray_bootstrap_secret,
        )
        app.state.web_sessions = WebGuiSessionBroker()
        manager.start_notification_worker()
        manager.start_todo_reset_scheduler()
        manager.start_completion_review_watchdog()
        manager.start_ledger_maintenance()
        _app_log.info("lifespan.ready managerId=%s", manager.manager_id)
        try:
            yield
        finally:
            _app_log.info("lifespan.shutdown managerId=%s", manager.manager_id)
            try:
                manager.stop_ledger_maintenance()
                manager.stop_completion_review_watchdog()
                manager.stop_todo_reset_scheduler()
                manager.stop_notification_worker()
                manager.close_attempt_logs()
                store.append_event(
                    "manager.stopped",
                    "manager",
                    manager.manager_id,
                    {"version": manager.VERSION},
                )
            finally:
                store.close()

    app = FastAPI(
        title="YeYu Gamer Manager",
        version=__version__,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )
    app.add_middleware(
        MutationBodyLimitMiddleware,
        max_body_bytes=MAX_MUTATION_BODY_BYTES,
    )

    @app.middleware("http")
    async def local_origin_and_request_id(request: Request, call_next):
        client_request_id = request.headers.get("x-request-id")
        try:
            request.app.state.store.assert_public_fencing_material_absent_from_values(
                request.url.path,
                tuple(request.query_params.multi_items()),
                client_request_id,
            )
        except PublicFencingMaterialRejected as error:
            return _request_problem(
                request,
                422,
                "Invalid request",
                str(error),
                "invalid_request",
            )
        request_id = client_request_id or str(uuid.uuid4())
        request_path = request.url.path
        request.state.actor_id = "anonymous"
        request.state.principal_id = "anonymous"
        request.state.actor_scopes = frozenset()
        request.state.web_session_token = None
        token = bearer_token(request.headers.get("authorization"))
        credential = request.app.state.actor_credentials.get(token or "")
        declared_actor = request.headers.get("x-yeyu-gamer-actor")
        if token:
            if credential is None:
                return _request_problem(
                    request,
                    401,
                    "Actor credential rejected",
                    "The presented Bearer credential is not registered.",
                    "actor_credential_rejected",
                    request_id,
                )
            if declared_actor != credential.actor_id:
                return _request_problem(
                    request,
                    403,
                    "Actor identity mismatch",
                    "The declared actor does not match the presented credential.",
                    "actor_identity_mismatch",
                    request_id,
                )
            request.state.actor_id = credential.actor_id
            request.state.principal_id = credential.principal_id
            request.state.actor_scopes = credential.scopes
        else:
            session_token = request.cookies.get("yeyu_session")
            web_session = request.app.state.web_sessions.session(
                session_token,
                touch=request.method in {"GET", "HEAD", "OPTIONS"},
            )
            if web_session is not None:
                request.state.actor_id = "webgui"
                request.state.principal_id = "webgui"
                request.state.actor_scopes = WEBGUI_SCOPES
                request.state.web_session_token = session_token

        public_api_reads = {
            "/health",
            "/api/v1/health",
            "/api/v1/meta",
            "/api/v1/openapi.json",
        }
        if (
            request.method in {"GET", "HEAD"}
            and (
                request_path.startswith("/api/v1/")
                or request_path in {"/status", "/logs", "/events", "/health"}
            )
            and request_path not in public_api_reads
            and request.state.actor_id == "anonymous"
        ):
            return _request_problem(
                request,
                401,
                "WebGUI session required",
                "Open YeYu Gamer from its tray or present a scoped Bearer credential.",
                "webgui_session_required",
                request_id,
            )
        if (
            request.method in {"GET", "HEAD"}
            and (
                request_path.startswith("/api/v1/")
                or request_path in {"/status", "/logs", "/events", "/health"}
            )
            and request_path not in public_api_reads
            and request.state.actor_id != "anonymous"
            and not _persistent_read_allowed(request.state.actor_id, request_path)
        ):
            return _request_problem(
                request,
                403,
                "Actor read scope rejected",
                "This local actor is not allowed to read that Manager projection.",
                "actor_read_scope_rejected",
                request_id,
            )

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            is_webgui_session_bootstrap = request_path in {
                "/api/v1/webgui/session-exchanges",
                "/api/v1/webgui/local-sessions",
            }
            if is_webgui_session_bootstrap:
                expected_origin = _origin_tuple(str(request.base_url).rstrip("/"))
                if token or _origin_tuple(origin or "") != expected_origin:
                    return _request_problem(
                        request,
                        403,
                        "WebGUI bootstrap context rejected",
                        "Session exchange requires the exact Manager origin and no Bearer identity.",
                        "webgui_bootstrap_context_rejected",
                        request_id,
                    )
                if request.headers.get("sec-fetch-site") != "same-origin":
                    return _request_problem(
                        request,
                        403,
                        "WebGUI bootstrap context rejected",
                        "Session exchange requires Sec-Fetch-Site: same-origin.",
                        "webgui_bootstrap_context_rejected",
                        request_id,
                    )
            elif origin:
                expected_origin = _origin_tuple(str(request.base_url).rstrip("/"))
                if _origin_tuple(origin) != expected_origin:
                    return _request_problem(
                        request,
                        403,
                        "Cross-origin write rejected",
                        "Browser mutations must use the exact Manager origin, including scheme and port.",
                        "cross_origin_write",
                        request_id,
                    )
                if request.headers.get("sec-fetch-site") not in {"same-origin", "none"}:
                    return _request_problem(
                        request,
                        403,
                        "Browser request context rejected",
                        "Browser mutations require Sec-Fetch-Site: same-origin.",
                        "cross_site_browser_write",
                        request_id,
                    )
                session_cookie = request.cookies.get("yeyu_session")
                csrf_cookie = request.cookies.get("yeyu_csrf")
                csrf_header = request.headers.get("x-csrf-token")
                web_session = request.app.state.web_sessions.session(
                    session_cookie, touch=False
                )
                if not (
                    web_session
                    and csrf_cookie
                    and csrf_header
                    and secrets.compare_digest(
                        csrf_cookie, web_session.csrf_token
                    )
                    and secrets.compare_digest(csrf_header, csrf_cookie)
                ):
                    return _request_problem(
                        request,
                        403,
                        "CSRF validation failed",
                        "Refresh YeYu Gamer WebGUI and retry the command.",
                        "csrf_validation_failed",
                        request_id,
                    )
                # Touch only after the request has passed CSRF validation. The
                # second lookup also prevents an already expired session from
                # being revived by a stale CSRF cookie/header pair.
                web_session = request.app.state.web_sessions.session(
                    session_cookie, touch=True
                )
                if web_session is None:
                    return _request_problem(
                        request,
                        403,
                        "WebGUI session expired",
                        "Open YeYu Gamer from its tray and retry the command.",
                        "webgui_session_expired",
                        request_id,
                    )
                request.state.actor_id = "webgui"
                request.state.principal_id = "webgui"
                request.state.actor_scopes = WEBGUI_SCOPES
                request.state.web_session_token = session_cookie
            else:
                if credential is None:
                    return _request_problem(
                        request,
                        401,
                        "Actor credential required",
                        "Non-browser Manager mutations require a scoped Bearer credential.",
                        "actor_credential_required",
                        request_id,
                    )
                request.state.actor_id = credential.actor_id
                request.state.principal_id = credential.principal_id
                request.state.actor_scopes = credential.scopes
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "X-YeYu-Gamer-OpenAPI-SHA256", request.app.state.openapi_sha256
        )
        response.headers.setdefault(
            "X-YeYu-Gamer-Web-Asset-SHA256", request.app.state.web_asset_sha256
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'",
        )
        return response

    @app.post(
        "/api/v1/webgui/bootstrap-nonces",
        response_model=WebGuiBootstrapNonceResponse,
        status_code=201,
        include_in_schema=True,
    )
    async def issue_webgui_bootstrap_nonce(
        request: Request,
    ) -> WebGuiBootstrapNonceResponse | JSONResponse:
        if "webgui:bootstrap" not in request.state.actor_scopes:
            return _request_problem(
                request,
                403,
                "WebGUI bootstrap forbidden",
                "Only the scoped YeYu Gamer tray may issue a WebGUI bootstrap nonce.",
                "webgui_bootstrap_forbidden",
                request.headers.get("x-request-id"),
            )
        nonce, ttl = request.app.state.web_sessions.issue_nonce()
        return WebGuiBootstrapNonceResponse(
            nonce=nonce,
            expiresInSeconds=ttl,
        )

    @app.post(
        "/api/v1/webgui/session-exchanges",
        response_model=WebGuiSessionResponse,
        include_in_schema=True,
    )
    async def exchange_webgui_session(
        body: WebGuiSessionExchangeRequest,
        request: Request,
    ) -> JSONResponse:
        session = request.app.state.web_sessions.exchange_nonce(body.nonce)
        if session is None:
            return _request_problem(
                request,
                401,
                "WebGUI bootstrap rejected",
                "The bootstrap nonce is invalid, expired, or has already been used.",
                "webgui_bootstrap_rejected",
                request.headers.get("x-request-id"),
            )
        response = JSONResponse(
            WebGuiSessionResponse(authenticated=True, actor="webgui").model_dump()
        )
        response.set_cookie(
            "yeyu_session",
            session.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        response.set_cookie(
            "yeyu_csrf",
            session.csrf_token,
            httponly=False,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.post(
        "/api/v1/webgui/local-sessions",
        response_model=WebGuiSessionResponse,
        include_in_schema=True,
    )
    async def establish_local_webgui_session(request: Request) -> JSONResponse:
        """Recover the same-origin local WebGUI after a Manager restart."""

        session = request.app.state.web_sessions.create_local_session()
        response = JSONResponse(
            WebGuiSessionResponse(authenticated=True, actor="webgui").model_dump()
        )
        response.set_cookie(
            "yeyu_session",
            session.session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        response.set_cookie(
            "yeyu_csrf",
            session.csrf_token,
            httponly=False,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.post(
        "/api/v1/webgui/session-revocations",
        response_model=WebGuiSessionRevocationResponse,
        include_in_schema=True,
    )
    async def revoke_webgui_session(request: Request) -> JSONResponse:
        if request.state.actor_id != "webgui":
            return _request_problem(
                request,
                403,
                "WebGUI session revocation forbidden",
                "Only the active WebGUI session may revoke itself.",
                "webgui_session_revocation_forbidden",
                request.headers.get("x-request-id"),
            )
        revoked = request.app.state.web_sessions.revoke_session(
            request.state.web_session_token
        )
        response = JSONResponse(
            WebGuiSessionRevocationResponse(revoked=revoked).model_dump()
        )
        response.delete_cookie("yeyu_session", path="/")
        response.delete_cookie("yeyu_csrf", path="/")
        return response

    @app.exception_handler(RecordNotFound)
    async def not_found(request: Request, error: RecordNotFound):
        return _request_problem(
            request,
            404,
            "Resource not found",
            str(error.args[0] if error.args else error),
            "not_found",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(request: Request, error: IdempotencyConflict):
        return _request_problem(
            request,
            409,
            "Idempotency conflict",
            str(error),
            "idempotency_conflict",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(StateVersionConflict)
    async def state_version_conflict(request: Request, error: StateVersionConflict):
        _app_log.info(
            "request.state_version_conflict method=%s path=%s expected=%s current=%s",
            request.method,
            request.url.path,
            error.expected,
            error.current,
        )
        return _request_problem(
            request,
            412,
            "State version precondition failed",
            str(error),
            "state_version_conflict",
            request.headers.get("x-request-id"),
            {"expectedStateVersion": error.expected, "currentStateVersion": error.current},
        )

    @app.exception_handler(StateVersionRequired)
    async def state_version_required(request: Request, error: StateVersionRequired):
        return _request_problem(
            request,
            428,
            "State version precondition required",
            str(error),
            "state_version_required",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(ManagerConflict)
    async def manager_conflict(request: Request, error: ManagerConflict):
        return _request_problem(
            request,
            409,
            "Manager conflict",
            str(error),
            "manager_conflict",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(ExecutionUnavailable)
    async def execution_unavailable(request: Request, error: ExecutionUnavailable):
        return _request_problem(
            request,
            409,
            "Execution unavailable",
            str(error),
            "execution_unavailable",
            request.headers.get("x-request-id"),
            error.details,
        )

    @app.exception_handler(ManagerValidation)
    async def manager_validation(request: Request, error: ManagerValidation):
        return _request_problem(
            request,
            422,
            "Invalid Manager request",
            str(error),
            "manager_validation",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(PublicFencingMaterialRejected)
    async def public_fencing_material_rejected(
        request: Request, error: PublicFencingMaterialRejected
    ):
        return _request_problem(
            request,
            422,
            "Invalid request",
            str(error),
            "invalid_request",
            None,
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError):
        return _request_problem(
            request,
            422,
            "Invalid request",
            str(error),
            "invalid_request",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, error: sqlite3.Error):
        """Return an actionable local failure instead of leaking a SQLite traceback."""

        _app_log.error(
            "request.storage_error method=%s path=%s error=%s: %s",
            request.method,
            request.url.path,
            type(error).__name__,
            error,
        )
        return _request_problem(
            request,
            503,
            "Manager storage unavailable",
            "Manager could not complete this request because its local SQLite state store is temporarily unavailable. No request receipt was returned, so the page cannot confirm whether a batch was created.",
            "manager_storage_unavailable",
            request.headers.get("x-request-id"),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, error: RequestValidationError):
        return _request_problem(
            request,
            422,
            "Request validation failed",
            "The request does not match the typed Manager contract.",
            "request_validation",
            request.headers.get("x-request-id"),
            error.errors(),
        )

    app.include_router(api)
    app.include_router(compat)

    index = resolved.web_dist / "index.html"
    if index.is_file():
        app.mount(
            "/",
            SpaStaticFiles(directory=resolved.web_dist, html=True, check_dir=True),
            name="webgui",
        )
    else:

        @app.get("/", include_in_schema=False)
        def webgui_not_built() -> dict[str, Any]:
            return {
                "name": "YeYu Gamer Manager",
                "version": __version__,
                "webGuiAvailable": False,
                "api": "/api/v1/meta",
            }

    return app


app = create_app()
