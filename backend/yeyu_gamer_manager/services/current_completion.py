"""Project current-GameDay acceptance from immutable Manager seals.

The legacy ``games.reward_claimed`` column is deliberately outside this
module.  A game is accepted only when the newest sealed CompletionContract in
the *current frozen Todo scope* says ``accepted_done``.  A newer blocked or
review-required contract supersedes an older acceptance for the same scope.

This is a pure read-model service: callers supply the current per-game Todo
scope and recent sealed batch snapshots; the helper performs no storage reads
and no mutations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from yeyu_gamer_manager.domain.completion_contract import (
    CompletionContractDecision,
    CompletionOutcome,
)


class CurrentCompletionStatus(StrEnum):
    ACCEPTED_DONE = "accepted_done"
    EVIDENCE_PENDING = "evidence_pending"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CurrentCompletionProjection:
    """The evidence-backed acceptance fact rendered by Manager projections."""

    status: CurrentCompletionStatus
    game_id: str
    scope_key: str
    scope_fingerprint: str
    period_keys: tuple[str, ...]
    batch_id: str | None = None
    seal_version: int | None = None
    game_day_key: str | None = None
    decision_id: str | None = None
    review_id: str | None = None
    decision: CompletionOutcome | None = None
    evidence_ids: tuple[str, ...] = ()
    screenshot_evidence_ids: tuple[str, ...] = ()
    reason_code: str = "no_current_sealed_completion_contract"


@dataclass(frozen=True, slots=True)
class _CurrentScope:
    scope_key: str
    scope_fingerprint: str
    period_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    batch_id: str
    batch_state: str
    seal_version: int
    decision_id: str
    decision: CompletionContractDecision


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _current_scope(value: Mapping[str, Any]) -> _CurrentScope:
    scope_key = value.get("scopeKey")
    scope_fingerprint = value.get("scopeFingerprint")
    period_keys = _strings(value.get("periodKeys"))
    if not isinstance(scope_key, str) or not scope_key:
        raise ValueError("current Todo scope requires scopeKey")
    if not isinstance(scope_fingerprint, str) or not scope_fingerprint:
        raise ValueError("current Todo scope requires scopeFingerprint")
    if not period_keys:
        raise ValueError("current Todo scope requires at least one periodKey")
    return _CurrentScope(
        scope_key=scope_key,
        scope_fingerprint=scope_fingerprint,
        period_keys=period_keys,
    )


def _frozen_game_scope(
    result: Mapping[str, Any], game_id: str
) -> Mapping[str, Any] | None:
    todo_scope = result.get("todoScope")
    if not isinstance(todo_scope, Mapping):
        return None
    games = todo_scope.get("games")
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        return None
    matches = [
        item
        for item in games
        if isinstance(item, Mapping) and item.get("gameId") == game_id
    ]
    return matches[0] if len(matches) == 1 else None


def _scope_matches(
    frozen: Mapping[str, Any] | None, current: _CurrentScope
) -> bool:
    if frozen is None:
        return False
    return (
        frozen.get("scopeKey") == current.scope_key
        and frozen.get("scopeFingerprint") == current.scope_fingerprint
        and frozenset(_strings(frozen.get("periodKeys")))
        == frozenset(current.period_keys)
    )


def _decision(value: Mapping[str, Any]) -> tuple[str, CompletionContractDecision] | None:
    payload = dict(value)
    decision_id = payload.pop("completionAdjudicationId", None)
    if not isinstance(decision_id, str) or not decision_id:
        return None
    try:
        decision = CompletionContractDecision.model_validate(payload)
    except ValidationError:
        return None
    # accepted_done is impossible without an immutable CompletionReview. Normal
    # promoted Adapter runs may receive a Manager-authored machine review;
    # visual-only or ambiguous results still require an Agent review.
    # Other sealed outcomes are also expected to reference their adjudicating
    # review; ignoring a malformed legacy contract is safer than reviving it.
    if not decision.review_id:
        return None
    return decision_id, decision


def _batch_candidates(
    *,
    game_id: str,
    current: _CurrentScope,
    batch: Mapping[str, Any],
) -> tuple[_Candidate, ...]:
    batch_id = batch.get("batch_id", batch.get("batchId"))
    result = batch.get("result")
    if not isinstance(batch_id, str) or not batch_id or not isinstance(result, Mapping):
        return ()
    seal_version = result.get("sealVersion")
    if isinstance(seal_version, bool) or not isinstance(seal_version, int) or seal_version < 1:
        return ()
    if not _scope_matches(_frozen_game_scope(result, game_id), current):
        return ()
    contracts = result.get("completionContracts")
    if not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)):
        return ()

    candidates: list[_Candidate] = []
    for value in contracts:
        if not isinstance(value, Mapping):
            continue
        parsed = _decision(value)
        if parsed is None:
            continue
        decision_id, decision = parsed
        if (
            decision.game_id != game_id
            or decision.game_day_key not in current.period_keys
        ):
            continue
        candidates.append(
            _Candidate(
                batch_id=batch_id,
                batch_state=str(batch.get("state") or ""),
                seal_version=seal_version,
                decision_id=decision_id,
                decision=decision,
            )
        )
    return tuple(candidates)


def _evidence(decision: CompletionContractDecision) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *decision.artifact_refs,
                *decision.accepted_evidence_refs,
                *decision.supporting_artifact_refs,
            )
        )
    )


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def project_current_game_completion(
    *,
    game_id: str,
    current_scope: Mapping[str, Any],
    sealed_batches: Sequence[Mapping[str, Any]],
    invalidated_at: datetime | str | None = None,
) -> CurrentCompletionProjection:
    """Return the newest immutable completion fact for one current Todo scope.

    ``sealVersion`` is the Manager event-ledger sequence and therefore decides
    recency even if callers obtained batches in creation-time order.  A seal is
    considered only when its frozen per-game Todo ``scopeKey``, fingerprint and
    period-key set exactly match the current scope.
    """

    if not game_id:
        raise ValueError("gameId is required")
    current = _current_scope(current_scope)
    required_remaining = current_scope.get("requiredRemaining", 0)
    if (
        not isinstance(required_remaining, bool)
        and isinstance(required_remaining, int)
        and required_remaining > 0
    ):
        return CurrentCompletionProjection(
            status=CurrentCompletionStatus.NONE,
            game_id=game_id,
            scope_key=current.scope_key,
            scope_fingerprint=current.scope_fingerprint,
            period_keys=current.period_keys,
            reason_code="current_todo_scope_has_unresolved_todos",
        )
    reset_watermark = _timestamp(invalidated_at)
    eligible_batches = []
    for batch in sealed_batches:
        result = batch.get("result")
        if reset_watermark is not None:
            sealed_at = _timestamp(
                result.get("sealedAt") if isinstance(result, Mapping) else None
            )
            # An explicit current-period reset invalidates every older seal.
            # Missing/invalid seal time is fail-closed once a reset watermark
            # exists; it must never resurrect a pre-reset completion fact.
            if sealed_at is None or sealed_at <= reset_watermark:
                continue
        eligible_batches.append(batch)
    candidates = [
        candidate
        for batch in eligible_batches
        for candidate in _batch_candidates(
            game_id=game_id,
            current=current,
            batch=batch,
        )
    ]
    if not candidates:
        return CurrentCompletionProjection(
            status=CurrentCompletionStatus.NONE,
            game_id=game_id,
            scope_key=current.scope_key,
            scope_fingerprint=current.scope_fingerprint,
            period_keys=current.period_keys,
        )

    newest_version = max(item.seal_version for item in candidates)
    newest = [item for item in candidates if item.seal_version == newest_version]
    if len(newest) != 1:
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for item in newest
                for evidence_id in _evidence(item.decision)
            )
        )
        screenshot_ids = tuple(
            dict.fromkeys(
                evidence_id
                for item in newest
                for evidence_id in item.decision.screenshot_artifact_refs
            )
        )
        return CurrentCompletionProjection(
            status=CurrentCompletionStatus.EVIDENCE_PENDING,
            game_id=game_id,
            scope_key=current.scope_key,
            scope_fingerprint=current.scope_fingerprint,
            period_keys=current.period_keys,
            batch_id=newest[0].batch_id,
            seal_version=newest_version,
            evidence_ids=evidence_ids,
            screenshot_evidence_ids=screenshot_ids,
            reason_code="ambiguous_current_completion_contracts",
        )

    candidate = newest[0]
    decision = candidate.decision
    accepted = (
        decision.outcome == CompletionOutcome.ACCEPTED_DONE
        and decision.accepted_done
        and candidate.batch_state != "cancelled"
    )
    return CurrentCompletionProjection(
        status=(
            CurrentCompletionStatus.ACCEPTED_DONE
            if accepted
            else CurrentCompletionStatus.EVIDENCE_PENDING
        ),
        game_id=game_id,
        scope_key=current.scope_key,
        scope_fingerprint=current.scope_fingerprint,
        period_keys=current.period_keys,
        batch_id=candidate.batch_id,
        seal_version=candidate.seal_version,
        game_day_key=decision.game_day_key,
        decision_id=candidate.decision_id,
        review_id=decision.review_id,
        decision=decision.outcome,
        evidence_ids=_evidence(decision),
        screenshot_evidence_ids=decision.screenshot_artifact_refs,
        reason_code=(
            "current_completion_contract_accepted"
            if accepted
            else (
                "accepted_contract_belongs_to_cancelled_batch"
                if decision.outcome == CompletionOutcome.ACCEPTED_DONE
                else "current_completion_contract_not_accepted"
            )
        ),
    )
