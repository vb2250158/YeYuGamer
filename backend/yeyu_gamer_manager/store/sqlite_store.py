from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")


_PUBLIC_FENCING_REJECTION = (
    "public persistence values must not contain private claim credentials"
)
_MAX_JSON_ESCAPE_SCAN_CHARACTERS = 8 * 1024 * 1024
_MAX_JSON_ESCAPE_NORMALIZATION_ROUNDS = 4
_PRIVATE_TEXT_COLUMNS = frozenset(
    {
        ("work_item_claims", "fencing_token"),
        ("work_item_fencing_token_history", "fencing_token"),
        ("run_attempts", "fencing_token_hash"),
        ("run_attempts", "cancel_authority_hash"),
        ("notification_deliveries", "lease_token"),
    }
)


_TODO_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {
            "pending",
            "in_progress",
            "skipped",
            "blocked",
            "review_required",
            "human_required",
        }
    ),
    "in_progress": frozenset(
        {
            "in_progress",
            "completed",
            "skipped",
            "blocked",
            "review_required",
            "human_required",
        }
    ),
    "skipped": frozenset(
        {"skipped", "in_progress", "blocked", "review_required", "human_required"}
    ),
    "blocked": frozenset(
        {"blocked", "in_progress", "skipped", "review_required", "human_required"}
    ),
    "review_required": frozenset(
        {"review_required", "in_progress", "skipped", "blocked", "human_required"}
    ),
    # An operator gate is deliberately not executable. A separate, explicit
    # release/resume contract must move it to a safe state before a new attempt.
    "human_required": frozenset(
        {"human_required", "review_required", "skipped", "blocked"}
    ),
    # A completed instance is immutable for its period. A reset creates a new,
    # deterministic instance for the next period instead of rolling state back.
    "completed": frozenset(),
}


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(
                character.lower()
                for character in str(key)
                if character.isalnum()
            )
            if any(
                sensitive in normalized
                for sensitive in ("fencingtoken", "cancelauthority", "authoritymac")
            ):
                return True
            if _contains_sensitive_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _redact_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_sensitive_keys(item)
            for key, item in value.items()
            if not any(
                sensitive
                in "".join(
                    character.lower()
                    for character in str(key)
                    if character.isalnum()
                )
                for sensitive in ("fencingtoken", "cancelauthority", "authoritymac")
            )
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_keys(item) for item in value]
    return value


class IdempotencyConflict(ValueError):
    """The same idempotency key was reused for a different request."""


class PublicFencingMaterialRejected(ValueError):
    """A public database value copied a private work-item claim credential."""


class RecordNotFound(KeyError):
    """A requested Manager entity does not exist."""


class StateVersionConflict(RuntimeError):
    """A mutation was based on a stale Manager state version."""

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            f"state version mismatch: expected {expected}, current {current}"
        )
        self.expected = expected
        self.current = current


class StateVersionRequired(RuntimeError):
    # A mutation omitted the required Manager state precondition.
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


# Batch ledger events used to embed the full ``result`` document (frozen Todo
# and evidence contracts for every run).  Thousands of ``batch.updated`` rows
# then dominated the database (hundreds of MB) and slowed every snapshot read.
# Consumers only need the identity/state projection; the authoritative result
# stays in the ``batches`` table.
BATCH_EVENT_RESULT_VALUE_LIMIT = 2048
BATCH_EVENT_RESULT_KEEP_KEYS = frozenset(
    {
        "acceptedDone",
        "acceptanceReason",
        "batchLineage",
        "batchActionAvailability",
        "candidateGameIds",
        "deferredGameIds",
        "executableGameIds",
        "gameDay",
        "outcome",
        "sealVersion",
        "sealedAt",
        "skippedCompletedGameIds",
        "unresolvedRequiredTodoIds",
        "finalGameRunIds",
    }
)


