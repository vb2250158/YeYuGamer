from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class ActorCredential:
    actor_id: str
    scopes: frozenset[str]
    principal_id: str


PERSISTENT_ACTOR_SCOPES: dict[str, frozenset[str]] = {
    "cli": frozenset(
        {"control:write", "agent:write", "lifecycle:write", "read:all"}
    ),
    "tray-lifecycle": frozenset({"lifecycle:write", "lifecycle:read"}),
    # Retained for older read-only clients. Mutating Agent work requires a
    # credential-bound agent.<worker>.token principal.
    "agent": frozenset({"agent:read"}),
    "rabiroute": frozenset({"agent:dispatch", "agent:dispatch:read"}),
}
AGENT_WORKER_SCOPES = frozenset({"agent:write", "agent:read"})

EPHEMERAL_TRAY_ACTOR_ID = "tray"
EPHEMERAL_TRAY_SCOPES = frozenset({"webgui:bootstrap"})
LEGACY_TRAY_TOKEN_FILE = "tray.token"

WEBGUI_SCOPES = frozenset(
    {"control:write", "agent:write", "lifecycle:write", "read:all"}
)

_AGENT_WORKER_TOKEN = re.compile(r"^agent\.([A-Za-z0-9][A-Za-z0-9_-]{0,39})\.token$")
DEFAULT_AGENT_WORKER_ID = "yeyu"


@dataclass(frozen=True, slots=True)
class WebGuiSession:
    session_token: str
    csrf_token: str
    created_at: float
    last_seen_at: float


class WebGuiSessionBroker:
    """Process-local bootstrap tickets and browser sessions.

    Nothing in this broker is written to SQLite, events, receipts, or logs.
    Restarting Manager therefore invalidates every outstanding nonce and
    browser session by construction.  The local WebGUI can establish a fresh
    same-origin session after that restart; the tray nonce is only an optional
    launch convenience, not a prerequisite for using the local panel.
    """

    def __init__(
        self,
        *,
        bootstrap_ttl_seconds: int = 60,
        session_absolute_ttl_seconds: int = 8 * 60 * 60,
        session_idle_ttl_seconds: int = 30 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 10 <= bootstrap_ttl_seconds <= 300:
            raise ValueError("bootstrap_ttl_seconds must be between 10 and 300")
        if session_idle_ttl_seconds < 10:
            raise ValueError("session_idle_ttl_seconds must be at least 10")
        if session_absolute_ttl_seconds < session_idle_ttl_seconds:
            raise ValueError(
                "session_absolute_ttl_seconds must not be shorter than the idle TTL"
            )
        self.bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self.session_absolute_ttl_seconds = session_absolute_ttl_seconds
        self.session_idle_ttl_seconds = session_idle_ttl_seconds
        self._clock = clock
        self._nonces: dict[str, float] = {}
        self._sessions: dict[str, WebGuiSession] = {}
        self._lock = threading.Lock()

    def issue_nonce(self) -> tuple[str, int]:
        now = self._clock()
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_expired(now)
            self._nonces[nonce] = now + self.bootstrap_ttl_seconds
        return nonce, self.bootstrap_ttl_seconds

    def exchange_nonce(self, nonce: str) -> WebGuiSession | None:
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            expires_at = self._nonces.pop(nonce, None)
            if expires_at is None or expires_at <= now:
                return None
            return self._create_session_locked(now)

    def create_local_session(self) -> WebGuiSession:
        """Create a fresh browser session for the same-origin local WebGUI."""

        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            return self._create_session_locked(now)

    def session(
        self, session_token: str | None, *, touch: bool = True
    ) -> WebGuiSession | None:
        if not session_token:
            return None
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            session = self._sessions.get(session_token)
            if session is None:
                return None
            if touch:
                session = replace(session, last_seen_at=now)
                self._sessions[session_token] = session
            return session

    def revoke_session(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            return self._sessions.pop(session_token, None) is not None

    def _prune_expired(self, now: float) -> None:
        expired = [nonce for nonce, expiry in self._nonces.items() if expiry <= now]
        for nonce in expired:
            self._nonces.pop(nonce, None)
        expired_sessions = [
            token
            for token, session in self._sessions.items()
            if session.created_at + self.session_absolute_ttl_seconds <= now
            or session.last_seen_at + self.session_idle_ttl_seconds <= now
        ]
        for token in expired_sessions:
            self._sessions.pop(token, None)

    def _create_session_locked(self, now: float) -> WebGuiSession:
        session = WebGuiSession(
            session_token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
        )
        self._sessions[session.session_token] = session
        return session


def provision_actor_credentials(
    directory: Path,
    *,
    tray_bootstrap_secret: str | None = None,
) -> dict[str, ActorCredential]:
    directory.mkdir(parents=True, exist_ok=True)
    # v3 persisted tray.token with both lifecycle and WebGUI bootstrap scopes.
    # It is never loaded again: removing it before registering credentials makes
    # a failed deletion a startup failure instead of silently retaining the old
    # same-user escalation path.
    (directory / LEGACY_TRAY_TOKEN_FILE).unlink(missing_ok=True)
    credentials: dict[str, ActorCredential] = {}
    for actor_id, scopes in PERSISTENT_ACTOR_SCOPES.items():
        path = directory / f"{actor_id}.token"
        if not path.exists():
            token = secrets.token_urlsafe(32)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, token.encode("ascii"))
            finally:
                os.close(descriptor)
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise RuntimeError(f"actor credential is too short: {actor_id}")
        credentials[token] = ActorCredential(
            actor_id=actor_id,
            scopes=scopes,
            principal_id=actor_id,
        )
    # The supported typed Agent facade uses a named worker principal by default.
    # Keep agent.token only for compatibility with older low-level clients.
    default_worker_path = directory / f"agent.{DEFAULT_AGENT_WORKER_ID}.token"
    if default_worker_path.exists() and (
        default_worker_path.is_symlink() or not default_worker_path.is_file()
    ):
        raise RuntimeError("default Agent worker credential path is unsafe")
    if not default_worker_path.exists():
        token = secrets.token_urlsafe(32)
        descriptor = os.open(
            default_worker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        try:
            os.write(descriptor, token.encode("ascii"))
        finally:
            os.close(descriptor)
    # Optional per-worker Agent credentials provide a credential-bound worker
    # identity without changing the public actor name used by existing typed
    # clients.  They are operator-provisioned, never created implicitly.
    for path in sorted(directory.glob("agent.*.token")):
        match = _AGENT_WORKER_TOKEN.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise RuntimeError(f"agent worker credential is too short: {path.name}")
        if token in credentials:
            raise RuntimeError(f"agent worker credential collided: {path.name}")
        credentials[token] = ActorCredential(
            actor_id="agent",
            scopes=AGENT_WORKER_SCOPES,
            principal_id=f"agent:{match.group(1)}",
        )
    if tray_bootstrap_secret is not None:
        if (
            len(tray_bootstrap_secret) < 48
            or len(tray_bootstrap_secret) > 128
            or not all(
                character.isascii()
                and (character.isalnum() or character in {"_", "-"})
                for character in tray_bootstrap_secret
            )
        ):
            raise RuntimeError("ephemeral tray bootstrap credential is invalid")
        if tray_bootstrap_secret in credentials:
            raise RuntimeError("ephemeral tray bootstrap credential collided")
        credentials[tray_bootstrap_secret] = ActorCredential(
            actor_id=EPHEMERAL_TRAY_ACTOR_ID,
            scopes=EPHEMERAL_TRAY_SCOPES,
            principal_id=EPHEMERAL_TRAY_ACTOR_ID,
        )
    return credentials


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
