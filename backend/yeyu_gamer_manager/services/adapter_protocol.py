from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


PROTOCOL_VERSION = "1.1"
SCHEMA_VERSION = 1
EXECUTION_PACKAGE_SCHEMA_VERSION = 2
EXECUTION_PACKAGE_ID = "legacy-night-rain-gamer"
HOST_PACKAGE_ID = "manager-adapter-host"

MAX_REQUEST_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 8 * 1024
MAX_TODOS_PER_REQUEST = 64
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_ARTIFACTS_PER_TODO = 20
MAX_RUN_ARTIFACT_BYTES = 256 * 1024 * 1024

ALLOWED_GAME_IDS = frozenset(
    {
        "WW",
        "PGR",
        "StarRail",
        "ZZZ",
        "Endfield",
        "GF2",
        "NTE",
        "FGO",
        "NIKKE",
        "BD2",
        "CZN",
    }
)
ALLOWED_CADENCES = frozenset({"daily", "weekly", "manual"})
ALLOWED_RISKS = frozenset({"observe_only", "routine_action"})
TODO_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "skipped",
        "blocked",
        "review_required",
        "human_required",
    }
)
RUN_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "partial",
        "blocked",
        "review_required",
        "human_required",
        "cancelled",
        "failed",
    }
)
TRANSPORT_OUTCOMES = frozenset({"clean", "cancelled", "timeout", "crashed"})
ALLOWED_ARTIFACT_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "text/plain"}
)
REQUIRED_FORBIDDEN_OPERATION_CLASSES = frozenset(
    {
        "gacha",
        "purchase",
        "dismantle",
        "enhance",
        "trade",
        "account_settings",
        "pvp",
        "irreversible_choice",
        "arbitrary_command",
        "arbitrary_path",
        "arbitrary_input",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPABILITY_REF = re.compile(
    r"^[a-z0-9][a-z0-9._:-]{0,95}@[1-9][0-9]*\.[0-9]+$"
)
_TODO_INSTANCE_ID = re.compile(
    r"^todo-instance-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_LEAF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AdapterProtocolError(ValueError):
    """Protocol or package input failed closed validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NoExecutableTodos(AdapterProtocolError):
    def __init__(self) -> None:
        super().__init__(
            "no_executable_todos",
            "Adapter execution requires at least one executable Todo instance",
        )


def run_artifact_byte_limit(
    *,
    executable_todo_count: int,
    max_artifacts_per_todo: int = MAX_ARTIFACTS_PER_TODO,
    max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
) -> int:
    """Return the hard aggregate artifact-byte limit for one run attempt."""

    if (
        executable_todo_count < 1
        or not 1 <= max_artifacts_per_todo <= MAX_ARTIFACTS_PER_TODO
        or not 1 <= max_artifact_bytes <= MAX_ARTIFACT_BYTES
    ):
        raise ValueError("artifact byte limit inputs are outside their bounds")
    return min(
        MAX_RUN_ARTIFACT_BYTES,
        executable_todo_count * max_artifacts_per_todo * max_artifact_bytes,
    )


def _exact_object(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
    context: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AdapterProtocolError("invalid_schema", f"{context} must be an object")
    allowed = required | (optional or set())
    missing = required - set(value)
    unexpected = set(value) - allowed
    if missing or unexpected:
        raise AdapterProtocolError(
            "invalid_schema",
            f"{context} fields differ from the protocol contract",
        )
    return value


def _text(value: object, *, field: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdapterProtocolError("invalid_schema", f"{field} is invalid")
    if any(ord(character) < 32 for character in value):
        raise AdapterProtocolError("invalid_schema", f"{field} contains control text")
    return value


def _identifier(value: object, *, field: str) -> str:
    text = _text(value, field=field, maximum=128)
    if not _IDENTIFIER.fullmatch(text):
        raise AdapterProtocolError("invalid_schema", f"{field} is not an identifier")
    return text


def _canonical_uuid(value: object, *, field: str) -> str:
    text = _text(value, field=field, maximum=36)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as error:
        raise AdapterProtocolError("invalid_scope", f"{field} is not a UUID") from error
    if str(parsed) != text:
        raise AdapterProtocolError(
            "invalid_scope", f"{field} must be a lowercase canonical UUID"
        )
    return text


def _todo_instance_id(value: object, *, field: str = "todoInstanceId") -> str:
    text = _text(value, field=field, maximum=50)
    matched = _TODO_INSTANCE_ID.fullmatch(text)
    if matched is None:
        raise AdapterProtocolError("invalid_scope", f"{field} is invalid")
    _canonical_uuid(matched.group(1), field=field)
    return text


def _capability_ref(value: object, *, field: str = "adapterCapabilityRef") -> str:
    text = _text(value, field=field, maximum=128)
    if not _CAPABILITY_REF.fullmatch(text):
        raise AdapterProtocolError("invalid_schema", f"{field} is invalid")
    return text


def _integer(
    value: object, *, field: str, minimum: int, maximum: int
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise AdapterProtocolError("invalid_schema", f"{field} is out of range")
    return value


def _iso_time(value: object, *, field: str) -> tuple[str, datetime]:
    text = _text(value, field=field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterProtocolError("invalid_schema", f"{field} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AdapterProtocolError("invalid_schema", f"{field} requires a timezone")
    return text, parsed


def _string_list(
    value: object,
    *,
    field: str,
    allow_empty: bool,
    uuid_values: bool = False,
    todo_instance_values: bool = False,
    capability_ref_values: bool = False,
    maximum: int = MAX_TODOS_PER_REQUEST,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise AdapterProtocolError("invalid_schema", f"{field} must be a bounded list")
    if not allow_empty and not value:
        raise NoExecutableTodos()
    if sum((uuid_values, todo_instance_values, capability_ref_values)) > 1:
        raise RuntimeError("string list validator kind is ambiguous")
    items = tuple(
        _canonical_uuid(item, field=field)
        if uuid_values
        else _todo_instance_id(item, field=field)
        if todo_instance_values
        else _capability_ref(item, field=field)
        if capability_ref_values
        else _identifier(item, field=field)
        for item in value
    )
    if len(set(items)) != len(items):
        raise AdapterProtocolError("duplicate_value", f"{field} contains duplicates")
    return items


def _decode_json_object(raw: bytes, *, maximum: int, context: str) -> Mapping[str, Any]:
    if not raw or len(raw) > maximum:
        raise AdapterProtocolError(
            "payload_too_large" if raw else "invalid_json",
            f"{context} size is invalid",
        )
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder()
        stripped = text.lstrip()
        value, end = decoder.raw_decode(stripped)
        if stripped[end:].strip():
            raise ValueError("trailing JSON")
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise AdapterProtocolError("invalid_json", f"{context} is not one JSON object") from error
    if not isinstance(value, dict):
        raise AdapterProtocolError("invalid_json", f"{context} must be a JSON object")
    return value


@dataclass(frozen=True)
class AdapterTodoTarget:
    todo_instance_id: str
    todo_definition_id: str
    definition_version: int
    operation: str
    risk: str
    adapter_capability_ref: str
    prior_attempts: int
    execution_disposition: str = "executable"

    @classmethod
    def from_document(cls, document: object) -> "AdapterTodoTarget":
        value = _exact_object(
            document,
            required={
                "todoInstanceId",
                "todoDefinitionId",
                "definitionVersion",
                "operation",
                "risk",
                "adapterCapabilityRef",
                "priorAttempts",
                "executionDisposition",
            },
            context="Todo target",
        )
        disposition = _text(
            value["executionDisposition"], field="executionDisposition", maximum=32
        )
        if disposition != "executable":
            raise AdapterProtocolError(
                "todo_not_executable",
                "Only executableTodoInstanceIds may enter Adapter execution",
            )
        risk = _text(value["risk"], field="risk", maximum=32)
        if risk not in ALLOWED_RISKS:
            raise AdapterProtocolError("risk_not_executable", "Todo risk is not executable")
        return cls(
            todo_instance_id=_todo_instance_id(
                value["todoInstanceId"], field="todoInstanceId"
            ),
            todo_definition_id=_identifier(
                value["todoDefinitionId"], field="todoDefinitionId"
            ),
            definition_version=_integer(
                value["definitionVersion"],
                field="definitionVersion",
                minimum=1,
                maximum=2**31 - 1,
            ),
            operation=_identifier(value["operation"], field="operation"),
            risk=risk,
            adapter_capability_ref=_capability_ref(
                value["adapterCapabilityRef"], field="adapterCapabilityRef"
            ),
            prior_attempts=_integer(
                value["priorAttempts"],
                field="priorAttempts",
                minimum=0,
                maximum=2**31 - 1,
            ),
            execution_disposition=disposition,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "todoInstanceId": self.todo_instance_id,
            "todoDefinitionId": self.todo_definition_id,
            "definitionVersion": self.definition_version,
            "operation": self.operation,
            "risk": self.risk,
            "adapterCapabilityRef": self.adapter_capability_ref,
            "priorAttempts": self.prior_attempts,
            "executionDisposition": self.execution_disposition,
        }


@dataclass(frozen=True)
class AdapterExecutionPlan:
    run_id: str
    run_attempt_id: str
    fencing_token: str
    cancel_authority: str
    game_id: str
    cadence: str
    manager_state_version: int
    catalog_version: str
    policy_digest: str
    issued_at: str
    expires_at: str
    timeout_seconds: int
    executable_todo_instance_ids: tuple[str, ...]
    todos: tuple[AdapterTodoTarget, ...]
    preserve_client_on_stop: bool = True

    @classmethod
    def from_document(cls, document: object) -> "AdapterExecutionPlan":
        value = _exact_object(
            document,
            required={
                "schemaVersion",
                "protocolVersion",
                "requestType",
                "runId",
                "runAttemptId",
                "fencingToken",
                "cancelAuthority",
                "gameId",
                "cadence",
                "managerStateVersion",
                "catalogVersion",
                "policyDigest",
                "issuedAt",
                "expiresAt",
                "timeoutSeconds",
                "preserveClientOnStop",
                "executableTodoInstanceIds",
                "todos",
            },
            context="execute request",
        )
        if value["schemaVersion"] != SCHEMA_VERSION:
            raise AdapterProtocolError("unsupported_schema", "schemaVersion is unsupported")
        if value["protocolVersion"] != PROTOCOL_VERSION:
            raise AdapterProtocolError(
                "protocol_version_mismatch", "protocolVersion is unsupported"
            )
        if value["requestType"] != "execute":
            raise AdapterProtocolError("invalid_schema", "requestType must be execute")
        game_id = _text(value["gameId"], field="gameId", maximum=32)
        if game_id not in ALLOWED_GAME_IDS:
            raise AdapterProtocolError("invalid_scope", "gameId is not allowed")
        cadence = _text(value["cadence"], field="cadence", maximum=16)
        if cadence not in ALLOWED_CADENCES:
            raise AdapterProtocolError("invalid_schema", "cadence is unsupported")
        token = _text(value["fencingToken"], field="fencingToken", maximum=256)
        if not _TOKEN.fullmatch(token):
            raise AdapterProtocolError("invalid_scope", "fencingToken is invalid")
        cancel_authority = _text(
            value["cancelAuthority"], field="cancelAuthority", maximum=256
        )
        if not _TOKEN.fullmatch(cancel_authority) or cancel_authority == token:
            raise AdapterProtocolError("invalid_scope", "cancelAuthority is invalid")
        digest = _text(value["policyDigest"], field="policyDigest", maximum=71)
        if not _SHA256.fullmatch(digest):
            raise AdapterProtocolError("invalid_schema", "policyDigest is invalid")
        issued_text, issued = _iso_time(value["issuedAt"], field="issuedAt")
        expires_text, expires = _iso_time(value["expiresAt"], field="expiresAt")
        if expires <= issued:
            raise AdapterProtocolError("invalid_schema", "expiresAt must follow issuedAt")
        if value["preserveClientOnStop"] is not True:
            raise AdapterProtocolError(
                "unsafe_stop_policy", "preserveClientOnStop must be true"
            )
        target_ids = _string_list(
            value["executableTodoInstanceIds"],
            field="executableTodoInstanceIds",
            allow_empty=False,
            todo_instance_values=True,
        )
        todo_documents = value["todos"]
        if not isinstance(todo_documents, list) or not todo_documents:
            raise NoExecutableTodos()
        if len(todo_documents) > MAX_TODOS_PER_REQUEST:
            raise AdapterProtocolError("invalid_schema", "todos exceeds its limit")
        todos = tuple(AdapterTodoTarget.from_document(item) for item in todo_documents)
        todo_ids = tuple(item.todo_instance_id for item in todos)
        if len(set(todo_ids)) != len(todo_ids):
            raise AdapterProtocolError("duplicate_value", "todos contains duplicate IDs")
        if todo_ids != target_ids:
            raise AdapterProtocolError(
                "executable_todo_mismatch",
                "todos must exactly match executableTodoInstanceIds in order",
            )
        timeout_seconds = _integer(
            value["timeoutSeconds"],
            field="timeoutSeconds",
            minimum=1,
            maximum=24 * 60 * 60,
        )
        if timeout_seconds > int((expires - issued).total_seconds()):
            raise AdapterProtocolError(
                "invalid_schema", "timeoutSeconds exceeds the request lease"
            )
        return cls(
            run_id=_canonical_uuid(value["runId"], field="runId"),
            run_attempt_id=_canonical_uuid(
                value["runAttemptId"], field="runAttemptId"
            ),
            fencing_token=token,
            cancel_authority=cancel_authority,
            game_id=game_id,
            cadence=cadence,
            manager_state_version=_integer(
                value["managerStateVersion"],
                field="managerStateVersion",
                minimum=0,
                maximum=2**63 - 1,
            ),
            catalog_version=_identifier(
                value["catalogVersion"], field="catalogVersion"
            ),
            policy_digest=digest,
            issued_at=issued_text,
            expires_at=expires_text,
            timeout_seconds=timeout_seconds,
            executable_todo_instance_ids=target_ids,
            todos=todos,
            preserve_client_on_stop=True,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "requestType": "execute",
            "runId": self.run_id,
            "runAttemptId": self.run_attempt_id,
            "fencingToken": self.fencing_token,
            "cancelAuthority": self.cancel_authority,
            "gameId": self.game_id,
            "cadence": self.cadence,
            "managerStateVersion": self.manager_state_version,
            "catalogVersion": self.catalog_version,
            "policyDigest": self.policy_digest,
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
            "timeoutSeconds": self.timeout_seconds,
            "preserveClientOnStop": self.preserve_client_on_stop,
            "executableTodoInstanceIds": list(self.executable_todo_instance_ids),
            "todos": [todo.to_document() for todo in self.todos],
        }


def parse_execute_request(raw: bytes) -> AdapterExecutionPlan:
    return AdapterExecutionPlan.from_document(
        _decode_json_object(raw, maximum=MAX_REQUEST_BYTES, context="execute request")
    )


def serialize_execute_request(plan: AdapterExecutionPlan) -> bytes:
    # Round-trip through the strict parser so manually constructed dataclasses do
    # not bypass the execution-only and scope invariants.
    payload = json.dumps(
        plan.to_document(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    parsed = parse_execute_request(payload)
    if parsed != plan:
        raise AdapterProtocolError("invalid_schema", "execute plan did not round-trip")
    if len(payload) + 1 > MAX_REQUEST_BYTES:
        raise AdapterProtocolError("payload_too_large", "execute request is too large")
    return payload + b"\n"


def serialize_cancel_control(
    plan: AdapterExecutionPlan, *, at: str, reason_code: str
) -> bytes:
    _iso_time(at, field="at")
    _identifier(reason_code, field="reasonCode")
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "controlType": "cancel",
        "runId": plan.run_id,
        "runAttemptId": plan.run_attempt_id,
        "fencingToken": plan.fencing_token,
        "at": at,
        "reasonCode": reason_code,
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_EVENT_BYTES:
        raise AdapterProtocolError("payload_too_large", "cancel control is too large")
    return encoded


def serialize_durable_cancel_control(
    *,
    run_id: str,
    run_attempt_id: str,
    cancel_authority: str,
    at: str,
    reason_code: str,
    nonce: str | None = None,
) -> bytes:
    """Build the fixed Host's restart-safe cancellation envelope.

    The ordinary stdin control remains fenced by ``fencingToken``.  This
    document is instead authenticated with a separate cancellation-only
    authority so a restarted Manager can request a stop without persisting or
    recovering the execution fencing grant.
    """

    _iso_time(at, field="at")
    _identifier(reason_code, field="reasonCode")
    _canonical_uuid(run_id, field="runId")
    _canonical_uuid(run_attempt_id, field="runAttemptId")
    if not _TOKEN.fullmatch(cancel_authority):
        raise AdapterProtocolError("invalid_scope", "cancelAuthority is invalid")
    nonce_value = nonce or secrets.token_urlsafe(32)
    if not _TOKEN.fullmatch(nonce_value):
        raise AdapterProtocolError("invalid_scope", "cancel nonce is invalid")
    canonical = "\n".join(
        (
            PROTOCOL_VERSION,
            "cancel",
            run_id,
            run_attempt_id,
            at,
            reason_code,
            nonce_value,
        )
    )
    authority_mac = hmac.new(
        cancel_authority.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "controlType": "cancel",
        "runId": run_id,
        "runAttemptId": run_attempt_id,
        "at": at,
        "reasonCode": reason_code,
        "nonce": nonce_value,
        "authorityMac": f"hmac-sha256:{authority_mac}",
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise AdapterProtocolError("payload_too_large", "durable cancel control is too large")
    return encoded


@dataclass(frozen=True)
class AdapterEvent:
    event_type: str
    sequence: int
    document: Mapping[str, Any]
    allowed_artifact_mime_types: tuple[str, ...]
    max_artifact_bytes: int
    max_artifacts_per_todo: int


@dataclass(frozen=True)
class AdapterRunResult:
    run_id: str
    run_attempt_id: str
    game_id: str
    status: str
    transport_outcome: str
    attempted_todo_instance_ids: tuple[str, ...]
    completed_todo_instance_ids: tuple[str, ...]
    unresolved_todo_instance_ids: tuple[str, ...]
    exit_code: int
    protocol_valid: bool
    code: str
    message: str


class AdapterEventStream:
    """Strict stateful JSONL parser for one execution attempt."""

    _BASE_FIELDS = {
        "schemaVersion",
        "protocolVersion",
        "eventType",
        "sequence",
        "runId",
        "runAttemptId",
        "fencingToken",
        "gameId",
        "at",
    }

    def __init__(
        self,
        plan: AdapterExecutionPlan,
        *,
        allowed_artifact_mime_types: Sequence[str] = tuple(
            sorted(ALLOWED_ARTIFACT_MIME_TYPES)
        ),
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        max_artifacts_per_todo: int = MAX_ARTIFACTS_PER_TODO,
        expected_package_version: str | None = None,
        expected_package_digest: str | None = None,
    ) -> None:
        self.plan = plan
        self.allowed_artifact_mime_types = frozenset(allowed_artifact_mime_types)
        self.max_artifact_bytes = _integer(
            max_artifact_bytes,
            field="maxArtifactBytes",
            minimum=1,
            maximum=MAX_ARTIFACT_BYTES,
        )
        self.max_artifacts_per_todo = _integer(
            max_artifacts_per_todo,
            field="maxArtifactsPerTodo",
            minimum=1,
            maximum=MAX_ARTIFACTS_PER_TODO,
        )
        self.max_run_artifact_bytes = run_artifact_byte_limit(
            executable_todo_count=len(plan.executable_todo_instance_ids),
            max_artifacts_per_todo=self.max_artifacts_per_todo,
            max_artifact_bytes=self.max_artifact_bytes,
        )
        self.expected_package_version = expected_package_version
        self.expected_package_digest = expected_package_digest
        _, self.issued_at = _iso_time(plan.issued_at, field="issuedAt")
        _, self.expires_at = _iso_time(plan.expires_at, field="expiresAt")
        self.next_sequence = 0
        self.hello_seen = False
        self.run_terminal: Mapping[str, Any] | None = None
        self.started: dict[str, str] = {}
        self.todo_terminals: dict[str, Mapping[str, Any]] = {}
        self.artifacts: dict[str, tuple[str, Mapping[str, Any]]] = {}
        self.artifact_counts_by_todo: dict[str, int] = {}
        self.artifact_bytes = 0

    def consume_line(self, raw: bytes) -> AdapterEvent:
        if self.run_terminal is not None:
            raise AdapterProtocolError(
                "event_after_terminal", "No event may follow run_terminal"
            )
        value = _decode_json_object(raw, maximum=MAX_EVENT_BYTES, context="event")
        event_type = _identifier(value.get("eventType"), field="eventType")
        fields_by_type: dict[str, tuple[set[str], set[str]]] = {
            "hello": (
                {
                    "packageId",
                    "packageVersion",
                    "packageDigest",
                    "runnerPid",
                    "acceptedTodoInstanceIds",
                },
                set(),
            ),
            "todo_attempt_started": (
                {"todoInstanceId", "todoAttemptId", "attemptNo", "operation"},
                set(),
            ),
            "todo_progress": (
                {"todoInstanceId", "todoAttemptId", "code"},
                {"message", "metrics"},
            ),
            "artifact_staged": (
                {
                    "todoInstanceId",
                    "todoAttemptId",
                    "artifactId",
                    "kind",
                    "fileName",
                    "mimeType",
                    "sizeBytes",
                    "sha256",
                    "capturedAt",
                },
                set(),
            ),
            "todo_terminal": (
                {
                    "todoInstanceId",
                    "todoAttemptId",
                    "status",
                    "reasonCode",
                    "reason",
                    "retryable",
                    "evidenceArtifactIds",
                },
                set(),
            ),
            "run_terminal": (
                {
                    "status",
                    "transportOutcome",
                    "attemptedTodoInstanceIds",
                    "completedTodoInstanceIds",
                    "unresolvedTodoInstanceIds",
                    "terminalEventDigest",
                    "exitCode",
                },
                set(),
            ),
        }
        if event_type not in fields_by_type:
            raise AdapterProtocolError("unknown_event", "eventType is unsupported")
        required, optional = fields_by_type[event_type]
        _exact_object(
            value,
            required=self._BASE_FIELDS | required,
            optional=optional,
            context=event_type,
        )
        if value["schemaVersion"] != SCHEMA_VERSION:
            raise AdapterProtocolError("unsupported_schema", "event schema is unsupported")
        if value["protocolVersion"] != PROTOCOL_VERSION:
            raise AdapterProtocolError(
                "protocol_version_mismatch", "event protocol is unsupported"
            )
        sequence = _integer(
            value["sequence"], field="sequence", minimum=0, maximum=2**63 - 1
        )
        if sequence != self.next_sequence:
            raise AdapterProtocolError(
                "event_sequence_mismatch", "event sequence is duplicated or out of order"
            )
        if (
            value["runId"] != self.plan.run_id
            or value["runAttemptId"] != self.plan.run_attempt_id
            or value["fencingToken"] != self.plan.fencing_token
            or value["gameId"] != self.plan.game_id
        ):
            raise AdapterProtocolError("event_scope_mismatch", "event scope did not match")
        _, event_at = _iso_time(value["at"], field="at")
        if event_at < self.issued_at or event_at > self.expires_at:
            raise AdapterProtocolError(
                "event_time_out_of_scope", "event timestamp is outside the attempt lease"
            )
        if not self.hello_seen and event_type != "hello":
            raise AdapterProtocolError("missing_hello", "hello must be the first event")
        if self.hello_seen and event_type == "hello":
            raise AdapterProtocolError("duplicate_hello", "hello may appear only once")

        if event_type == "hello":
            self._consume_hello(value)
        elif event_type == "todo_attempt_started":
            self._consume_started(value)
        elif event_type == "todo_progress":
            self._require_active_todo(value)
            _identifier(value["code"], field="code")
            if "message" in value:
                _text(value["message"], field="message", maximum=2048)
            if "metrics" in value and not isinstance(value["metrics"], dict):
                raise AdapterProtocolError("invalid_schema", "metrics must be an object")
        elif event_type == "artifact_staged":
            self._consume_artifact(value)
        elif event_type == "todo_terminal":
            self._consume_todo_terminal(value)
        else:
            self._consume_run_terminal(value)

        self.next_sequence += 1
        return AdapterEvent(
            event_type=event_type,
            sequence=sequence,
            document=value,
            allowed_artifact_mime_types=tuple(
                sorted(self.allowed_artifact_mime_types)
            ),
            max_artifact_bytes=self.max_artifact_bytes,
            max_artifacts_per_todo=self.max_artifacts_per_todo,
        )

    def _consume_hello(self, value: Mapping[str, Any]) -> None:
        if value["packageId"] != EXECUTION_PACKAGE_ID:
            raise AdapterProtocolError("package_scope_mismatch", "packageId is invalid")
        package_version = _identifier(value["packageVersion"], field="packageVersion")
        package_digest = _text(value["packageDigest"], field="packageDigest")
        if not _SHA256.fullmatch(package_digest):
            raise AdapterProtocolError("invalid_schema", "packageDigest is invalid")
        if (
            self.expected_package_version is not None
            and package_version != self.expected_package_version
        ):
            raise AdapterProtocolError(
                "package_scope_mismatch", "runner packageVersion differs from manifest"
            )
        if (
            self.expected_package_digest is not None
            and package_digest.removeprefix("sha256:")
            != self.expected_package_digest.removeprefix("sha256:")
        ):
            raise AdapterProtocolError(
                "package_scope_mismatch", "runner packageDigest differs from manifest"
            )
        _integer(value["runnerPid"], field="runnerPid", minimum=1, maximum=2**31 - 1)
        accepted = _string_list(
            value["acceptedTodoInstanceIds"],
            field="acceptedTodoInstanceIds",
            allow_empty=False,
            todo_instance_values=True,
        )
        if accepted != self.plan.executable_todo_instance_ids:
            raise AdapterProtocolError(
                "executable_todo_mismatch", "runner did not accept the exact Todo scope"
            )
        self.hello_seen = True

    def _target(self, value: Mapping[str, Any]) -> AdapterTodoTarget:
        todo_id = _todo_instance_id(value["todoInstanceId"], field="todoInstanceId")
        for target in self.plan.todos:
            if target.todo_instance_id == todo_id:
                return target
        raise AdapterProtocolError("event_scope_mismatch", "event Todo is not executable")

    def _require_active_todo(self, value: Mapping[str, Any]) -> AdapterTodoTarget:
        target = self._target(value)
        executable_order = tuple(self.plan.executable_todo_instance_ids)
        target_index = executable_order.index(target.todo_instance_id)
        if any(
            self.todo_terminals.get(todo_id, {}).get("status")
            in {
                "failed",
                "blocked",
                "review_required",
                "human_required",
            }
            for todo_id in executable_order[:target_index]
        ):
            raise AdapterProtocolError(
                "todo_after_blocking_predecessor",
                "A downstream Todo cannot continue after an earlier Todo blocked the run",
            )
        attempt_id = _canonical_uuid(value["todoAttemptId"], field="todoAttemptId")
        if self.started.get(target.todo_instance_id) != attempt_id:
            raise AdapterProtocolError(
                "todo_attempt_mismatch", "Todo event does not match an active attempt"
            )
        if target.todo_instance_id in self.todo_terminals:
            raise AdapterProtocolError("todo_already_terminal", "Todo is already terminal")
        return target

    def _consume_started(self, value: Mapping[str, Any]) -> None:
        target = self._target(value)
        if target.todo_instance_id in self.started:
            raise AdapterProtocolError("duplicate_attempt", "Todo was started twice")
        executable_order = tuple(self.plan.executable_todo_instance_ids)
        target_index = executable_order.index(target.todo_instance_id)
        predecessors = executable_order[:target_index]
        blocking_predecessors = tuple(
            todo_id
            for todo_id in predecessors
            if self.todo_terminals.get(todo_id, {}).get("status")
            in {
                "failed",
                "blocked",
                "review_required",
                "human_required",
            }
        )
        if blocking_predecessors:
            raise AdapterProtocolError(
                "todo_after_blocking_predecessor",
                "A downstream Todo cannot start after an earlier Todo blocked the run",
            )
        attempt_id = _canonical_uuid(value["todoAttemptId"], field="todoAttemptId")
        attempt_no = _integer(
            value["attemptNo"], field="attemptNo", minimum=1, maximum=2**31 - 1
        )
        if attempt_no != target.prior_attempts + 1:
            raise AdapterProtocolError("attempt_number_mismatch", "attemptNo is stale")
        if value["operation"] != target.operation:
            raise AdapterProtocolError("operation_mismatch", "operation changed in runner")
        self.started[target.todo_instance_id] = attempt_id

    def _consume_artifact(self, value: Mapping[str, Any]) -> None:
        target = self._require_active_todo(value)
        artifact_id = _canonical_uuid(value["artifactId"], field="artifactId")
        if artifact_id in self.artifacts:
            raise AdapterProtocolError("duplicate_artifact", "artifactId was reused")
        _identifier(value["kind"], field="kind")
        file_name = _text(value["fileName"], field="fileName", maximum=128)
        if not _LEAF_NAME.fullmatch(file_name) or Path(file_name).name != file_name:
            raise AdapterProtocolError("unsafe_artifact_path", "fileName is not a leaf")
        mime = _text(value["mimeType"], field="mimeType", maximum=64)
        if mime not in self.allowed_artifact_mime_types:
            raise AdapterProtocolError("artifact_mime_denied", "mimeType is not allowed")
        size_bytes = _integer(
            value["sizeBytes"],
            field="sizeBytes",
            minimum=1,
            maximum=self.max_artifact_bytes,
        )
        digest = _text(value["sha256"], field="sha256", maximum=64)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AdapterProtocolError("invalid_schema", "artifact sha256 is invalid")
        _, captured_at = _iso_time(value["capturedAt"], field="capturedAt")
        if captured_at < self.issued_at or captured_at > self.expires_at:
            raise AdapterProtocolError(
                "artifact_time_out_of_scope",
                "artifact capture time is outside the attempt lease",
            )
        current_count = self.artifact_counts_by_todo.get(target.todo_instance_id, 0)
        if current_count >= self.max_artifacts_per_todo:
            raise AdapterProtocolError(
                "artifact_count_exceeded", "Todo artifact count exceeds its limit"
            )
        if self.artifact_bytes + size_bytes > self.max_run_artifact_bytes:
            raise AdapterProtocolError(
                "artifact_run_bytes_exceeded", "Run artifact bytes exceed their limit"
            )
        self.artifacts[artifact_id] = (target.todo_instance_id, value)
        self.artifact_counts_by_todo[target.todo_instance_id] = current_count + 1
        self.artifact_bytes += size_bytes

    def _consume_todo_terminal(self, value: Mapping[str, Any]) -> None:
        target = self._require_active_todo(value)
        status = _text(value["status"], field="status", maximum=32)
        if status not in TODO_TERMINAL_STATUSES:
            raise AdapterProtocolError("invalid_status", "Todo status is unsupported")
        _identifier(value["reasonCode"], field="reasonCode")
        _text(value["reason"], field="reason", maximum=2048)
        if not isinstance(value["retryable"], bool):
            raise AdapterProtocolError("invalid_schema", "retryable must be boolean")
        evidence = _string_list(
            value["evidenceArtifactIds"],
            field="evidenceArtifactIds",
            allow_empty=True,
            uuid_values=True,
            maximum=20,
        )
        if status == "completed" and not evidence:
            raise AdapterProtocolError(
                "completed_without_evidence", "completed requires fresh evidence"
            )
        if status == "completed" and value["retryable"] is not False:
            raise AdapterProtocolError(
                "invalid_schema", "completed cannot be marked retryable"
            )
        if status == "human_required" and value["retryable"] is not False:
            raise AdapterProtocolError(
                "unsafe_human_retry",
                "human_required cannot be marked retryable",
            )
        for artifact_id in evidence:
            artifact = self.artifacts.get(artifact_id)
            if artifact is None or artifact[0] != target.todo_instance_id:
                raise AdapterProtocolError(
                    "evidence_scope_mismatch", "evidence is not staged for this Todo"
                )
        self.todo_terminals[target.todo_instance_id] = value

    def _consume_run_terminal(self, value: Mapping[str, Any]) -> None:
        status = _text(value["status"], field="status", maximum=32)
        if status not in RUN_TERMINAL_STATUSES:
            raise AdapterProtocolError("invalid_status", "run status is unsupported")
        transport = _text(
            value["transportOutcome"], field="transportOutcome", maximum=32
        )
        if transport not in TRANSPORT_OUTCOMES:
            raise AdapterProtocolError(
                "invalid_status", "transportOutcome is unsupported"
            )
        attempted = _string_list(
            value["attemptedTodoInstanceIds"],
            field="attemptedTodoInstanceIds",
            allow_empty=True,
            todo_instance_values=True,
        )
        completed = _string_list(
            value["completedTodoInstanceIds"],
            field="completedTodoInstanceIds",
            allow_empty=True,
            todo_instance_values=True,
        )
        unresolved = _string_list(
            value["unresolvedTodoInstanceIds"],
            field="unresolvedTodoInstanceIds",
            allow_empty=True,
            todo_instance_values=True,
        )
        expected_attempted = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if todo_id in self.started
        )
        expected_completed = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if self.todo_terminals.get(todo_id, {}).get("status") == "completed"
        )
        expected_unresolved = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if todo_id not in set(expected_completed)
        )
        if attempted != expected_attempted:
            raise AdapterProtocolError("run_summary_mismatch", "attempted Todo summary differs")
        if completed != expected_completed:
            raise AdapterProtocolError("run_summary_mismatch", "completed Todo summary differs")
        if unresolved != expected_unresolved:
            raise AdapterProtocolError("run_summary_mismatch", "unresolved Todo summary differs")
        if status == "completed" and unresolved:
            raise AdapterProtocolError(
                "run_summary_mismatch", "completed run still has unresolved Todos"
            )
        if status != "completed" and not unresolved:
            raise AdapterProtocolError(
                "run_summary_mismatch", "non-completed run has no unresolved Todos"
            )
        human_required = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if self.todo_terminals.get(todo_id, {}).get("status")
            == "human_required"
        )
        if status == "human_required" and not human_required:
            raise AdapterProtocolError(
                "run_summary_mismatch",
                "human_required run has no human_required Todo terminal",
            )
        if human_required and status != "human_required":
            raise AdapterProtocolError(
                "run_summary_mismatch",
                "human_required Todo terminal must stop the run as human_required",
            )
        digest = _text(
            value["terminalEventDigest"], field="terminalEventDigest", maximum=71
        )
        if not _SHA256.fullmatch(digest):
            raise AdapterProtocolError("invalid_schema", "terminalEventDigest is invalid")
        _integer(value["exitCode"], field="exitCode", minimum=-2**31, maximum=2**31 - 1)
        self.run_terminal = value

    def finish(self, *, process_exit_code: int) -> AdapterRunResult:
        completed = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if self.todo_terminals.get(todo_id, {}).get("status") == "completed"
        )
        attempted = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if todo_id in self.started
        )
        unresolved = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if todo_id not in set(completed)
        )
        if self.run_terminal is None:
            return AdapterRunResult(
                run_id=self.plan.run_id,
                run_attempt_id=self.plan.run_attempt_id,
                game_id=self.plan.game_id,
                status="failed",
                transport_outcome="crashed",
                attempted_todo_instance_ids=attempted,
                completed_todo_instance_ids=completed,
                unresolved_todo_instance_ids=unresolved,
                exit_code=process_exit_code,
                protocol_valid=False,
                code="missing_run_terminal",
                message="Adapter process exited without run_terminal",
            )
        declared_exit = int(self.run_terminal["exitCode"])
        # A runner may legitimately stop with a non-zero code after emitting a
        # complete, validated run_terminal (e.g. "one selected step failed").
        # Only a code that differs from the declared one means the process
        # died outside its own protocol.  Windows reports negative C# exit
        # codes as unsigned DWORDs, so compare the low 32 bits.
        if (process_exit_code & 0xFFFFFFFF) != (declared_exit & 0xFFFFFFFF):
            return AdapterRunResult(
                run_id=self.plan.run_id,
                run_attempt_id=self.plan.run_attempt_id,
                game_id=self.plan.game_id,
                status="failed",
                transport_outcome="crashed",
                attempted_todo_instance_ids=attempted,
                completed_todo_instance_ids=completed,
                unresolved_todo_instance_ids=unresolved,
                exit_code=process_exit_code,
                protocol_valid=False,
                code="process_exit_mismatch",
                message=(
                    "Process exit did not match the declared terminal exit code "
                    f"(process={process_exit_code}, declared={declared_exit})"
                ),
            )
        return AdapterRunResult(
            run_id=self.plan.run_id,
            run_attempt_id=self.plan.run_attempt_id,
            game_id=self.plan.game_id,
            status=str(self.run_terminal["status"]),
            transport_outcome=str(self.run_terminal["transportOutcome"]),
            attempted_todo_instance_ids=attempted,
            completed_todo_instance_ids=completed,
            unresolved_todo_instance_ids=unresolved,
            exit_code=process_exit_code,
            protocol_valid=True,
            code="run_terminal",
            message=(
                "Adapter run produced a validated terminal event"
                if declared_exit == 0
                else f"Adapter run produced a validated terminal event with exit code {declared_exit}"
            ),
        )

    def failure_result(
        self,
        *,
        code: str,
        message: str,
        process_exit_code: int = -1,
        transport_outcome: str = "crashed",
    ) -> AdapterRunResult:
        if transport_outcome not in TRANSPORT_OUTCOMES:
            raise AdapterProtocolError(
                "invalid_status", "failure transport outcome is unsupported"
            )
        completed = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if self.todo_terminals.get(todo_id, {}).get("status") == "completed"
        )
        attempted = tuple(
            todo_id
            for todo_id in self.plan.executable_todo_instance_ids
            if todo_id in self.started
        )
        return AdapterRunResult(
            run_id=self.plan.run_id,
            run_attempt_id=self.plan.run_attempt_id,
            game_id=self.plan.game_id,
            status="failed",
            transport_outcome=transport_outcome,
            attempted_todo_instance_ids=attempted,
            completed_todo_instance_ids=completed,
            unresolved_todo_instance_ids=tuple(
                todo_id
                for todo_id in self.plan.executable_todo_instance_ids
                if todo_id not in set(completed)
            ),
            exit_code=process_exit_code,
            protocol_valid=False,
            code=code,
            message=message,
        )


@dataclass(frozen=True)
class OperationBinding:
    handler_id: str
    mode: str
    action_class: str
    risk: str
    todo_definition_ids: tuple[str, ...]
    adapter_capability_refs: tuple[str, ...]
    supports_resume: bool
    timeout_seconds: int
    required_evidence_kinds: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPackageManifest:
    package_root: Path
    package_version: str
    build_id: str
    entry_point: Path
    package_digest: str
    promotion_status: str
    payload_digest: str
    replay_suite_digest: str
    shadow_suite_digest: str
    canary_suite_digest: str
    promotion_receipt_id: str | None
    supported_game_ids: tuple[str, ...]
    operation_bindings: Mapping[str, Mapping[str, OperationBinding]]
    allowed_mime_types: tuple[str, ...]
    max_artifact_bytes: int
    max_artifacts_per_todo: int

    def binding_for(self, game_id: str, operation: str) -> OperationBinding:
        try:
            binding = self.operation_bindings[game_id][operation]
        except KeyError as error:
            raise AdapterProtocolError(
                "operation_not_promoted", "Todo operation has no promoted binding"
            ) from error
        return binding


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_file(root: Path, value: object, *, field: str) -> tuple[str, Path]:
    relative = _text(value, field=field, maximum=256)
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in relative
    ):
        raise AdapterProtocolError("unsafe_package_path", f"{field} is unsafe")
    candidate = root.joinpath(*pure.parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or _is_reparse_point(candidate):
        raise AdapterProtocolError("unsafe_package_path", f"{field} escaped package")
    return relative, resolved


def _promotion_digest(value: object, *, field: str, allow_zero: bool = False) -> str:
    digest = _text(value, field=field, maximum=71)
    if not digest.startswith("sha256:") or not _SHA256.fullmatch(digest):
        raise AdapterProtocolError("invalid_execution_manifest", f"{field} is invalid")
    if not allow_zero and digest.removeprefix("sha256:") == "0" * 64:
        raise AdapterProtocolError(
            "execution_package_unpromoted", f"{field} is an untrusted zero digest"
        )
    return digest


def _payload_digest(
    declared_files: Mapping[str, tuple[Path, str, int]],
) -> str:
    builder = hashlib.sha256()
    payload_count = 0
    for relative in sorted(declared_files):
        if relative == "promotion-receipt.json":
            continue
        _, digest, size = declared_files[relative]
        builder.update(relative.encode("utf-8"))
        builder.update(b"\0")
        builder.update(str(size).encode("ascii"))
        builder.update(b"\0")
        builder.update(digest.encode("ascii"))
        builder.update(b"\n")
        payload_count += 1
    if payload_count == 0:
        raise AdapterProtocolError("invalid_execution_manifest", "package payload is empty")
    return "sha256:" + builder.hexdigest()


def _verify_promotion_receipt(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    promotion: Mapping[str, Any],
    declared_files: Mapping[str, tuple[Path, str, int]],
    payload_digest: str,
    receipt_resolver: Callable[[str], Mapping[str, Any]] | None,
) -> tuple[str, dict[str, Any]]:
    receipt_relative = str(promotion["receiptFile"])
    if receipt_relative != "promotion-receipt.json":
        raise AdapterProtocolError(
            "invalid_execution_manifest", "receiptFile must be promotion-receipt.json"
        )
    declared = declared_files.get(receipt_relative)
    if declared is None:
        raise AdapterProtocolError(
            "execution_package_unpromoted", "promotion receipt is not declared"
        )
    receipt_path, receipt_hash, _ = declared
    receipt_digest = _promotion_digest(
        promotion["receiptSha256"], field="receiptSha256"
    )
    if receipt_digest != "sha256:" + receipt_hash:
        raise AdapterProtocolError(
            "execution_hash_mismatch", "promotion receipt hash differs from manifest"
        )
    receipt_id = _text(
        promotion["receiptResourceId"], field="receiptResourceId", maximum=36
    )
    try:
        parsed_id = uuid.UUID(receipt_id)
    except ValueError as error:
        raise AdapterProtocolError(
            "invalid_execution_manifest", "receiptResourceId is not a UUID"
        ) from error
    if str(parsed_id) != receipt_id:
        raise AdapterProtocolError(
            "invalid_execution_manifest", "receiptResourceId is not canonical"
        )
    receipt = _decode_json_object(
        receipt_path.read_bytes(), maximum=64 * 1024, context="promotion receipt"
    )
    _exact_object(
        receipt,
        required={
            "schemaVersion",
            "resourceType",
            "resourceId",
            "state",
            "packageId",
            "packageVersion",
            "buildId",
            "supportedGameIds",
            "payloadDigest",
            "replaySuiteDigest",
            "shadowSuiteDigest",
            "canarySuiteDigest",
            "candidateTestEvidenceSha256",
            "managerCanaryEvidenceSha256",
            "hostEntryPointSha256",
            "issuedAt",
        },
        context="promotion receipt",
    )
    if (
        receipt["schemaVersion"] != 1
        or receipt["resourceType"] != "adapter-promotion-receipt"
        or receipt["resourceId"] != receipt_id
        or receipt["state"] != "passed"
        or receipt["packageId"] != manifest["packageId"]
        or receipt["packageVersion"] != manifest["packageVersion"]
        or receipt["buildId"] != manifest["buildId"]
        or receipt["supportedGameIds"] != manifest["supportedGameIds"]
        or receipt["payloadDigest"] != payload_digest
        or receipt["replaySuiteDigest"] != promotion["replaySuiteDigest"]
        or receipt["shadowSuiteDigest"] != promotion["shadowSuiteDigest"]
        or receipt["canarySuiteDigest"] != promotion["canarySuiteDigest"]
    ):
        raise AdapterProtocolError(
            "execution_package_unpromoted",
            "promotion receipt belongs to another package or evidence set",
        )
    for field in (
        "candidateTestEvidenceSha256",
        "managerCanaryEvidenceSha256",
        "hostEntryPointSha256",
    ):
        _promotion_digest(receipt[field], field=field)
    _iso_time(receipt["issuedAt"], field="issuedAt")
    # hostEntryPointSha256 is immutable provenance for the no-process canary
    # that issued this receipt. Runtime compatibility is defined by the stable
    # host package and protocol fields in the execution manifest, not by an
    # exact executable digest from a previous Host build.
    if receipt_resolver is not None:
        try:
            resource = receipt_resolver(receipt_id)
        except Exception as error:
            raise AdapterProtocolError(
                "promotion_receipt_missing",
                "Manager-owned promotion receipt resource is missing",
            ) from error
        if (
            resource.get("resource_id") != receipt_id
            or resource.get("resource_type") != "adapter-promotion-receipt"
            or resource.get("state") != "passed"
            or resource.get("document") != receipt
        ):
            raise AdapterProtocolError(
                "promotion_receipt_mismatch",
                "installed promotion receipt differs from the Manager ledger",
            )
    return receipt_id, receipt


def _verify_execution_package(
    package_root: Path,
    *,
    allow_candidate: bool,
    receipt_resolver: Callable[[str], Mapping[str, Any]] | None = None,
) -> ExecutionPackageManifest:
    root = package_root.resolve()
    if not root.is_dir():
        raise AdapterProtocolError("execution_package_missing", "Execution package is missing")
    if _is_reparse_point(package_root) or _is_reparse_point(root):
        raise AdapterProtocolError(
            "unsafe_execution_package", "Execution package root is a reparse point"
        )
    manifest_path = root / "install-manifest.json"
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        raise AdapterProtocolError(
            "execution_manifest_missing", "Execution package manifest is missing"
        )
    raw = manifest_path.read_bytes()
    value = _decode_json_object(raw, maximum=64 * 1024, context="execution manifest")
    required = {
        "schemaVersion",
        "packageId",
        "packageVersion",
        "buildId",
        "builtAt",
        "installedAt",
        "protocolVersions",
        "hostPackageId",
        "minHostVersion",
        "entryPoint",
        "files",
        "supportedGameIds",
        "operationBindings",
        "forbiddenOperationClasses",
        "limits",
        "artifactPolicy",
        "security",
        "promotion",
        "executionReady",
    }
    _exact_object(value, required=required, context="execution manifest")
    if value["schemaVersion"] != EXECUTION_PACKAGE_SCHEMA_VERSION:
        raise AdapterProtocolError("invalid_execution_manifest", "schemaVersion must be 2")
    if value["packageId"] != EXECUTION_PACKAGE_ID:
        raise AdapterProtocolError("invalid_execution_manifest", "packageId is invalid")
    package_version = _identifier(value["packageVersion"], field="packageVersion")
    build_id = _identifier(value["buildId"], field="buildId")
    _iso_time(value["builtAt"], field="builtAt")
    _iso_time(value["installedAt"], field="installedAt")
    protocols = _string_list(
        value["protocolVersions"],
        field="protocolVersions",
        allow_empty=False,
        maximum=8,
    )
    if PROTOCOL_VERSION not in protocols:
        raise AdapterProtocolError(
            "protocol_version_mismatch", "Execution package does not support v1.1"
        )
    if value["hostPackageId"] != HOST_PACKAGE_ID:
        raise AdapterProtocolError("invalid_execution_manifest", "hostPackageId is invalid")
    _identifier(value["minHostVersion"], field="minHostVersion")

    file_documents = value["files"]
    if not isinstance(file_documents, list) or not file_documents or len(file_documents) > 128:
        raise AdapterProtocolError("invalid_execution_manifest", "files is invalid")
    declared_files: dict[str, tuple[Path, str, int]] = {}
    for document in file_documents:
        item = _exact_object(
            document,
            required={"path", "sha256", "sizeBytes"},
            context="execution file",
        )
        relative, resolved = _safe_relative_file(root, item["path"], field="path")
        if relative == "install-manifest.json" or relative in declared_files:
            raise AdapterProtocolError("invalid_execution_manifest", "file path is duplicated")
        digest = _text(item["sha256"], field="sha256", maximum=64)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AdapterProtocolError("invalid_execution_manifest", "file hash is invalid")
        size = _integer(
            item["sizeBytes"], field="sizeBytes", minimum=1, maximum=2**63 - 1
        )
        if not resolved.is_file() or _is_reparse_point(resolved):
            raise AdapterProtocolError("unsafe_execution_package", "declared file is unsafe")
        if resolved.stat().st_size != size:
            raise AdapterProtocolError("execution_size_mismatch", "file size differs")
        if _sha256_file(resolved) != digest:
            raise AdapterProtocolError("execution_hash_mismatch", "file hash differs")
        declared_files[relative] = (resolved, digest, size)

    entry_relative, entry_point = _safe_relative_file(
        root, value["entryPoint"], field="entryPoint"
    )
    if entry_relative not in declared_files or entry_point.name != "runner.exe":
        raise AdapterProtocolError("invalid_execution_manifest", "entryPoint is not declared")
    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file()
    }
    actual_entries = list(root.rglob("*"))
    if any(_is_reparse_point(item) for item in actual_entries):
        raise AdapterProtocolError("unsafe_execution_package", "package has a reparse point")
    expected_files = set(declared_files) | {"install-manifest.json"}
    if actual_files != expected_files:
        raise AdapterProtocolError(
            "execution_package_extras", "package contains missing or unexpected files"
        )

    games = _string_list(
        value["supportedGameIds"],
        field="supportedGameIds",
        allow_empty=False,
        maximum=len(ALLOWED_GAME_IDS),
    )
    if any(game not in ALLOWED_GAME_IDS for game in games):
        raise AdapterProtocolError("invalid_execution_manifest", "gameId is not allowed")

    forbidden = frozenset(
        _string_list(
            value["forbiddenOperationClasses"],
            field="forbiddenOperationClasses",
            allow_empty=False,
            maximum=64,
        )
    )
    if not REQUIRED_FORBIDDEN_OPERATION_CLASSES.issubset(forbidden):
        raise AdapterProtocolError(
            "unsafe_execution_manifest", "hard-denied operation classes are incomplete"
        )
    binding_document = value["operationBindings"]
    if not isinstance(binding_document, dict) or set(binding_document) != set(games):
        raise AdapterProtocolError(
            "invalid_execution_manifest", "operationBindings game scope differs"
        )
    bindings: dict[str, dict[str, OperationBinding]] = {}
    for game_id, operations_document in binding_document.items():
        if not isinstance(operations_document, dict) or not operations_document:
            raise AdapterProtocolError(
                "invalid_execution_manifest", "game operationBindings is empty"
            )
        game_bindings: dict[str, OperationBinding] = {}
        for operation, document in operations_document.items():
            operation = _identifier(operation, field="operation")
            item = _exact_object(
                document,
                required={
                    "handlerId",
                    "mode",
                    "actionClass",
                    "risk",
                    "todoDefinitionIds",
                    "adapterCapabilityRefs",
                    "supportsResume",
                    "timeoutSeconds",
                    "requiredEvidenceKinds",
                },
                context="operation binding",
            )
            action_class = _identifier(item["actionClass"], field="actionClass")
            risk = _text(item["risk"], field="risk", maximum=32)
            mode = _text(item["mode"], field="mode", maximum=32)
            if action_class in forbidden or risk not in ALLOWED_RISKS:
                raise AdapterProtocolError(
                    "unsafe_execution_manifest", "binding enables a denied action"
                )
            if mode not in {"granular", "whole_run"}:
                raise AdapterProtocolError(
                    "invalid_execution_manifest", "binding mode is not executable"
                )
            if not isinstance(item["supportsResume"], bool):
                raise AdapterProtocolError(
                    "invalid_execution_manifest", "supportsResume must be boolean"
                )
            evidence_kinds = _string_list(
                item["requiredEvidenceKinds"],
                field="requiredEvidenceKinds",
                allow_empty=False,
                maximum=16,
            )
            game_bindings[operation] = OperationBinding(
                handler_id=_identifier(item["handlerId"], field="handlerId"),
                mode=mode,
                action_class=action_class,
                risk=risk,
                todo_definition_ids=_string_list(
                    item["todoDefinitionIds"],
                    field="todoDefinitionIds",
                    allow_empty=False,
                    maximum=32,
                ),
                adapter_capability_refs=_string_list(
                    item["adapterCapabilityRefs"],
                    field="adapterCapabilityRefs",
                    allow_empty=False,
                    capability_ref_values=True,
                    maximum=16,
                ),
                supports_resume=item["supportsResume"],
                timeout_seconds=_integer(
                    item["timeoutSeconds"],
                    field="timeoutSeconds",
                    minimum=1,
                    maximum=24 * 60 * 60,
                ),
                required_evidence_kinds=evidence_kinds,
            )
        bindings[game_id] = game_bindings

    limits = _exact_object(
        value["limits"],
        required={"maxRequestBytes", "maxEventBytes", "maxArtifactsPerTodo"},
        context="limits",
    )
    max_artifacts_per_todo = _integer(
        limits["maxArtifactsPerTodo"],
        field="maxArtifactsPerTodo",
        minimum=1,
        maximum=100,
    )
    if (
        limits["maxRequestBytes"] != MAX_REQUEST_BYTES
        or limits["maxEventBytes"] != MAX_EVENT_BYTES
        or max_artifacts_per_todo > MAX_ARTIFACTS_PER_TODO
    ):
        raise AdapterProtocolError("invalid_execution_manifest", "limits differ")
    artifact_policy = _exact_object(
        value["artifactPolicy"],
        required={"allowedMimeTypes", "maxArtifactBytes"},
        context="artifactPolicy",
    )
    allowed_mime = tuple(artifact_policy["allowedMimeTypes"]) if isinstance(
        artifact_policy["allowedMimeTypes"], list
    ) else ()
    if (
        not allowed_mime
        or any(
            not isinstance(item, str) or item not in ALLOWED_ARTIFACT_MIME_TYPES
            for item in allowed_mime
        )
        or len(set(allowed_mime)) != len(allowed_mime)
    ):
        raise AdapterProtocolError(
            "invalid_execution_manifest", "artifact MIME allowlist is invalid"
        )
    max_artifact = _integer(
        artifact_policy["maxArtifactBytes"],
        field="maxArtifactBytes",
        minimum=1,
        maximum=MAX_ARTIFACT_BYTES,
    )
    security = _exact_object(
        value["security"],
        required={
            "allowsArbitraryCommand",
            "allowsArbitraryPath",
            "allowsArbitraryInput",
        },
        context="security",
    )
    if any(security[field] is not False for field in security):
        raise AdapterProtocolError(
            "unsafe_execution_manifest", "arbitrary execution APIs must be disabled"
        )
    promotion = _exact_object(
        value["promotion"],
        required={
            "status",
            "replaySuiteDigest",
            "shadowSuiteDigest",
            "canarySuiteDigest",
            "payloadDigest",
            "receiptFile",
            "receiptSha256",
            "receiptResourceId",
        },
        context="promotion",
    )
    promotion_status = _text(promotion["status"], field="status", maximum=32)
    payload_digest = _payload_digest(declared_files)
    if promotion["payloadDigest"] != payload_digest:
        raise AdapterProtocolError(
            "execution_hash_mismatch", "promotion payloadDigest differs from files"
        )
    replay_digest = _promotion_digest(
        promotion["replaySuiteDigest"], field="replaySuiteDigest"
    )
    shadow_digest = _promotion_digest(
        promotion["shadowSuiteDigest"], field="shadowSuiteDigest"
    )
    receipt_id: str | None = None
    if promotion_status == "candidate" and allow_candidate:
        _promotion_digest(
            promotion["canarySuiteDigest"],
            field="canarySuiteDigest",
            allow_zero=True,
        )
        if (
            promotion["receiptFile"] != ""
            or promotion["receiptSha256"] != ""
            or promotion["receiptResourceId"] != ""
            or value["executionReady"] is not False
            or "promotion-receipt.json" in declared_files
        ):
            raise AdapterProtocolError(
                "invalid_execution_manifest",
                "candidate package already contains promotion authority",
            )
        canary_digest = str(promotion["canarySuiteDigest"])
    elif promotion_status == "promoted":
        canary_digest = _promotion_digest(
            promotion["canarySuiteDigest"], field="canarySuiteDigest"
        )
        if value["executionReady"] is not True:
            raise AdapterProtocolError(
                "execution_package_unpromoted", "executionReady must be true"
            )
        receipt_id, _ = _verify_promotion_receipt(
            root=root,
            manifest=value,
            promotion=promotion,
            declared_files=declared_files,
            payload_digest=payload_digest,
            receipt_resolver=receipt_resolver,
        )
    else:
        raise AdapterProtocolError("execution_package_unpromoted", "package is not promoted")

    package_digest_builder = hashlib.sha256()
    package_digest_builder.update(raw)
    for relative in sorted(declared_files):
        package_digest_builder.update(relative.encode("utf-8"))
        package_digest_builder.update(declared_files[relative][1].encode("ascii"))
    return ExecutionPackageManifest(
        package_root=root,
        package_version=package_version,
        build_id=build_id,
        entry_point=entry_point,
        package_digest=package_digest_builder.hexdigest(),
        promotion_status=promotion_status,
        payload_digest=payload_digest,
        replay_suite_digest=replay_digest,
        shadow_suite_digest=shadow_digest,
        canary_suite_digest=canary_digest,
        promotion_receipt_id=receipt_id,
        supported_game_ids=games,
        operation_bindings=bindings,
        allowed_mime_types=allowed_mime,
        max_artifact_bytes=max_artifact,
        max_artifacts_per_todo=max_artifacts_per_todo,
    )


def verify_execution_package(
    package_root: Path,
    *,
    receipt_resolver: Callable[[str], Mapping[str, Any]] | None = None,
) -> ExecutionPackageManifest:
    return _verify_execution_package(
        package_root,
        allow_candidate=False,
        receipt_resolver=receipt_resolver,
    )


def verify_execution_candidate(package_root: Path) -> ExecutionPackageManifest:
    manifest = _verify_execution_package(package_root, allow_candidate=True)
    if manifest.promotion_status != "candidate":
        raise AdapterProtocolError(
            "execution_package_not_candidate", "installed package is already promoted"
        )
    return manifest


def validate_plan_against_manifest(
    plan: AdapterExecutionPlan, manifest: ExecutionPackageManifest
) -> None:
    if plan.game_id not in manifest.supported_game_ids:
        raise AdapterProtocolError(
            "game_not_promoted", "Execution package does not support this game"
        )
    maximum_plan_seconds = 0
    for target in plan.todos:
        binding = manifest.binding_for(plan.game_id, target.operation)
        maximum_plan_seconds += binding.timeout_seconds
        if binding.risk != target.risk:
            raise AdapterProtocolError(
                "risk_binding_mismatch", "Todo risk differs from promoted binding"
            )
        if target.todo_definition_id not in binding.todo_definition_ids:
            raise AdapterProtocolError(
                "todo_definition_not_promoted",
                "Todo definition is not bound to this promoted operation",
            )
        if target.adapter_capability_ref not in binding.adapter_capability_refs:
            raise AdapterProtocolError(
                "capability_not_promoted",
                "Todo capability is not bound to this promoted operation",
            )
    if plan.timeout_seconds > maximum_plan_seconds:
        raise AdapterProtocolError(
            "timeout_binding_mismatch",
            "Plan timeout exceeds the sum of promoted Todo handler limits",
        )