def _batch_event_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Project a batch record into a compact, stable event payload."""

    result = record.get("result")
    if not isinstance(result, dict):
        return dict(record)
    compact: dict[str, Any] = {}
    omitted: dict[str, int] = {}
    for key, value in result.items():
        encoded = _json(value)
        if key in BATCH_EVENT_RESULT_KEEP_KEYS or len(encoded) <= BATCH_EVENT_RESULT_VALUE_LIMIT:
            compact[key] = value
        else:
            omitted[key] = len(encoded)
    if omitted:
        compact["_omittedResultKeys"] = omitted
    payload = dict(record)
    payload["result"] = compact
    return payload


class SqliteStore:
    """One-process, single-writer SQLite store with an append-only event ledger."""

    def __init__(self, path: Path):
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._fencing_token_values: tuple[str, ...] = ()
        self._fencing_token_data_version: int | None = None
        self._public_text_guards_ready = False

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("store is not initialized")
        return self._connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        self._connection = connection
        # sqlite3.executescript controls its own transaction boundary, so schema
        # creation must not be nested inside the explicit BEGIN IMMEDIATE scope.
        with self._lock:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    order_index INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reward_claimed INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    cadence TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'plan',
                    state TEXT NOT NULL,
                    game_ids_json TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS game_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    account_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    cadence TEXT NOT NULL,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    exit_code INTEGER,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    todo_instance_ids_json TEXT NOT NULL DEFAULT '[]',
                    completed_todo_instance_ids_json TEXT NOT NULL DEFAULT '[]',
                    completion_todo_instance_ids_json TEXT NOT NULL DEFAULT '[]',
                    completion_scope_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS work_items (
                    work_item_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    game_id TEXT,
                    cadence TEXT,
                    run_id TEXT,
                    requested_by TEXT NOT NULL,
                    note TEXT NOT NULL,
                    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                    allowed_capability_refs_json TEXT NOT NULL DEFAULT '[]',
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_decisions (
                    decision_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_item_claims (
                    claim_id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
                    claimant TEXT NOT NULL,
                    state TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                    ,fencing_token TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS work_item_fencing_token_history (
                    fencing_token TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_invocations (
                    invocation_id TEXT PRIMARY KEY,
                    capability TEXT NOT NULL,
                    state TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_entity
                    ON events(entity_type, entity_id, sequence);
                CREATE TABLE IF NOT EXISTS idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_receipts (
                    command_id TEXT PRIMARY KEY,
                    receipt_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_resources (
                    resource_id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manager_resources_type
                    ON manager_resources(resource_type, created_at);
                CREATE TABLE IF NOT EXISTS todo_definitions (
                    todo_definition_id TEXT PRIMARY KEY,
                    definition_version INTEGER NOT NULL,
                    catalog_version TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    cadence TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    order_index INTEGER NOT NULL,
                    required INTEGER NOT NULL,
                    risk TEXT NOT NULL,
                    automation_difficulty TEXT NOT NULL,
                    adapter_capability_ref TEXT,
                    automation_state TEXT NOT NULL,
                    initial_status TEXT NOT NULL,
                    initial_reason TEXT NOT NULL,
                    reset_rule_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(game_id, cadence, operation)
                );
                CREATE INDEX IF NOT EXISTS idx_todo_definitions_game
                    ON todo_definitions(game_id, cadence, active, order_index);
                CREATE TABLE IF NOT EXISTS todo_instances (
                    todo_instance_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    todo_definition_id TEXT NOT NULL REFERENCES todo_definitions(todo_definition_id),
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    cadence TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    period_starts_at TEXT NOT NULL,
                    period_ends_at TEXT NOT NULL,
                    definition_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    run_id TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    last_attempt_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, todo_definition_id, period_key)
                );
                CREATE INDEX IF NOT EXISTS idx_todo_instances_period
                    ON todo_instances(game_id, cadence, period_key, status);
                CREATE TABLE IF NOT EXISTS run_attempts (
                    run_attempt_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    cadence TEXT NOT NULL,
                    state TEXT NOT NULL,
                    fencing_token_hash TEXT NOT NULL,
                    cancel_authority_hash TEXT NOT NULL DEFAULT '',
                    plan_json TEXT NOT NULL,
                    process_id INTEGER,
                    exit_code INTEGER,
                    result_json TEXT NOT NULL,
                    attempt_ordinal INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_attempts_run
                    ON run_attempts(run_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_attempts_one_active
                    ON run_attempts(run_id)
                    WHERE state IN ('starting', 'running', 'cancelling');
                CREATE TABLE IF NOT EXISTS todo_attempts (
                    todo_attempt_id TEXT PRIMARY KEY,
                    run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    todo_instance_id TEXT NOT NULL REFERENCES todo_instances(todo_instance_id),
                    attempt_number INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    retryable INTEGER NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_attempt_id, todo_instance_id),
                    UNIQUE(todo_instance_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_todo_attempts_instance
                    ON todo_attempts(todo_instance_id, attempt_number);
                CREATE TABLE IF NOT EXISTS adapter_events (
                    run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_attempt_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_adapter_events_type
                    ON adapter_events(event_type, created_at);
                CREATE TABLE IF NOT EXISTS controller_leases (
                    controller_lease_id TEXT PRIMARY KEY,
                    desktop_id TEXT NOT NULL,
                    manager_id TEXT NOT NULL,
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    game_day_key TEXT NOT NULL,
                    holder_principal_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    fencing_hash TEXT NOT NULL UNIQUE,
                    fencing_fingerprint TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    ended_at TEXT,
                    end_reason_code TEXT,
                    end_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(desktop_id, generation)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_controller_leases_one_active_desktop
                    ON controller_leases(desktop_id) WHERE state = 'active';
                CREATE INDEX IF NOT EXISTS idx_controller_leases_attempt
                    ON controller_leases(run_attempt_id, created_at);
                CREATE TABLE IF NOT EXISTS todo_blockers (
                    blocker_id TEXT PRIMARY KEY,
                    manager_id TEXT NOT NULL,
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    todo_instance_id TEXT NOT NULL REFERENCES todo_instances(todo_instance_id),
                    todo_attempt_id TEXT NOT NULL REFERENCES todo_attempts(todo_attempt_id),
                    game_day_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    code TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    retryable INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    artifact_refs_json TEXT NOT NULL,
                    raised_at TEXT NOT NULL,
                    transitioned_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_code TEXT,
                    resolution_reason TEXT,
                    resolution_artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                    release_id TEXT,
                    release_explicit INTEGER NOT NULL DEFAULT 0,
                    released_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_blockers_one_active
                    ON todo_blockers(run_id, todo_instance_id) WHERE state = 'active';
                CREATE INDEX IF NOT EXISTS idx_todo_blockers_attempt
                    ON todo_blockers(run_attempt_id, todo_instance_id, revision);
                CREATE TABLE IF NOT EXISTS resume_intents (
                    resume_intent_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    predecessor_run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    source_run_revision INTEGER NOT NULL,
                    source_attempt_revision INTEGER NOT NULL,
                    decision_hash TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    work_item_id TEXT,
                    successor_run_attempt_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(
                        run_id, predecessor_run_attempt_id,
                        source_run_revision, source_attempt_revision
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_resume_intents_run
                    ON resume_intents(run_id, created_at);
                CREATE TABLE IF NOT EXISTS batch_lineage (
                    batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id),
                    root_batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    predecessor_batch_id TEXT REFERENCES batches(batch_id),
                    continuation_ordinal INTEGER NOT NULL,
                    resume_intent_id TEXT UNIQUE REFERENCES resume_intents(resume_intent_id),
                    created_at TEXT NOT NULL,
                    CHECK(continuation_ordinal >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_batch_lineage_root
                    ON batch_lineage(root_batch_id, continuation_ordinal, created_at);
                CREATE TABLE IF NOT EXISTS batch_run_memberships (
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    ordinal INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    resume_intent_id TEXT REFERENCES resume_intents(resume_intent_id),
                    latest_run_attempt_id TEXT REFERENCES run_attempts(run_attempt_id),
                    terminal_outcome TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, run_id),
                    UNIQUE(batch_id, ordinal),
                    CHECK(ordinal >= 0),
                    CHECK(role IN ('initial', 'continuation')),
                    CHECK(state IN (
                        'queued', 'resume_pending', 'active', 'terminal',
                        'cancelled', 'reconciliation_required'
                    ))
                );
                CREATE INDEX IF NOT EXISTS idx_batch_run_memberships_run
                    ON batch_run_memberships(run_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_run_membership_resume_intent
                    ON batch_run_memberships(resume_intent_id)
                    WHERE resume_intent_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS batch_cancel_requests (
                    cancel_request_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    active_run_attempt_ids_json TEXT NOT NULL,
                    work_item_id TEXT REFERENCES work_items(work_item_id),
                    delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_delivery_at TEXT,
                    next_retry_at TEXT,
                    last_error_class TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(state IN (
                        'requested', 'signal_delivered',
                        'reconciliation_required', 'acknowledged', 'sealed'
                    ))
                );
                CREATE INDEX IF NOT EXISTS idx_batch_cancel_requests_batch
                    ON batch_cancel_requests(batch_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_cancel_requests_one_active
                    ON batch_cancel_requests(batch_id)
                    WHERE state IN (
                        'requested', 'signal_delivered',
                        'reconciliation_required', 'acknowledged'
                    );
                CREATE TABLE IF NOT EXISTS execution_control_facts (
                    fact_id TEXT PRIMARY KEY,
                    fact_type TEXT NOT NULL,
                    manager_id TEXT NOT NULL,
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    run_id TEXT NOT NULL REFERENCES game_runs(run_id),
                    run_attempt_id TEXT NOT NULL REFERENCES run_attempts(run_attempt_id),
                    todo_instance_id TEXT,
                    game_day_key TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(fact_type, fact_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_control_facts_scope
                    ON execution_control_facts(
                        fact_type, run_id, run_attempt_id, todo_instance_id, created_at
                    );
                CREATE TABLE IF NOT EXISTS notification_policy (
                    policy_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    automatic_dispatch INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    recipient_binding_id TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    notification_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    seal_version INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    recipient_binding_id TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    dispatch_gate TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    text_body TEXT NOT NULL,
                    html_body TEXT NOT NULL,
                    report_html TEXT NOT NULL DEFAULT '',
                    attachment_refs_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    last_error_class TEXT NOT NULL DEFAULT '',
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, seal_version, channel, recipient_binding_id)
                );
                CREATE INDEX IF NOT EXISTS idx_notification_dispatch
                    ON notification_deliveries(state, dispatch_gate, next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS notification_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    notification_id TEXT NOT NULL REFERENCES notification_deliveries(notification_id),
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT,
                    error_class TEXT NOT NULL DEFAULT '',
                    retry_scheduled_at TEXT,
                    transport_receipt_hash TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(notification_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_notification_attempts_delivery
                    ON notification_attempts(notification_id, attempt_number);
                """
            )
            self._ensure_column("notification_deliveries", "report_html", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("batches", "mode", "TEXT NOT NULL DEFAULT 'plan'")
            self._ensure_column(
                "work_items", "artifact_refs_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(
                "work_items",
                "allowed_capability_refs_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                "work_item_claims", "fencing_token", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "game_runs", "todo_instance_ids_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(
                "game_runs",
                "completed_todo_instance_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                "game_runs",
                "completion_todo_instance_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                "game_runs", "completion_scope_version", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column("game_runs", "account_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column("game_runs", "account_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("todo_instances", "account_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column("run_attempts", "account_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(
                "todo_instances",
                "period_starts_at",
                "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
            )
            self._ensure_column(
                "todo_instances",
                "period_ends_at",
                "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
            )
            self._ensure_column(
                "todo_definitions", "definition_version", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                "todo_definitions",
                "catalog_version",
                "TEXT NOT NULL DEFAULT 'legacy-v1'",
            )
            self._ensure_column(
                "todo_definitions",
                "source_hash",
                "TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'",
            )
            self._ensure_column(
                "run_attempts", "attempt_ordinal", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                "run_attempts", "cancel_authority_hash", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column("todo_blockers", "manager_id", "TEXT")
            self._ensure_column("todo_blockers", "game_id", "TEXT")
            self._ensure_column(
                "batch_cancel_requests",
                "delivery_attempt_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column("batch_cancel_requests", "last_delivery_at", "TEXT")
            self._ensure_column("batch_cancel_requests", "next_retry_at", "TEXT")
            self._ensure_column(
                "batch_cancel_requests", "last_error_class", "TEXT NOT NULL DEFAULT ''"
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS trg_work_item_claim_fencing_history_insert
                AFTER INSERT ON work_item_claims
                WHEN typeof(NEW.fencing_token) IN ('text', 'blob')
                  AND length(CAST(NEW.fencing_token AS TEXT)) > 0
                BEGIN
                    INSERT OR IGNORE INTO work_item_fencing_token_history(
                        fencing_token, first_seen_at
                    ) VALUES (
                        CAST(NEW.fencing_token AS TEXT),
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_item_claim_fencing_history_update
                AFTER UPDATE OF fencing_token ON work_item_claims
                WHEN typeof(NEW.fencing_token) IN ('text', 'blob')
                  AND length(CAST(NEW.fencing_token AS TEXT)) > 0
                BEGIN
                    INSERT OR IGNORE INTO work_item_fencing_token_history(
                        fencing_token, first_seen_at
                    ) VALUES (
                        CAST(NEW.fencing_token AS TEXT),
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    );
                END;
                """
            )
        self._migrate_account_todo_identity()
        with self._write_scope():
            self._normalize_private_fencing_tokens_and_history()
            needs_public_fencing_migration = (
                connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 6"
                ).fetchone()
                is None
            )
            if needs_public_fencing_migration:
                # Identity replacements are applied consistently across parent
                # and child tables before the one-time v6 migration commits.
                connection.execute("PRAGMA defer_foreign_keys=ON")
                self._scrub_legacy_public_fencing_material()
                self._assert_public_fencing_material_absent()
            else:
                # A preserved older executable could have written through a
                # different connection after v6 was marked.  Every startup does
                # one read-only verification and scrubs only on a real hit.
                try:
                    self._assert_public_fencing_material_absent()
                except PublicFencingMaterialRejected:
                    connection.execute("PRAGMA defer_foreign_keys=ON")
                    self._scrub_legacy_public_fencing_material()
                    self._assert_public_fencing_material_absent()
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (1, _now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (2, _now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (3, _now()),
            )
            timestamp = _now()
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (4, timestamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (5, timestamp),
            )
            if needs_public_fencing_migration:
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (6, timestamp),
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (7, timestamp),
            )
            needs_execution_control_migration = (
                connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 8"
                ).fetchone()
                is None
            )
            if needs_execution_control_migration:
                connection.execute(
                    """
                    UPDATE run_attempts AS current
                    SET attempt_ordinal = (
                        SELECT COUNT(*) FROM run_attempts AS previous
                        WHERE previous.run_id = current.run_id
                          AND (
                              previous.created_at < current.created_at
                              OR (
                                  previous.created_at = current.created_at
                                  AND previous.run_attempt_id <= current.run_attempt_id
                              )
                          )
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (8, timestamp),
                )
            needs_blocker_scope_migration = (
                connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 9"
                ).fetchone()
                is None
            )
            if needs_blocker_scope_migration:
                connection.execute(
                    """
                    UPDATE todo_blockers SET game_id = (
                        SELECT game_id FROM game_runs
                        WHERE game_runs.run_id = todo_blockers.run_id
                    ) WHERE game_id IS NULL
                    """
                )
                connection.execute(
                    """
                    UPDATE todo_blockers SET manager_id = COALESCE(
                        (
                            SELECT manager_id FROM controller_leases
                            WHERE controller_leases.run_attempt_id =
                                todo_blockers.run_attempt_id
                            ORDER BY generation DESC LIMIT 1
                        ),
                        'legacy-manager'
                    ) WHERE manager_id IS NULL
                    """
                )
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (9, timestamp),
                )
            needs_batch_topology_migration = (
                connection.execute(
                    "SELECT 1 FROM schema_version WHERE version = 10"
                ).fetchone()
                is None
            )
            if needs_batch_topology_migration:
                self._backfill_batch_topology_locked(timestamp)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (10, timestamp),
                )
            # v11 adds an explicit immutable GameRun completion scope. Existing
            # rows intentionally remain version 0; guessing their missing
            # optional/review-only Todos would make old runs look acceptable.
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (11, timestamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (12, timestamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (13, timestamp),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO notification_policy(
                    policy_id, enabled, automatic_dispatch, channel,
                    recipient_binding_id, updated_by, created_at, updated_at
                ) VALUES ('default', 1, 1, 'email', 'self-email', 'manager-default', ?, ?)
                """,
                (timestamp, timestamp),
            )
        with self._lock:
            self._reload_private_fencing_tokens_locked()
            self._install_public_text_guards_locked()

    def _normalize_private_fencing_tokens_and_history(self) -> None:
        """Normalize legacy SQLite values and retain every observed private grant."""

        timestamp = _now()
        historical_rows = self.connection.execute(
            """
            SELECT rowid AS _rowid_, fencing_token
            FROM work_item_fencing_token_history
            """
        ).fetchall()
        for row in historical_rows:
            token = row["fencing_token"]
            if isinstance(token, str) and token:
                continue
            normalized = ""
            if isinstance(token, bytes):
                try:
                    normalized = token.decode("utf-8")
                except UnicodeDecodeError:
                    normalized = ""
            self.connection.execute(
                "DELETE FROM work_item_fencing_token_history WHERE rowid = ?",
                (int(row["_rowid_"]),),
            )
            if normalized:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO work_item_fencing_token_history(
                        fencing_token, first_seen_at
                    ) VALUES (?, ?)
                    """,
                    (normalized, timestamp),
                )

        claim_rows = self.connection.execute(
            """
            SELECT rowid AS _rowid_, state, fencing_token
            FROM work_item_claims
            """
        ).fetchall()
        for row in claim_rows:
            original = row["fencing_token"]
            normalized = original if isinstance(original, str) else ""
            if isinstance(original, bytes):
                try:
                    normalized = original.decode("utf-8")
                except UnicodeDecodeError:
                    normalized = ""
            state = row["state"] if isinstance(row["state"], str) else "expired"
            if not normalized:
                normalized = uuid.uuid4().hex
                state = "expired"
            if original != normalized or row["state"] != state:
                self.connection.execute(
                    """
                    UPDATE work_item_claims
                    SET fencing_token = ?, state = ?
                    WHERE rowid = ?
                    """,
                    (normalized, state, int(row["_rowid_"])),
                )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO work_item_fencing_token_history(
                    fencing_token, first_seen_at
                ) VALUES (?, ?)
                """,
                (normalized, timestamp),
            )

    def _scrub_legacy_public_fencing_material(self) -> None:
        """Remove historical claim grants from every public TEXT/JSON column."""

        columns_by_table = self._public_text_columns()
        tokens = self._private_fencing_tokens()
        replacements = {
            token: f"[removed-private-claim-{uuid.uuid4().hex}]" for token in tokens
        }
        ambiguous_replacements: dict[str, str] = {}
        token_pattern = (
            re.compile("|".join(re.escape(token) for token in tokens))
            if tokens
            else None
        )

        def replace_text_semantics(value: str) -> str:
            if token_pattern is None:
                return value
            cleaned = token_pattern.sub(
                lambda match: replacements[match.group(0)], value
            )
            normalizations = self._json_escape_normalizations(cleaned)
            if normalizations is None:
                return ambiguous_replacements.setdefault(
                    value,
                    f"[removed-ambiguous-public-text-{uuid.uuid4().hex}]",
                )
            for candidate in reversed(normalizations):
                if token_pattern.search(candidate) is not None:
                    return token_pattern.sub(
                        lambda match: replacements[match.group(0)], candidate
                    )
            return cleaned

        def replace_decoded(value: Any) -> Any:
            if isinstance(value, str):
                return replace_text_semantics(value)
            if isinstance(value, dict):
                return {
                    replace_decoded(key): replace_decoded(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [replace_decoded(item) for item in value]
            return value

        def contains_token(row: sqlite3.Row, columns: tuple[str, ...]) -> bool:
            for column in columns:
                value = row[column]
                if value is None:
                    continue
                if not isinstance(value, str):
                    return True
                if self._text_values_are_safe_for_tokens(tokens, value) == 0:
                    return True
            return False

        def command_id_from_response(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            try:
                document = json.loads(value)
            except json.JSONDecodeError:
                return None
            if not isinstance(document, dict):
                return None
            command_id = document.get("commandId") or document.get("command_id")
            return str(command_id) if command_id else None

        # Idempotency rows and command receipts are replay caches.  If a secret
        # reached either identity or body, deleting the cache and its command
        # ledger is safer than rewriting an identity and risking a collision.
        contaminated_idempotency_rowids: set[int] = set()
        contaminated_receipt_rowids: set[int] = set()
        contaminated_command_ids: set[str] = set()
        idempotency_columns = columns_by_table.get("idempotency", ())
        idempotency_rows = self.connection.execute(
            "SELECT rowid AS _rowid_, * FROM idempotency"
        ).fetchall()
        for row in idempotency_rows:
            if contains_token(row, idempotency_columns):
                contaminated_idempotency_rowids.add(int(row["_rowid_"]))
                command_id = command_id_from_response(row["response_json"])
                if command_id:
                    contaminated_command_ids.add(command_id)
        receipt_columns = columns_by_table.get("command_receipts", ())
        receipt_rows = self.connection.execute(
            "SELECT rowid AS _rowid_, * FROM command_receipts"
        ).fetchall()
        for row in receipt_rows:
            if contains_token(row, receipt_columns):
                contaminated_receipt_rowids.add(int(row["_rowid_"]))
                contaminated_command_ids.add(str(row["command_id"]))
        for row in idempotency_rows:
            command_id = command_id_from_response(row["response_json"])
            if command_id and command_id in contaminated_command_ids:
                contaminated_idempotency_rowids.add(int(row["_rowid_"]))
        for row in receipt_rows:
            if str(row["command_id"]) in contaminated_command_ids:
                contaminated_receipt_rowids.add(int(row["_rowid_"]))
        for rowid in contaminated_idempotency_rowids:
            self.connection.execute("DELETE FROM idempotency WHERE rowid = ?", (rowid,))
        for rowid in contaminated_receipt_rowids:
            self.connection.execute("DELETE FROM command_receipts WHERE rowid = ?", (rowid,))

        event_columns = columns_by_table.get("events", ())
        event_rows = self.connection.execute(
            "SELECT rowid AS _rowid_, * FROM events"
        ).fetchall()
        for row in event_rows:
            associated_command = (
                str(row["entity_type"]) == "command"
                and str(row["entity_id"]) in contaminated_command_ids
            )
            if associated_command:
                self.connection.execute(
                    "DELETE FROM events WHERE rowid = ?", (int(row["_rowid_"]),)
                )

        for table, columns in columns_by_table.items():
            if not columns:
                continue
            quoted_table = self._quote_identifier(table)
            selected = ", ".join(self._quote_identifier(column) for column in columns)
            rows = self.connection.execute(
                f"SELECT rowid AS _rowid_, {selected} FROM {quoted_table}"
            ).fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in columns:
                    value = row[column]
                    if value is None:
                        continue
                    if isinstance(value, str):
                        cleaned = value
                    elif isinstance(value, bytes):
                        try:
                            cleaned = value.decode("utf-8")
                        except UnicodeDecodeError:
                            cleaned = f"[removed-invalid-public-text-{uuid.uuid4().hex}]"
                    else:
                        cleaned = str(value)
                    if token_pattern is not None:
                        cleaned = token_pattern.sub(
                            lambda match: replacements[match.group(0)], cleaned
                        )
                    parsed_document = False
                    if cleaned.lstrip()[:1] in {'{', '[', '"'}:
                        try:
                            document = json.loads(cleaned)
                        except json.JSONDecodeError:
                            pass
                        else:
                            parsed_document = True
                            decoded_cleaned = replace_decoded(document)
                            if _contains_sensitive_key(decoded_cleaned):
                                decoded_cleaned = _redact_sensitive_keys(
                                    decoded_cleaned
                                )
                            if (
                                decoded_cleaned != document
                                or cleaned != value
                            ):
                                cleaned = _json(decoded_cleaned)
                    if not parsed_document:
                        cleaned = replace_text_semantics(cleaned)
                    if cleaned != value:
                        updates[column] = cleaned
                if not updates:
                    continue
                assignments = ", ".join(
                    f"{self._quote_identifier(column)} = ?" for column in updates
                )
                self.connection.execute(
                    f"UPDATE {quoted_table} SET {assignments} WHERE rowid = ?",
                    (*updates.values(), int(row["_rowid_"])),
                )

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def _public_text_columns(self) -> dict[str, tuple[str, ...]]:
        tables = [
            str(row["name"])
            for row in self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        result: dict[str, tuple[str, ...]] = {}
        for table in tables:
            columns = tuple(
                str(row["name"])
                for row in self.connection.execute(
                    f"PRAGMA table_info({self._quote_identifier(table)})"
                ).fetchall()
                if "TEXT" in str(row["type"]).upper()
                and (table, str(row["name"])) not in _PRIVATE_TEXT_COLUMNS
            )
            if columns:
                result[table] = columns
        return result

    def _data_version_locked(self) -> int:
        row = self.connection.execute("PRAGMA data_version").fetchone()
        if row is None:
            raise RuntimeError("SQLite data version is unavailable")
        return int(row[0])

    def _reload_private_fencing_tokens_locked(
        self, *, data_version: int | None = None
    ) -> None:
        self._fencing_token_values = tuple(
            sorted(
                {
                    str(row[0])
                    for row in self.connection.execute(
                        """
                        SELECT fencing_token FROM work_item_claims
                        WHERE typeof(fencing_token) = 'text' AND fencing_token <> ''
                        UNION
                        SELECT fencing_token FROM work_item_fencing_token_history
                        WHERE typeof(fencing_token) = 'text' AND fencing_token <> ''
                        """
                    ).fetchall()
                },
                key=len,
                reverse=True,
            )
        )

        self._fencing_token_data_version = (
            self._data_version_locked() if data_version is None else data_version
        )

    def _refresh_private_fencing_tokens_locked(self) -> None:
        data_version = self._data_version_locked()
        if self._fencing_token_data_version != data_version:
            self._reload_private_fencing_tokens_locked(data_version=data_version)

    def _remember_private_fencing_token(self, value: Any) -> int:
        with self._lock:
            token = str(value or "")
            if token and token not in self._fencing_token_values:
                self._fencing_token_values = tuple(
                    sorted(
                        {*self._fencing_token_values, token},
                        key=len,
                        reverse=True,
                    )
                )
        return 1

    @staticmethod
    def _decode_one_json_escape_layer(text: str) -> str:
        """Decode one JSON-string escape layer without requiring a JSON wrapper."""

        simple_escapes = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        result: list[str] = []
        index = 0
        while index < len(text):
            if text[index] != "\\" or index + 1 >= len(text):
                result.append(text[index])
                index += 1
                continue
            escape = text[index + 1]
            if escape in simple_escapes:
                result.append(simple_escapes[escape])
                index += 2
                continue
            if escape != "u" or index + 6 > len(text):
                result.append(text[index])
                index += 1
                continue
            encoded = text[index + 2 : index + 6]
            if re.fullmatch(r"[0-9A-Fa-f]{4}", encoded) is None:
                result.append(text[index])
                index += 1
                continue
            code_unit = int(encoded, 16)
            if 0xD800 <= code_unit <= 0xDBFF:
                pair_start = index + 6
                pair_end = pair_start + 6
                if (
                    pair_end <= len(text)
                    and text[pair_start : pair_start + 2] == "\\u"
                    and re.fullmatch(
                        r"[0-9A-Fa-f]{4}", text[pair_start + 2 : pair_end]
                    )
                    is not None
                ):
                    low_surrogate = int(text[pair_start + 2 : pair_end], 16)
                    if 0xDC00 <= low_surrogate <= 0xDFFF:
                        result.append(
                            chr(
                                0x10000
                                + ((code_unit - 0xD800) << 10)
                                + (low_surrogate - 0xDC00)
                            )
                        )
                        index = pair_end
                        continue
                result.append(text[index : index + 6])
                index += 6
                continue
            if 0xDC00 <= code_unit <= 0xDFFF:
                result.append(text[index : index + 6])
                index += 6
                continue
            result.append(chr(code_unit))
            index += 6
        return "".join(result)

    @classmethod
    def _json_escape_normalizations(cls, text: str) -> tuple[str, ...] | None:
        """Return bounded semantic candidates, or None for ambiguous input."""

        if len(text) > _MAX_JSON_ESCAPE_SCAN_CHARACTERS:
            return None if "\\" in text else ()
        candidates: list[str] = []
        current = text
        for _ in range(_MAX_JSON_ESCAPE_NORMALIZATION_ROUNDS):
            decoded = cls._decode_one_json_escape_layer(current)
            if decoded == current:
                return tuple(candidates)
            candidates.append(decoded)
            current = decoded
        if cls._decode_one_json_escape_layer(current) != current:
            return None
        return tuple(candidates)

    @classmethod
    def _text_values_are_safe_for_tokens(
        cls, tokens: tuple[str, ...], *values: Any
    ) -> int:
        for value in values:
            if value is None:
                continue
            if not isinstance(value, str):
                return 0
            text = value
            if any(token in text for token in tokens):
                return 0
            if tokens:
                normalized = cls._json_escape_normalizations(text)
                if normalized is None:
                    return 0
                if any(
                    token in candidate
                    for candidate in normalized
                    for token in tokens
                ):
                    return 0
            if text.lstrip()[:1] in {'{', '[', '"'}:
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    pass
                else:
                    if _contains_sensitive_key(decoded) or (
                        cls._value_contains_fencing_token(decoded, tokens)
                    ):
                        return 0
        return 1

    def _public_text_values_are_safe(self, *values: Any) -> int:
        with self._lock:
            if self._public_text_guards_ready:
                self._refresh_private_fencing_tokens_locked()
            return self._text_values_are_safe_for_tokens(
                self._fencing_token_values, *values
            )

    def _claim_candidate_values_are_safe(
        self, fencing_token: Any, *public_values: Any
    ) -> int:
        token = str(fencing_token or "")
        return self._text_values_are_safe_for_tokens(
            (token,) if token else (), *public_values
        )

    def _install_public_text_guards_locked(self) -> None:
        """Protect every public TEXT column, including future writer methods."""

        connection = self.connection
        connection.create_function(
            "yeyu_public_text_values_are_safe",
            -1,
            self._public_text_values_are_safe,
        )
        connection.create_function(
            "yeyu_remember_private_fencing_token",
            1,
            self._remember_private_fencing_token,
        )
        connection.create_function(
            "yeyu_claim_candidate_values_are_safe",
            -1,
            self._claim_candidate_values_are_safe,
        )
        for table, columns in self._public_text_columns().items():
            digest = hashlib.sha256(table.encode("utf-8")).hexdigest()[:16]
            arguments = ", ".join(
                f"NEW.{self._quote_identifier(column)}" for column in columns
            )
            type_checks = " AND ".join(
                f"typeof(NEW.{self._quote_identifier(column)}) IN ('text', 'null')"
                for column in columns
            )
            for operation in ("INSERT", "UPDATE"):
                trigger_name = self._quote_identifier(
                    f"yeyu_public_text_{operation.lower()}_{digest}"
                )
                connection.execute(
                    f"""
                    CREATE TEMP TRIGGER {trigger_name}
                    BEFORE {operation} ON main.{self._quote_identifier(table)}
                    WHEN NOT ({type_checks})
                      OR yeyu_public_text_values_are_safe({arguments}) = 0
                    BEGIN
                        SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
                    END
                    """
                )
        claim_public_columns = self._public_text_columns().get(
            "work_item_claims", ()
        )
        claim_public_arguments = ", ".join(
            f"NEW.{self._quote_identifier(column)}"
            for column in claim_public_columns
        )
        for operation in ("INSERT", "UPDATE"):
            connection.execute(
                f"""
                CREATE TEMP TRIGGER yeyu_private_claim_token_type_{operation.lower()}
                BEFORE {operation} ON main.work_item_claims
                WHEN typeof(NEW.fencing_token) <> 'text'
                  OR NEW.fencing_token = ''
                BEGIN
                    SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
                END
                """
            )
            connection.execute(
                f"""
                CREATE TEMP TRIGGER yeyu_claim_candidate_{operation.lower()}
                BEFORE {operation} ON main.work_item_claims
                WHEN typeof(NEW.fencing_token) = 'text'
                  AND NEW.fencing_token <> ''
                  AND yeyu_claim_candidate_values_are_safe(
                      NEW.fencing_token, {claim_public_arguments}
                  ) = 0
                BEGIN
                    SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
                END
                """
            )
        connection.execute(
            """
            CREATE TEMP TRIGGER yeyu_private_claim_token_insert
            AFTER INSERT ON main.work_item_claims
            WHEN NEW.fencing_token IS NOT NULL AND NEW.fencing_token <> ''
            BEGIN
                INSERT OR IGNORE INTO work_item_fencing_token_history(
                    fencing_token, first_seen_at
                ) VALUES (NEW.fencing_token, CURRENT_TIMESTAMP);
                SELECT yeyu_remember_private_fencing_token(NEW.fencing_token);
            END
            """
        )
        connection.execute(
            """
            CREATE TEMP TRIGGER yeyu_private_claim_token_update
            AFTER UPDATE OF fencing_token ON main.work_item_claims
            WHEN NEW.fencing_token IS NOT NULL AND NEW.fencing_token <> ''
            BEGIN
                INSERT OR IGNORE INTO work_item_fencing_token_history(
                    fencing_token, first_seen_at
                ) VALUES (NEW.fencing_token, CURRENT_TIMESTAMP);
                SELECT yeyu_remember_private_fencing_token(NEW.fencing_token);
            END
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TRIGGER yeyu_private_token_history_type_insert
            BEFORE INSERT ON main.work_item_fencing_token_history
            WHEN typeof(NEW.fencing_token) <> 'text'
              OR NEW.fencing_token = ''
            BEGIN
                SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
            END
            """
        )
        connection.execute(
            """
            CREATE TEMP TRIGGER yeyu_private_token_history_remember_insert
            AFTER INSERT ON main.work_item_fencing_token_history
            BEGIN
                SELECT yeyu_remember_private_fencing_token(NEW.fencing_token);
            END
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TRIGGER yeyu_private_token_history_no_update
            BEFORE UPDATE ON main.work_item_fencing_token_history
            BEGIN
                SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TRIGGER yeyu_private_token_history_no_delete
            BEFORE DELETE ON main.work_item_fencing_token_history
            BEGIN
                SELECT RAISE(ABORT, '{_PUBLIC_FENCING_REJECTION}');
            END
            """
        )
        self._public_text_guards_ready = True

    def _private_fencing_tokens(self) -> tuple[str, ...]:
        with self._lock:
            if self._public_text_guards_ready:
                self._refresh_private_fencing_tokens_locked()
            else:
                self._reload_private_fencing_tokens_locked()
            return self._fencing_token_values

    @staticmethod
    def _value_contains_fencing_token(value: Any, tokens: tuple[str, ...]) -> bool:
        if isinstance(value, str):
            return any(token in value for token in tokens)
        if isinstance(value, dict):
            return any(
                SqliteStore._value_contains_fencing_token(key, tokens)
                or SqliteStore._value_contains_fencing_token(item, tokens)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple, set)):
            return any(
                SqliteStore._value_contains_fencing_token(item, tokens)
                for item in value
            )
        return False

    @classmethod
    def _python_public_value_is_safe(
        cls, value: Any, tokens: tuple[str, ...]
    ) -> bool:
        if value is None or isinstance(value, (bool, int, float)):
            return True
        if isinstance(value, str):
            return cls._text_values_are_safe_for_tokens(tokens, value) == 1
        if isinstance(value, bytes):
            return False
        if isinstance(value, dict):
            if _contains_sensitive_key(value):
                return False
            return all(
                cls._python_public_value_is_safe(key, tokens)
                and cls._python_public_value_is_safe(item, tokens)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple, set)):
            return all(
                cls._python_public_value_is_safe(item, tokens) for item in value
            )
        return cls._text_values_are_safe_for_tokens(tokens, str(value)) == 1

    def _assert_public_values(self, *values: Any) -> None:
        tokens = self._private_fencing_tokens()
        if any(
            not self._python_public_value_is_safe(value, tokens) for value in values
        ):
            raise PublicFencingMaterialRejected(_PUBLIC_FENCING_REJECTION)

    def assert_public_fencing_material_absent_from_values(
        self, *values: Any
    ) -> None:
        """Reject public values containing any current or historical claim grant."""

        self._assert_public_values(*values)

    def _assert_candidate_fencing_token_is_private(
        self, token: str, public_claim: dict[str, Any]
    ) -> None:
        if not token:
            raise ValueError("work item claim fencing token is empty")
        candidate = (token,)
        if self._value_contains_fencing_token(public_claim, candidate):
            raise PublicFencingMaterialRejected(_PUBLIC_FENCING_REJECTION)
        for table, columns in self._public_text_columns().items():
            quoted_table = self._quote_identifier(table)
            for column in columns:
                quoted_column = self._quote_identifier(column)
                found = self.connection.execute(
                    f"""
                    SELECT 1 FROM {quoted_table}
                    WHERE instr(COALESCE(CAST({quoted_column} AS TEXT), ''), ?) > 0
                    LIMIT 1
                    """,
                    (token,),
                ).fetchone()
                if found is not None:
                    raise PublicFencingMaterialRejected(_PUBLIC_FENCING_REJECTION)
                for row in self.connection.execute(
                    f"SELECT {quoted_column} FROM {quoted_table} "
                    f"WHERE {quoted_column} IS NOT NULL"
                ).fetchall():
                    value = row[0]
                    if self._text_values_are_safe_for_tokens(candidate, value) == 0:
                        raise PublicFencingMaterialRejected(
                            _PUBLIC_FENCING_REJECTION
                        )

    def _assert_public_fencing_material_absent(self) -> None:
        tokens = self._private_fencing_tokens()
        for table, columns in self._public_text_columns().items():
            quoted_table = self._quote_identifier(table)
            selected = ", ".join(self._quote_identifier(column) for column in columns)
            for row in self.connection.execute(
                f"SELECT {selected} FROM {quoted_table}"
            ).fetchall():
                for column in columns:
                    value = row[column]
                    if value is None:
                        continue
                    if self._text_values_are_safe_for_tokens(tokens, value) == 0:
                        raise PublicFencingMaterialRejected(
                            _PUBLIC_FENCING_REJECTION
                        )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._fencing_token_values = ()
            self._fencing_token_data_version = None
            self._public_text_guards_ready = False

    @staticmethod
    def _legacy_batch_run_ids(result: dict[str, Any]) -> list[str]:
        """Extract only historical Manager-owned run references for v10 backfill."""

        values: list[str] = []
        for key in ("finalGameRunIds", "completedRunIds", "failedRunIds"):
            candidate = result.get(key, [])
            if isinstance(candidate, list):
                values.extend(str(item) for item in candidate if item)
        phase = result.get("completionReviewPhase")
        if isinstance(phase, dict):
            context = phase.get("frozenFinalContext")
            if isinstance(context, dict):
                candidate = context.get("finalRunIds", [])
                if isinstance(candidate, list):
                    values.extend(str(item) for item in candidate if item)
        return list(dict.fromkeys(values))

    def _backfill_batch_topology_locked(self, timestamp: str) -> None:
        """Build durable ownership from old Manager facts without changing seals."""

        rows = self.connection.execute(
            "SELECT batch_id, state, result_json FROM batches ORDER BY created_at"
        ).fetchall()
        for row in rows:
            batch_id = str(row["batch_id"])
            self.connection.execute(
                """
                INSERT OR IGNORE INTO batch_lineage(
                    batch_id, root_batch_id, predecessor_batch_id,
                    continuation_ordinal, resume_intent_id, created_at
                ) VALUES (?, ?, NULL, 0, NULL, ?)
                """,
                (batch_id, batch_id, timestamp),
            )
            result = _decode(row["result_json"], {})
            run_ids = self._legacy_batch_run_ids(result)
            queued_rows = self.connection.execute(
                "SELECT run_id FROM game_runs WHERE message = ? ORDER BY created_at",
                (f"queued by batch {batch_id}",),
            ).fetchall()
            run_ids.extend(str(item["run_id"]) for item in queued_rows)
            run_ids = list(dict.fromkeys(run_ids))
            sealed = isinstance(result, dict) and result.get("sealVersion") is not None
            for ordinal, run_id in enumerate(run_ids):
                run_row = self.connection.execute(
                    "SELECT state FROM game_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run_row is None:
                    continue
                run_state = str(run_row["state"])
                membership_state = (
                    "terminal"
                    if sealed
                    or run_state
                    not in {"pending_execution", "queued", "running", "cancelling"}
                    else "active"
                    if run_state in {"running", "cancelling"}
                    else "queued"
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO batch_run_memberships(
                        batch_id, run_id, ordinal, role, state,
                        resume_intent_id, latest_run_attempt_id,
                        terminal_outcome, created_at, updated_at
                    ) VALUES (?, ?, ?, 'initial', ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        batch_id,
                        run_id,
                        ordinal,
                        membership_state,
                        timestamp,
                        timestamp,
                    ),
                )

    def _migrate_account_todo_identity(self) -> None:
        """Replace the legacy per-day unique constraint without changing any ID or seal."""
        with self._lock:
            if self.connection.execute("SELECT 1 FROM schema_version WHERE version = 14").fetchone():
                return
            if self._transaction_depth:
                raise RuntimeError("account schema migration requires its own transaction")
            sql = str(self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'todo_instances'"
            ).fetchone()["sql"])
            legacy_unique = r"UNIQUE\s*\(\s*todo_definition_id\s*,\s*period_key\s*\)"
            rebuilt_sql, replacements = re.subn(legacy_unique,
                "UNIQUE(account_id, todo_definition_id, period_key)", sql, flags=re.IGNORECASE)
            self.connection.execute("PRAGMA foreign_keys=OFF")
            try:
                with self._write_scope():
                    if replacements:
                        if replacements != 1:
                            raise ValueError("unexpected Todo identity constraints")
                        objects = self.connection.execute(
                            "SELECT sql FROM sqlite_master WHERE tbl_name = 'todo_instances' "
                            "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
                        ).fetchall()
                        columns = [str(row["name"]) for row in self.connection.execute("PRAGMA table_info(todo_instances)")]
                        column_sql = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
                        rebuilt_sql, renamed = re.subn(
                            r'^(CREATE TABLE(?: IF NOT EXISTS)?\s+)(?:"todo_instances"|todo_instances)(?=\s*\()',
                            r"\1todo_instances_v14", rebuilt_sql, count=1, flags=re.IGNORECASE,
                        )
                        if renamed != 1:
                            raise ValueError("unrecognized legacy Todo table declaration")
                        self.connection.execute(rebuilt_sql)
                        self.connection.execute(f"INSERT INTO todo_instances_v14 ({column_sql}) SELECT {column_sql} FROM todo_instances")
                        self.connection.execute("DROP TABLE todo_instances")
                        self.connection.execute("ALTER TABLE todo_instances_v14 RENAME TO todo_instances")
                        for item in objects:
                            self.connection.execute(str(item["sql"]))
                    unique_scopes = {
                        tuple(column["name"] for column in self.connection.execute(
                            f"PRAGMA index_info({self._quote_identifier(str(index['name']))})"
                        ))
                        for index in self.connection.execute("PRAGMA index_list(todo_instances)")
                        if index["unique"]
                    }
                    if ("account_id", "todo_definition_id", "period_key") not in unique_scopes:
                        raise ValueError("Todo account identity constraint is missing")
                    if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise ValueError("account migration would leave broken foreign keys")
                    self.connection.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (14, _now()))
            finally:
                self.connection.execute("PRAGMA foreign_keys=ON")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        allowed = {
            ("notification_deliveries", "report_html"),
            ("batches", "mode"),
            ("work_items", "artifact_refs_json"),
            ("work_items", "allowed_capability_refs_json"),
            ("work_item_claims", "fencing_token"),
            ("game_runs", "todo_instance_ids_json"),
            ("game_runs", "completed_todo_instance_ids_json"),
            ("game_runs", "completion_todo_instance_ids_json"),
            ("game_runs", "completion_scope_version"),
            ("game_runs", "account_id"),
            ("game_runs", "account_snapshot_json"),
            ("todo_instances", "account_id"),
            ("run_attempts", "account_id"),
            ("todo_instances", "period_starts_at"),
            ("todo_instances", "period_ends_at"),
            ("todo_definitions", "definition_version"),
            ("todo_definitions", "catalog_version"),
            ("todo_definitions", "source_hash"),
            ("run_attempts", "attempt_ordinal"),
            ("run_attempts", "cancel_authority_hash"),
            ("todo_blockers", "manager_id"),
            ("todo_blockers", "game_id"),
            ("batch_cancel_requests", "delivery_attempt_count"),
            ("batch_cancel_requests", "last_delivery_at"),
            ("batch_cancel_requests", "next_retry_at"),
            ("batch_cancel_requests", "last_error_class"),
        }
        if (table, column) not in allowed:
            raise ValueError("unsupported schema migration")
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                if self._public_text_guards_ready:
                    self._refresh_private_fencing_tokens_locked()
                self.connection.execute("BEGIN IMMEDIATE")
                if self._public_text_guards_ready:
                    # Close the window between the preflight refresh and the
                    # write-lock acquisition.  Once BEGIN IMMEDIATE succeeds,
                    # no other writer can publish a new history token first.
                    self._refresh_private_fencing_tokens_locked()
            self._transaction_depth += 1
            try:
                yield
                if outermost:
                    self.connection.execute("COMMIT")
            except Exception as error:
                if outermost:
                    try:
                        if self.connection.in_transaction:
                            self.connection.execute("ROLLBACK")
                    finally:
                        if self._public_text_guards_ready:
                            self._reload_private_fencing_tokens_locked()
                if (
                    isinstance(error, sqlite3.IntegrityError)
                    and str(error) == _PUBLIC_FENCING_REJECTION
                ):
                    raise PublicFencingMaterialRejected(
                        _PUBLIC_FENCING_REJECTION
                    ) from None
                raise
            finally:
                self._transaction_depth -= 1

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Group Manager-owned journal and projection writes in one transaction."""

        with self._write_scope():
            yield

    def run_idempotent(
        self,
        *,
        key: str,
        method: str,
        path: str,
        request: dict[str, Any],
        status_code: int,
        expected_state_version: int | None,
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        # Dedicated fencingToken request members are private authentication
        # inputs, not public data.  Everything else in the mutation plus the
        # public ledger identity must be free of every live or historical grant.
        self._assert_public_values(
            key,
            method,
            path,
            _redact_sensitive_keys(request),
        )
        request_hash = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
        with self._write_scope():
            existing = self.connection.execute(
                "SELECT * FROM idempotency WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["method"] != method
                    or existing["path"] != path
                    or existing["request_hash"] != request_hash
                ):
                    raise IdempotencyConflict(
                        "idempotency key already belongs to a different request"
                    )
                return _decode(existing["response_json"], {}), True

            if expected_state_version is not None:
                current_version = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM events"
                    ).fetchone()[0]
                )
                if current_version != expected_state_version:
                    raise StateVersionConflict(expected_state_version, current_version)

            response = operation()
            if _contains_sensitive_key(response):
                raise ValueError(
                    "idempotency responses must not persist fencing material"
                )
            self._assert_public_values(response)
            self.connection.execute(
                """
                INSERT INTO idempotency(
                    idempotency_key, method, path, request_hash,
                    status_code, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, method, path, request_hash, status_code, _json(response), _now()),
            )
            return response, False

    def get_metadata(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.connection.execute(
                "SELECT value_json FROM metadata WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else _decode(row["value_json"], default)

    def set_metadata(self, key: str, value: Any) -> None:
        self._assert_public_values(key, value)
        with self._write_scope():
            self.connection.execute(
                """
                INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, _json(value), _now()),
            )

    def import_legacy(
        self,
        *,
        config_values: dict[str, Any],
        games: list[dict[str, Any]],
        source_info: dict[str, Any],
        policy_projection: dict[str, Any],
    ) -> bool:
        source_hash = hashlib.sha256(_json(source_info).encode("utf-8")).hexdigest()
        if self.get_metadata("legacy.source_hash") == source_hash:
            return False

        with self._write_scope():
            config_is_empty = (
                self.connection.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 0
            )
            timestamp = _now()
            if config_is_empty:
                for key, value in config_values.items():
                    self.connection.execute(
                        "INSERT INTO config(key, value_json, source, updated_at) VALUES (?, ?, ?, ?)",
                        (key, _json(value), "legacy-import", timestamp),
                    )

            for game in games:
                existing = self.connection.execute(
                    "SELECT game_id FROM games WHERE game_id = ?", (game["game_id"],)
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        """
                        INSERT INTO games(
                            game_id, display_name, order_index, enabled, state,
                            reward_claimed, message, policy_json, updated_at
                        ) VALUES (?, ?, ?, ?, 'unknown', 0, '', ?, ?)
                        """,
                        (
                            game["game_id"],
                            game["display_name"],
                            game["order_index"],
                            1 if game["enabled"] else 0,
                            _json(game.get("policy", {})),
                            timestamp,
                        ),
                    )
                else:
                    self.connection.execute(
                        """
                        UPDATE games SET display_name = ?, policy_json = ?
                        WHERE game_id = ?
                        """,
                        (
                            game["display_name"],
                            _json(game.get("policy", {})),
                            game["game_id"],
                        ),
                    )

            self._upsert_metadata_locked("legacy.sources", source_info, timestamp)
            self._upsert_metadata_locked(
                "legacy.policy_projection", policy_projection, timestamp
            )
            self._upsert_metadata_locked("legacy.source_hash", source_hash, timestamp)
            self.append_event(
                "legacy.imported",
                "manager",
                "legacy",
                {"sources": source_info, "initialConfigImport": config_is_empty},
            )
        return True

    def _upsert_metadata_locked(self, key: str, value: Any, timestamp: str) -> None:
        self._assert_public_values(key, value)
        self.connection.execute(
            """
            INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, _json(value), timestamp),
        )

    def list_games(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM games ORDER BY order_index, game_id"
            ).fetchall()
        return [self._game(row) for row in rows]

    def get_game(self, game_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(game_id)
        return self._game(row)

    @staticmethod
    def _game(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "game_id": row["game_id"],
            "display_name": row["display_name"],
            "order_index": row["order_index"],
            "enabled": bool(row["enabled"]),
            "state": row["state"],
            "reward_claimed": bool(row["reward_claimed"]),
            "message": row["message"],
            "updated_at": row["updated_at"],
            "policy": _decode(row["policy_json"], {}),
        }

    def sync_todo_definitions(self, definitions: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert the built-in catalog without changing existing instances."""

        timestamp = _now()
        catalog_ids = {str(item["todo_definition_id"]) for item in definitions}
        changed = 0
        inserted = 0
        with self._write_scope():
            known_games = {
                str(row["game_id"])
                for row in self.connection.execute("SELECT game_id FROM games").fetchall()
            }
            for item in definitions:
                if item["game_id"] not in known_games:
                    raise ValueError(f"todo catalog references unknown GameId: {item['game_id']}")
                record = {
                    **item,
                    "active": True,
                }
                row = self.connection.execute(
                    "SELECT * FROM todo_definitions WHERE todo_definition_id = ?",
                    (record["todo_definition_id"],),
                ).fetchone()
                existing = self._todo_definition(row) if row is not None else None
                comparable = {
                    key: value
                    for key, value in record.items()
                    if key != "updated_at"
                }
                if existing is not None:
                    existing = {
                        key: value
                        for key, value in existing.items()
                        if key != "updated_at"
                    }
                if existing == comparable:
                    continue
                if row is None:
                    inserted += 1
                changed += 1
                self.connection.execute(
                    """
                    INSERT INTO todo_definitions(
                        todo_definition_id, definition_version, catalog_version,
                        source_hash, game_id, cadence, operation, title, category,
                        order_index, required, risk,
                        automation_difficulty, adapter_capability_ref,
                        automation_state, initial_status, initial_reason,
                        reset_rule_json, source_refs_json, active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(todo_definition_id) DO UPDATE SET
                        definition_version = excluded.definition_version,
                        catalog_version = excluded.catalog_version,
                        source_hash = excluded.source_hash,
                        game_id = excluded.game_id,
                        cadence = excluded.cadence,
                        operation = excluded.operation,
                        title = excluded.title,
                        category = excluded.category,
                        order_index = excluded.order_index,
                        required = excluded.required,
                        risk = excluded.risk,
                        automation_difficulty = excluded.automation_difficulty,
                        adapter_capability_ref = excluded.adapter_capability_ref,
                        automation_state = excluded.automation_state,
                        initial_status = excluded.initial_status,
                        initial_reason = excluded.initial_reason,
                        reset_rule_json = excluded.reset_rule_json,
                        source_refs_json = excluded.source_refs_json,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        record["todo_definition_id"],
                        int(record["definition_version"]),
                        record["catalog_version"],
                        record["source_hash"],
                        record["game_id"],
                        record["cadence"],
                        record["operation"],
                        record["title"],
                        record["category"],
                        int(record["order_index"]),
                        int(bool(record["required"])),
                        record["risk"],
                        record["automation_difficulty"],
                        record.get("adapter_capability_ref"),
                        record["automation_state"],
                        record["initial_status"],
                        record.get("initial_reason", ""),
                        _json(record["reset_rule"]),
                        _json(record.get("source_refs", [])),
                        1,
                        timestamp,
                    ),
                )
            existing_catalog_rows = self.connection.execute(
                "SELECT todo_definition_id FROM todo_definitions WHERE todo_definition_id LIKE 'todo.v1.%' AND active = 1"
            ).fetchall()
            retired = 0
            for row in existing_catalog_rows:
                definition_id = str(row["todo_definition_id"])
                if definition_id in catalog_ids:
                    continue
                self.connection.execute(
                    "UPDATE todo_definitions SET active = 0, updated_at = ? WHERE todo_definition_id = ?",
                    (timestamp, definition_id),
                )
                retired += 1
            if changed or retired:
                self.append_event(
                    "todo.catalog-synced",
                    "todo-catalog",
                    "built-in-v1",
                    {
                        "definitionCount": len(definitions),
                        "changedCount": changed,
                        "insertedCount": inserted,
                        "retiredCount": retired,
                    },
                )
        return {
            "definition_count": len(definitions),
            "changed_count": changed,
            "inserted_count": inserted,
            "retired_count": retired,
        }

    def list_todo_definitions(
        self,
        *,
        game_id: str | None = None,
        cadence: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if game_id is not None:
            clauses.append("game_id = ?")
            parameters.append(game_id)
        if cadence is not None:
            clauses.append("cadence = ?")
            parameters.append(cadence)
        if active_only:
            clauses.append("active = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM todo_definitions{where} ORDER BY game_id, cadence, order_index, todo_definition_id",
                tuple(parameters),
            ).fetchall()
        return [self._todo_definition(row) for row in rows]

    def get_todo_definition(self, todo_definition_id: str) -> dict[str, Any]:
        return self._one(
            "todo_definitions",
            "todo_definition_id",
            todo_definition_id,
            self._todo_definition,
        )

    @staticmethod
    def _todo_definition(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "todo_definition_id": row["todo_definition_id"],
            "definition_version": int(row["definition_version"]),
            "catalog_version": row["catalog_version"],
            "source_hash": row["source_hash"],
            "game_id": row["game_id"],
            "cadence": row["cadence"],
            "operation": row["operation"],
            "title": row["title"],
            "category": row["category"],
            "order_index": int(row["order_index"]),
            "required": bool(row["required"]),
            "risk": row["risk"],
            "automation_difficulty": row["automation_difficulty"],
            "adapter_capability_ref": row["adapter_capability_ref"],
            "automation_state": row["automation_state"],
            "initial_status": row["initial_status"],
            "initial_reason": row["initial_reason"],
            "reset_rule": _decode(row["reset_rule_json"], {}),
            "source_refs": _decode(row["source_refs_json"], []),
            "active": bool(row["active"]),
            "updated_at": row["updated_at"],
        }

    def reconcile_todo_instances(
        self,
        instances: list[dict[str, Any]],
        *,
        intent: str,
        requested_by: str,
        reason: str,
    ) -> dict[str, Any]:
        timestamp = _now()
        created_ids: list[str] = []
        existing_ids: list[str] = []
        refreshed_ids: list[str] = []
        reset_ids: list[str] = []
        normalized_conditional_ids: list[str] = []
        retired_blocker_ids: list[str] = []
        scoped_periods: dict[tuple[str, str, str], set[str]] = {}
        for item in instances:
            scoped_periods.setdefault(
                (str(item["game_id"]), str(item.get("account_id", "default")), str(item["cadence"])), set()
            ).add(str(item["period_key"]))
        with self._write_scope():
            for item in instances:
                row = self.connection.execute(
                    "SELECT * FROM todo_instances WHERE account_id = ? AND todo_definition_id = ? AND period_key = ?",
                    (item.get("account_id", "default"), item["todo_definition_id"], item["period_key"]),
                ).fetchone()
                if row is not None:
                    todo_instance_id = str(row["todo_instance_id"])
                    existing_ids.append(todo_instance_id)
                    if intent == "reset":
                        if str(row["status"]) == "in_progress":
                            raise ValueError(
                                "cannot explicitly reset an in-progress Todo instance"
                            )
                        prior_evidence = _decode(row["evidence_refs_json"], [])
                        self.connection.execute(
                            """
                            UPDATE todo_instances SET
                                definition_snapshot_json = ?, status = ?,
                                reason = ?, evidence_refs_json = '[]',
                                run_id = NULL, started_at = NULL,
                                completed_at = NULL, last_attempt_at = NULL,
                                updated_at = ?
                            WHERE todo_instance_id = ?
                            """,
                            (
                                _json(dict(item["definition_snapshot"])),
                                item["status"],
                                item.get("reason", ""),
                                timestamp,
                                todo_instance_id,
                            ),
                        )
                        reset_ids.append(todo_instance_id)
                        self.append_event(
                            "todo.current-period-explicit-reset",
                            "todo-instance",
                            todo_instance_id,
                            {
                                "requestedBy": requested_by,
                                "reason": reason,
                                "priorStatus": str(row["status"]),
                                "priorRunId": row["run_id"],
                                "priorEvidenceRefs": prior_evidence,
                                "resetStatus": item["status"],
                                "periodKey": item["period_key"],
                            },
                        )
                        continue
                    latest_attempt = self.connection.execute(
                        """
                        SELECT * FROM todo_attempts
                        WHERE todo_instance_id = ?
                        ORDER BY attempt_number DESC LIMIT 1
                        """,
                        (todo_instance_id,),
                    ).fetchone()
                    if (
                        str(row["status"]) == "skipped"
                        and latest_attempt is not None
                        and str(latest_attempt["state"]) == "skipped"
                        and str(latest_attempt["reason_code"])
                        == "upstream_stage_not_needed"
                        and not bool(latest_attempt["retryable"])
                        and bool(_decode(latest_attempt["evidence_refs_json"], []))
                    ):
                        # A selected conditional daily that the upstream tool
                        # proved was not needed today is a satisfied obligation,
                        # not a permanently unresolved skip.  Preserve the exact
                        # attempt-owned evidence and reason while normalizing the
                        # current-period aggregate fact.
                        self.connection.execute(
                            """
                            UPDATE todo_instances SET status = 'completed',
                                completed_at = ?, updated_at = ?
                            WHERE todo_instance_id = ? AND status = 'skipped'
                            """,
                            (timestamp, timestamp, todo_instance_id),
                        )
                        normalized_conditional_ids.append(todo_instance_id)
                    current_snapshot = _decode(row["definition_snapshot_json"], {})
                    candidate_snapshot = dict(item["definition_snapshot"])
                    # A current-period instance may safely adopt a repaired
                    # catalog definition only while it is still pristine.  A
                    # run/attempt/evidence makes the old snapshot an immutable
                    # audit fact and the new definition waits for the next
                    # period instead.
                    if (
                        current_snapshot.get("source_hash")
                        != candidate_snapshot.get("source_hash")
                        and int(row["attempts"]) == 0
                        and row["run_id"] is None
                        and not _decode(row["evidence_refs_json"], [])
                        and str(row["status"])
                        not in {"completed", "in_progress", "human_required"}
                    ):
                        self.connection.execute(
                            """
                            UPDATE todo_instances SET
                                definition_snapshot_json = ?, status = ?,
                                reason = ?, started_at = NULL,
                                completed_at = NULL, last_attempt_at = NULL,
                                updated_at = ?
                            WHERE todo_instance_id = ?
                            """,
                            (
                                _json(candidate_snapshot),
                                item["status"],
                                item.get("reason", ""),
                                timestamp,
                                todo_instance_id,
                            ),
                        )
                        refreshed_ids.append(todo_instance_id)
                    continue
                self.connection.execute(
                    """
                    INSERT INTO todo_instances(
                        todo_instance_id, todo_definition_id, game_id, cadence, account_id,
                        period_key, period_starts_at, period_ends_at,
                        definition_snapshot_json, status, attempts,
                        reason, evidence_refs_json, run_id, started_at,
                        completed_at, last_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '[]', NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        item["todo_instance_id"],
                        item["todo_definition_id"],
                        item["game_id"],
                        item["cadence"],
                        item.get("account_id", "default"),
                        item["period_key"],
                        item["period_starts_at"],
                        item["period_ends_at"],
                        _json(item["definition_snapshot"]),
                        item["status"],
                        item.get("reason", ""),
                        timestamp,
                        timestamp,
                    ),
                )
                created_ids.append(str(item["todo_instance_id"]))
            # A blocker belongs to one immutable Todo period.  Once Manager has
            # reconciled a newer period for the same game/cadence, keeping the
            # old blocker "active" pollutes the live execution-control summary
            # and the Today UI.  Retire it as history without creating an
            # explicit human release: an old human/login gate must never become
            # authorization to resume its predecessor run.
            if scoped_periods:
                blocker_rows = self.connection.execute(
                    """
                    SELECT tb.*, ti.game_id AS todo_game_id,
                           ti.account_id, ti.cadence, ti.period_key
                    FROM todo_blockers AS tb
                    JOIN todo_instances AS ti
                      ON ti.todo_instance_id = tb.todo_instance_id
                    WHERE tb.state = 'active'
                    ORDER BY tb.transitioned_at, tb.blocker_id
                    """
                ).fetchall()
                reset_id_set = set(reset_ids)
                for blocker_row in blocker_rows:
                    scope = (
                        str(blocker_row["todo_game_id"]),
                        str(blocker_row["account_id"]),
                        str(blocker_row["cadence"]),
                    )
                    current_periods = scoped_periods.get(scope)
                    if current_periods is None:
                        continue
                    todo_instance_id = str(blocker_row["todo_instance_id"])
                    explicitly_reset = todo_instance_id in reset_id_set
                    period_expired = str(blocker_row["period_key"]) not in current_periods
                    if not (explicitly_reset or period_expired):
                        continue
                    blocker_id = str(blocker_row["blocker_id"])
                    resolution_code = (
                        "todo_explicitly_reset"
                        if explicitly_reset
                        else "todo_period_expired"
                    )
                    resolution_reason = (
                        "current-period Todo was explicitly reset; prior blocker retained as history"
                        if explicitly_reset
                        else "Todo period expired; prior blocker retained as history"
                    )
                    cursor = self.connection.execute(
                        """
                        UPDATE todo_blockers SET state = 'resolved',
                            resolved_at = ?, resolution_code = ?,
                            resolution_reason = ?,
                            resolution_artifact_refs_json = '[]',
                            release_id = NULL, release_explicit = 0,
                            released_by = NULL, transitioned_at = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE blocker_id = ? AND state = 'active'
                        """,
                        (
                            timestamp,
                            resolution_code,
                            resolution_reason,
                            timestamp,
                            timestamp,
                            blocker_id,
                        ),
                    )
                    if cursor.rowcount == 1:
                        retired_blocker_ids.append(blocker_id)
                        record = self.get_todo_blocker(blocker_id)
                        self.append_event(
                            "todo-blocker.period-retired",
                            "todo-blocker",
                            blocker_id,
                            {
                                **record,
                                "retirementIntent": intent,
                                "explicitHumanRelease": False,
                            },
                        )
            self.append_event(
                "todo.reset-reconciled" if intent == "reset" else "todo.reconciled",
                "todo-period",
                str(uuid.uuid4()),
                {
                    "intent": intent,
                    "requestedBy": requested_by,
                    "reason": reason,
                    "createdTodoInstanceIds": created_ids,
                    "existingTodoInstanceIds": existing_ids,
                    "refreshedTodoInstanceIds": refreshed_ids,
                    "resetTodoInstanceIds": reset_ids,
                    "normalizedConditionalTodoInstanceIds": normalized_conditional_ids,
                    "retiredTodoBlockerIds": retired_blocker_ids,
                },
            )
        return {
            "created_todo_instance_ids": created_ids,
            "existing_todo_instance_ids": existing_ids,
            "refreshed_todo_instance_ids": refreshed_ids,
            "reset_todo_instance_ids": reset_ids,
            "normalized_conditional_todo_instance_ids": normalized_conditional_ids,
            "retired_todo_blocker_ids": retired_blocker_ids,
        }

    def list_todo_instances(
        self,
        *,
        game_id: str | None = None,
        account_id: str | None = None,
        cadence: str | None = None,
        period_key: str | None = None,
        status: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("ti.game_id", game_id),
            ("ti.account_id", account_id),
            ("ti.cadence", cadence),
            ("ti.period_key", period_key),
            ("ti.status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT ti.*
                FROM todo_instances AS ti
                JOIN games AS g ON g.game_id = ti.game_id
                JOIN todo_definitions AS td
                  ON td.todo_definition_id = ti.todo_definition_id
                {where}
                ORDER BY g.order_index,
                         CASE ti.cadence WHEN 'daily' THEN 0 ELSE 1 END,
                         ti.period_key DESC,
                         td.order_index,
                         ti.todo_definition_id
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [self._todo_instance(row) for row in rows]

    def get_todo_instance(self, todo_instance_id: str) -> dict[str, Any]:
        return self._one(
            "todo_instances",
            "todo_instance_id",
            todo_instance_id,
            self._todo_instance,
        )

    @staticmethod
    def _todo_instance(row: sqlite3.Row) -> dict[str, Any]:
        definition = _decode(row["definition_snapshot_json"], {})
        return {
            "todo_instance_id": row["todo_instance_id"],
            "account_id": row["account_id"],
            "todo_definition_id": row["todo_definition_id"],
            "definition_version": int(definition.get("definition_version", 1)),
            "catalog_version": definition.get("catalog_version", "legacy-v1"),
            "source_hash": definition.get(
                "source_hash",
                "0000000000000000000000000000000000000000000000000000000000000000",
            ),
            "game_id": row["game_id"],
            "cadence": row["cadence"],
            "period_key": row["period_key"],
            "period_starts_at": row["period_starts_at"],
            "period_ends_at": row["period_ends_at"],
            "operation": definition.get("operation", ""),
            "title": definition.get("title", ""),
            "category": definition.get("category", ""),
            "order_index": int(definition.get("order_index", 0)),
            "required": bool(definition.get("required", False)),
            "risk": definition.get("risk", "approval_required"),
            "automation_difficulty": definition.get("automation_difficulty", "unknown"),
            "adapter_capability_ref": definition.get("adapter_capability_ref"),
            "automation_state": definition.get("automation_state", "unknown"),
            "reset_rule": definition.get("reset_rule", {}),
            "source_refs": definition.get("source_refs", []),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "reason": row["reason"],
            "evidence_refs": _decode(row["evidence_refs_json"], []),
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "last_attempt_at": row["last_attempt_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def transition_todo_instance(
        self,
        todo_instance_id: str,
        *,
        status: str,
        reason: str,
        evidence_refs: list[str],
        run_id: str | None,
        increment_attempt: bool,
        requested_by: str,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            before = self.get_todo_instance(todo_instance_id)
            if run_id is not None:
                run = self.get_game_run(run_id)
                if (run["game_id"], run["account_id"], run["cadence"]) != (
                    before["game_id"], before["account_id"], before["cadence"]
                ):
                    raise ValueError("Todo transition account scope differs from its GameRun")
            if status not in _TODO_TRANSITIONS.get(before["status"], frozenset()):
                raise ValueError(
                    f"invalid Todo transition: {before['status']} -> {status}"
                )
            if status in {
                "skipped",
                "blocked",
                "review_required",
                "human_required",
            } and not reason.strip():
                raise ValueError(f"{status} requires a reason")
            merged_evidence = list(before["evidence_refs"])
            for evidence_ref in evidence_refs:
                if evidence_ref not in merged_evidence:
                    merged_evidence.append(evidence_ref)
            if status == "completed" and not merged_evidence:
                raise ValueError("completed todo items require at least one evidenceRef")
            should_increment = increment_attempt or (
                status == "in_progress" and before["status"] != "in_progress"
            )
            attempts = int(before["attempts"]) + (1 if should_increment else 0)
            started_at = before["started_at"]
            if status == "in_progress" and started_at is None:
                started_at = timestamp
            completed_at = timestamp if status == "completed" else None
            last_attempt_at = timestamp if should_increment else before["last_attempt_at"]
            self.connection.execute(
                """
                UPDATE todo_instances SET
                    status = ?, attempts = ?, reason = ?, evidence_refs_json = ?,
                    run_id = COALESCE(?, run_id), started_at = ?, completed_at = ?,
                    last_attempt_at = ?, updated_at = ?
                WHERE todo_instance_id = ?
                """,
                (
                    status,
                    attempts,
                    reason,
                    _json(merged_evidence),
                    run_id,
                    started_at,
                    completed_at,
                    last_attempt_at,
                    timestamp,
                    todo_instance_id,
                ),
            )
            after = self.get_todo_instance(todo_instance_id)
            self.append_event(
                "todo.status-transitioned",
                "todo-instance",
                todo_instance_id,
                {
                    "requestedBy": requested_by,
                    "fromStatus": before["status"],
                    "toStatus": after["status"],
                    "attempts": after["attempts"],
                    "reason": reason,
                    "evidenceRefs": merged_evidence,
                    "runId": after["run_id"],
                },
            )
        return after

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT key, value_json, updated_at FROM config ORDER BY key"
            ).fetchall()
        return {
            "values": {row["key"]: _decode(row["value_json"], None) for row in rows},
            "allowed_game_ids": [game["game_id"] for game in self.list_games()],
            "legacy_sources": self.get_metadata("legacy.sources", {}),
            "updated_at": max((row["updated_at"] for row in rows), default=None),
        }

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            current = self.get_config()["values"]
            if patch.get("enabled") is not None:
                enabled = dict(current.get("enabled", {}))
                enabled.update(patch["enabled"])
                patch = {**patch, "enabled": enabled}
            if patch.get("game_paths") is not None:
                game_paths = dict(current.get("game_paths", {}))
                game_paths.update(patch["game_paths"])
                patch = {**patch, "game_paths": game_paths}
            if patch.get("daily_tool_profiles") is not None:
                profiles = dict(current.get("daily_tool_profiles", {}))
                profiles.update(patch["daily_tool_profiles"])
                patch = {**patch, "daily_tool_profiles": profiles}
            if patch.get("daily_todo_selection") is not None:
                selection = dict(current.get("daily_todo_selection", {}))
                selection.update(patch["daily_todo_selection"])
                patch = {**patch, "daily_todo_selection": selection}
            for key, value in patch.items():
                self.connection.execute(
                    """
                    INSERT INTO config(key, value_json, source, updated_at) VALUES (?, ?, 'manager', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        source = 'manager',
                        updated_at = excluded.updated_at
                    """,
                    (key, _json(value), timestamp),
                )
            if patch.get("enabled") is not None:
                for game_id, enabled in patch["enabled"].items():
                    self.connection.execute(
                        "UPDATE games SET enabled = ?, updated_at = ? WHERE game_id = ?",
                        (1 if enabled else 0, timestamp, game_id),
                    )
            if patch.get("order") is not None:
                for index, game_id in enumerate(patch["order"]):
                    self.connection.execute(
                        "UPDATE games SET order_index = ?, updated_at = ? WHERE game_id = ?",
                        (index, timestamp, game_id),
                    )
            self.append_event("config.updated", "config", "global", {"changes": patch})
        return self.get_config()

    def create_batch(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        batch_id = str(data.get("batch_id") or uuid.uuid4())
        root_batch_id = str(data.get("root_batch_id") or batch_id)
        predecessor_batch_id = data.get("predecessor_batch_id")
        continuation_ordinal = int(data.get("continuation_ordinal", 0))
        resume_intent_id = data.get("resume_intent_id")
        if continuation_ordinal < 0:
            raise ValueError("continuation ordinal cannot be negative")
        if continuation_ordinal == 0 and predecessor_batch_id is not None:
            raise ValueError("root batches cannot have a predecessor")
        if continuation_ordinal == 0 and root_batch_id != batch_id:
            raise ValueError("root batches must identify themselves as the lineage root")
        if continuation_ordinal > 0 and not predecessor_batch_id:
            raise ValueError("continuation batches require a predecessor")
        record = {
            "batch_id": batch_id,
            "cadence": data["cadence"],
            "mode": str(data.get("mode", "plan")),
            "state": data["state"],
            "game_ids": data["game_ids"],
            "requested_by": data["requested_by"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "result": data.get("result", {}),
        }
        with self._write_scope():
            if predecessor_batch_id is not None:
                predecessor = self.get_batch(str(predecessor_batch_id))
                if predecessor["result"].get("sealVersion") is None:
                    raise ValueError("continuation predecessor must already be sealed")
                if predecessor["root_batch_id"] != root_batch_id:
                    raise ValueError("continuation root differs from predecessor lineage")
                if continuation_ordinal != int(predecessor["continuation_ordinal"]) + 1:
                    raise ValueError("continuation ordinal is not the predecessor successor")
            self.connection.execute(
                """
                INSERT INTO batches(
                    batch_id, cadence, mode, state, game_ids_json,
                    requested_by, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["batch_id"],
                    record["cadence"],
                    record["mode"],
                    record["state"],
                    _json(record["game_ids"]),
                    record["requested_by"],
                    _json(record["result"]),
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO batch_lineage(
                    batch_id, root_batch_id, predecessor_batch_id,
                    continuation_ordinal, resume_intent_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    root_batch_id,
                    predecessor_batch_id,
                    continuation_ordinal,
                    resume_intent_id,
                    timestamp,
                ),
            )
            record = self.get_batch(batch_id)
            self.append_event(
                "batch.created",
                "batch",
                record["batch_id"],
                _batch_event_payload(record),
            )
        return record

    def create_or_get_continuation_batch(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Create the one continuation owned by a durable resume intent."""

        resume_intent_id = str(data["resume_intent_id"])
        predecessor_batch_id = str(data["predecessor_batch_id"])
        run_id = str(data["run_id"])
        with self._write_scope():
            existing = self.connection.execute(
                "SELECT batch_id FROM batch_lineage WHERE resume_intent_id = ?",
                (resume_intent_id,),
            ).fetchone()
            if existing is not None:
                batch = self.get_batch(str(existing["batch_id"]))
                if batch["predecessor_batch_id"] != predecessor_batch_id:
                    raise ValueError("resume intent is already bound to another predecessor")
                membership = self.get_batch_run_membership(
                    batch["batch_id"], run_id
                )
                if membership["resume_intent_id"] != resume_intent_id:
                    raise ValueError("resume continuation membership diverged")
                return batch, True

            predecessor = self.get_batch(predecessor_batch_id)
            if predecessor["result"].get("sealVersion") is None:
                raise ValueError("continuation predecessor must already be sealed")
            run = self.get_game_run(run_id)
            account_id = run["account_id"]
            account_targets = predecessor["result"].get("accountTargets")
            if isinstance(account_targets, list):
                if not any(isinstance(target, dict)
                           and target.get("gameId") == run["game_id"]
                           and target.get("accountId", "default") == account_id
                           for target in account_targets):
                    raise ValueError("continuation account is outside predecessor scope")
            elif account_id != "default":
                raise ValueError("continuation predecessor has no frozen account scope")
            continuation_targets = data.get("result", {}).get("accountTargets")
            if continuation_targets is not None and (
                not isinstance(continuation_targets, list)
                or len(continuation_targets) != 1
                or not isinstance(continuation_targets[0], dict)
                or continuation_targets[0].get("gameId") != run["game_id"]
                or continuation_targets[0].get("accountId", "default") != account_id
            ):
                raise ValueError("continuation target differs from its GameRun account")
            if run["game_id"] not in set(predecessor["game_ids"]):
                # Older batches could have candidate scope only in their seal.
                candidates = set(
                    predecessor["result"].get("candidateGameIds", [])
                )
                if run["game_id"] not in candidates:
                    raise ValueError("continuation run is outside predecessor scope")
            batch = self.create_batch(
                {
                    "cadence": predecessor["cadence"],
                    "mode": "execute",
                    "state": data.get("state", "pending_execution"),
                    "game_ids": [run["game_id"]],
                    "requested_by": data["requested_by"],
                    "result": dict(data.get("result", {})),
                    "root_batch_id": predecessor["root_batch_id"],
                    "predecessor_batch_id": predecessor_batch_id,
                    "continuation_ordinal": (
                        int(predecessor["continuation_ordinal"]) + 1
                    ),
                    "resume_intent_id": resume_intent_id,
                }
            )
            self.add_batch_run_membership(
                batch["batch_id"],
                run_id,
                ordinal=0,
                role="continuation",
                state="resume_pending",
                resume_intent_id=resume_intent_id,
            )
            return self.get_batch(batch["batch_id"]), False

    def add_batch_run_membership(
        self,
        batch_id: str,
        run_id: str,
        *,
        ordinal: int,
        role: str,
        state: str = "queued",
        resume_intent_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            batch = self.get_batch(batch_id)
            if batch["result"].get("sealVersion") is not None:
                raise ValueError("sealed batch membership is immutable")
            self.get_game_run(run_id)
            existing = self.connection.execute(
                """
                SELECT * FROM batch_run_memberships
                WHERE batch_id = ? AND run_id = ?
                """,
                (batch_id, run_id),
            ).fetchone()
            if existing is not None:
                record = self._batch_run_membership(existing)
                expected = (ordinal, role, state, resume_intent_id)
                actual = (
                    record["ordinal"],
                    record["role"],
                    record["state"],
                    record["resume_intent_id"],
                )
                if actual != expected:
                    raise ValueError("batch run membership already has another identity")
                return record
            self.connection.execute(
                """
                INSERT INTO batch_run_memberships(
                    batch_id, run_id, ordinal, role, state, resume_intent_id,
                    latest_run_attempt_id, terminal_outcome, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    batch_id,
                    run_id,
                    ordinal,
                    role,
                    state,
                    resume_intent_id,
                    timestamp,
                    timestamp,
                ),
            )
            record = self.get_batch_run_membership(batch_id, run_id)
            self.append_event(
                "batch-run-membership.created",
                "batch",
                batch_id,
                record,
            )
            return record

    @staticmethod
    def _batch_run_membership(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": row["batch_id"],
            "run_id": row["run_id"],
            "ordinal": int(row["ordinal"]),
            "role": row["role"],
            "state": row["state"],
            "resume_intent_id": row["resume_intent_id"],
            "latest_run_attempt_id": row["latest_run_attempt_id"],
            "terminal_outcome": row["terminal_outcome"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_batch_run_membership(
        self, batch_id: str, run_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM batch_run_memberships
                WHERE batch_id = ? AND run_id = ?
                """,
                (batch_id, run_id),
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"{batch_id}:{run_id}")
        return self._batch_run_membership(row)

    def list_batch_run_memberships(
        self,
        *,
        batch_id: str | None = None,
        run_id: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if batch_id is not None:
            clauses.append("batch_id = ?")
            parameters.append(batch_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT * FROM batch_run_memberships{where}
                ORDER BY batch_id, ordinal, created_at LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [self._batch_run_membership(row) for row in rows]

    def update_batch_run_membership(
        self,
        batch_id: str,
        run_id: str,
        *,
        state: str,
        resume_intent_id: str | None = None,
        latest_run_attempt_id: str | None = None,
        terminal_outcome: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        allowed = {
            "queued": {
                "queued",
                "resume_pending",
                "active",
                "terminal",
                "cancelled",
                "reconciliation_required",
            },
            "resume_pending": {
                "resume_pending",
                "active",
                "cancelled",
                "reconciliation_required",
                "terminal",
            },
            "active": {"active", "terminal", "cancelled", "reconciliation_required"},
            "reconciliation_required": {
                "reconciliation_required",
                "resume_pending",
                "active",
                "terminal",
                "cancelled",
            },
            # A terminal member may only be reopened by the Manager's typed
            # same-GameRun resume flow.  The run identity is preserved; a new
            # GameRun must never be substituted into the Batch.
            "terminal": {"terminal", "resume_pending"},
            "cancelled": {"cancelled"},
        }
        with self._write_scope():
            batch = self.get_batch(batch_id)
            if batch["result"].get("sealVersion") is not None:
                raise ValueError("sealed batch membership is immutable")
            before = self.get_batch_run_membership(batch_id, run_id)
            if state not in allowed.get(str(before["state"]), set()):
                raise ValueError("batch run membership transition is invalid")
            resolved_latest_attempt_id = (
                latest_run_attempt_id
                if latest_run_attempt_id is not None
                else before["latest_run_attempt_id"]
            )
            resolved_resume_intent_id = (
                resume_intent_id
                if resume_intent_id is not None
                else before["resume_intent_id"]
            )
            resolved_terminal_outcome = (
                None
                if state == "resume_pending"
                else terminal_outcome
                if terminal_outcome is not None
                else before["terminal_outcome"]
            )
            self.connection.execute(
                """
                UPDATE batch_run_memberships SET state = ?,
                    resume_intent_id = ?, latest_run_attempt_id = ?,
                    terminal_outcome = ?, updated_at = ?
                WHERE batch_id = ? AND run_id = ?
                """,
                (
                    state,
                    resolved_resume_intent_id,
                    resolved_latest_attempt_id,
                    resolved_terminal_outcome,
                    timestamp,
                    batch_id,
                    run_id,
                ),
            )
            record = self.get_batch_run_membership(batch_id, run_id)
            if record != before:
                self.append_event(
                    "batch-run-membership.updated", "batch", batch_id, record
                )
            return record

    @staticmethod
    def _batch_cancel_request(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cancel_request_id": row["cancel_request_id"],
            "batch_id": row["batch_id"],
            "state": row["state"],
            "reason": row["reason"],
            "requested_by": row["requested_by"],
            "active_run_attempt_ids": _decode(
                row["active_run_attempt_ids_json"], []
            ),
            "work_item_id": row["work_item_id"],
            "delivery_attempt_count": int(row["delivery_attempt_count"]),
            "last_delivery_at": row["last_delivery_at"],
            "next_retry_at": row["next_retry_at"],
            "last_error_class": row["last_error_class"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_or_get_batch_cancel_request(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        timestamp = _now()
        batch_id = str(data["batch_id"])
        with self._write_scope():
            batch = self.get_batch(batch_id)
            if batch["result"].get("sealVersion") is not None:
                raise ValueError("sealed batches cannot accept cancellation")
            existing = self.connection.execute(
                """
                SELECT * FROM batch_cancel_requests WHERE batch_id = ?
                    AND state IN (
                        'requested', 'signal_delivered',
                        'reconciliation_required', 'acknowledged'
                    )
                ORDER BY created_at DESC LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
            if existing is not None:
                return self._batch_cancel_request(existing), True
            request_id = str(data.get("cancel_request_id") or uuid.uuid4())
            active_ids = list(dict.fromkeys(data.get("active_run_attempt_ids", [])))
            self.connection.execute(
                """
                INSERT INTO batch_cancel_requests(
                    cancel_request_id, batch_id, state, reason, requested_by,
                    active_run_attempt_ids_json, work_item_id, created_at, updated_at
                ) VALUES (?, ?, 'requested', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    request_id,
                    batch_id,
                    str(data["reason"]),
                    str(data["requested_by"]),
                    _json(active_ids),
                    timestamp,
                    timestamp,
                ),
            )
            record = self.get_batch_cancel_request(request_id)
            self.append_event(
                "batch-cancel.requested", "batch", batch_id, record
            )
            return record, False

    def get_batch_cancel_request(self, cancel_request_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM batch_cancel_requests WHERE cancel_request_id = ?",
                (cancel_request_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(cancel_request_id)
        return self._batch_cancel_request(row)

    def get_active_batch_cancel_request(
        self, batch_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM batch_cancel_requests WHERE batch_id = ?
                    AND state IN (
                        'requested', 'signal_delivered',
                        'reconciliation_required', 'acknowledged'
                    )
                ORDER BY created_at DESC LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
        return self._batch_cancel_request(row) if row is not None else None

    def list_batch_cancel_requests(
        self, *, batch_id: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        parameters: tuple[Any, ...]
        if batch_id is None:
            query = (
                "SELECT * FROM batch_cancel_requests "
                "ORDER BY created_at DESC LIMIT ?"
            )
            parameters = (limit,)
        else:
            query = (
                "SELECT * FROM batch_cancel_requests WHERE batch_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            parameters = (batch_id, limit)
        with self._lock:
            rows = self.connection.execute(query, parameters).fetchall()
        return [self._batch_cancel_request(row) for row in rows]

    def update_batch_cancel_request(
        self,
        cancel_request_id: str,
        *,
        state: str,
        active_run_attempt_ids: list[str] | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        transitions = {
            "requested": {
                "requested", "signal_delivered", "reconciliation_required",
                "acknowledged", "sealed",
            },
            "signal_delivered": {
                "signal_delivered", "reconciliation_required",
                "acknowledged", "sealed",
            },
            "reconciliation_required": {
                "reconciliation_required", "acknowledged", "sealed",
            },
            "acknowledged": {"acknowledged", "sealed"},
            "sealed": {"sealed"},
        }
        with self._write_scope():
            before = self.get_batch_cancel_request(cancel_request_id)
            if state not in transitions.get(str(before["state"]), set()):
                raise ValueError("batch cancellation transition is invalid")
            attempt_ids = (
                before["active_run_attempt_ids"]
                if active_run_attempt_ids is None
                else list(dict.fromkeys(active_run_attempt_ids))
            )
            self.connection.execute(
                """
                UPDATE batch_cancel_requests SET state = ?,
                    active_run_attempt_ids_json = ?,
                    work_item_id = COALESCE(?, work_item_id), updated_at = ?
                WHERE cancel_request_id = ?
                """,
                (
                    state,
                    _json(attempt_ids),
                    work_item_id,
                    timestamp,
                    cancel_request_id,
                ),
            )
            record = self.get_batch_cancel_request(cancel_request_id)
            if record != before:
                self.append_event(
                    "batch-cancel.updated", "batch", record["batch_id"], record
                )
            return record

    def record_batch_cancel_delivery_attempt(
        self,
        cancel_request_id: str,
        *,
        error_class: str = "",
        retry_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist every cooperative-cancel delivery attempt before retry/repair."""

        timestamp = _now()
        bounded_error = str(error_class)[:160]
        with self._write_scope():
            before = self.get_batch_cancel_request(cancel_request_id)
            if before["state"] != "requested":
                return before
            self.connection.execute(
                """
                UPDATE batch_cancel_requests
                SET delivery_attempt_count = delivery_attempt_count + 1,
                    last_delivery_at = ?, next_retry_at = ?,
                    last_error_class = ?, updated_at = ?
                WHERE cancel_request_id = ? AND state = 'requested'
                """,
                (
                    timestamp,
                    retry_at,
                    bounded_error,
                    timestamp,
                    cancel_request_id,
                ),
            )
            record = self.get_batch_cancel_request(cancel_request_id)
            self.append_event(
                "batch-cancel.delivery-attempted",
                "batch",
                record["batch_id"],
                {
                    "cancelRequestId": cancel_request_id,
                    "attemptNumber": record["delivery_attempt_count"],
                    "outcome": "retry_scheduled" if retry_at else (
                        "failed" if bounded_error else "delivered"
                    ),
                    "nextRetryAt": retry_at,
                    "errorClass": bounded_error,
                },
            )
            return record

    def list_batches(
        self, limit: int = 100, *, created_after: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if created_after is not None:
                rows = self.connection.execute(
                    "SELECT * FROM batches WHERE created_at >= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (created_after, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._batch(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._one("batches", "batch_id", batch_id, self._batch)

    def update_batch(
        self, batch_id: str, *, state: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            current = self.get_batch(batch_id)
            if current["result"].get("sealVersion") is not None:
                raise ValueError("sealed batches are immutable")
            cursor = self.connection.execute(
                "UPDATE batches SET state = ?, result_json = ?, updated_at = ? WHERE batch_id = ?",
                (state, _json(result), timestamp, batch_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(batch_id)
            record = self.get_batch(batch_id)
            self.append_event(
                "batch.updated", "batch", batch_id, _batch_event_payload(record)
            )
        return record

    def seal_batch(
        self,
        batch_id: str,
        *,
        state: str,
        result: dict[str, Any],
        notification_draft_factory: Callable[
            [int, dict[str, Any], dict[str, Any]], dict[str, Any]
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Persist the terminal batch fact before attempting notification rendering.

        The batch seal and its notification seed commit together. Rendering is a
        second, independent transaction so a template, artifact, or secret-provider
        failure can never make the terminal batch mutable again.
        """

        if state not in {"done", "failed", "blocked", "review_required", "cancelled"}:
            raise ValueError("a sealed batch requires a terminal or review state")
        sealed_at = _now()
        notification_seed: dict[str, Any] | None = None
        notification_policy: dict[str, Any] | None = None
        sealed_result: dict[str, Any]
        with self._write_scope():
            existing = self.get_batch(batch_id)
            if existing["result"].get("sealVersion") is not None:
                # Consumers deduplicate on batchId:sealVersion. A retried
                # sealer must return the frozen record without another event.
                return existing
            seal_version = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events"
                ).fetchone()[0]
            )
            sealed_result = {
                **result,
                "batchLineage": dict(existing["result"]["batchLineage"]),
                "memberRunIds": list(existing["member_run_ids"]),
                "runMemberships": list(existing["run_memberships"]),
                "sealVersion": seal_version,
                "sealedAt": sealed_at,
                "outcome": state,
            }
            cursor = self.connection.execute(
                "UPDATE batches SET state = ?, result_json = ?, updated_at = ? WHERE batch_id = ?",
                (state, _json(sealed_result), sealed_at, batch_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(batch_id)
            record = self.get_batch(batch_id)
            event = self.append_event(
                "batch.sealed", "batch", batch_id, _batch_event_payload(record)
            )
            if int(event["sequence"]) != seal_version:
                raise RuntimeError("batch seal version diverged from the event ledger")
            if existing.get("mode") == "execute":
                notification_policy = self.get_notification_policy()
                notification_seed = self._create_notification_render_seed_locked(
                    batch_id=batch_id,
                    seal_version=seal_version,
                    sealed_result=sealed_result,
                    policy=notification_policy,
                    timestamp=sealed_at,
                )

        if notification_seed is not None and notification_policy is not None:
            try:
                if notification_draft_factory is None:
                    raise ValueError("executed batch seal has no notification renderer")
                draft = notification_draft_factory(
                    seal_version, dict(sealed_result), dict(notification_policy)
                )
                self._complete_notification_render(
                    notification_seed["notification_id"],
                    draft=draft,
                    policy=notification_policy,
                )
            except Exception:
                # The seal above is already committed. Keep renderer diagnostics
                # coarse so template or provider exceptions cannot leak secrets.
                self._fail_notification_render(notification_seed["notification_id"])
        return record

    def _batch(self, row: sqlite3.Row) -> dict[str, Any]:
        # Row projection is not just JSON decoding: it performs three related
        # SQLite reads below.  Callers may have released their own read lock
        # after selecting ``row`` (for example list_batches), so this mapper
        # owns the complete projection boundary.
        with self._lock:
            return self._batch_locked(row)

    def _batch_locked(self, row: sqlite3.Row) -> dict[str, Any]:
        batch_id = str(row["batch_id"])
        lineage = self.connection.execute(
            "SELECT * FROM batch_lineage WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        memberships = self.list_batch_run_memberships(batch_id=batch_id)
        cancel_rows = self.list_batch_cancel_requests(batch_id=batch_id, limit=1)
        lineage_document = {
            "rootBatchId": (
                str(lineage["root_batch_id"]) if lineage is not None else batch_id
            ),
            "predecessorBatchId": (
                str(lineage["predecessor_batch_id"])
                if lineage is not None and lineage["predecessor_batch_id"] is not None
                else None
            ),
            "continuationOrdinal": (
                int(lineage["continuation_ordinal"]) if lineage is not None else 0
            ),
            "resumeIntentId": (
                str(lineage["resume_intent_id"])
                if lineage is not None and lineage["resume_intent_id"] is not None
                else None
            ),
            "runIds": [item["run_id"] for item in memberships],
        }
        result = _decode(row["result_json"], {})
        # A sealed result is an immutable notification/audit fact.  Its lineage
        # was copied into result_json by seal_batch and must never be replaced by
        # a later projection.  Top-level fields below remain the live projection.
        if result.get("sealVersion") is not None:
            projected_result = dict(result)
            # Legacy seals predate v10.  Expose their one-time migration
            # projection without overwriting any value frozen by a newer seal.
            projected_result.setdefault("batchLineage", lineage_document)
            projected_result.setdefault(
                "memberRunIds", [item["run_id"] for item in memberships]
            )
            projected_result.setdefault("runMemberships", memberships)
        else:
            projected_result = {
                **result,
                "batchLineage": lineage_document,
                "memberRunIds": [item["run_id"] for item in memberships],
                "runMemberships": memberships,
            }
        return {
            "batch_id": row["batch_id"],
            "cadence": row["cadence"],
            "mode": row["mode"],
            "state": row["state"],
            "game_ids": _decode(row["game_ids_json"], []),
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result": projected_result,
            "root_batch_id": lineage_document["rootBatchId"],
            "predecessor_batch_id": lineage_document["predecessorBatchId"],
            "continuation_ordinal": lineage_document["continuationOrdinal"],
            "continuation_resume_intent_id": lineage_document["resumeIntentId"],
            "member_run_ids": [item["run_id"] for item in memberships],
            "run_memberships": memberships,
            "cancel_request": cancel_rows[0] if cancel_rows else None,
        }

    def get_notification_policy(self) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM notification_policy WHERE policy_id = 'default'"
            ).fetchone()
        if row is None:
            raise RuntimeError("notification policy was not initialized")
        return {
            "enabled": bool(row["enabled"]),
            "automatic_dispatch": bool(row["automatic_dispatch"]),
            "channel": row["channel"],
            "recipient_binding_id": row["recipient_binding_id"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_notification_policy(
        self, patch: dict[str, Any], *, requested_by: str
    ) -> dict[str, Any]:
        if not patch:
            raise ValueError("notification policy patch is empty")
        allowed = {"enabled", "automatic_dispatch", "recipient_binding_id"}
        if not set(patch).issubset(allowed):
            raise ValueError("notification policy patch contains unsupported fields")
        timestamp = _now()
        with self._write_scope():
            current = self.get_notification_policy()
            updated = {**current, **patch}
            self.connection.execute(
                """
                UPDATE notification_policy SET
                    enabled = ?, automatic_dispatch = ?, recipient_binding_id = ?,
                    updated_by = ?, updated_at = ?
                WHERE policy_id = 'default'
                """,
                (
                    1 if updated["enabled"] else 0,
                    1 if updated["automatic_dispatch"] else 0,
                    updated["recipient_binding_id"],
                    requested_by,
                    timestamp,
                ),
            )
            record = self.get_notification_policy()
            self.append_event(
                "notification.policy.updated",
                "notification-policy",
                "default",
                {
                    "enabled": record["enabled"],
                    "automaticDispatch": record["automatic_dispatch"],
                    "recipientBindingId": record["recipient_binding_id"],
                    "requestedBy": requested_by,
                },
            )
        return record

    @staticmethod
    def _notification_identity(
        batch_id: str, seal_version: int, channel: str, recipient_binding_id: str
    ) -> tuple[str, str]:
        dedupe = f"{batch_id}:{seal_version}:{channel}:{recipient_binding_id}"
        namespace = uuid.UUID("43748285-cb54-4ce0-a162-815f993e9d5c")
        notification_id = str(uuid.uuid5(namespace, dedupe))
        message_hash = hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:40]
        return notification_id, f"<{message_hash}.yeyu-gamer@localhost>"

    @staticmethod
    def _notification_outcome_from_seal(sealed_result: dict[str, Any]) -> str:
        outcome = sealed_result.get("notificationOutcome")
        if outcome not in {"completed", "blocked"}:
            raise ValueError(
                "sealed result must freeze notificationOutcome as completed or blocked"
            )
        return str(outcome)

    @staticmethod
    def _validate_notification_draft(
        draft: dict[str, Any],
    ) -> tuple[str, list[str]]:
        required = {"outcome", "subject", "text_body", "html_body", "attachment_refs"}
        if not required.issubset(draft):
            raise ValueError("notification draft is incomplete")
        outcome = str(draft["outcome"])
        if outcome not in {"completed", "blocked"}:
            raise ValueError("notification draft outcome is invalid")
        if not isinstance(draft.get("report_html", ""), str):
            raise ValueError("notification report HTML must be text")
        attachment_refs = list(draft.get("attachment_refs", []))
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > 160
            or "/" in item
            or "\\" in item
            for item in attachment_refs
        ):
            raise ValueError("notification attachment references must be opaque IDs")
        return outcome, attachment_refs

    @staticmethod
    def _notification_dispatch_fields(
        policy: dict[str, Any],
        secret_state: str,
        timestamp: str,
        disposition: str = "",
    ) -> tuple[str, str | None, str]:
        last_error_class = (
            "secret_invalid"
            if secret_state == "invalid"
            else "secret_missing"
            if secret_state != "configured"
            else ""
        )
        if not policy["enabled"]:
            return "disabled", None, last_error_class
        if not policy["automatic_dispatch"]:
            return "manual_review", None, last_error_class
        if disposition == "stale_batch":
            # A batch sealed long after its round (recovery of an old review
            # phase) is history, not a round report.  Keep the draft for an
            # explicit send request; never dispatch it automatically.
            return "stale_batch", None, last_error_class
        if secret_state == "configured":
            return "automatic", timestamp, ""
        if secret_state == "invalid":
            return "manual_review", None, "secret_invalid"
        return "secret_missing", None, "secret_missing"

    def _create_notification_render_seed_locked(
        self,
        *,
        batch_id: str,
        seal_version: int,
        sealed_result: dict[str, Any],
        policy: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        """Create a non-dispatchable seed in the same transaction as BatchSeal."""

        channel = str(policy["channel"])
        recipient_binding_id = str(policy["recipient_binding_id"])
        notification_id, message_id = self._notification_identity(
            batch_id, seal_version, channel, recipient_binding_id
        )
        gate = "disabled" if not policy["enabled"] else "manual_review"
        outcome = self._notification_outcome_from_seal(sealed_result)
        cursor = self.connection.execute(
            """
            INSERT INTO notification_deliveries(
                notification_id, batch_id, seal_version, channel,
                recipient_binding_id, message_id, state, dispatch_gate,
                outcome, subject, text_body, html_body, report_html, attachment_refs_json,
                attempt_count, next_attempt_at, lease_owner, lease_token,
                lease_expires_at, last_error_class, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, '', '', '', '', '[]', 0,
                      NULL, NULL, NULL, NULL, 'render_pending', NULL, ?, ?)
            ON CONFLICT(batch_id, seal_version, channel, recipient_binding_id) DO NOTHING
            """,
            (
                notification_id,
                batch_id,
                seal_version,
                channel,
                recipient_binding_id,
                message_id,
                gate,
                outcome,
                timestamp,
                timestamp,
            ),
        )
        record = self.get_notification_delivery(notification_id)
        if cursor.rowcount == 1:
            self.append_event(
                "notification.created",
                "notification",
                notification_id,
                {
                    "batchId": batch_id,
                    "sealVersion": seal_version,
                    "channel": channel,
                    "recipientBindingId": recipient_binding_id,
                    "state": record["state"],
                    "dispatchGate": record["dispatch_gate"],
                    "outcome": outcome,
                    "renderState": "pending",
                },
            )
        return record

    def _complete_notification_render(
        self,
        notification_id: str,
        *,
        draft: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace a render seed without changing its stable delivery identity."""

        outcome, attachment_refs = self._validate_notification_draft(draft)
        timestamp = _now()
        secret_state = str(draft.get("secret_state", "missing"))
        gate, next_attempt_at, last_error_class = self._notification_dispatch_fields(
            policy, secret_state, timestamp, str(draft.get("dispatch_disposition", ""))
        )
        with self._write_scope():
            current = self.get_notification_delivery(notification_id)
            if current["last_error_class"] not in {"render_pending", "render_failed"}:
                return current
            if current["state"] not in {"draft", "failed"} or int(
                current["attempt_count"]
            ) != 0:
                raise ValueError("notification render seed is no longer mutable")
            if (
                current["channel"] != str(policy["channel"])
                or current["recipient_binding_id"]
                != str(policy["recipient_binding_id"])
            ):
                raise ValueError("notification render policy identity changed")
            self.connection.execute(
                """
                UPDATE notification_deliveries SET
                    state = 'draft', dispatch_gate = ?, outcome = ?, subject = ?,
                    text_body = ?, html_body = ?, report_html = ?, attachment_refs_json = ?,
                    next_attempt_at = ?, last_error_class = ?, updated_at = ?
                WHERE notification_id = ? AND attempt_count = 0
                  AND last_error_class IN ('render_pending', 'render_failed')
                """,
                (
                    gate,
                    outcome,
                    str(draft["subject"]),
                    str(draft["text_body"]),
                    str(draft["html_body"]),
                    draft.get("report_html", ""),
                    _json(attachment_refs),
                    next_attempt_at,
                    last_error_class,
                    timestamp,
                    notification_id,
                ),
            )
            record = self.get_notification_delivery(notification_id)
            self.append_event(
                "notification.gate.changed",
                "notification",
                notification_id,
                {
                    "dispatchGate": gate,
                    "errorClass": last_error_class,
                    "renderState": "rendered",
                },
            )
        return record

    def _fail_notification_render(self, notification_id: str) -> dict[str, Any]:
        """Make a failed render visible and permanently non-dispatchable."""

        timestamp = _now()
        with self._write_scope():
            current = self.get_notification_delivery(notification_id)
            if current["last_error_class"] == "render_failed":
                return current
            if current["last_error_class"] != "render_pending":
                return current
            gate = (
                "disabled"
                if current["dispatch_gate"] == "disabled"
                else "manual_review"
            )
            self.connection.execute(
                """
                UPDATE notification_deliveries SET
                    state = 'failed', dispatch_gate = ?, next_attempt_at = NULL,
                    last_error_class = 'render_failed', updated_at = ?
                WHERE notification_id = ? AND attempt_count = 0
                  AND last_error_class = 'render_pending'
                """,
                (gate, timestamp, notification_id),
            )
            record = self.get_notification_delivery(notification_id)
            self.append_event(
                "notification.failed",
                "notification",
                notification_id,
                {
                    "attemptNumber": 0,
                    "outcome": "render_failed",
                    "errorClass": "render_failed",
                    "phase": "render",
                },
            )
        return record

    def _create_notification_delivery_locked(
        self,
        *,
        batch_id: str,
        seal_version: int,
        draft: dict[str, Any],
        policy: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        outcome, attachment_refs = self._validate_notification_draft(draft)
        channel = str(policy["channel"])
        recipient_binding_id = str(policy["recipient_binding_id"])
        notification_id, message_id = self._notification_identity(
            batch_id, seal_version, channel, recipient_binding_id
        )
        secret_state = str(draft.get("secret_state", "missing"))
        gate, next_attempt_at, last_error_class = self._notification_dispatch_fields(
            policy, secret_state, timestamp, str(draft.get("dispatch_disposition", ""))
        )
        self.connection.execute(
            """
            INSERT INTO notification_deliveries(
                notification_id, batch_id, seal_version, channel,
                recipient_binding_id, message_id, state, dispatch_gate,
                outcome, subject, text_body, html_body, report_html, attachment_refs_json,
                attempt_count, next_attempt_at, lease_owner, lease_token,
                lease_expires_at, last_error_class, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL,
                      NULL, NULL, ?, NULL, ?, ?)
            ON CONFLICT(batch_id, seal_version, channel, recipient_binding_id) DO NOTHING
            """,
            (
                notification_id,
                batch_id,
                seal_version,
                channel,
                recipient_binding_id,
                message_id,
                gate,
                outcome,
                str(draft["subject"]),
                str(draft["text_body"]),
                str(draft["html_body"]),
                draft.get("report_html", ""),
                _json(attachment_refs),
                next_attempt_at,
                last_error_class,
                timestamp,
                timestamp,
            ),
        )
        record = self.get_notification_delivery(notification_id)
        self.append_event(
            "notification.created",
            "notification",
            notification_id,
            {
                "batchId": batch_id,
                "sealVersion": seal_version,
                "channel": channel,
                "recipientBindingId": recipient_binding_id,
                "state": record["state"],
                "dispatchGate": record["dispatch_gate"],
                "outcome": outcome,
            },
        )
        return record

    def list_notification_deliveries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM notification_deliveries ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._notification_delivery(row) for row in rows]

    def get_notification_delivery(self, notification_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM notification_deliveries WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(notification_id)
        return self._notification_delivery(row)

    @staticmethod
    def _notification_delivery(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "notification_id": row["notification_id"],
            "batch_id": row["batch_id"],
            "seal_version": int(row["seal_version"]),
            "channel": row["channel"],
            "recipient_binding_id": row["recipient_binding_id"],
            "message_id": row["message_id"],
            "state": row["state"],
            "dispatch_gate": row["dispatch_gate"],
            "outcome": row["outcome"],
            "subject": row["subject"],
            "text_body": row["text_body"],
            "html_body": row["html_body"],
            "report_html": row["report_html"],
            "attachment_refs": _decode(row["attachment_refs_json"], []),
            "attempt_count": int(row["attempt_count"]),
            "next_attempt_at": row["next_attempt_at"],
            "lease_owner": row["lease_owner"],
            "lease_token": row["lease_token"],
            "lease_expires_at": row["lease_expires_at"],
            "last_error_class": row["last_error_class"],
            "sent_at": row["sent_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_notification_attempts(
        self, notification_id: str
    ) -> list[dict[str, Any]]:
        self.get_notification_delivery(notification_id)
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM notification_attempts
                WHERE notification_id = ? ORDER BY attempt_number
                """,
                (notification_id,),
            ).fetchall()
        return [self._notification_attempt(row) for row in rows]

    @staticmethod
    def _notification_attempt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "notification_id": row["notification_id"],
            "attempt_number": int(row["attempt_number"]),
            "state": row["state"],
            "outcome": row["outcome"],
            "error_class": row["error_class"],
            "retry_scheduled_at": row["retry_scheduled_at"],
            "transport_receipt_hash": row["transport_receipt_hash"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
        }

    def list_dispatchable_notifications(
        self, now: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM notification_deliveries
                WHERE state IN ('draft', 'failed')
                  AND dispatch_gate = 'automatic'
                  AND attempt_count < 3
                  AND next_attempt_at IS NOT NULL
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at, created_at LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [self._notification_delivery(row) for row in rows]

    def claim_notification_delivery(
        self,
        notification_id: str,
        *,
        owner: str,
        now: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        if not 30 <= lease_seconds <= 600:
            raise ValueError("notification lease must be between 30 and 600 seconds")
        expires = (
            datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)
        ).isoformat()
        lease_token = str(uuid.uuid4())
        with self._write_scope():
            row = self.connection.execute(
                "SELECT * FROM notification_deliveries WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(notification_id)
            if (
                row["state"] not in {"draft", "failed"}
                or row["dispatch_gate"] != "automatic"
                or row["attempt_count"] >= 3
                or row["next_attempt_at"] is None
                or row["next_attempt_at"] > now
            ):
                return None
            attempt_number = int(row["attempt_count"]) + 1
            cursor = self.connection.execute(
                """
                UPDATE notification_deliveries SET
                    state = 'sending', attempt_count = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE notification_id = ? AND state IN ('draft', 'failed')
                  AND dispatch_gate = 'automatic' AND attempt_count = ?
                """,
                (
                    attempt_number,
                    owner,
                    lease_token,
                    expires,
                    now,
                    notification_id,
                    int(row["attempt_count"]),
                ),
            )
            if cursor.rowcount != 1:
                return None
            attempt_id = str(uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO notification_attempts(
                    attempt_id, notification_id, attempt_number, state, outcome,
                    error_class, retry_scheduled_at, transport_receipt_hash,
                    lease_owner, started_at, completed_at, created_at
                ) VALUES (?, ?, ?, 'sending', NULL, '', NULL, '', ?, ?, NULL, ?)
                """,
                (attempt_id, notification_id, attempt_number, owner, now, now),
            )
            self.append_event(
                "notification.sending",
                "notification",
                notification_id,
                {"attemptNumber": attempt_number, "leaseOwner": owner},
            )
            return self.get_notification_delivery(notification_id)

    def finish_notification_attempt(
        self,
        notification_id: str,
        *,
        lease_token: str,
        outcome: str,
        error_class: str,
        now: str,
        transport_receipt_hash: str = "",
    ) -> dict[str, Any]:
        allowed = {
            "sent",
            "transient_failure",
            "permanent_failure",
            "ambiguous",
        }
        if outcome not in allowed:
            raise ValueError("notification attempt outcome is invalid")
        with self._write_scope():
            delivery = self.get_notification_delivery(notification_id)
            if delivery["state"] == "sent":
                return delivery
            if delivery["state"] != "sending" or delivery["lease_token"] != lease_token:
                raise ValueError("notification lease is stale")
            attempt_number = int(delivery["attempt_count"])
            if outcome == "sent":
                state = "sent"
                gate = delivery["dispatch_gate"]
                next_attempt_at = None
                sent_at = now
                attempt_state = "sent"
            elif outcome == "transient_failure" and attempt_number < 3:
                delay = 120 if attempt_number == 1 else 900
                state = "failed"
                gate = "automatic"
                next_attempt_at = (
                    datetime.fromisoformat(now) + timedelta(seconds=delay)
                ).isoformat()
                sent_at = None
                attempt_state = "failed"
            else:
                state = "failed"
                gate = "manual_review"
                next_attempt_at = None
                sent_at = None
                attempt_state = "failed"
            self.connection.execute(
                """
                UPDATE notification_attempts SET
                    state = ?, outcome = ?, error_class = ?,
                    retry_scheduled_at = ?, transport_receipt_hash = ?,
                    completed_at = ?
                WHERE notification_id = ? AND attempt_number = ? AND state = 'sending'
                """,
                (
                    attempt_state,
                    outcome,
                    error_class,
                    next_attempt_at,
                    transport_receipt_hash,
                    now,
                    notification_id,
                    attempt_number,
                ),
            )
            self.connection.execute(
                """
                UPDATE notification_deliveries SET
                    state = ?, dispatch_gate = ?, next_attempt_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_class = ?, sent_at = ?, updated_at = ?
                WHERE notification_id = ?
                """,
                (
                    state,
                    gate,
                    next_attempt_at,
                    error_class,
                    sent_at,
                    now,
                    notification_id,
                ),
            )
            record = self.get_notification_delivery(notification_id)
            self.append_event(
                f"notification.{state}",
                "notification",
                notification_id,
                {
                    "attemptNumber": attempt_number,
                    "outcome": outcome,
                    "errorClass": error_class,
                    "retryScheduledAt": next_attempt_at,
                },
            )
        return record

    def recover_expired_notification_leases(self, now: str) -> int:
        recovered = 0
        with self._write_scope():
            rows = self.connection.execute(
                """
                SELECT notification_id, attempt_count FROM notification_deliveries
                WHERE state = 'sending' AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                notification_id = row["notification_id"]
                attempt_number = int(row["attempt_count"])
                self.connection.execute(
                    """
                    UPDATE notification_attempts SET
                        state = 'failed', outcome = 'ambiguous',
                        error_class = 'lease_expired_ambiguous', completed_at = ?
                    WHERE notification_id = ? AND attempt_number = ? AND state = 'sending'
                    """,
                    (now, notification_id, attempt_number),
                )
                self.connection.execute(
                    """
                    UPDATE notification_deliveries SET
                        state = 'failed', dispatch_gate = 'manual_review',
                        next_attempt_at = NULL, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_class = 'lease_expired_ambiguous', updated_at = ?
                    WHERE notification_id = ? AND state = 'sending'
                    """,
                    (now, notification_id),
                )
                self.append_event(
                    "notification.failed",
                    "notification",
                    notification_id,
                    {
                        "attemptNumber": attempt_number,
                        "outcome": "ambiguous",
                        "errorClass": "lease_expired_ambiguous",
                    },
                )
                recovered += 1
        return recovered

    def set_notification_dispatch_gate(
        self, notification_id: str, *, gate: str, error_class: str
    ) -> dict[str, Any]:
        if gate not in {"secret_missing", "manual_review", "disabled"}:
            raise ValueError("notification dispatch gate is invalid")
        timestamp = _now()
        with self._write_scope():
            current = self.get_notification_delivery(notification_id)
            if current["state"] == "sent":
                return current
            if current["state"] == "sending":
                return current
            if current["last_error_class"] in {"render_pending", "render_failed"}:
                return current
            self.connection.execute(
                """
                UPDATE notification_deliveries SET dispatch_gate = ?,
                    next_attempt_at = NULL, last_error_class = ?, updated_at = ?
                WHERE notification_id = ?
                """,
                (gate, error_class, timestamp, notification_id),
            )
            record = self.get_notification_delivery(notification_id)
            self.append_event(
                "notification.gate.changed",
                "notification",
                notification_id,
                {"dispatchGate": gate, "errorClass": error_class},
            )
        return record

    def arm_notification_delivery(
        self,
        notification_id: str,
        *,
        requested_by: str,
        reason: str,
        secret_state: str,
        confirm_ambiguous: bool,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            current = self.get_notification_delivery(notification_id)
            if current["state"] == "sent":
                raise ValueError("sent notifications are immutable")
            if current["state"] == "sending":
                raise ValueError("notification is already sending")
            if int(current["attempt_count"]) >= 3:
                raise ValueError("notification retry budget is exhausted")
            if current["last_error_class"] in {"render_pending", "render_failed"}:
                raise ValueError("notification rendering is incomplete")
            if current["last_error_class"] in {
                "transport_ambiguous",
                "worker_internal",
                "lease_expired_ambiguous",
            } and not confirm_ambiguous:
                raise ValueError("ambiguous delivery requires explicit confirmation")
            if secret_state == "configured":
                gate = "automatic"
                next_attempt_at = timestamp
                error_class = ""
            elif secret_state == "invalid":
                gate = "manual_review"
                next_attempt_at = None
                error_class = "secret_invalid"
            else:
                gate = "secret_missing"
                next_attempt_at = None
                error_class = "secret_missing"
            self.connection.execute(
                """
                UPDATE notification_deliveries SET
                    dispatch_gate = ?, next_attempt_at = ?,
                    last_error_class = ?, updated_at = ?
                WHERE notification_id = ?
                """,
                (gate, next_attempt_at, error_class, timestamp, notification_id),
            )
            record = self.get_notification_delivery(notification_id)
            self.append_event(
                "notification.dispatch.requested",
                "notification",
                notification_id,
                {
                    "requestedBy": requested_by,
                    "reason": reason,
                    "dispatchGate": gate,
                    "confirmAmbiguous": confirm_ambiguous,
                },
            )
        return record

    def create_game_run(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        execution_ids = list(dict.fromkeys(data.get("todo_instance_ids", [])))
        completed_ids = list(
            dict.fromkeys(data.get("completed_todo_instance_ids", []))
        )
        explicit_completion_ids = data.get("completion_todo_instance_ids")
        completion_ids = list(
            dict.fromkeys(
                explicit_completion_ids
                if explicit_completion_ids is not None
                else [*execution_ids, *completed_ids]
            )
        )
        if len(execution_ids) != len(data.get("todo_instance_ids", [])):
            raise ValueError("execution Todo scope contains duplicate IDs")
        if len(completed_ids) != len(data.get("completed_todo_instance_ids", [])):
            raise ValueError("completed Todo scope contains duplicate IDs")
        if explicit_completion_ids is not None and len(completion_ids) != len(
            explicit_completion_ids
        ):
            raise ValueError("completion Todo scope contains duplicate IDs")
        if not set(execution_ids).issubset(completion_ids):
            raise ValueError("execution Todo scope exceeds completion scope")
        if not set(completed_ids).issubset(completion_ids):
            raise ValueError("completed Todo scope exceeds completion scope")
        record = {
            "run_id": str(uuid.uuid4()),
            "account_id": data.get("account_id", "default"),
            "account_snapshot": dict(data.get("account_snapshot") or {}),
            "game_id": data["game_id"],
            "cadence": data["cadence"],
            "state": data["state"],
            "mode": data["mode"],
            "requested_by": data["requested_by"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "exit_code": None,
            "message": data.get("message", ""),
            "todo_instance_ids": execution_ids,
            "completed_todo_instance_ids": completed_ids,
            "completion_todo_instance_ids": completion_ids,
            # Rows created through the current Store API always carry an
            # immutable scope. Existing rows migrated with DEFAULT 0 remain
            # distinguishable and therefore fail closed for acceptance.
            "completion_scope_version": int(
                data.get("completion_scope_version", 1)
            ),
        }
        if record["completion_scope_version"] != 1:
            raise ValueError("new GameRuns require completion scope version 1")
        with self._write_scope():
            if not isinstance(record["account_id"], str) or not record["account_id"]:
                raise ValueError("GameRun account ID is required")
            for todo_id in completion_ids:
                todo = self.get_todo_instance(todo_id)
                if (todo["game_id"], todo["account_id"], todo["cadence"]) != (
                    record["game_id"], record["account_id"], record["cadence"]
                ):
                    raise ValueError("GameRun Todo account scope is inconsistent")
            self.connection.execute(
                """
                INSERT INTO game_runs(
                    run_id, game_id, cadence, state, mode, requested_by,
                    exit_code, message, created_at, updated_at,
                    todo_instance_ids_json, completed_todo_instance_ids_json,
                    completion_todo_instance_ids_json, completion_scope_version,
                    account_id, account_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record["game_id"],
                    record["cadence"],
                    record["state"],
                    record["mode"],
                    record["requested_by"],
                    record["exit_code"],
                    record["message"],
                    timestamp,
                    timestamp,
                    _json(record["todo_instance_ids"]),
                    _json(record["completed_todo_instance_ids"]),
                    _json(record["completion_todo_instance_ids"]),
                    record["completion_scope_version"],
                    record["account_id"],
                    _json(record["account_snapshot"]),
                ),
            )
            self.append_event("game-run.created", "game-run", record["run_id"], record)
        return record

    def update_game_run(
        self,
        run_id: str,
        *,
        state: str,
        exit_code: int | None = None,
        message: str = "",
        completed_todo_instance_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            existing = self.get_game_run(run_id)
            completed_ids = (
                existing["completed_todo_instance_ids"]
                if completed_todo_instance_ids is None
                else list(dict.fromkeys(completed_todo_instance_ids))
            )
            allowed_completed_ids = (
                set(existing["completion_todo_instance_ids"])
                if int(existing.get("completion_scope_version", 0)) == 1
                else {
                    *existing.get("todo_instance_ids", []),
                    *existing.get("completed_todo_instance_ids", []),
                }
            )
            if not set(completed_ids).issubset(allowed_completed_ids):
                raise ValueError("completed Todo scope exceeds its GameRun")
            cursor = self.connection.execute(
                """
                UPDATE game_runs SET state = ?, exit_code = ?, message = ?,
                    completed_todo_instance_ids_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    state,
                    exit_code,
                    message,
                    _json(completed_ids),
                    timestamp,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(run_id)
            record = self.get_game_run(run_id)
            self.append_event("game-run.updated", "game-run", run_id, record)
        return record

    def list_game_runs(
        self, limit: int = 100, *, created_after: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if created_after is not None:
                rows = self.connection.execute(
                    "SELECT * FROM game_runs WHERE created_at >= ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (created_after, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM game_runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._game_run(row) for row in rows]

    def get_game_run(self, run_id: str) -> dict[str, Any]:
        return self._one("game_runs", "run_id", run_id, self._game_run)

    def has_account_execution(self, game_id: str, account_id: str) -> bool:
        """A frozen execute GameRun binds identity even before its first attempt."""
        with self._lock:
            return self.connection.execute(
                "SELECT 1 FROM game_runs WHERE game_id = ? AND account_id = ? "
                "AND mode = 'execute' LIMIT 1",
                (game_id, account_id),
            ).fetchone() is not None

    @staticmethod
    def _game_run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "account_id": row["account_id"],
            "account_snapshot": _decode(row["account_snapshot_json"], {}),
            "game_id": row["game_id"],
            "cadence": row["cadence"],
            "state": row["state"],
            "mode": row["mode"],
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "exit_code": row["exit_code"],
            "message": row["message"],
            "todo_instance_ids": _decode(row["todo_instance_ids_json"], []),
            "completed_todo_instance_ids": _decode(
                row["completed_todo_instance_ids_json"], []
            ),
            "completion_todo_instance_ids": _decode(
                row["completion_todo_instance_ids_json"], []
            ),
            "completion_scope_version": int(row["completion_scope_version"]),
        }

    def create_run_attempt(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        safe_plan = dict(data["plan"])
        if _contains_sensitive_key(safe_plan):
            raise ValueError("run attempt plan must not persist fencing material")
        fencing_token_hash = str(data["fencing_token_hash"])
        if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", fencing_token_hash) is None:
            raise ValueError("run attempt fencing value must be a sha256 digest")
        cancel_authority_hash = str(data.get("cancel_authority_hash", ""))
        if re.fullmatch(r"sha256:[0-9a-f]{64}", cancel_authority_hash) is None:
            raise ValueError("run attempt cancel authority must be a sha256 digest")
        record = {
            "run_attempt_id": data["run_attempt_id"],
            "account_id": data.get("account_id", safe_plan.get("accountId", "default")),
            "run_id": data["run_id"],
            "game_id": data["game_id"],
            "cadence": data["cadence"],
            "state": data.get("state", "starting"),
            "fencing_token_hash": fencing_token_hash,
            "cancel_authority_hash": cancel_authority_hash,
            "plan": safe_plan,
            "process_id": data.get("process_id"),
            "exit_code": None,
            "result": {},
            "attempt_ordinal": int(data.get("attempt_ordinal", 0)),
            "started_at": timestamp,
            "completed_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self._write_scope():
            run = self.get_game_run(record["run_id"])
            if (
                run["game_id"] != record["game_id"]
                or run["account_id"] != record["account_id"]
                or safe_plan.get("accountId", "default") != run["account_id"]
                or run["cadence"] != record["cadence"]
            ):
                raise ValueError("run attempt scope differs from its GameRun")
            planned_ids = list(record["plan"].get("executableTodoInstanceIds", []))
            planned_todos = list(record["plan"].get("todos", []))
            session_reentry_ids = list(
                record["plan"].get("sessionReentryTodoInstanceIds", [])
            )
            planned_todo_ids = [
                item.get("todoInstanceId") if isinstance(item, dict) else None
                for item in planned_todos
            ]
            run_scope = list(run["todo_instance_ids"])
            if (
                not planned_ids
                or len(planned_ids) != len(set(planned_ids))
                or any(todo_id not in run_scope for todo_id in planned_ids)
                or planned_ids
                != [todo_id for todo_id in run_scope if todo_id in set(planned_ids)]
                or planned_todo_ids != planned_ids
                or len(session_reentry_ids) != len(set(session_reentry_ids))
                or any(todo_id not in planned_ids for todo_id in session_reentry_ids)
            ):
                raise ValueError("run attempt plan differs from its GameRun Todo scope")
            if record["state"] != "starting":
                raise ValueError("new run attempts must start in the starting state")
            expected_ordinal = int(
                self.connection.execute(
                    "SELECT COUNT(*) + 1 FROM run_attempts WHERE run_id = ?",
                    (record["run_id"],),
                ).fetchone()[0]
            )
            if record["attempt_ordinal"] == 0:
                record["attempt_ordinal"] = expected_ordinal
            if record["attempt_ordinal"] != expected_ordinal:
                raise ValueError("run attempt ordinal is stale")
            self.connection.execute(
                """
                INSERT INTO run_attempts(
                    run_attempt_id, run_id, game_id, cadence, state,
                    fencing_token_hash, cancel_authority_hash, plan_json, process_id, exit_code,
                    result_json, attempt_ordinal, started_at, completed_at,
                    created_at, updated_at, account_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_attempt_id"],
                    record["run_id"],
                    record["game_id"],
                    record["cadence"],
                    record["state"],
                    record["fencing_token_hash"],
                    record["cancel_authority_hash"],
                    _json(record["plan"]),
                    record["process_id"],
                    record["exit_code"],
                    _json(record["result"]),
                    record["attempt_ordinal"],
                    record["started_at"],
                    record["completed_at"],
                    record["created_at"],
                    record["updated_at"],
                    record["account_id"],
                ),
            )
            self.append_event(
                "run-attempt.created",
                "run-attempt",
                record["run_attempt_id"],
                self._public_run_attempt(record),
            )
        return record

    @staticmethod
    def _public_run_attempt(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"fencing_token_hash", "cancel_authority_hash"}
        }

    @staticmethod
    def _run_attempt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_attempt_id": row["run_attempt_id"],
            "account_id": row["account_id"],
            "run_id": row["run_id"],
            "game_id": row["game_id"],
            "cadence": row["cadence"],
            "state": row["state"],
            "fencing_token_hash": row["fencing_token_hash"],
            "cancel_authority_hash": row["cancel_authority_hash"],
            "plan": _decode(row["plan_json"], {}),
            "process_id": row["process_id"],
            "exit_code": row["exit_code"],
            "result": _decode(row["result_json"], {}),
            "attempt_ordinal": int(row["attempt_ordinal"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_run_attempt(self, run_attempt_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM run_attempts WHERE run_attempt_id = ?",
                (run_attempt_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(run_attempt_id)
        return self._run_attempt(row)

    def list_run_attempts(
        self, *, run_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            if run_id is None:
                rows = self.connection.execute(
                    """
                    SELECT * FROM run_attempts
                    ORDER BY created_at DESC, run_attempt_id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM run_attempts WHERE run_id = ?
                    ORDER BY attempt_ordinal DESC LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
        return [self._run_attempt(row) for row in rows]

    def update_run_attempt(
        self,
        run_attempt_id: str,
        *,
        state: str,
        process_id: int | None = None,
        exit_code: int | None = None,
        result: dict[str, Any] | None = None,
        completed: bool = False,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            before = self.get_run_attempt(run_attempt_id)
            transitions = {
                "starting": {"starting", "running", "cancelling", "completed", "partial", "blocked", "review_required", "human_required", "cancelled", "failed"},
                "running": {"running", "cancelling", "completed", "partial", "blocked", "review_required", "human_required", "cancelled", "failed"},
                "cancelling": {
                    "cancelling",
                    "cancelled",
                    "blocked",
                    "review_required",
                    "human_required",
                    "failed",
                },
            }
            if before["state"] not in transitions:
                raise ValueError("terminal run attempts are immutable")
            if state not in transitions[before["state"]]:
                raise ValueError("run attempt transition is invalid")
            is_terminal = state not in {"starting", "running", "cancelling"}
            if completed != is_terminal:
                raise ValueError("run attempt completion marker differs from state")
            merged_result = {**before["result"], **(result or {})}
            if _contains_sensitive_key(merged_result):
                raise ValueError("run attempt result must not persist fencing material")
            completed_at = timestamp if completed else before["completed_at"]
            self.connection.execute(
                """
                UPDATE run_attempts SET state = ?, process_id = COALESCE(?, process_id),
                    exit_code = ?, result_json = ?, completed_at = ?, updated_at = ?
                WHERE run_attempt_id = ?
                """,
                (
                    state,
                    process_id,
                    exit_code,
                    _json(merged_result),
                    completed_at,
                    timestamp,
                    run_attempt_id,
                ),
            )
            record = self.get_run_attempt(run_attempt_id)
            self.append_event(
                "run-attempt.updated",
                "run-attempt",
                run_attempt_id,
                self._public_run_attempt(record),
            )
        return record

    def append_adapter_event(
        self,
        *,
        run_attempt_id: str,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
        fencing_token_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        if sequence < 0:
            raise ValueError("Adapter event sequence cannot be negative")
        if _contains_sensitive_key(payload):
            raise ValueError("Adapter event payload must not persist fencing material")
        self._assert_public_values(run_attempt_id, event_type, payload)
        payload_json = _json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        timestamp = _now()
        with self._write_scope():
            attempt = self.get_run_attempt(run_attempt_id)
            if attempt["fencing_token_hash"] != fencing_token_hash:
                raise ValueError("Adapter event fencing token is stale")
            existing = self.connection.execute(
                """
                SELECT * FROM adapter_events
                WHERE run_attempt_id = ? AND sequence = ?
                """,
                (run_attempt_id, sequence),
            ).fetchone()
            if existing is not None:
                if (
                    existing["event_type"] != event_type
                    or existing["payload_hash"] != payload_hash
                ):
                    raise ValueError("Adapter event sequence was reused with new content")
                return self._adapter_event(existing), True
            if attempt["state"] not in {"starting", "running", "cancelling"}:
                raise ValueError("Adapter event belongs to a terminal run attempt")
            next_sequence = int(
                self.connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1
                    FROM adapter_events WHERE run_attempt_id = ?
                    """,
                    (run_attempt_id,),
                ).fetchone()[0]
            )
            if sequence != next_sequence:
                raise ValueError("Adapter event sequence is not contiguous")
            self.connection.execute(
                """
                INSERT INTO adapter_events(
                    run_attempt_id, sequence, event_type,
                    payload_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_attempt_id,
                    sequence,
                    event_type,
                    payload_hash,
                    payload_json,
                    timestamp,
                ),
            )
            record = {
                "run_attempt_id": run_attempt_id,
                "sequence": sequence,
                "event_type": event_type,
                "payload_hash": payload_hash,
                "payload": dict(payload),
                "created_at": timestamp,
            }
            self.append_event(
                f"adapter.{event_type}",
                "run-attempt",
                run_attempt_id,
                {
                    "sequence": sequence,
                    "eventType": event_type,
                    "payloadHash": payload_hash,
                },
            )
        return record, False

    @staticmethod
    def _adapter_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_attempt_id": row["run_attempt_id"],
            "sequence": int(row["sequence"]),
            "event_type": row["event_type"],
            "payload_hash": row["payload_hash"],
            "payload": _decode(row["payload_json"], {}),
            "created_at": row["created_at"],
        }

    def list_adapter_events(self, run_attempt_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM adapter_events WHERE run_attempt_id = ?
                ORDER BY sequence
                """,
                (run_attempt_id,),
            ).fetchall()
        return [self._adapter_event(row) for row in rows]

    def _retire_retryable_blockers_for_fresh_rebatch(
        self,
        *,
        todo_instance_id: str,
        successor_run_id: str,
        successor_run_attempt_id: str,
        timestamp: str,
    ) -> list[str]:
        """Close only retryable routine blockers superseded by a fresh batch.

        This helper runs inside ``start_todo_attempt``'s write transaction,
        after the successor's frozen plan has been validated.  Human and
        policy/safety blockers are deliberately excluded; starting a new batch
        can never impersonate an explicit takeover release.
        """

        rows = self.connection.execute(
            """
            SELECT * FROM todo_blockers
            WHERE todo_instance_id = ? AND run_id <> ?
              AND state = 'active' AND retryable = 1
              AND kind IN ('contract_invariant', 'review_required')
            ORDER BY transitioned_at, blocker_id
            """,
            (todo_instance_id, successor_run_id),
        ).fetchall()
        retired: list[str] = []
        for row in rows:
            blocker_id = str(row["blocker_id"])
            cursor = self.connection.execute(
                """
                UPDATE todo_blockers SET state = 'resolved', resolved_at = ?,
                    resolution_code = 'superseded_by_fresh_rebatch',
                    resolution_reason = ?,
                    resolution_artifact_refs_json = '[]', release_id = NULL,
                    release_explicit = 0, released_by = NULL,
                    transitioned_at = ?, revision = revision + 1, updated_at = ?
                WHERE blocker_id = ? AND state = 'active' AND retryable = 1
                  AND kind IN ('contract_invariant', 'review_required')
                """,
                (
                    timestamp,
                    (
                        "validated fresh batch attempt superseded the retryable "
                        f"blocker; successorRunId={successor_run_id}"
                    ),
                    timestamp,
                    timestamp,
                    blocker_id,
                ),
            )
            if cursor.rowcount != 1:
                continue
            retired.append(blocker_id)
            self.append_event(
                "todo-blocker.superseded-by-fresh-rebatch",
                "todo-blocker",
                blocker_id,
                {
                    "blockerId": blocker_id,
                    "todoInstanceId": todo_instance_id,
                    "predecessorRunId": str(row["run_id"]),
                    "successorRunId": successor_run_id,
                    "successorRunAttemptId": successor_run_attempt_id,
                    "resolutionCode": "superseded_by_fresh_rebatch",
                    "explicitHumanRelease": False,
                },
            )
        return retired

    def start_todo_attempt(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            run_attempt = self.get_run_attempt(data["run_attempt_id"])
            todo = self.get_todo_instance(data["todo_instance_id"])
            if run_attempt["state"] not in {"starting", "running"}:
                raise ValueError("Todo attempt belongs to an inactive run attempt")
            if run_attempt["run_id"] != data["run_id"]:
                raise ValueError("Todo attempt differs from its run attempt")
            planned_ids = list(
                run_attempt["plan"].get("executableTodoInstanceIds", [])
            )
            if data["todo_instance_id"] not in planned_ids:
                raise ValueError("Todo attempt is outside the fenced execution plan")
            target = next(
                (
                    item
                    for item in run_attempt["plan"].get("todos", [])
                    if item.get("todoInstanceId") == data["todo_instance_id"]
                ),
                None,
            )
            if (
                not isinstance(target, dict)
                or target.get("todoDefinitionId") != todo["todo_definition_id"]
                or target.get("operation") != todo["operation"]
                or target.get("risk") != todo["risk"]
            ):
                raise ValueError("Todo attempt differs from its frozen plan target")
            session_reentry = data["todo_instance_id"] in set(
                run_attempt["plan"].get("sessionReentryTodoInstanceIds", [])
            )
            completion_recovery_replay = data["todo_instance_id"] in set(
                run_attempt["plan"].get(
                    "completionRecoveryReplayTodoInstanceIds", []
                )
            )
            completed_replay = session_reentry or completion_recovery_replay
            if (
                todo["game_id"] != run_attempt["game_id"]
                or todo["account_id"] != run_attempt["account_id"]
                or todo["cadence"] != run_attempt["cadence"]
                or todo["operation"] != data["operation"]
            ):
                raise ValueError("Todo attempt scope differs from its snapshot")
            if todo["risk"] not in {"routine_action", "observe_only"}:
                raise ValueError("Todo risk is not executable")
            previous_attempt = self.connection.execute(
                """
                SELECT * FROM todo_attempts WHERE todo_instance_id = ?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (data["todo_instance_id"],),
            ).fetchone()
            retryable_resume = bool(
                todo["status"] == "blocked"
                and previous_attempt is not None
                and bool(previous_attempt["retryable"])
                and todo["run_id"] == run_attempt["run_id"]
            )
            # The execution plan is created only from a freshly evaluated,
            # eligible dispatch projection.  A blocked routine Todo can
            # therefore enter a different, newly created GameRun as a fresh
            # batch attempt; it must not be mistaken for an unsafe same-run
            # resume merely because its previous attempt ended blocked.
            fresh_rebatch = bool(
                todo["status"] == "blocked"
                and todo["run_id"]
                and todo["run_id"] != run_attempt["run_id"]
            )
            fresh_review_rebatch = bool(
                todo["status"] == "review_required"
                and todo["run_id"]
                and todo["run_id"] != run_attempt["run_id"]
            )
            if todo["status"] not in {"pending", "in_progress"} and not (
                retryable_resume
                or fresh_rebatch
                or fresh_review_rebatch
                or (completed_replay and todo["status"] == "completed")
            ):
                raise ValueError("Todo state is not executable")
            if todo["status"] == "completed" and not completed_replay:
                raise ValueError("completed Todo instances are immutable")
            if fresh_rebatch or fresh_review_rebatch:
                self._retire_retryable_blockers_for_fresh_rebatch(
                    todo_instance_id=data["todo_instance_id"],
                    successor_run_id=run_attempt["run_id"],
                    successor_run_attempt_id=run_attempt["run_attempt_id"],
                    timestamp=timestamp,
                )
            attempt_number = int(data["attempt_number"])
            if attempt_number != int(todo["attempts"]) + 1:
                raise ValueError("Todo attempt number is stale")
            record = {
                "todo_attempt_id": data["todo_attempt_id"],
                "run_attempt_id": data["run_attempt_id"],
                "todo_instance_id": data["todo_instance_id"],
                "attempt_number": attempt_number,
                "operation": data["operation"],
                "state": "running",
                "reason_code": "",
                "reason": "",
                "retryable": False,
                "evidence_refs": [],
                "started_at": timestamp,
                "completed_at": None,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            self.connection.execute(
                """
                INSERT INTO todo_attempts(
                    todo_attempt_id, run_attempt_id, todo_instance_id,
                    attempt_number, operation, state, reason_code, reason,
                    retryable, evidence_refs_json, started_at, completed_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["todo_attempt_id"],
                    record["run_attempt_id"],
                    record["todo_instance_id"],
                    record["attempt_number"],
                    record["operation"],
                    record["state"],
                    record["reason_code"],
                    record["reason"],
                    0,
                    "[]",
                    timestamp,
                    None,
                    timestamp,
                    timestamp,
                ),
            )
            if completed_replay:
                # Keep the accepted GameDay completion fact byte-for-byte
                # stable while recording a new per-process session attempt.
                # The attempt ledger carries this run's fresh evidence and
                # outcome; only its monotonic attempt counter advances here.
                self.connection.execute(
                    """
                    UPDATE todo_instances SET attempts = attempts + 1,
                        last_attempt_at = ?, updated_at = ?
                    WHERE todo_instance_id = ? AND status = 'completed'
                    """,
                    (timestamp, timestamp, data["todo_instance_id"]),
                )
                self.append_event(
                    (
                        "todo.session-reentry-started"
                        if session_reentry
                        else "todo.completion-recovery-replay-started"
                    ),
                    "todo-instance",
                    data["todo_instance_id"],
                    {
                        "runId": data["run_id"],
                        "runAttemptId": data["run_attempt_id"],
                        "todoAttemptId": record["todo_attempt_id"],
                        "attempts": attempt_number,
                    },
                )
            else:
                self.transition_todo_instance(
                    data["todo_instance_id"],
                    status="in_progress",
                    reason="Manager Adapter attempt started",
                    evidence_refs=[],
                    run_id=data["run_id"],
                    increment_attempt=True,
                    requested_by="manager-adapter",
                )
            self.append_event(
                "todo-attempt.started",
                "todo-attempt",
                record["todo_attempt_id"],
                record,
            )
        return record

    @staticmethod
    def _todo_attempt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "todo_attempt_id": row["todo_attempt_id"],
            "run_attempt_id": row["run_attempt_id"],
            "todo_instance_id": row["todo_instance_id"],
            "attempt_number": int(row["attempt_number"]),
            "operation": row["operation"],
            "state": row["state"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "retryable": bool(row["retryable"]),
            "evidence_refs": _decode(row["evidence_refs_json"], []),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_todo_attempt(self, todo_attempt_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM todo_attempts WHERE todo_attempt_id = ?",
                (todo_attempt_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(todo_attempt_id)
        return self._todo_attempt(row)

    def list_todo_attempts(
        self,
        *,
        todo_instance_id: str | None = None,
        run_attempt_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if todo_instance_id is not None and run_attempt_id is not None:
                rows = self.connection.execute(
                    """
                    SELECT * FROM todo_attempts
                    WHERE todo_instance_id = ? AND run_attempt_id = ?
                    ORDER BY attempt_number DESC LIMIT ?
                    """,
                    (todo_instance_id, run_attempt_id, limit),
                ).fetchall()
            elif todo_instance_id is None and run_attempt_id is None:
                rows = self.connection.execute(
                    "SELECT * FROM todo_attempts ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif todo_instance_id is not None:
                rows = self.connection.execute(
                    """
                    SELECT * FROM todo_attempts WHERE todo_instance_id = ?
                    ORDER BY attempt_number DESC LIMIT ?
                    """,
                    (todo_instance_id, limit),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM todo_attempts WHERE run_attempt_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (run_attempt_id, limit),
                ).fetchall()
        return [self._todo_attempt(row) for row in rows]

    def finish_todo_attempt(
        self,
        todo_attempt_id: str,
        *,
        status: str,
        reason_code: str,
        reason: str,
        retryable: bool,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        if status not in {
            "completed",
            "skipped",
            "blocked",
            "review_required",
            "human_required",
        }:
            raise ValueError("Todo attempt terminal status is invalid")
        if status == "human_required" and retryable:
            raise ValueError("human_required Todo attempts cannot be retryable")
        timestamp = _now()
        with self._write_scope():
            before = self.get_todo_attempt(todo_attempt_id)
            if before["state"] != "running":
                raise ValueError("Todo attempt is already terminal")
            run_attempt = self.get_run_attempt(before["run_attempt_id"])
            if run_attempt["state"] not in {"starting", "running", "cancelling"}:
                raise ValueError("Todo attempt belongs to a terminal run attempt")
            if len(evidence_refs) != len(set(evidence_refs)):
                raise ValueError("Todo attempt evidence contains duplicate references")
            for artifact_id in evidence_refs:
                artifact = self.get_resource("artifact", artifact_id)
                document = artifact["document"]
                if (
                    document.get("runAttemptId") != before["run_attempt_id"]
                    or document.get("accountId", "default") != run_attempt["account_id"]
                    or document.get("todoAttemptId") != todo_attempt_id
                    or document.get("todoInstanceId") != before["todo_instance_id"]
                ):
                    raise ValueError("Todo evidence belongs to another attempt")
            if status == "completed" and not evidence_refs:
                raise ValueError("completed Todo attempts require fresh evidence")
            if status == "completed":
                active_blocker = self.connection.execute(
                    """
                    SELECT blocker_id FROM todo_blockers
                    WHERE run_id = ? AND todo_instance_id = ? AND state = 'active'
                    LIMIT 1
                    """,
                    (run_attempt["run_id"], before["todo_instance_id"]),
                ).fetchone()
                if active_blocker is not None:
                    raise ValueError(
                        "completed Todo attempts cannot bypass an active blocker"
                    )
            session_reentry = before["todo_instance_id"] in set(
                run_attempt["plan"].get("sessionReentryTodoInstanceIds", [])
            )
            completion_recovery_replay = before["todo_instance_id"] in set(
                run_attempt["plan"].get(
                    "completionRecoveryReplayTodoInstanceIds", []
                )
            )
            self.connection.execute(
                """
                UPDATE todo_attempts SET state = ?, reason_code = ?, reason = ?,
                    retryable = ?, evidence_refs_json = ?, completed_at = ?, updated_at = ?
                WHERE todo_attempt_id = ? AND state = 'running'
                """,
                (
                    status,
                    reason_code,
                    reason,
                    1 if retryable else 0,
                    _json(evidence_refs),
                    timestamp,
                    timestamp,
                    todo_attempt_id,
                ),
            )
            if not (session_reentry or completion_recovery_replay):
                self.transition_todo_instance(
                    before["todo_instance_id"],
                    status=status,
                    reason=reason,
                    evidence_refs=evidence_refs,
                    run_id=run_attempt["run_id"],
                    increment_attempt=False,
                    requested_by="manager-adapter",
                )
            record = self.get_todo_attempt(todo_attempt_id)
            self.append_event(
                "todo-attempt.finished",
                "todo-attempt",
                todo_attempt_id,
                record,
            )
        return record

    @staticmethod
    def _controller_lease(row: sqlite3.Row, *, include_hash: bool) -> dict[str, Any]:
        record = {
            "controller_lease_id": row["controller_lease_id"],
            "desktop_id": row["desktop_id"],
            "manager_id": row["manager_id"],
            "game_id": row["game_id"],
            "run_id": row["run_id"],
            "run_attempt_id": row["run_attempt_id"],
            "game_day_key": row["game_day_key"],
            "holder_principal_id": row["holder_principal_id"],
            "generation": int(row["generation"]),
            "fencing_fingerprint": row["fencing_fingerprint"],
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "state": row["state"],
            "ended_at": row["ended_at"],
            "end_reason_code": row["end_reason_code"],
            "end_reason": row["end_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_hash:
            record["fencing_hash"] = row["fencing_hash"]
        return record

    @staticmethod
    def _public_controller_lease(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "controllerLeaseId": record["controller_lease_id"],
            "desktopId": record["desktop_id"],
            "managerId": record["manager_id"],
            "gameId": record["game_id"],
            "runId": record["run_id"],
            "runAttemptId": record["run_attempt_id"],
            "gameDayKey": record["game_day_key"],
            "holderPrincipalId": record["holder_principal_id"],
            "generation": record["generation"],
            # Deliberately avoid a field name containing "fencingToken".  The
            # public value is a truncated digest, never the private grant.
            "fencingFingerprint": record["fencing_fingerprint"],
            "acquiredAt": record["acquired_at"],
            "expiresAt": record["expires_at"],
            "state": record["state"],
            "endedAt": record["ended_at"],
            "endReasonCode": record["end_reason_code"],
            "endReason": record["end_reason"],
            "createdAt": record["created_at"],
            "updatedAt": record["updated_at"],
        }

    def create_controller_lease(self, data: dict[str, Any]) -> dict[str, Any]:
        """Atomically acquire the one durable desktop controller lease."""

        digest = str(data["fencing_hash"])
        fingerprint = str(data["fencing_fingerprint"])
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ValueError("controller fencing hash must be a sha256 digest")
        if fingerprint != f"sha256:{digest.removeprefix('sha256:')[:16]}":
            raise ValueError("controller fencing fingerprint differs from its hash")
        if str(data.get("state", "active")) != "active":
            raise ValueError("new controller leases must be active")
        acquired_at = str(data["acquired_at"])
        expires_at = str(data["expires_at"])
        if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(acquired_at):
            raise ValueError("controller lease expiry must follow acquisition")
        if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            raise ValueError("controller lease cannot be acquired after expiry")
        timestamp = _now()
        with self._write_scope():
            run = self.get_game_run(str(data["run_id"]))
            attempt = self.get_run_attempt(str(data["run_attempt_id"]))
            todo_periods = {
                self.get_todo_instance(todo_id)["period_key"]
                for todo_id in attempt["plan"].get("executableTodoInstanceIds", [])
            }
            attempt_fencing_hash = str(attempt["fencing_token_hash"])
            if not attempt_fencing_hash.startswith("sha256:"):
                attempt_fencing_hash = f"sha256:{attempt_fencing_hash}"
            if (
                attempt["run_id"] != run["run_id"]
                or attempt["game_id"] != run["game_id"]
                or str(data["game_id"]) != run["game_id"]
                or str(data["manager_id"]) == ""
                or digest != attempt_fencing_hash
                or todo_periods != {str(data["game_day_key"])}
            ):
                raise ValueError("controller lease scope differs from its run attempt")
            desktop_id = str(data["desktop_id"])
            expected_generation = int(
                self.connection.execute(
                    """
                    SELECT COALESCE(MAX(generation), 0) + 1
                    FROM controller_leases WHERE desktop_id = ?
                    """,
                    (desktop_id,),
                ).fetchone()[0]
            )
            generation = int(data.get("generation", expected_generation))
            if generation != expected_generation:
                raise ValueError("controller lease generation is stale")
            active = self.connection.execute(
                """
                SELECT controller_lease_id FROM controller_leases
                WHERE desktop_id = ? AND state = 'active' LIMIT 1
                """,
                (desktop_id,),
            ).fetchone()
            # Expiry is an observation, not an implicit authorization.  The old
            # owner must be explicitly released/revoked before this insert.
            if active is not None:
                raise ValueError("interactive desktop already has an active controller")
            lease_id = str(data["controller_lease_id"])
            self.connection.execute(
                """
                INSERT INTO controller_leases(
                    controller_lease_id, desktop_id, manager_id, game_id, run_id,
                    run_attempt_id, game_day_key, holder_principal_id, generation,
                    fencing_hash, fencing_fingerprint, acquired_at, expires_at,
                    state, ended_at, end_reason_code, end_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, NULL, ?, ?)
                """,
                (
                    lease_id,
                    desktop_id,
                    str(data["manager_id"]),
                    str(data["game_id"]),
                    str(data["run_id"]),
                    str(data["run_attempt_id"]),
                    str(data["game_day_key"]),
                    str(data["holder_principal_id"]),
                    generation,
                    digest,
                    fingerprint,
                    acquired_at,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            record = self.get_controller_lease_private(lease_id)
            self.append_event(
                "controller-lease.acquired",
                "controller-lease",
                lease_id,
                self._public_controller_lease(record),
            )
        return record

    def get_controller_lease_private(self, controller_lease_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM controller_leases WHERE controller_lease_id = ?",
                (controller_lease_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(controller_lease_id)
        return self._controller_lease(row, include_hash=True)

    def get_controller_lease_for_attempt(
        self, run_attempt_id: str, *, active_only: bool = False
    ) -> dict[str, Any] | None:
        clause = " AND state = 'active'" if active_only else ""
        with self._lock:
            row = self.connection.execute(
                f"""
                SELECT * FROM controller_leases WHERE run_attempt_id = ?{clause}
                ORDER BY generation DESC LIMIT 1
                """,
                (run_attempt_id,),
            ).fetchone()
        return None if row is None else self._controller_lease(row, include_hash=True)

    def list_controller_leases(
        self, *, run_id: str | None = None, active_only: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if active_only:
            clauses.append("state = 'active'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM controller_leases{where} ORDER BY generation DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [
            self._public_controller_lease(self._controller_lease(row, include_hash=False))
            for row in rows
        ]

    def list_controller_leases_private(
        self, *, desktop_id: str, limit: int = 1000
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM controller_leases WHERE desktop_id = ?
                ORDER BY generation LIMIT ?
                """,
                (desktop_id, limit),
            ).fetchall()
        return [self._controller_lease(row, include_hash=True) for row in rows]

    def transition_controller_lease(
        self,
        controller_lease_id: str,
        *,
        state: str,
        reason_code: str,
        reason: str,
        ended_at: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"released", "revoked", "expired"}:
            raise ValueError("controller lease terminal state is invalid")
        timestamp = ended_at or _now()
        with self._write_scope():
            before = self.get_controller_lease_private(controller_lease_id)
            if before["state"] != "active":
                if (
                    before["state"] == state
                    and before["end_reason_code"] == reason_code
                    and before["end_reason"] == reason
                ):
                    return before
                raise ValueError("controller lease is already terminal")
            if state == "expired" and datetime.fromisoformat(timestamp) < datetime.fromisoformat(
                before["expires_at"]
            ):
                raise ValueError("controller lease cannot expire before its deadline")
            self.connection.execute(
                """
                UPDATE controller_leases SET state = ?, ended_at = ?,
                    end_reason_code = ?, end_reason = ?, updated_at = ?
                WHERE controller_lease_id = ? AND state = 'active'
                """,
                (state, timestamp, reason_code, reason, timestamp, controller_lease_id),
            )
            record = self.get_controller_lease_private(controller_lease_id)
            self.append_event(
                f"controller-lease.{state}",
                "controller-lease",
                controller_lease_id,
                self._public_controller_lease(record),
            )
        return record

    def transition_controller_lease_for_attempt(
        self,
        run_attempt_id: str,
        *,
        state: str,
        reason_code: str,
        reason: str,
    ) -> dict[str, Any] | None:
        with self._write_scope():
            current = self.get_controller_lease_for_attempt(
                run_attempt_id, active_only=True
            )
            if current is None:
                return None
            return self.transition_controller_lease(
                current["controller_lease_id"],
                state=state,
                reason_code=reason_code,
                reason=reason,
            )

    def revoke_stale_controller_leases(self, current_manager_id: str) -> int:
        """Revoke old-process ownership on startup without performing game I/O."""

        with self._write_scope():
            rows = self.connection.execute(
                """
                SELECT controller_lease_id, run_attempt_id, run_id
                FROM controller_leases
                WHERE state = 'active' AND manager_id <> ? ORDER BY generation
                """,
                (current_manager_id,),
            ).fetchall()
            for row in rows:
                self.transition_controller_lease(
                    str(row["controller_lease_id"]),
                    state="revoked",
                    reason_code="manager_process_restarted",
                    reason="previous Manager process instance no longer owns the desktop",
                )
                attempt = self.get_run_attempt(str(row["run_attempt_id"]))
                if attempt["state"] in {"starting", "running", "cancelling"}:
                    blocker_status: dict[str, dict[str, Any]] = {}
                    for todo_attempt in self.list_todo_attempts(
                        run_attempt_id=attempt["run_attempt_id"], limit=5000
                    ):
                        if todo_attempt["state"] != "running":
                            continue
                        terminal = self.finish_todo_attempt(
                            todo_attempt["todo_attempt_id"],
                            status="review_required",
                            reason_code="manager_process_restarted",
                            reason=(
                                "Manager restarted while Adapter outcome was unknown; "
                                "fresh reconciliation is required"
                            ),
                            retryable=False,
                            evidence_refs=[],
                        )
                        blocker_status[terminal["todo_instance_id"]] = {
                            "status": (
                                "not_persisted_missing_current_attempt_evidence"
                            ),
                            "blockerId": None,
                        }
                    self.update_run_attempt(
                        attempt["run_attempt_id"],
                        state="review_required",
                        result={
                            "code": "manager_process_restarted",
                            "message": (
                                "old process ownership was revoked; no game action was taken"
                            ),
                            "blockerPersistenceStatus": blocker_status,
                        },
                        completed=True,
                    )
                    run = self.get_game_run(str(row["run_id"]))
                    if run["state"] not in {"done", "cancelled"}:
                        self.update_game_run(
                            run["run_id"],
                            state="review_required",
                            message=(
                                "previous Manager process was revoked; observation/reconcile required"
                            ),
                        )
        return len(rows)

    @staticmethod
    def _todo_blocker(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "blocker_id": row["blocker_id"],
            "manager_id": row["manager_id"],
            "game_id": row["game_id"],
            "run_id": row["run_id"],
            "run_attempt_id": row["run_attempt_id"],
            "todo_instance_id": row["todo_instance_id"],
            "todo_attempt_id": row["todo_attempt_id"],
            "game_day_key": row["game_day_key"],
            "kind": row["kind"],
            "code": row["code"],
            "state": row["state"],
            "revision": int(row["revision"]),
            "retryable": bool(row["retryable"]),
            "reason": row["reason"],
            "artifact_refs": _decode(row["artifact_refs_json"], []),
            "raised_at": row["raised_at"],
            "transitioned_at": row["transitioned_at"],
            "resolved_at": row["resolved_at"],
            "resolution_code": row["resolution_code"],
            "resolution_reason": row["resolution_reason"],
            "resolution_artifact_refs": _decode(
                row["resolution_artifact_refs_json"], []
            ),
            "release_id": row["release_id"],
            "release_explicit": bool(row["release_explicit"]),
            "released_by": row["released_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_todo_blockers(
        self,
        *,
        run_id: str | None = None,
        todo_instance_id: str | None = None,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if todo_instance_id is not None:
            clauses.append("todo_instance_id = ?")
            parameters.append(todo_instance_id)
        if active_only:
            clauses.append("state = 'active'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM todo_blockers{where} ORDER BY transitioned_at DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [self._todo_blocker(row) for row in rows]

    def get_todo_blocker(self, blocker_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM todo_blockers WHERE blocker_id = ?", (blocker_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(blocker_id)
        return self._todo_blocker(row)

    def create_or_reuse_todo_blocker(
        self, data: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Persist a typed blocker only from current-attempt opaque evidence."""

        artifact_refs = list(dict.fromkeys(str(item) for item in data["artifact_refs"]))
        if not artifact_refs:
            raise ValueError("Todo blocker requires current-attempt evidence")
        kind = str(data["kind"])
        retryable = bool(data.get("retryable", False))
        if kind == "human_required" and retryable:
            raise ValueError("human_required blockers cannot be retryable")
        timestamp = str(data.get("raised_at") or _now())
        with self._write_scope():
            attempt = self.get_run_attempt(str(data["run_attempt_id"]))
            todo_attempt = self.get_todo_attempt(str(data["todo_attempt_id"]))
            todo = self.get_todo_instance(str(data["todo_instance_id"]))
            controller = self.get_controller_lease_for_attempt(
                attempt["run_attempt_id"]
            )
            if (
                attempt["run_id"] != str(data["run_id"])
                or attempt["game_id"] != str(data["game_id"])
                or controller is None
                or controller["manager_id"] != str(data["manager_id"])
                or todo_attempt["run_attempt_id"] != attempt["run_attempt_id"]
                or todo_attempt["todo_instance_id"] != todo["todo_instance_id"]
                or todo["period_key"] != str(data["game_day_key"])
            ):
                raise ValueError("Todo blocker scope differs from its current attempt")
            if todo_attempt["state"] not in {
                "blocked",
                "review_required",
                "human_required",
            }:
                raise ValueError("Todo blocker requires a blocked terminal attempt")
            if not set(artifact_refs).issubset(set(todo_attempt["evidence_refs"])):
                raise ValueError("Todo blocker evidence differs from its Todo attempt")
            for artifact_id in artifact_refs:
                artifact = self.get_resource("artifact", artifact_id)["document"]
                if (
                    artifact.get("runAttemptId") != attempt["run_attempt_id"]
                    or artifact.get("todoAttemptId") != todo_attempt["todo_attempt_id"]
                    or artifact.get("todoInstanceId") != todo["todo_instance_id"]
                ):
                    raise ValueError("Todo blocker evidence belongs to another attempt")
            existing_row = self.connection.execute(
                """
                SELECT * FROM todo_blockers
                WHERE run_id = ? AND todo_instance_id = ? AND state = 'active'
                LIMIT 1
                """,
                (attempt["run_id"], todo["todo_instance_id"]),
            ).fetchone()
            if existing_row is not None:
                return self._todo_blocker(existing_row), "reused_active"
            blocker_id = str(data.get("blocker_id") or uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO todo_blockers(
                    blocker_id, manager_id, game_id, run_id,
                    run_attempt_id, todo_instance_id,
                    todo_attempt_id, game_day_key, kind, code, state, revision,
                    retryable, reason, artifact_refs_json, raised_at,
                    transitioned_at, resolved_at, resolution_code,
                    resolution_reason, resolution_artifact_refs_json, release_id,
                    release_explicit, released_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, '[]', NULL, 0, NULL, ?, ?)
                """,
                (
                    blocker_id,
                    str(data["manager_id"]),
                    attempt["game_id"],
                    attempt["run_id"],
                    attempt["run_attempt_id"],
                    todo["todo_instance_id"],
                    todo_attempt["todo_attempt_id"],
                    todo["period_key"],
                    kind,
                    str(data["code"]),
                    1 if retryable else 0,
                    str(data["reason"]),
                    _json(artifact_refs),
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            record = self.get_todo_blocker(blocker_id)
            self.append_event(
                "todo-blocker.raised", "todo-blocker", blocker_id, record
            )
        return record, "created"

    def resolve_todo_blocker(
        self,
        blocker_id: str,
        *,
        resolution_code: str,
        resolution_reason: str,
        resolution_artifact_refs: list[str] | None = None,
        release_id: str | None = None,
        explicit_release: bool = False,
        released_by: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            before = self.get_todo_blocker(blocker_id)
            if before["state"] != "active":
                if release_id is not None and before["release_id"] == release_id:
                    return before
                raise ValueError("Todo blocker is already resolved")
            if before["kind"] == "human_required" and not explicit_release:
                raise ValueError("human_required blocker needs explicit release")
            artifacts = list(dict.fromkeys(resolution_artifact_refs or []))
            self.connection.execute(
                """
                UPDATE todo_blockers SET state = 'resolved', resolved_at = ?,
                    resolution_code = ?, resolution_reason = ?,
                    resolution_artifact_refs_json = ?, release_id = ?,
                    release_explicit = ?, released_by = ?, transitioned_at = ?,
                    revision = ?, updated_at = ?
                WHERE blocker_id = ? AND state = 'active'
                """,
                (
                    timestamp,
                    resolution_code,
                    resolution_reason,
                    _json(artifacts),
                    release_id,
                    1 if explicit_release else 0,
                    released_by,
                    timestamp,
                    int(before["revision"]) + 1,
                    timestamp,
                    blocker_id,
                ),
            )
            record = self.get_todo_blocker(blocker_id)
            self.append_event(
                "todo-blocker.resolved", "todo-blocker", blocker_id, record
            )
        return record

    @staticmethod
    def _resume_intent(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "resume_intent_id": row["resume_intent_id"],
            "run_id": row["run_id"],
            "predecessor_run_attempt_id": row["predecessor_run_attempt_id"],
            "source_run_revision": int(row["source_run_revision"]),
            "source_attempt_revision": int(row["source_attempt_revision"]),
            "decision_hash": row["decision_hash"],
            "decision": _decode(row["decision_json"], {}),
            "state": row["state"],
            "work_item_id": row["work_item_id"],
            "successor_run_attempt_id": row["successor_run_attempt_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_or_get_resume_intent(self, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        decision = dict(data["decision"])
        if _contains_sensitive_key(decision):
            raise ValueError("resume intent cannot contain fencing material")
        decision_hash = hashlib.sha256(_json(decision).encode("utf-8")).hexdigest()
        timestamp = _now()
        with self._write_scope():
            existing = self.connection.execute(
                """
                SELECT * FROM resume_intents WHERE run_id = ?
                    AND predecessor_run_attempt_id = ?
                    AND source_run_revision = ? AND source_attempt_revision = ?
                """,
                (
                    data["run_id"],
                    data["predecessor_run_attempt_id"],
                    int(data["source_run_revision"]),
                    int(data["source_attempt_revision"]),
                ),
            ).fetchone()
            if existing is not None:
                record = self._resume_intent(existing)
                if record["decision_hash"] != decision_hash:
                    raise ValueError("resume intent snapshot produced another decision")
                return record, True
            intent_id = str(data.get("resume_intent_id") or uuid.uuid4())
            self.connection.execute(
                """
                INSERT INTO resume_intents(
                    resume_intent_id, run_id, predecessor_run_attempt_id,
                    source_run_revision, source_attempt_revision, decision_hash,
                    decision_json, state, work_item_id, successor_run_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    intent_id,
                    data["run_id"],
                    data["predecessor_run_attempt_id"],
                    int(data["source_run_revision"]),
                    int(data["source_attempt_revision"]),
                    decision_hash,
                    _json(decision),
                    str(data["state"]),
                    timestamp,
                    timestamp,
                ),
            )
            record = self.get_resume_intent(intent_id)
            self.append_event("resume-intent.created", "resume-intent", intent_id, record)
        return record, False

    def get_resume_intent(self, resume_intent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM resume_intents WHERE resume_intent_id = ?",
                (resume_intent_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(resume_intent_id)
        return self._resume_intent(row)

    def list_resume_intents(
        self,
        *,
        run_id: str | None = None,
        state: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM resume_intents{where} ORDER BY created_at LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [self._resume_intent(row) for row in rows]

    def update_resume_intent(
        self,
        resume_intent_id: str,
        *,
        state: str,
        work_item_id: str | None = None,
        successor_run_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            before = self.get_resume_intent(resume_intent_id)
            self.connection.execute(
                """
                UPDATE resume_intents SET state = ?,
                    work_item_id = COALESCE(?, work_item_id),
                    successor_run_attempt_id = COALESCE(?, successor_run_attempt_id),
                    updated_at = ? WHERE resume_intent_id = ?
                """,
                (state, work_item_id, successor_run_attempt_id, timestamp, resume_intent_id),
            )
            record = self.get_resume_intent(resume_intent_id)
            if record != before:
                self.append_event(
                    "resume-intent.updated", "resume-intent", resume_intent_id, record
                )
        return record

    def record_execution_control_fact(
        self,
        *,
        fact_type: str,
        fact_id: str,
        manager_id: str,
        game_id: str,
        run_id: str,
        run_attempt_id: str,
        game_day_key: str,
        document: dict[str, Any],
        todo_instance_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an already-observed fact; this method never synthesizes one."""

        if fact_type not in {
            "window_binding",
            "focus_lease",
            "observation",
            "action_receipt",
            "checkpoint",
        }:
            raise ValueError("unsupported execution-control fact type")
        def public_document(value: Any) -> Any:
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    normalized = "".join(
                        character.lower()
                        for character in str(key)
                        if character.isalnum()
                    )
                    safe_key = (
                        "fencingFingerprint"
                        if normalized == "fencingtokenfingerprint"
                        else str(key)
                    )
                    result[safe_key] = public_document(item)
                return result
            if isinstance(value, (list, tuple)):
                return [public_document(item) for item in value]
            return value

        document = public_document(document)
        if _contains_sensitive_key(document):
            raise ValueError("execution-control fact cannot contain fencing material")
        self._assert_public_values(document)
        timestamp = _now()
        with self._write_scope():
            attempt = self.get_run_attempt(run_attempt_id)
            if attempt["run_id"] != run_id or attempt["game_id"] != game_id:
                raise ValueError("execution-control fact scope differs from attempt")
            controller = self.get_controller_lease_for_attempt(run_attempt_id)
            if controller is None or controller["manager_id"] != manager_id:
                raise ValueError("execution-control fact has no Manager controller scope")
            if todo_instance_id is not None:
                todo = self.get_todo_instance(todo_instance_id)
                if (
                    todo_instance_id
                    not in attempt["plan"].get("executableTodoInstanceIds", [])
                    or todo["period_key"] != game_day_key
                ):
                    raise ValueError("execution-control fact Todo/GameDay scope is stale")
            self.connection.execute(
                """
                INSERT INTO execution_control_facts(
                    fact_id, fact_type, manager_id, game_id, run_id,
                    run_attempt_id, todo_instance_id, game_day_key,
                    document_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    fact_type,
                    manager_id,
                    game_id,
                    run_id,
                    run_attempt_id,
                    todo_instance_id,
                    game_day_key,
                    _json(document),
                    timestamp,
                ),
            )
            record = {
                "factId": fact_id,
                "factType": fact_type,
                "managerId": manager_id,
                "gameId": game_id,
                "runId": run_id,
                "runAttemptId": run_attempt_id,
                "todoInstanceId": todo_instance_id,
                "gameDayKey": game_day_key,
                "document": document,
                "createdAt": timestamp,
            }
            self.append_event(
                f"execution-control.{fact_type}.recorded",
                "execution-control-fact",
                fact_id,
                record,
            )
        return record

    def list_execution_control_facts(
        self,
        *,
        fact_type: str | None = None,
        run_id: str | None = None,
        run_attempt_id: str | None = None,
        todo_instance_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("fact_type", fact_type),
            ("run_id", run_id),
            ("run_attempt_id", run_attempt_id),
            ("todo_instance_id", todo_instance_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT * FROM execution_control_facts{where}
                ORDER BY created_at DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [
            {
                "factId": row["fact_id"],
                "factType": row["fact_type"],
                "managerId": row["manager_id"],
                "gameId": row["game_id"],
                "runId": row["run_id"],
                "runAttemptId": row["run_attempt_id"],
                "todoInstanceId": row["todo_instance_id"],
                "gameDayKey": row["game_day_key"],
                "document": _decode(row["document_json"], {}),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def execution_control_summary(self) -> dict[str, Any]:
        with self._lock:
            active_controllers = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM controller_leases WHERE state = 'active'"
                ).fetchone()[0]
            )
            active_blockers = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM todo_blockers WHERE state = 'active'"
                ).fetchone()[0]
            )
            fact_rows = self.connection.execute(
                """
                SELECT fact_type, COUNT(*) AS count FROM execution_control_facts
                GROUP BY fact_type
                """
            ).fetchall()
        fact_counts = {str(row["fact_type"]): int(row["count"]) for row in fact_rows}
        return {
            "activeControllerLeaseCount": active_controllers,
            "activeTodoBlockerCount": active_blockers,
            "factCounts": fact_counts,
            "providers": {
                "windowBinding": "disabled-unimplemented",
                "focusLease": "disabled-unimplemented",
                "observation": "disabled-unimplemented",
                "actionReceipt": "disabled-unimplemented",
                "checkpoint": "disabled-unimplemented",
            },
        }

    def create_work_item(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        record = {
            "work_item_id": str(uuid.uuid4()),
            "kind": data["kind"],
            "state": data["state"],
            "game_id": data.get("game_id"),
            "cadence": data.get("cadence"),
            "run_id": data.get("run_id"),
            "requested_by": data["requested_by"],
            "note": data.get("note", ""),
            "artifact_refs": data.get("artifact_refs", []),
            "allowed_capability_refs": data.get("allowed_capability_refs", []),
            "created_at": timestamp,
            "updated_at": timestamp,
            "result": data.get("result", {}),
        }
        if _contains_sensitive_key(record["result"]):
            raise ValueError("work item results must not persist fencing material")
        self._assert_public_values(record)
        with self._write_scope():
            self.connection.execute(
                """
                INSERT INTO work_items(
                    work_item_id, kind, state, game_id, cadence, run_id,
                    requested_by, note, artifact_refs_json,
                    allowed_capability_refs_json, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["work_item_id"],
                    record["kind"],
                    record["state"],
                    record["game_id"],
                    record["cadence"],
                    record["run_id"],
                    record["requested_by"],
                    record["note"],
                    _json(record["artifact_refs"]),
                    _json(record["allowed_capability_refs"]),
                    _json(record["result"]),
                    timestamp,
                    timestamp,
                ),
            )
            self.append_event(
                "agent-work-item.created", "work-item", record["work_item_id"], record
            )
        return record

    def list_work_items(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM work_items ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._work_item(row) for row in rows]

    def get_work_item(self, work_item_id: str) -> dict[str, Any]:
        return self._one("work_items", "work_item_id", work_item_id, self._work_item)

    def update_work_item(
        self, work_item_id: str, *, state: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._write_scope():
            existing = self.get_work_item(work_item_id)
            merged_result = {**existing["result"], **result}
            if _contains_sensitive_key(merged_result):
                raise ValueError("work item results must not persist fencing material")
            self._assert_public_values(work_item_id, state, merged_result)
            cursor = self.connection.execute(
                "UPDATE work_items SET state = ?, result_json = ?, updated_at = ? WHERE work_item_id = ?",
                (state, _json(merged_result), timestamp, work_item_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(work_item_id)
            record = self.get_work_item(work_item_id)
            self.append_event(
                "agent-work-item.updated", "work-item", work_item_id, record
            )
        return record

    def append_work_item_artifact(
        self, work_item_id: str, artifact_id: str
    ) -> dict[str, Any]:
        """Append one Manager-owned opaque artifact to an existing work-item scope."""
        timestamp = _now()
        with self._write_scope():
            existing = self.get_work_item(work_item_id)
            refs = list(existing["artifact_refs"])
            if artifact_id not in refs:
                refs.append(artifact_id)
            self._assert_public_values(work_item_id, artifact_id, refs)
            cursor = self.connection.execute(
                "UPDATE work_items SET artifact_refs_json = ?, updated_at = ? WHERE work_item_id = ?",
                (_json(refs), timestamp, work_item_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(work_item_id)
            record = self.get_work_item(work_item_id)
            self.append_event(
                "agent-work-item.updated", "work-item", work_item_id, record
            )
        return record

    @staticmethod
    def _work_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "work_item_id": row["work_item_id"],
            "kind": row["kind"],
            "state": row["state"],
            "game_id": row["game_id"],
            "cadence": row["cadence"],
            "run_id": row["run_id"],
            "requested_by": row["requested_by"],
            "note": row["note"],
            "artifact_refs": _decode(row["artifact_refs_json"], []),
            "allowed_capability_refs": _decode(
                row["allowed_capability_refs_json"], []
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result": _decode(row["result_json"], {}),
        }

    def create_claim_decision(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        record = {
            "decision_id": str(uuid.uuid4()),
            "claim_id": data["claim_id"],
            "decision": data["decision"],
            "reason": data["reason"],
            "evidence_ids": data.get("evidence_ids", []),
            "requested_by": data["requested_by"],
            "created_at": timestamp,
        }
        with self._write_scope():
            self.connection.execute(
                "INSERT INTO claim_decisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record["decision_id"],
                    record["claim_id"],
                    record["decision"],
                    record["reason"],
                    _json(record["evidence_ids"]),
                    record["requested_by"],
                    timestamp,
                ),
            )
            self.append_event(
                "claim-decision.created", "claim-decision", record["decision_id"], record
            )
        return record

    def create_work_item_claim(self, data: dict[str, Any]) -> dict[str, Any]:
        record = {
            "claim_id": str(uuid.uuid4()),
            "work_item_id": data["work_item_id"],
            "claimant": data["claimant"],
            "state": "active",
            "claimed_at": data["claimed_at"],
            "expires_at": data["expires_at"],
            "fencing_token": data["fencing_token"],
        }
        with self._write_scope():
            active = self.connection.execute(
                """
                SELECT * FROM work_item_claims
                WHERE work_item_id = ? AND state = 'active' AND expires_at > ?
                """,
                (record["work_item_id"], record["claimed_at"]),
            ).fetchone()
            if active is not None:
                if active["claimant"] != record["claimant"]:
                    raise ValueError("work item already has an active claim")
                self.connection.execute(
                    "UPDATE work_item_claims SET expires_at = ? WHERE claim_id = ?",
                    (record["expires_at"], active["claim_id"]),
                )
                renewed = self.get_work_item_claim_private(active["claim_id"])
                self._assert_public_values(self._public_work_item_claim(renewed))
                self.append_event(
                    "work-item.claim-renewed",
                    "work-item",
                    record["work_item_id"],
                    self._public_work_item_claim(renewed),
                )
                return renewed
            self.connection.execute(
                """
                UPDATE work_item_claims SET state = 'expired'
                WHERE work_item_id = ? AND state = 'active' AND expires_at <= ?
                """,
                (record["work_item_id"], record["claimed_at"]),
            )
            self._assert_candidate_fencing_token_is_private(
                str(record["fencing_token"]), self._public_work_item_claim(record)
            )
            self.connection.execute(
                """
                INSERT INTO work_item_claims(
                    claim_id, work_item_id, claimant, state,
                    claimed_at, expires_at, fencing_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["claim_id"],
                    record["work_item_id"],
                    record["claimant"],
                    record["state"],
                    record["claimed_at"],
                    record["expires_at"],
                    record["fencing_token"],
                ),
            )
            self.append_event(
                "work-item.claimed",
                "work-item",
                record["work_item_id"],
                self._public_work_item_claim(record),
            )
        return record

    def resolve_work_item_claim(self, claim_id: str, decision_id: str) -> dict[str, Any]:
        with self._write_scope():
            cursor = self.connection.execute(
                "UPDATE work_item_claims SET state = 'resolved' WHERE claim_id = ? AND state = 'active'",
                (claim_id,),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(claim_id)
            record = self.get_work_item_claim_private(claim_id)
            self.append_event(
                "work-item.claim-resolved",
                "work-item",
                record["work_item_id"],
                {**self._public_work_item_claim(record), "decisionId": decision_id},
            )
        return record

    def get_work_item_claim(self, claim_id: str) -> dict[str, Any]:
        return self._one(
            "work_item_claims", "claim_id", claim_id, self._work_item_claim
        )

    def get_work_item_claim_private(self, claim_id: str) -> dict[str, Any]:
        return self._one(
            "work_item_claims", "claim_id", claim_id, self._work_item_claim_private
        )

    def list_work_item_claims(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM work_item_claims ORDER BY claimed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._work_item_claim(row) for row in rows]

    @staticmethod
    def _work_item_claim(row: sqlite3.Row) -> dict[str, Any]:
        return SqliteStore._public_work_item_claim(
            SqliteStore._work_item_claim_private(row)
        )

    @staticmethod
    def _work_item_claim_private(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "claim_id": row["claim_id"],
            "work_item_id": row["work_item_id"],
            "claimant": row["claimant"],
            "state": row["state"],
            "claimed_at": row["claimed_at"],
            "expires_at": row["expires_at"],
            "fencing_token": row["fencing_token"],
        }

    @staticmethod
    def _public_work_item_claim(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in record.items() if key != "fencing_token"
        }

    def list_claim_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM claim_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._claim_decision(row) for row in rows]

    def get_claim_decision(self, decision_id: str) -> dict[str, Any]:
        return self._one(
            "claim_decisions", "decision_id", decision_id, self._claim_decision
        )

    @staticmethod
    def _claim_decision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "claim_id": row["claim_id"],
            "decision": row["decision"],
            "reason": row["reason"],
            "evidence_ids": _decode(row["evidence_ids_json"], []),
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
        }

    def create_capability_invocation(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        record = {
            "invocation_id": str(uuid.uuid4()),
            "capability": data["capability"],
            "state": data["state"],
            "arguments": data.get("arguments", {}),
            "requested_by": data["requested_by"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "result": data.get("result", {}),
        }
        with self._write_scope():
            self.connection.execute(
                "INSERT INTO capability_invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["invocation_id"],
                    record["capability"],
                    record["state"],
                    _json(record["arguments"]),
                    record["requested_by"],
                    _json(record["result"]),
                    timestamp,
                    timestamp,
                ),
            )
            self.append_event(
                "capability-invocation.created",
                "capability-invocation",
                record["invocation_id"],
                record,
            )
        return record

    def list_capability_invocations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM capability_invocations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._capability_invocation(row) for row in rows]

    def get_capability_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self._one(
            "capability_invocations",
            "invocation_id",
            invocation_id,
            self._capability_invocation,
        )

    @staticmethod
    def _capability_invocation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "invocation_id": row["invocation_id"],
            "capability": row["capability"],
            "state": row["state"],
            "arguments": _decode(row["arguments_json"], {}),
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result": _decode(row["result_json"], {}),
        }

    def append_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if _contains_sensitive_key(payload):
            raise ValueError("public events must not persist fencing material")
        self._assert_public_values(event_type, entity_type, entity_id, payload)
        event_id = str(uuid.uuid4())
        timestamp = _now()
        with self._write_scope():
            cursor = self.connection.execute(
                """
                INSERT INTO events(
                    event_id, event_type, entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, event_type, entity_type, entity_id, _json(payload), timestamp),
            )
            sequence = cursor.lastrowid
        return {
            "sequence": sequence,
            "event_id": event_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "created_at": timestamp,
        }

    def list_events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def list_log_events(
        self,
        *,
        after: int = 0,
        before: int | None = None,
        recent: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read the event ledger using the pagination modes exposed by ``/logs``.

        Event streaming deliberately keeps using :meth:`list_events`, whose
        forward-only ``sequence > after`` contract must not change.  Logs also
        need a bounded tail and backward paging: both queries read descending
        so SQLite applies ``LIMIT`` to the newest matching rows, then the result
        is reversed for chronological presentation.
        """

        if recent and before is not None:
            raise ValueError("recent and before log cursors are mutually exclusive")
        if (recent or before is not None) and after:
            raise ValueError("after cannot be combined with recent or before")

        if not recent and before is None:
            return self.list_events(after=after, limit=limit)

        with self._lock:
            if recent:
                rows = self.connection.execute(
                    "SELECT * FROM events ORDER BY sequence DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self.connection.execute(
                    """
                    SELECT * FROM events
                    WHERE sequence < ?
                    ORDER BY sequence DESC
                    LIMIT ?
                    """,
                    (before, limit),
                ).fetchall()
        rows.reverse()
        return [self._event(row) for row in rows]

    def latest_todo_reset_at(self, todo_instance_ids: set[str]) -> str | None:
        """Return the latest explicit-reset watermark for an exact Todo scope."""

        normalized = sorted(
            {str(value) for value in todo_instance_ids if str(value)}
        )
        if not normalized:
            return None
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            row = self.connection.execute(
                f"""
                SELECT created_at FROM events
                WHERE event_type = 'todo.current-period-explicit-reset'
                  AND entity_type = 'todo-instance'
                  AND entity_id IN ({placeholders})
                ORDER BY sequence DESC LIMIT 1
                """,
                normalized,
            ).fetchone()
        return str(row["created_at"]) if row is not None else None

    def latest_event_sequence(self) -> int:
        with self._lock:
            value = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events"
            ).fetchone()[0]
        return int(value)

    def entity_revision(self, entity_type: str, entity_id: str) -> int:
        """Return the last durable ledger sequence for one Manager entity."""

        with self._lock:
            value = self.connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) FROM events
                WHERE entity_type = ? AND entity_id = ?
                """,
                (entity_type, entity_id),
            ).fetchone()[0]
        return int(value)

    def active_execution_summary(self) -> dict[str, list[dict[str, str]]]:
        """Return durable executions that make Manager shutdown unsafe."""
        active_states = ("pending_execution", "queued", "running", "cancelling")
        placeholders = ",".join("?" for _ in active_states)
        with self._lock:
            batches = self.connection.execute(
                f"SELECT batch_id, state FROM batches WHERE state IN ({placeholders}) "
                "ORDER BY created_at",
                active_states,
            ).fetchall()
            runs = self.connection.execute(
                f"SELECT run_id, state FROM game_runs WHERE state IN ({placeholders}) "
                "ORDER BY created_at",
                active_states,
            ).fetchall()
        return {
            "batches": [
                {"batchId": str(row["batch_id"]), "state": str(row["state"])}
                for row in batches
            ],
            "gameRuns": [
                {"runId": str(row["run_id"]), "state": str(row["state"])}
                for row in runs
            ],
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "payload": _decode(row["payload_json"], {}),
            "created_at": row["created_at"],
        }

    def quick_check(self) -> str:
        with self._lock:
            return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])

    # Ledger rows that must survive retention because a later read depends on
    # their existence rather than on the latest projection.
    LEDGER_PROTECTED_EVENT_TYPES = (
        "todo.current-period-explicit-reset",
        "batch.sealed",
        "manager.started",
    )

    def ledger_size_report(self) -> dict[str, Any]:
        with self._lock:
            page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
            freelist = int(self.connection.execute("PRAGMA freelist_count").fetchone()[0])
            counts = {
                table: int(
                    self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in ("events", "adapter_events", "idempotency", "command_receipts")
            }
        return {
            "fileBytes": page_count * page_size,
            "freeBytes": freelist * page_size,
            "rows": counts,
        }

    def prune_ledger(
        self,
        *,
        now: datetime | None = None,
        event_retention_days: int = 14,
        receipt_retention_days: int = 14,
    ) -> dict[str, int]:
        """Delete aged ledger rows that no projection or CAS check still needs.

        ``events.sequence`` is AUTOINCREMENT, so deleting old rows never moves
        the state version.  The newest event of every entity is always kept so
        ``entity_revision`` keeps answering, and protected event types stay.
        Idempotency keys and command receipts older than the receipt window are
        dropped together so a replay can never find one without the other.
        """

        moment = now or datetime.now(timezone.utc)
        event_cutoff = (moment - timedelta(days=max(1, event_retention_days))).isoformat()
        receipt_cutoff = (moment - timedelta(days=max(1, receipt_retention_days))).isoformat()
        protected = ",".join("?" for _ in self.LEDGER_PROTECTED_EVENT_TYPES)
        with self._write_scope():
            events = self.connection.execute(
                f"""
                DELETE FROM events
                WHERE created_at < ?
                  AND event_type NOT IN ({protected})
                  AND sequence NOT IN (
                      SELECT MAX(sequence) FROM events GROUP BY entity_type, entity_id
                  )
                """,
                (event_cutoff, *self.LEDGER_PROTECTED_EVENT_TYPES),
            ).rowcount
            idempotency = self.connection.execute(
                "DELETE FROM idempotency WHERE created_at < ?", (receipt_cutoff,)
            ).rowcount
            receipts = self.connection.execute(
                """
                DELETE FROM command_receipts
                WHERE updated_at < ?
                  AND json_extract(receipt_json, '$.state') != 'running'
                """,
                (receipt_cutoff,),
            ).rowcount
        return {
            "events": int(events),
            "idempotency": int(idempotency),
            "commandReceipts": int(receipts),
        }

    def compact_batch_event_payloads(self, *, minimum_bytes: int = 8192) -> int:
        """Rewrite historical batch events that still embed the full result.

        Older Managers stored the complete batch result in every
        ``batch.updated`` row.  The rows keep their sequence/identity; only the
        payload is reduced to the same projection new events use.
        """

        rewritten = 0
        with self._write_scope():
            rows = self.connection.execute(
                """
                SELECT sequence, payload_json FROM events
                WHERE event_type IN ('batch.created', 'batch.updated', 'batch.sealed')
                  AND length(payload_json) > ?
                """,
                (minimum_bytes,),
            ).fetchall()
            for row in rows:
                payload = _decode(row["payload_json"], {})
                if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                    continue
                compact = _json(_batch_event_payload(payload))
                if len(compact) >= len(row["payload_json"]):
                    continue
                self.connection.execute(
                    "UPDATE events SET payload_json = ? WHERE sequence = ?",
                    (compact, int(row["sequence"])),
                )
                rewritten += 1
        return rewritten

    def vacuum_if_fragmented(self, *, minimum_free_bytes: int = 64 * 1024 * 1024) -> bool:
        """Rebuild the file when enough space was freed; blocks other writers briefly."""

        report = self.ledger_size_report()
        if report["freeBytes"] < minimum_free_bytes:
            return False
        with self._lock:
            if self._transaction_depth != 0 or self.connection.in_transaction:
                return False
            self.connection.execute("VACUUM")
        return True

    def save_command_receipt(self, command_id: str, receipt: dict[str, Any]) -> None:
        if _contains_sensitive_key(receipt):
            raise ValueError("command receipts must not persist fencing material")
        self._assert_public_values(command_id, receipt)
        with self._write_scope():
            self.connection.execute(
                """
                INSERT INTO command_receipts(command_id, receipt_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                    receipt_json = excluded.receipt_json,
                    updated_at = excluded.updated_at
                """,
                (command_id, _json(receipt), _now()),
            )

    def get_command_receipt(self, command_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT receipt_json FROM command_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFound(command_id)
        return _decode(row["receipt_json"], {})

    def snapshot_data(self) -> dict[str, Any]:
        """Return one lock-consistent projection without mutating any source."""
        with self._lock:
            games = self.list_games()
            batches = self.list_batches(20)
            game_runs = self.list_game_runs(100)
            work_items = self.list_work_items(100)
            decisions = self.list_claim_decisions(100)
            latest_sequence = self.latest_event_sequence()
        return {
            "games": games,
            "batches": batches,
            "game_runs": game_runs,
            "work_items": work_items,
            "decisions": decisions,
            "latest_event_sequence": latest_sequence,
        }

    def create_resource(
        self,
        resource_type: str,
        *,
        state: str,
        document: dict[str, Any],
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        if resource_type not in {
            "artifact",
            "evidence-review",
            "repair-session",
            "repair-verification",
            "adapter-governance-request",
            "adapter-diagnostic-canary",
            "adapter-promotion-receipt",
            "diagnostic-bundle",
            "run-control-request",
            "notification",
            "config-validation",
            "automation-assessment",
            "completion-review",
            "completion-adjudication",
            "window-observation",
        }:
            raise ValueError("unsupported Manager resource type")
        timestamp = _now()
        record = {
            "resource_id": resource_id or str(uuid.uuid4()),
            "resource_type": resource_type,
            "state": state,
            "document": dict(document),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self._write_scope():
            self.connection.execute(
                """
                INSERT INTO manager_resources(
                    resource_id, resource_type, state, document_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["resource_id"],
                    resource_type,
                    state,
                    _json(record["document"]),
                    timestamp,
                    timestamp,
                ),
            )
            self.append_event(
                f"{resource_type}.created",
                resource_type,
                record["resource_id"],
                record,
            )
        return record

    def list_resources(
        self, resource_type: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM manager_resources
                WHERE resource_type = ? ORDER BY created_at DESC LIMIT ?
                """,
                (resource_type, limit),
            ).fetchall()
        return [self._resource(row) for row in rows]

    def get_adapter_artifact_usage(
        self,
        *,
        run_attempt_id: str,
        todo_instance_id: str,
    ) -> dict[str, int]:
        """Aggregate one Adapter attempt's artifact quota usage in SQLite."""

        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS run_artifact_count,
                    COALESCE(SUM(
                        CASE
                            WHEN json_extract(
                                document_json, '$.todoInstanceId'
                            ) = ? THEN 1
                            ELSE 0
                        END
                    ), 0) AS todo_artifact_count,
                    COALESCE(SUM(
                        CASE
                            WHEN json_type(
                                document_json, '$.sizeBytes'
                            ) = 'integer'
                            AND json_extract(
                                document_json, '$.sizeBytes'
                            ) >= 1
                            THEN json_extract(
                                document_json, '$.sizeBytes'
                            )
                            ELSE 0
                        END
                    ), 0) AS run_artifact_bytes,
                    COALESCE(SUM(
                        CASE
                            WHEN json_type(
                                document_json, '$.sizeBytes'
                            ) = 'integer'
                            AND json_extract(
                                document_json, '$.sizeBytes'
                            ) >= 1
                            THEN 0
                            ELSE 1
                        END
                    ), 0) AS invalid_size_count
                FROM manager_resources
                WHERE resource_type = 'artifact'
                  AND json_extract(document_json, '$.runAttemptId') = ?
                """,
                (todo_instance_id, run_attempt_id),
            ).fetchone()
        if row is None:  # pragma: no cover - aggregate queries always return a row
            return {
                "run_artifact_count": 0,
                "todo_artifact_count": 0,
                "run_artifact_bytes": 0,
                "invalid_size_count": 0,
            }
        return {
            "run_artifact_count": int(row["run_artifact_count"]),
            "todo_artifact_count": int(row["todo_artifact_count"]),
            "run_artifact_bytes": int(row["run_artifact_bytes"]),
            "invalid_size_count": int(row["invalid_size_count"]),
        }

    def get_resource(
        self, resource_type: str, resource_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM manager_resources
                WHERE resource_type = ? AND resource_id = ?
                """,
                (resource_type, resource_id),
            ).fetchone()
        if row is None:
            raise RecordNotFound(resource_id)
        return self._resource(row)

    def update_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        state: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        if resource_type in {
            "completion-review",
            "completion-adjudication",
            "adapter-diagnostic-canary",
            "adapter-promotion-receipt",
        }:
            raise ValueError(f"{resource_type} resources are immutable")
        timestamp = _now()
        with self._write_scope():
            cursor = self.connection.execute(
                """
                UPDATE manager_resources
                SET state = ?, document_json = ?, updated_at = ?
                WHERE resource_type = ? AND resource_id = ?
                """,
                (state, _json(document), timestamp, resource_type, resource_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(resource_id)
            record = self.get_resource(resource_type, resource_id)
            self.append_event(
                f"{resource_type}.updated", resource_type, resource_id, record
            )
        return record

    @staticmethod
    def _resource(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "resource_id": row["resource_id"],
            "resource_type": row["resource_type"],
            "state": row["state"],
            "document": _decode(row["document_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _one(
        self,
        table: str,
        key_column: str,
        key: str,
        mapper: Callable[[sqlite3.Row], T],
    ) -> T:
        allowed = {
            ("batches", "batch_id"),
            ("game_runs", "run_id"),
            ("work_items", "work_item_id"),
            ("claim_decisions", "decision_id"),
            ("capability_invocations", "invocation_id"),
            ("work_item_claims", "claim_id"),
            ("todo_definitions", "todo_definition_id"),
            ("todo_instances", "todo_instance_id"),
        }
        if (table, key_column) not in allowed:
            raise ValueError("unsupported lookup")
        with self._lock:
            row = self.connection.execute(
                f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(key)
        return mapper(row)
