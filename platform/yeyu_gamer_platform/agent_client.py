"""Narrow Agent and RabiRoute facades over the typed Manager client.

These facades intentionally expose no arbitrary URL, file path, shell, click,
or generic request primitive. Manager remains the only action authority.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .api_client import (
    AgentWorkItem,
    ArtifactContent,
    ArtifactMetadata,
    CommandAccepted,
    ManagerApiClient,
    WorkItemClaimAccepted,
    WorkItemDispatched,
)
from .config import PlatformConfig


DEFAULT_AGENT_WORKER_ID = "yeyu"
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")


class AgentManagerClient:
    """Claim-scoped Manager surface for the local Agent runtime."""

    def __init__(
        self, config: PlatformConfig, *, worker_id: str = DEFAULT_AGENT_WORKER_ID
    ) -> None:
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("worker_id must be a safe local Agent worker identifier")
        token_path = config.actor_token_directory / f"agent.{worker_id}.token"
        if token_path.is_symlink() or not token_path.is_file():
            raise FileNotFoundError(f"Agent worker credential is unavailable: {worker_id}")
        worker_token = token_path.read_text(encoding="ascii").strip()
        self.worker_id = worker_id
        self._client = ManagerApiClient(
            config,
            actor="agent",
            actor_token_override=worker_token,
        )

    def list_work_items(self) -> Any:
        return self._client.list_work_items()

    def get_work_item(self, work_item_id: str) -> AgentWorkItem:
        return self._client.get_work_item(work_item_id)

    def get_command_status(self, command_id: str) -> Any:
        return self._client.command(command_id)

    def get_artifact(
        self,
        artifact_id: str,
        *,
        work_item_id: str,
        claim_id: str,
        fencing_token: str,
    ) -> ArtifactMetadata:
        return self._client.get_artifact(
            artifact_id,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )

    def get_artifact_content(
        self,
        artifact_id: str,
        *,
        work_item_id: str,
        claim_id: str,
        fencing_token: str,
    ) -> ArtifactContent:
        return self._client.get_artifact_content(
            artifact_id,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
        )

    def claim_work_item(
        self,
        work_item_id: str,
        *,
        idempotency_key: str,
        lease_seconds: int = 300,
        expected_state_version: int | None = None,
    ) -> WorkItemClaimAccepted:
        return self._client.claim_work_item(
            work_item_id,
            idempotency_key=idempotency_key,
            lease_seconds=lease_seconds,
            expected_state_version=expected_state_version,
        )

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
        return self._client.submit_claim_decision(
            claim_id,
            fencing_token,
            decision,
            reason,
            idempotency_key=idempotency_key,
            evidence_ids=evidence_ids,
            todo_diagnoses=todo_diagnoses,
            todo_diagnosis=todo_diagnosis,
            completion_review=completion_review,
            expected_state_version=expected_state_version,
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
        return self._client.invoke_claimed_capability(
            capability_ref,
            arguments,
            work_item_id=work_item_id,
            claim_id=claim_id,
            fencing_token=fencing_token,
            idempotency_key=idempotency_key,
            expected_state_version=expected_state_version,
        )


class RabiRouteManagerClient:
    """Dispatch-only mutation surface for RabiRoute."""

    def __init__(self, config: PlatformConfig) -> None:
        self._client = ManagerApiClient(config, actor="rabiroute")

    def get_work_item(self, work_item_id: str) -> AgentWorkItem:
        return self._client.get_work_item(work_item_id)

    def get_command_status(self, command_id: str) -> Any:
        return self._client.command(command_id)

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
        return self._client.dispatch_work_item(
            kind,
            idempotency_key=idempotency_key,
            game_id=game_id,
            cadence=cadence,
            run_id=run_id,
            note=note,
            artifact_refs=artifact_refs,
            allowed_capability_refs=allowed_capability_refs,
            expected_state_version=expected_state_version,
        )
