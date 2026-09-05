"""Typed, loopback-only HTTP client for the YeYu Gamer Manager API."""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PlatformConfig


JsonObject = dict[str, Any]
WORK_ITEM_KINDS = frozenset(
    {
        "run_game",
        "run_batch",
        "diagnose_game",
        "cancel_run",
        "observation",
        "incident_review",
        "evidence_review",
        "repair_validation",
    }
)


class ManagerApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass(frozen=True, slots=True)
class ManagerHealth:
    ok: bool
    status: str
    version: str | None
    active_batch_id: str | None
    raw: JsonObject

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "ManagerHealth":
        status = str(payload.get("status", "unknown"))
        ok_value = payload.get("ok")
        if isinstance(ok_value, bool):
            ok = ok_value
        else:
            ok = status.lower() in {"ok", "healthy", "ready", "running"}
        active = payload.get("activeBatchId") or payload.get("active_batch_id")
        version = payload.get("version") or payload.get("managerVersion")
        return cls(
            ok=ok,
            status=status,
            version=str(version) if version is not None else None,
            active_batch_id=str(active) if active is not None else None,
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class CommandAccepted:
    command_id: str
    status_url: str | None
    accepted_state_version: int | None
    raw: JsonObject

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "CommandAccepted":
        command_id = payload.get("commandId") or payload.get("command_id")
        if not command_id:
            raise ManagerApiError("Manager response did not contain commandId")
        state_version = payload.get("acceptedStateVersion")
        if state_version is None:
            state_version = payload.get("accepted_state_version")
        return cls(
            command_id=str(command_id),
            status_url=(
                str(payload.get("statusUrl")) if payload.get("statusUrl") else None
            ),
            accepted_state_version=(
                int(state_version) if state_version is not None else None
            ),
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class WebGuiBootstrap:
    nonce: str
    expires_in_seconds: int

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "WebGuiBootstrap":
        nonce = payload.get("nonce")
        expires = payload.get("expiresInSeconds")
        if (
            not isinstance(nonce, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{40,128}", nonce) is None
            or isinstance(expires, bool)
            or not isinstance(expires, int)
            or not 10 <= expires <= 300
        ):
            raise ManagerApiError("Manager returned an invalid WebGUI bootstrap")
        return cls(nonce=nonce, expires_in_seconds=expires)


@dataclass(frozen=True, slots=True)
class AgentWorkItem:
    work_item_id: str
    kind: str
    state: str
    requested_by: str
    game_id: str | None
    artifact_refs: tuple[str, ...]
    allowed_capability_refs: tuple[str, ...]
    raw: JsonObject

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "AgentWorkItem":
        work_item_id = payload.get("workItemId") or payload.get("work_item_id")
        if not work_item_id:
            raise ManagerApiError("Manager work item response lacked workItemId")
        return cls(
            work_item_id=str(work_item_id),
            kind=str(payload.get("kind", "unknown")),
            state=str(payload.get("state", "unknown")),
            requested_by=str(
                payload.get("requestedBy") or payload.get("requested_by") or ""
            ),
            game_id=(
                str(payload.get("gameId") or payload.get("game_id"))
                if payload.get("gameId") or payload.get("game_id")
                else None
            ),
            artifact_refs=tuple(
                str(value)
                for value in (
                    payload.get("artifactRefs") or payload.get("artifact_refs") or []
                )
            ),
            allowed_capability_refs=tuple(
                str(value)
                for value in (
                    payload.get("allowedCapabilityRefs")
                    or payload.get("allowed_capability_refs")
                    or []
                )
            ),
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: str
    kind: str
    content_type: str
    size_bytes: int
    content_hash: str
    file_name: str
    run_attempt_id: str | None
    todo_instance_id: str | None
    todo_attempt_id: str | None
    game_day_key: str | None
    raw: JsonObject

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "ArtifactMetadata":
        artifact_id = payload.get("artifactId") or payload.get("artifact_id")
        if not artifact_id:
            raise ManagerApiError("Manager artifact response lacked artifactId")
        return cls(
            artifact_id=str(artifact_id),
            kind=str(payload.get("kind", "unknown")),
            content_type=str(
                payload.get("contentType") or payload.get("content_type") or ""
            ),
            size_bytes=int(payload.get("sizeBytes") or payload.get("size_bytes") or 0),
            content_hash=str(payload.get("hash") or payload.get("content_hash") or ""),
            file_name=str(
                payload.get("fileName") or payload.get("file_name") or ""
            ),
            run_attempt_id=(
                str(payload.get("runAttemptId") or payload.get("run_attempt_id"))
                if payload.get("runAttemptId") or payload.get("run_attempt_id")
                else None
            ),
            todo_instance_id=(
                str(payload.get("todoInstanceId") or payload.get("todo_instance_id"))
                if payload.get("todoInstanceId") or payload.get("todo_instance_id")
                else None
            ),
            todo_attempt_id=(
                str(payload.get("todoAttemptId") or payload.get("todo_attempt_id"))
                if payload.get("todoAttemptId") or payload.get("todo_attempt_id")
                else None
            ),
            game_day_key=(
                str(payload.get("gameDayKey") or payload.get("game_day_key"))
                if payload.get("gameDayKey") or payload.get("game_day_key")
                else None
            ),
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    artifact_id: str
    content_type: str | None
    content_disposition: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class WorkItemClaimAccepted:
    receipt: CommandAccepted
    work_item_id: str
    claim_id: str
    fencing_token: str

    @classmethod
    def from_receipt(cls, receipt: CommandAccepted) -> "WorkItemClaimAccepted":
        result = receipt.raw.get("result")
        claim = result.get("claim") if isinstance(result, dict) else None
        if not isinstance(claim, dict):
            raise ManagerApiError("Manager claim receipt lacked result.claim")
        work_item_id = claim.get("workItemId") or claim.get("work_item_id")
        claim_id = claim.get("claimId") or claim.get("claim_id")
        fencing_token = claim.get("fencingToken") or claim.get("fencing_token")
        if not work_item_id or not claim_id or not fencing_token:
            raise ManagerApiError("Manager claim receipt was incomplete")
        return cls(
            receipt=receipt,
            work_item_id=str(work_item_id),
            claim_id=str(claim_id),
            fencing_token=str(fencing_token),
        )


@dataclass(frozen=True, slots=True)
class WorkItemDispatched:
    receipt: CommandAccepted
    work_item_id: str

    @classmethod
    def from_receipt(cls, receipt: CommandAccepted) -> "WorkItemDispatched":
        result = receipt.raw.get("result")
        work_item = result.get("workItem") if isinstance(result, dict) else None
        if not isinstance(work_item, dict):
            raise ManagerApiError("Manager dispatch receipt lacked result.workItem")
        work_item_id = work_item.get("workItemId") or work_item.get("work_item_id")
        if not work_item_id:
            raise ManagerApiError("Manager dispatch receipt lacked workItemId")
        return cls(receipt=receipt, work_item_id=str(work_item_id))


def _token_from_file(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    token = path.read_text(encoding="utf-8-sig").strip()
    return token or None


def _actor_token_path(path: Path | None, actor: str) -> Path | None:
    if path is None:
        return None
    rendered = Path(str(path).replace("{actor}", actor))
    if "{actor}" in str(path):
        return rendered
    if rendered.exists():
        return rendered / f"{actor}.token" if rendered.is_dir() else rendered
    # New configurations point at the protected actors directory. Keep an
    # older explicit *.token file working during migration.
    return rendered if rendered.suffix.lower() == ".token" else rendered / f"{actor}.token"


def _opaque_id(value: str, *, name: str, maximum_length: int = 160) -> str:
    if (
        not value
        or len(value) > maximum_length
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None
        or value in {".", ".."}
    ):
        raise ValueError(f"{name} must be an opaque identifier, not a path")
    return value


def _fencing_token(value: str) -> str:
    if (
        len(value) < 16
        or len(value) > 160
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ValueError("fencing_token is invalid")
    return value


def _game_day_key(value: object) -> str:
    rendered = str(value)
    if re.fullmatch(r"(?:week:)?\d{4}-\d{2}-\d{2}", rendered) is None:
        raise ValueError("completion game_day_key must be a daily or weekly period key")
    return rendered


def _bounded_text(value: object, *, name: str, maximum_length: int) -> str:
    if not isinstance(value, str) or len(value) > maximum_length:
        raise ValueError(f"{name} must be text no longer than {maximum_length}")
    return value


def _todo_diagnosis(
    value: Mapping[str, Any], *, multi_todo_contract: bool = False
) -> JsonObject:
    allowed = {
        "todoInstanceId",
        "difficulty",
        "confidence",
        "basis",
        "failureStage",
        "recommendation",
    }
    required = {"todoInstanceId", "difficulty", "confidence", "basis"}
    if multi_todo_contract:
        allowed.update({"automatable", "issue", "evidenceIds"})
        required.add("automatable")
    if set(value) - allowed or not required.issubset(value):
        raise ValueError("todo_diagnosis has an invalid typed shape")
    todo_id = _opaque_id(str(value["todoInstanceId"]), name="todo_instance_id")
    difficulty = value["difficulty"]
    if difficulty not in {"easy", "moderate", "hard", "unsupported", "unknown"}:
        raise ValueError("todo_diagnosis difficulty is invalid")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise ValueError("todo_diagnosis confidence must be between zero and one")
    basis = value["basis"]
    if not isinstance(basis, (list, tuple)) or not 1 <= len(basis) <= 20:
        raise ValueError("todo_diagnosis basis must contain 1 to 20 entries")
    rendered_basis = [
        _bounded_text(item, name="todo_diagnosis basis", maximum_length=1000)
        for item in basis
    ]
    rendered = {
        "todoInstanceId": todo_id,
        "difficulty": difficulty,
        "confidence": float(confidence),
        "basis": rendered_basis,
        "failureStage": _bounded_text(
            value.get("failureStage", ""),
            name="todo_diagnosis failureStage",
            maximum_length=160,
        ),
        "recommendation": _bounded_text(
            value.get("recommendation", ""),
            name="todo_diagnosis recommendation",
            maximum_length=1000,
        ),
    }
    if multi_todo_contract:
        automatable = value["automatable"]
        if not isinstance(automatable, bool):
            raise ValueError("todo_diagnosis automatable must be boolean")
        rendered["automatable"] = automatable
        rendered["issue"] = _bounded_text(
            value.get("issue", ""),
            name="todo_diagnosis issue",
            maximum_length=1000,
        )
        evidence_ids = value.get("evidenceIds", [])
        if not isinstance(evidence_ids, (list, tuple)) or len(evidence_ids) > 50:
            raise ValueError("todo_diagnosis evidenceIds must contain at most 50 IDs")
        rendered_evidence_ids = [
            _opaque_id(str(item), name="todo_diagnosis evidence_id")
            for item in evidence_ids
        ]
        if len(rendered_evidence_ids) != len(set(rendered_evidence_ids)):
            raise ValueError("todo_diagnosis evidenceIds contains duplicates")
        rendered["evidenceIds"] = rendered_evidence_ids
    return rendered


def _completion_todo_review(
    value: Mapping[str, Any], evidence_ids: set[str]
) -> JsonObject:
    if not isinstance(value, Mapping) or set(value) != {
        "todoInstanceId", "verdict", "reasonCode", "artifactRefs"
    }:
        raise ValueError("completion_review Todo verdict has an invalid typed shape")
    todo_id = _opaque_id(
        _bounded_text(value["todoInstanceId"], name="completion Todo ID", maximum_length=200),
        name="completion todo_instance_id",
        maximum_length=200,
    )
    verdict = value["verdict"]
    if not isinstance(verdict, str) or verdict not in {
        "confirmed", "rejected", "review_required"
    }:
        raise ValueError("completion_review Todo verdict is invalid")
    reason_code = _bounded_text(
        value["reasonCode"], name="completion Todo reasonCode", maximum_length=160
    )
    if re.fullmatch(r"[a-z][a-z0-9_.-]*", reason_code) is None:
        raise ValueError("completion_review Todo reasonCode is invalid")
    refs = value["artifactRefs"]
    if not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 20:
        raise ValueError("completion_review Todo artifactRefs must contain 1 to 20 IDs")
    rendered_refs = [
        _opaque_id(
            _bounded_text(item, name="completion Todo artifact_ref", maximum_length=160),
            name="completion Todo artifact_ref",
        )
        for item in refs
    ]
    if len(rendered_refs) != len(set(rendered_refs)):
        raise ValueError("completion_review Todo artifactRefs contains duplicates")
    if not set(rendered_refs).issubset(evidence_ids):
        raise ValueError("completion_review Todo artifactRefs must be decision evidence IDs")
    return {
        "todoInstanceId": todo_id,
        "verdict": verdict,
        "reasonCode": reason_code,
        "artifactRefs": rendered_refs,
    }


def _completion_review(value: Mapping[str, Any], evidence_ids: set[str]) -> JsonObject:
    required = {"gameId", "runId", "runAttemptId", "gameDayKey", "predicates"}
    if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - (required | {"todoReviews"}):
        raise ValueError("completion_review has an invalid typed shape")
    game_id = _opaque_id(str(value["gameId"]), name="completion game_id", maximum_length=80)
    run_id = _opaque_id(str(value["runId"]), name="completion run_id")
    run_attempt_id = _opaque_id(
        str(value["runAttemptId"]), name="completion run_attempt_id"
    )
    game_day_key = _game_day_key(value["gameDayKey"])
    predicates = value["predicates"]
    if not isinstance(predicates, (list, tuple)) or len(predicates) > 20:
        raise ValueError("completion_review predicates must contain at most 20 entries")
    rendered: list[JsonObject] = []
    predicate_ids: set[str] = set()
    for predicate in predicates:
        if not isinstance(predicate, Mapping) or set(predicate) != {
            "predicateId",
            "metrics",
            "artifactRefs",
        }:
            raise ValueError("completion_review predicate has an invalid typed shape")
        predicate_id = _opaque_id(
            str(predicate["predicateId"]),
            name="completion predicate_id",
            maximum_length=200,
        )
        if predicate_id in predicate_ids:
            raise ValueError("completion_review predicate IDs must be unique")
        predicate_ids.add(predicate_id)
        metrics = predicate["metrics"]
        if not isinstance(metrics, Mapping) or not 1 <= len(metrics) <= 20:
            raise ValueError("completion_review metrics must contain 1 to 20 entries")
        rendered_metrics: JsonObject = {}
        for raw_name, metric in metrics.items():
            name = _bounded_text(
                raw_name, name="completion metric name", maximum_length=80
            )
            if not name or not isinstance(metric, (bool, int, str)):
                raise ValueError("completion_review metrics allow only bool, int, or text")
            rendered_metrics[name] = metric
        refs = predicate["artifactRefs"]
        if not isinstance(refs, (list, tuple)) or not 1 <= len(refs) <= 20:
            raise ValueError("completion_review artifactRefs must contain 1 to 20 IDs")
        rendered_refs = [
            _opaque_id(str(item), name="completion artifact_ref") for item in refs
        ]
        if len(rendered_refs) != len(set(rendered_refs)):
            raise ValueError("completion_review artifactRefs contains duplicates")
        if not set(rendered_refs).issubset(evidence_ids):
            raise ValueError("completion_review artifactRefs must be decision evidence IDs")
        rendered.append(
            {
                "predicateId": predicate_id,
                "metrics": rendered_metrics,
                "artifactRefs": rendered_refs,
            }
        )
    # The Manager explicitly defaults omitted Todo verdicts to an empty list
    # for non-accepting reviews. Never discard submitted v2 semantic verdicts.
    todo_reviews = value.get("todoReviews", [])
    if not isinstance(todo_reviews, (list, tuple)) or len(todo_reviews) > 100:
        raise ValueError("completion_review todoReviews must contain at most 100 entries")
    rendered_todos = [
        _completion_todo_review(item, evidence_ids) for item in todo_reviews
    ]
    todo_ids = [item["todoInstanceId"] for item in rendered_todos]
    if len(todo_ids) != len(set(todo_ids)):
        raise ValueError("completion_review contains duplicate Todo verdicts")
    return {
        "gameId": game_id,
        "runId": run_id,
        "runAttemptId": run_attempt_id,
        "gameDayKey": game_day_key,
        "predicates": rendered,
        "todoReviews": rendered_todos,
    }


class ManagerApiClient:
    """Small contract client; it never exposes generic URL or shell passthrough."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        actor: str,
        actor_token_override: str | None = None,
        user_agent: str = "YeYuGamer-Platform/0.1",
    ) -> None:
        config.validate()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", actor) is None:
            raise ValueError("actor must be a lowercase local actor identifier")
        self.config = config
        self.actor = actor
        if actor_token_override is not None and (
            len(actor_token_override) < 32
            or len(actor_token_override) > 256
            or any(
                ord(character) < 33 or ord(character) > 126
                for character in actor_token_override
            )
        ):
            raise ValueError("actor_token_override must contain visible ASCII only")
        self._actor_token_override = actor_token_override
        self.user_agent = user_agent

    def _actor_token(self) -> str | None:
        # The WebGUI bootstrap actor is process-local by contract. Never fall
        # back to the retired tray.token file, even if an old file is recreated
        # in the actor directory after Manager startup.
        if self.actor == "tray":
            return self._actor_token_override
        return self._actor_token_override or _token_from_file(
            _actor_token_path(self.config.actor_token_file, self.actor)
        )

    def _url(self, resource: str) -> str:
        if not resource or resource.startswith(("http://", "https://", "//")):
            raise ValueError("resource must be an API-relative path")
        return f"{self.config.manager_base_url}/{resource.lstrip('/')}"

    def _artifact_claim_context(
        self,
        *,
        work_item_id: str | None,
        claim_id: str | None,
        fencing_token: str | None,
    ) -> tuple[str, str, str] | None:
        supplied = any(value is not None for value in (work_item_id, claim_id, fencing_token))
        if self.actor == "agent":
            if work_item_id is None or claim_id is None or fencing_token is None:
                raise ValueError(
                    "Agent artifact reads require work_item_id, claim_id, and fencing_token"
                )
            return (
                _opaque_id(work_item_id, name="work_item_id", maximum_length=120),
                _opaque_id(claim_id, name="claim_id", maximum_length=120),
                _fencing_token(fencing_token),
            )
        if supplied:
            raise ValueError("artifact claim context is only valid for the agent actor")
        if self.actor not in {"webgui", "cli"}:
            raise PermissionError(
                "artifact reads require webgui, cli, or an active Agent claim"
            )
        return None

    def _request(
        self,
        method: str,
        resource: str,
        *,
        body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_state_version: int | None = None,
        artifact_claim: tuple[str, str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        encoded = None
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "X-YeYu-Gamer-Actor": self.actor,
            "X-Request-ID": str(uuid.uuid4()),
        }
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        token = self._actor_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key is not None:
            if (
                not idempotency_key
                or len(idempotency_key) > 128
                or any(ord(character) < 33 or ord(character) > 126 for character in idempotency_key)
            ):
                raise ValueError(
                    "idempotency_key must contain 1 to 128 visible ASCII characters"
                )
            headers["Idempotency-Key"] = idempotency_key
        if expected_state_version is not None:
            headers["If-Match"] = str(expected_state_version)
        if artifact_claim is not None:
            if self.actor != "agent":
                raise PermissionError("artifact claim headers require the agent actor")
            work_item_id, claim_id, fencing_token = artifact_claim
            headers["X-YeYu-Gamer-Work-Item-Id"] = work_item_id
            headers["Claim-Id"] = claim_id
            headers["Fencing-Token"] = fencing_token

        request = urllib.request.Request(
            self._url(resource), data=encoded, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else self.config.request_timeout_seconds
                ),
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")[:4096]
            raise ManagerApiError(
                f"Manager returned HTTP {error.code} for {method} {resource}",
                status_code=error.code,
                response_body=raw,
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            raise ManagerApiError(
                f"Manager is unavailable for {method} {resource}: {error}"
            ) from error

        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagerApiError(
                f"Manager returned invalid JSON for {method} {resource}"
            ) from error

    def _request_artifact_content(
        self,
        artifact_id: str,
        *,
        artifact_claim: tuple[str, str, str] | None,
    ) -> ArtifactContent:
        resource = f"artifacts/{artifact_id}/content"
        headers = {
            "Accept": "*/*",
            "User-Agent": self.user_agent,
            "X-YeYu-Gamer-Actor": self.actor,
            "X-Request-ID": str(uuid.uuid4()),
        }
        token = self._actor_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if artifact_claim is not None:
            if self.actor != "agent":
                raise PermissionError("artifact claim headers require the agent actor")
            work_item_id, claim_id, fencing_token = artifact_claim
            headers["X-YeYu-Gamer-Work-Item-Id"] = work_item_id
            headers["Claim-Id"] = claim_id
            headers["Fencing-Token"] = fencing_token
        request = urllib.request.Request(
            self._url(resource), method="GET", headers=headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type")
                disposition = response.headers.get("Content-Disposition")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")[:4096]
            raise ManagerApiError(
                f"Manager returned HTTP {error.code} for GET {resource}",
                status_code=error.code,
                response_body=raw,
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            raise ManagerApiError(
                f"Manager is unavailable for GET {resource}: {error}"
            ) from error
        return ArtifactContent(
            artifact_id=artifact_id,
            content_type=content_type,
            content_disposition=disposition,
            data=data,
        )

    @staticmethod
    def _object(payload: Any, *, resource: str) -> JsonObject:
        if not isinstance(payload, dict):
            raise ManagerApiError(f"Manager returned a non-object for {resource}")
        return payload

    def _require_actor(self, required: str, operation: str) -> None:
        if self.actor != required:
            raise PermissionError(
                f"{operation} requires the scoped {required!r} actor client"
            )

    def health(self) -> ManagerHealth:
        try:
            payload = self._request("GET", "health")
        except ManagerApiError as error:
            # Transitional builds may expose only /meta.  This is a typed,
            # read-only compatibility probe, not a second control plane.
            if error.status_code != 404:
                raise
            payload = self._request("GET", "meta")
        return ManagerHealth.from_payload(self._object(payload, resource="health"))

    def meta(self) -> JsonObject:
        return self._object(self._request("GET", "meta"), resource="meta")

    def issue_webgui_bootstrap(self) -> WebGuiBootstrap:
        self._require_actor("tray", "issue_webgui_bootstrap")
        if self._actor_token_override is None:
            raise PermissionError(
                "issue_webgui_bootstrap requires the tray's in-memory credential"
            )
        payload = self._object(
            self._request("POST", "webgui/bootstrap-nonces", body={}),
            resource="WebGUI bootstrap",
        )
        return WebGuiBootstrap.from_payload(payload)

    # The full snapshot projects every game, Todo scope and recent batch; it is
    # legitimately slower than a health probe, so it gets a wider bound than
    # the config's short request timeout.
    SNAPSHOT_TIMEOUT_SECONDS = 20.0

    def snapshot(self) -> JsonObject:
        return self._object(
            self._request(
                "GET",
                "snapshot",
                timeout_seconds=max(
                    self.SNAPSHOT_TIMEOUT_SECONDS, self.config.request_timeout_seconds
                ),
            ),
            resource="snapshot",
        )

    def _resolve_expected_state_version(
        self, expected_state_version: int | None
    ) -> int:
        """Return an explicit CAS version, reading the current snapshot if omitted.

        The caller's idempotency key is deliberately not involved in this read.
        A repeated POST therefore keeps the exact same key and body; Manager can
        replay the stored receipt before applying the newly observed CAS value.
        """

        supplied = expected_state_version is not None
        if expected_state_version is None:
            snapshot = self.snapshot()
            expected_state_version = snapshot.get("stateVersion")
            if expected_state_version is None:
                expected_state_version = snapshot.get("state_version")
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 0
        ):
            if supplied:
                raise ValueError(
                    "expected_state_version must be a non-negative integer"
                )
            raise ManagerApiError(
                "Manager snapshot did not contain a valid non-negative stateVersion"
            )
        return expected_state_version

    def games(self) -> Any:
        return self._request("GET", "games")

    def create_daily_batch(
        self,
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "batches",
            body={
                "kind": "daily",
                "mode": "execute",
                "requestedBy": self.actor,
            },
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(self._object(payload, resource="batches"))

    def create_game_run(
        self,
        game_id: str,
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        if not game_id or any(char in game_id for char in "/\\?#"):
            raise ValueError("game_id contains unsupported characters")
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            f"games/{game_id}/run-requests",
            body={"kind": "daily", "requestedBy": self.actor},
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="game run request")
        )

    def cancel_batch(
        self,
        batch_id: str,
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        if not batch_id or "/" in batch_id:
            raise ValueError("invalid batch_id")
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            f"batches/{batch_id}/cancel-requests",
            body={"reason": "operator_request", "requestedBy": self.actor},
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="batch cancel request")
        )

    def resume_batch(
        self,
        batch_id: str,
        *,
        reason: str = "operator_request",
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        batch_id = _opaque_id(batch_id, name="batch_id")
        reason = _bounded_text(reason, name="resume reason", maximum_length=1000)
        if not reason:
            raise ValueError("resume reason is required")
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            f"batches/{batch_id}/resume-requests",
            body={"reason": reason, "requestedBy": self.actor},
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="batch resume request")
        )

    def list_work_items(self) -> Any:
        return self._request("GET", "agent/work-items")

    def get_work_item(self, work_item_id: str) -> AgentWorkItem:
        work_item_id = _opaque_id(work_item_id, name="work_item_id", maximum_length=120)
        payload = self._object(
            self._request("GET", f"agent/work-items/{work_item_id}"),
            resource="agent work item",
        )
        return AgentWorkItem.from_payload(payload)

    def get_artifact(
        self,
        artifact_id: str,
        *,
        work_item_id: str | None = None,
        claim_id: str | None = None,
        fencing_token: str | None = None,
    ) -> ArtifactMetadata:
        artifact_id = _opaque_id(artifact_id, name="artifact_id")
        artifact_claim = self._artifact_claim_context(
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )
        payload = self._object(
            self._request(
                "GET",
                f"artifacts/{artifact_id}",
                artifact_claim=artifact_claim,
            ),
            resource="artifact",
        )
        return ArtifactMetadata.from_payload(payload)

    def get_artifact_content(
        self,
        artifact_id: str,
        *,
        work_item_id: str | None = None,
        claim_id: str | None = None,
        fencing_token: str | None = None,
    ) -> ArtifactContent:
        artifact_id = _opaque_id(artifact_id, name="artifact_id")
        artifact_claim = self._artifact_claim_context(
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )
        return self._request_artifact_content(
            artifact_id,
            artifact_claim=artifact_claim,
        )

    def dispatch_work_item(
        self,
        kind: str,
        *,
        idempotency_key: str,
        game_id: str | None = None,
        cadence: str | None = None,
        run_id: str | None = None,
        note: str = "",
        artifact_refs: tuple[str, ...] = (),
        allowed_capability_refs: tuple[str, ...] = (),
        expected_state_version: int | None = None,
    ) -> WorkItemDispatched:
        self._require_actor("rabiroute", "dispatch_work_item")
        if kind not in WORK_ITEM_KINDS:
            raise ValueError(f"unsupported work item kind: {kind}")
        if game_id is not None:
            game_id = _opaque_id(game_id, name="game_id", maximum_length=40)
        if cadence is not None and cadence not in {"daily", "weekly"}:
            raise ValueError("cadence must be daily or weekly")
        if run_id is not None:
            run_id = _opaque_id(run_id, name="run_id", maximum_length=80)
        if len(note) > 500:
            raise ValueError("note exceeds 500 characters")
        artifacts = [
            _opaque_id(value, name="artifact_ref") for value in artifact_refs
        ]
        if len(artifacts) > 50:
            raise ValueError("artifact_refs exceeds 50 items")
        capabilities = [str(value) for value in allowed_capability_refs]
        if len(capabilities) > 50 or any(
            not value or len(value) > 100 or "/" in value or "\\" in value
            for value in capabilities
        ):
            raise ValueError("allowed_capability_refs contains an invalid reference")
        body: JsonObject = {
            "kind": kind,
            "mode": "plan",
            "requestedBy": self.actor,
            "note": note,
            "artifactRefs": artifacts,
            "allowedCapabilityRefs": capabilities,
        }
        if game_id is not None:
            body["gameId"] = game_id
        if cadence is not None:
            body["cadence"] = cadence
        if run_id is not None:
            body["runId"] = run_id
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "agent/work-items",
            body=body,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        receipt = CommandAccepted.from_payload(
            self._object(payload, resource="work item dispatch")
        )
        return WorkItemDispatched.from_receipt(receipt)

    def claim_work_item(
        self,
        work_item_id: str,
        *,
        idempotency_key: str,
        lease_seconds: int = 300,
        expected_state_version: int | None = None,
    ) -> WorkItemClaimAccepted:
        self._require_actor("agent", "claim_work_item")
        work_item_id = _opaque_id(work_item_id, name="work_item_id", maximum_length=120)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 30 and 3600")
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            f"agent/work-items/{work_item_id}/claims",
            body={"claimant": self.actor, "leaseSeconds": lease_seconds},
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        receipt = CommandAccepted.from_payload(
            self._object(payload, resource="work item claim")
        )
        return WorkItemClaimAccepted.from_receipt(receipt)

    def submit_claim_decision(
        self,
        claim_id: str,
        fencing_token: str,
        decision: str,
        reason: str,
        *,
        idempotency_key: str,
        evidence_ids: tuple[str, ...] = (),
        todo_diagnoses: Sequence[Mapping[str, Any]] | None = None,
        todo_diagnosis: Mapping[str, Any] | None = None,
        completion_review: Mapping[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        self._require_actor("agent", "submit_claim_decision")
        claim_id = _opaque_id(claim_id, name="claim_id", maximum_length=120)
        fencing_token = _fencing_token(fencing_token)
        if decision not in {"accepted", "rejected", "review_required"}:
            raise ValueError("decision is invalid")
        if not reason or len(reason) > 1000:
            raise ValueError("reason must contain 1 to 1000 characters")
        evidence = [
            _opaque_id(value, name="evidence_id") for value in evidence_ids
        ]
        if len(evidence) > 50:
            raise ValueError("evidence_ids exceeds 50 items")
        body: JsonObject = {
            "claimId": claim_id,
            "fencingToken": fencing_token,
            "decision": decision,
            "reason": reason,
            "evidenceIds": evidence,
            "requestedBy": self.actor,
        }
        if todo_diagnoses is not None and todo_diagnosis is not None:
            raise ValueError("todo_diagnoses and legacy todo_diagnosis are mutually exclusive")
        if todo_diagnoses is not None:
            if len(todo_diagnoses) > 100:
                raise ValueError("todo_diagnoses exceeds 100 items")
            rendered_diagnoses = [
                _todo_diagnosis(value, multi_todo_contract=True)
                for value in todo_diagnoses
            ]
            todo_ids = [str(value["todoInstanceId"]) for value in rendered_diagnoses]
            if len(todo_ids) != len(set(todo_ids)):
                raise ValueError("todo_diagnoses contains duplicate todoInstanceId values")
            body["todoDiagnoses"] = rendered_diagnoses
        elif todo_diagnosis is not None:
            body["todoDiagnosis"] = _todo_diagnosis(todo_diagnosis)
        else:
            body["todoDiagnoses"] = []
        if completion_review is not None:
            body["completionReview"] = _completion_review(
                completion_review, set(evidence)
            )
            if decision == "accepted" and (
                not body["completionReview"]["todoReviews"]
                or any(
                    item["verdict"] != "confirmed"
                    for item in body["completionReview"]["todoReviews"]
                )
            ):
                raise ValueError(
                    "accepted completion_review requires explicit confirmed Todo verdicts"
                )
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "claims/decisions",
            body=body,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="claim decision")
        )

    def invoke_claimed_capability(
        self,
        capability_ref: str,
        arguments: Mapping[str, Any],
        *,
        work_item_id: str,
        claim_id: str,
        fencing_token: str,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        self._require_actor("agent", "invoke_claimed_capability")
        if not capability_ref or len(capability_ref) > 100:
            raise ValueError("capability_ref is required")
        work_item_id = _opaque_id(work_item_id, name="work_item_id", maximum_length=120)
        claim_id = _opaque_id(claim_id, name="claim_id", maximum_length=120)
        fencing_token = _fencing_token(fencing_token)
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "capability-invocations",
            body={
                "capability": capability_ref,
                "arguments": dict(arguments),
                "requestedBy": self.actor,
                "workItemId": work_item_id,
                "claimId": claim_id,
                "fencingToken": fencing_token,
            },
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="claimed capability invocation")
        )

    def list_capabilities(self) -> Any:
        return self._request("GET", "capabilities")

    def invoke_capability(
        self,
        capability_ref: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        if not capability_ref:
            raise ValueError("capability_ref is required")
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "capability-invocations",
            body={
                "capability": capability_ref,
                "arguments": dict(arguments),
                "requestedBy": self.actor,
            },
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="capability invocation")
        )

    def command(self, command_id: str) -> JsonObject:
        if not command_id or "/" in command_id:
            raise ValueError("invalid command_id")
        return self._object(
            self._request("GET", f"commands/{command_id}"), resource="command"
        )

    def request_safe_stop(
        self,
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "manager/stop-requests",
            body={
                "requestedBy": self.actor,
                "reason": "operator-request",
            },
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="manager stop request")
        )

    def request_restart(
        self,
        *,
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> CommandAccepted:
        expected_state_version = self._resolve_expected_state_version(
            expected_state_version
        )
        payload = self._request(
            "POST",
            "manager/restart-requests",
            body={
                "requestedBy": self.actor,
                "reason": "operator-request",
            },
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )
        return CommandAccepted.from_payload(
            self._object(payload, resource="manager restart request")
        )
