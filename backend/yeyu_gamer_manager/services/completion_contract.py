"""Pure CompletionContract adjudication over Manager-projected facts.

Integration point:

1. In the same Manager read/commit transaction, load one GameRun, its current
   RunAttempt, joined TodoAttempt facts, owned artifacts, current-period active
   blockers and the accepted/rejected Agent claim decision.
2. Project those rows into :class:`CompletionContractSnapshot`. Structured
   predicate observations come only from a typed visual-review result. A
   Manager-authored promoted-Adapter review is a separate tool-authoritative
   path and never fabricates visual metrics from an exit code or tool log.
3. Call :func:`adjudicate_completion` before sealing the GameRun/Batch.  Persist
   the returned decision as an immutable adjudication record and set
   ``acceptedDone`` only from ``decision.accepted_done``.

This module never reads storage, starts a process, captures a window or sends a
notification.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yeyu_gamer_manager.domain.completion_contract import (
    AgentPredicateObservation,
    AgentReviewDecision,
    CompletionContractDecision,
    CompletionContractSnapshot,
    CompletionOutcome,
    CompletionPredicateCode,
    CompletionRunAttemptState,
    CompletionTodoState,
    EvidenceArtifactFact,
    EvidenceExclusionReason,
    ExcludedEvidence,
    PredicateEvaluation,
    PredicateResultState,
    TodoReviewVerdict,
)


STARRAIL_POINTS_OBSERVATION = "starrail.daily_training.points"
STARRAIL_REWARD_TIERS_OBSERVATION = "starrail.daily_training.reward_tiers"
COMPLETION_REVIEW_CONTRACT_SCHEMA = "completion-review-contract/v2"
COMPLETION_POLICY_VERSION = "2"
COMPLETION_EVIDENCE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "text/plain"}
)
COMPLETION_REVIEW_EVIDENCE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg"}
)


class MetricOperator(StrEnum):
    EQUALS = "equals"
    AT_LEAST = "at_least"


@dataclass(frozen=True, slots=True)
class IntegerMetricConstraint:
    metric: str
    operator: MetricOperator
    expected: int

    def matches(self, metrics: Mapping[str, bool | int | str]) -> bool:
        value = metrics.get(self.metric)
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if self.operator == MetricOperator.EQUALS:
            return value == self.expected
        return value >= self.expected


@dataclass(frozen=True, slots=True)
class ReviewedArtifactPredicateRule:
    code: CompletionPredicateCode
    observation_id: str
    constraints: tuple[IntegerMetricConstraint, ...]
    allowed_artifact_kinds: frozenset[str]
    satisfied_message: str
    missing_message: str


@dataclass(frozen=True, slots=True)
class GameCompletionPolicy:
    game_id: str
    cadence: str
    timezone: str
    reset_utc_offset_minutes: int
    reset_local_time: time
    predicate_rules: tuple[ReviewedArtifactPredicateRule, ...] = ()


class CompletionPolicyRegistry:
    """Immutable game-policy registry; extensions return a new registry."""

    def __init__(self, policies: Iterable[GameCompletionPolicy]) -> None:
        by_game: dict[str, GameCompletionPolicy] = {}
        for policy in policies:
            if policy.game_id in by_game:
                raise ValueError(f"duplicate completion policy: {policy.game_id}")
            if not -14 * 60 <= policy.reset_utc_offset_minutes <= 14 * 60:
                raise ValueError(
                    f"invalid reset UTC offset for {policy.game_id}"
                )
            by_game[policy.game_id] = policy
        self._policies = MappingProxyType(by_game)

    def resolve(self, game_id: str) -> GameCompletionPolicy | None:
        return self._policies.get(game_id)

    def with_policy(self, policy: GameCompletionPolicy) -> "CompletionPolicyRegistry":
        return CompletionPolicyRegistry(
            [
                *(
                    item
                    for game_id, item in self._policies.items()
                    if game_id != policy.game_id
                ),
                policy,
            ]
        )

    @property
    def game_ids(self) -> tuple[str, ...]:
        return tuple(self._policies)


_STARRAIL_SCREENSHOT_KINDS = frozenset(
    {
        "game-ui-main-window",
        "game-ui-daily-training-panel",
    }
)


STARRAIL_COMPLETION_POLICY = GameCompletionPolicy(
    game_id="StarRail",
    cadence="daily",
    timezone="Asia/Shanghai",
    reset_utc_offset_minutes=8 * 60,
    reset_local_time=time(4, 0),
    predicate_rules=(
        ReviewedArtifactPredicateRule(
            code=CompletionPredicateCode.STARRAIL_DAILY_TRAINING_500,
            observation_id=STARRAIL_POINTS_OBSERVATION,
            constraints=(
                IntegerMetricConstraint("current", MetricOperator.EQUALS, 500),
                IntegerMetricConstraint("target", MetricOperator.EQUALS, 500),
            ),
            allowed_artifact_kinds=_STARRAIL_SCREENSHOT_KINDS,
            satisfied_message=(
                "Agent visual review confirmed the current-attempt daily "
                "training panel at 500/500."
            ),
            missing_message=(
                "A current-attempt raw screenshot with an Agent-reviewed "
                "500/500 observation is required."
            ),
        ),
        ReviewedArtifactPredicateRule(
            code=(
                CompletionPredicateCode.STARRAIL_FIVE_REWARD_TIERS_CLAIMED
            ),
            observation_id=STARRAIL_REWARD_TIERS_OBSERVATION,
            constraints=(
                IntegerMetricConstraint("claimed", MetricOperator.EQUALS, 5),
                IntegerMetricConstraint("total", MetricOperator.EQUALS, 5),
            ),
            allowed_artifact_kinds=_STARRAIL_SCREENSHOT_KINDS,
            satisfied_message=(
                "Agent visual review confirmed all five daily-training reward "
                "tiers were claimed."
            ),
            missing_message=(
                "A current-attempt raw screenshot with an Agent-reviewed 5/5 "
                "reward-tier observation is required."
            ),
        ),
    ),
)


DEFAULT_COMPLETION_POLICIES = CompletionPolicyRegistry(
    (STARRAIL_COMPLETION_POLICY,)
)


def policy_for_frozen_game_day(
    *,
    game_id: str,
    cadence: str,
    timezone_name: str,
    reset_utc_offset_minutes: int,
    reset_local_time: time,
    base_policies: CompletionPolicyRegistry = DEFAULT_COMPLETION_POLICIES,
) -> GameCompletionPolicy:
    """Bind a game's predicate rules to its frozen Todo reset contract.

    The Todo instance is the reset-time source of truth.  Static policies only
    contribute game-specific visual predicates; they may not silently replace
    a per-game reset override that was frozen when the current Todo instances
    were created.  Games without additional visual metrics still receive the
    generic CompletionContract predicates (current terminal attempt, completed
    required Todo, fresh evidence, accepted Agent review, and no blockers).
    """

    base = base_policies.resolve(game_id)
    predicate_rules = (
        base.predicate_rules
        if base is not None and base.cadence == cadence
        else ()
    )
    return GameCompletionPolicy(
        game_id=game_id,
        cadence=cadence,
        timezone=timezone_name,
        reset_utc_offset_minutes=reset_utc_offset_minutes,
        reset_local_time=reset_local_time,
        predicate_rules=predicate_rules,
    )


def _policy_predicates(policy: GameCompletionPolicy) -> list[dict[str, object]]:
    return [
        {
            "predicateId": rule.observation_id,
            "constraints": [
                {
                    "metric": constraint.metric,
                    "operator": str(constraint.operator),
                    "expected": constraint.expected,
                }
                for constraint in rule.constraints
            ],
            "allowedArtifactKinds": sorted(rule.allowed_artifact_kinds),
        }
        for rule in policy.predicate_rules
    ]


def completion_policy_identity(policy: GameCompletionPolicy) -> tuple[str, str]:
    predicates = _policy_predicates(policy)
    version_material = {
        "version": COMPLETION_POLICY_VERSION,
        "gameId": policy.game_id,
        "cadence": policy.cadence,
        "timezone": policy.timezone,
        "resetTime": policy.reset_local_time.strftime("%H:%M"),
        "predicates": predicates,
    }
    policy_digest = hashlib.sha256(
        json.dumps(
            version_material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return (
        f"{policy.game_id}.{policy.cadence}",
        f"{COMPLETION_POLICY_VERSION}+sha256.{policy_digest}",
    )


def policy_review_contract(
    policy: GameCompletionPolicy,
    *,
    period_starts_at: datetime,
    period_ends_at: datetime,
    policy_status: str = "supported",
    unsupported_reason: str | None = None,
) -> dict[str, object]:
    """Return the bounded UI/Agent contract for one evidence-review work item.

    The contract freezes both the reset window and the game-specific predicate
    vocabulary.  WebGUI and Agent clients render this document; they must not
    carry a second copy of game completion rules.
    """

    if period_starts_at.tzinfo is None or period_ends_at.tzinfo is None:
        raise ValueError("completion review period boundaries must be timezone-aware")
    if period_starts_at >= period_ends_at:
        raise ValueError("completion review period must be ordered")
    if policy_status not in {"supported", "unsupported"}:
        raise ValueError("invalid completion review policy status")
    if policy_status == "unsupported" and not unsupported_reason:
        raise ValueError("unsupported completion policy requires a reason")

    predicates = _policy_predicates(policy)
    policy_id, policy_version = completion_policy_identity(policy)
    return {
        "schemaVersion": COMPLETION_REVIEW_CONTRACT_SCHEMA,
        "policyId": policy_id,
        "policyVersion": policy_version,
        "policyStatus": policy_status,
        "gameId": policy.game_id,
        "cadence": policy.cadence,
        "timezone": policy.timezone,
        "resetTime": policy.reset_local_time.strftime("%H:%M"),
        "periodStartsAt": period_starts_at.isoformat(),
        "periodEndsAt": period_ends_at.isoformat(),
        "unsupportedReason": unsupported_reason,
        "todoReviewContract": {
            "requiredForAccepted": True,
            "acceptedVerdict": str(TodoReviewVerdict.CONFIRMED),
            "reasonCodeRequired": True,
            "artifactRefsRequired": True,
            "allowedEvidenceContentTypes": sorted(
                COMPLETION_REVIEW_EVIDENCE_CONTENT_TYPES
            ),
        },
        "predicates": predicates,
    }


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _evaluation(
    code: CompletionPredicateCode,
    state: PredicateResultState,
    message: str,
    *,
    artifact_refs: Iterable[str] = (),
    todo_instance_ids: Iterable[str] = (),
) -> PredicateEvaluation:
    return PredicateEvaluation(
        code=code,
        state=state,
        message=message,
        artifact_refs=_unique(artifact_refs),
        todo_instance_ids=_unique(todo_instance_ids),
    )


def _inside_game_day(snapshot: CompletionContractSnapshot, value) -> bool:
    return snapshot.game_day.starts_at <= value < snapshot.game_day.ends_at


def _attempt_lineage(
    snapshot: CompletionContractSnapshot,
) -> tuple[RunAttemptCompletionFact, ...]:
    """Return the frozen same-GameDay attempt lineage used by the contract.

    The fallback keeps older persisted snapshots readable. New Manager
    projections always provide the complete same-GameDay lineage so a safe
    successor batch can skip Todo facts already completed by a predecessor
    GameRun without replaying one-time or stamina-spending actions.
    """

    if snapshot.attempt_lineage:
        return snapshot.attempt_lineage
    if snapshot.current_attempt is not None:
        return (snapshot.current_attempt,)
    return ()


def _attempt_is_in_scope(
    snapshot: CompletionContractSnapshot,
    attempt: RunAttemptCompletionFact,
) -> bool:
    return (
        attempt.game_id == snapshot.game_id
        and attempt.cadence == snapshot.cadence
        and _inside_game_day(snapshot, attempt.started_at)
        and (
            attempt.completed_at is None
            or _inside_game_day(snapshot, attempt.completed_at)
        )
    )


def _scoped_attempt_ids(snapshot: CompletionContractSnapshot) -> frozenset[str]:
    return frozenset(
        attempt.run_attempt_id
        for attempt in _attempt_lineage(snapshot)
        if _attempt_is_in_scope(snapshot, attempt)
    )


def _valid_policy_window(
    snapshot: CompletionContractSnapshot, policy: GameCompletionPolicy
) -> bool:
    try:
        zone = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        # Packaged Windows Python does not necessarily carry the IANA tzdata
        # wheel. China Standard Time has no DST, so its explicit frozen UTC
        # offset is an exact fallback. Unknown zones still fail closed.
        if (
            policy.timezone != "Asia/Shanghai"
            or policy.reset_utc_offset_minutes != 8 * 60
        ):
            return False
        zone = timezone(timedelta(hours=8), name="Asia/Shanghai")
    start = snapshot.game_day.starts_at.astimezone(zone)
    end = snapshot.game_day.ends_at.astimezone(zone)
    expected_days = 1 if policy.cadence == "daily" else 7
    expected_offset = timedelta(minutes=policy.reset_utc_offset_minutes)
    return (
        start.timetz().replace(tzinfo=None) == policy.reset_local_time
        and end.timetz().replace(tzinfo=None) == policy.reset_local_time
        and start.utcoffset() == expected_offset
        and end.utcoffset() == expected_offset
        and end.date() == start.date() + timedelta(days=expected_days)
    )


def _classify_evidence(
    snapshot: CompletionContractSnapshot,
) -> tuple[dict[str, EvidenceArtifactFact], tuple[ExcludedEvidence, ...]]:
    scoped_attempt_runs = {
        attempt.run_attempt_id: attempt.run_id
        for attempt in _attempt_lineage(snapshot)
        if _attempt_is_in_scope(snapshot, attempt)
    }
    accepted: dict[str, EvidenceArtifactFact] = {}
    reasons_by_artifact: dict[str, list[EvidenceExclusionReason]] = {}
    for evidence in snapshot.evidence:
        reasons: list[EvidenceExclusionReason] = []
        if evidence.game_id != snapshot.game_id:
            reasons.append(EvidenceExclusionReason.GAME_MISMATCH)
        if scoped_attempt_runs.get(evidence.run_attempt_id) != evidence.run_id:
            reasons.append(EvidenceExclusionReason.RUN_MISMATCH)
        if evidence.run_attempt_id not in scoped_attempt_runs:
            reasons.append(EvidenceExclusionReason.ATTEMPT_MISMATCH)
        if evidence.game_day_key != snapshot.game_day.period_key:
            reasons.append(EvidenceExclusionReason.GAME_DAY_MISMATCH)
        if evidence.captured_at < snapshot.game_day.starts_at:
            reasons.append(EvidenceExclusionReason.CAPTURED_BEFORE_RESET)
        if evidence.captured_at >= snapshot.game_day.ends_at:
            reasons.append(EvidenceExclusionReason.CAPTURED_AFTER_PERIOD)
        if not evidence.integrity_valid:
            reasons.append(EvidenceExclusionReason.ENTITY_INTEGRITY_FAILED)
        if evidence.content_type.lower() not in COMPLETION_EVIDENCE_CONTENT_TYPES:
            reasons.append(EvidenceExclusionReason.CONTENT_TYPE_NOT_ALLOWED)
        if reasons:
            reasons_by_artifact[evidence.artifact_id] = reasons
        else:
            accepted[evidence.artifact_id] = evidence

    # A byte-identical screenshot may be repeated for diagnostics, but it may
    # not impersonate different operation/kind semantics.  Reject the entire
    # ambiguous digest group so selection order cannot choose a convenient
    # survivor and manufacture a before/after or cross-Todo proof pair.
    images_by_hash: dict[str, list[EvidenceArtifactFact]] = {}
    for evidence in accepted.values():
        if evidence.is_screenshot and evidence.content_hash:
            images_by_hash.setdefault(evidence.content_hash, []).append(evidence)
    for evidence_group in images_by_hash.values():
        semantic_owners = {
            (item.todo_instance_id, item.kind) for item in evidence_group
        }
        if len(semantic_owners) <= 1:
            continue
        for evidence in evidence_group:
            accepted.pop(evidence.artifact_id, None)
            reasons_by_artifact.setdefault(evidence.artifact_id, []).append(
                EvidenceExclusionReason.DUPLICATE_SEMANTIC_REUSE
            )

    evidence_by_id = {item.artifact_id: item for item in snapshot.evidence}
    excluded = tuple(
        ExcludedEvidence(
            artifact_id=artifact_id,
            reasons=tuple(dict.fromkeys(reasons)),
            integrity_reason_code=(
                evidence_by_id[artifact_id].integrity_reason_code
                if EvidenceExclusionReason.ENTITY_INTEGRITY_FAILED in reasons
                else ""
            ),
        )
        for artifact_id, reasons in reasons_by_artifact.items()
    )
    return accepted, excluded


def _attempt_evaluations(
    snapshot: CompletionContractSnapshot,
) -> tuple[PredicateEvaluation, PredicateEvaluation, PredicateEvaluation]:
    lineage = _attempt_lineage(snapshot)
    scoped_ids = _scoped_attempt_ids(snapshot)
    lineage_valid = bool(lineage) and len(scoped_ids) == len(lineage)
    lineage_evaluation = _evaluation(
        CompletionPredicateCode.RUN_ATTEMPT_LINEAGE_SCOPE,
        (
            PredicateResultState.SATISFIED
            if lineage_valid
            else PredicateResultState.BLOCKED
        ),
        (
            "Every attempt in the completion lineage belongs to this game, cadence, and GameDay."
            if lineage_valid
            else "The completion lineage is empty or crosses the game, cadence, or GameDay scope."
        ),
    )
    attempt = snapshot.current_attempt
    if attempt is None:
        return (
            _evaluation(
                CompletionPredicateCode.CURRENT_ATTEMPT_SCOPE,
                PredicateResultState.MISSING,
                "No current RunAttempt was supplied for the GameRun.",
            ),
            _evaluation(
                CompletionPredicateCode.CURRENT_ATTEMPT_COMPLETED,
                PredicateResultState.MISSING,
                "Completion requires a terminal completed current RunAttempt.",
            ),
            lineage_evaluation,
        )

    scope_valid = (
        attempt.run_id == snapshot.run_id
        and attempt.game_id == snapshot.game_id
        and attempt.cadence == snapshot.cadence
        and _inside_game_day(snapshot, attempt.started_at)
        and (
            attempt.state != CompletionRunAttemptState.COMPLETED
            or (
                attempt.completed_at is not None
                and _inside_game_day(snapshot, attempt.completed_at)
            )
        )
    )
    scope = _evaluation(
        CompletionPredicateCode.CURRENT_ATTEMPT_SCOPE,
        (
            PredicateResultState.SATISFIED
            if scope_valid
            else PredicateResultState.BLOCKED
        ),
        (
            "The current RunAttempt belongs to this GameRun and GameDay."
            if scope_valid
            else "The supplied current RunAttempt is outside the GameRun or GameDay."
        ),
    )

    if attempt.state == CompletionRunAttemptState.COMPLETED:
        terminal_state = PredicateResultState.SATISFIED
        message = "The current RunAttempt reached its completed terminal state."
    elif attempt.state in {
        CompletionRunAttemptState.BLOCKED,
        CompletionRunAttemptState.HUMAN_REQUIRED,
        CompletionRunAttemptState.FAILED,
        CompletionRunAttemptState.CANCELLED,
    }:
        terminal_state = PredicateResultState.BLOCKED
        message = f"The current RunAttempt ended as {attempt.state}."
    else:
        terminal_state = PredicateResultState.MISSING
        message = f"The current RunAttempt is {attempt.state}, not completed."
    terminal = _evaluation(
        CompletionPredicateCode.CURRENT_ATTEMPT_COMPLETED,
        terminal_state,
        message,
    )
    return scope, terminal, lineage_evaluation


def _todo_evaluations(
    snapshot: CompletionContractSnapshot,
    accepted_evidence: Mapping[str, EvidenceArtifactFact],
) -> tuple[PredicateEvaluation, ...]:
    required = tuple(item for item in snapshot.todos if item.required)
    present = _evaluation(
        CompletionPredicateCode.REQUIRED_TODOS_PRESENT,
        (
            PredicateResultState.SATISFIED
            if required
            else PredicateResultState.MISSING
        ),
        (
            "The GameRun contains required Todo facts."
            if required
            else "A GameRun with no required Todo facts cannot be accepted."
        ),
    )

    incomplete = tuple(
        item for item in required if item.status != CompletionTodoState.COMPLETED
    )
    hard_stops = tuple(
        item
        for item in incomplete
        if item.status
        in {
            CompletionTodoState.BLOCKED,
            CompletionTodoState.HUMAN_REQUIRED,
            CompletionTodoState.FAILED,
            CompletionTodoState.CANCELLED,
        }
    )
    if hard_stops:
        completed_state = PredicateResultState.BLOCKED
    elif incomplete:
        completed_state = PredicateResultState.MISSING
    else:
        completed_state = PredicateResultState.SATISFIED
    completed = _evaluation(
        CompletionPredicateCode.REQUIRED_TODOS_COMPLETED,
        completed_state,
        (
            "Every required Todo is completed."
            if not incomplete
            else "One or more required Todos are unresolved."
        ),
        todo_instance_ids=(item.todo_instance_id for item in incomplete),
        artifact_refs=(
            artifact
            for item in incomplete
            for artifact in item.evidence_refs
        ),
    )

    scoped_attempt_ids = _scoped_attempt_ids(snapshot)
    scoped_attempt_runs = {
        attempt.run_attempt_id: attempt.run_id
        for attempt in _attempt_lineage(snapshot)
        if _attempt_is_in_scope(snapshot, attempt)
    }
    wrong_scope = tuple(
        item
        for item in required
        if item.game_id != snapshot.game_id
        or item.run_attempt_id not in scoped_attempt_ids
        or scoped_attempt_runs.get(item.run_attempt_id) != item.run_id
    )
    scope = _evaluation(
        CompletionPredicateCode.REQUIRED_TODOS_LINEAGE_SCOPE,
        (
            PredicateResultState.SATISFIED
            if required and not wrong_scope
            else PredicateResultState.MISSING
        ),
        (
            "Every required Todo completion belongs to the frozen same-GameDay attempt lineage."
            if required and not wrong_scope
            else "Required Todo completion facts are missing or belong to another game, cadence, GameDay, or attempt owner."
        ),
        todo_instance_ids=(item.todo_instance_id for item in wrong_scope),
    )

    wrong_day = tuple(
        item
        for item in required
        if item.game_day_key != snapshot.game_day.period_key
        or item.completed_at is None
        or not _inside_game_day(snapshot, item.completed_at)
    )
    game_day = _evaluation(
        CompletionPredicateCode.REQUIRED_TODOS_CURRENT_GAME_DAY,
        (
            PredicateResultState.SATISFIED
            if required and not wrong_day
            else PredicateResultState.MISSING
        ),
        (
            "Every required Todo completion is inside the current GameDay."
            if required and not wrong_day
            else "Required Todo completion facts are stale or have no completion time."
        ),
        todo_instance_ids=(item.todo_instance_id for item in wrong_day),
    )

    missing_evidence = tuple(
        item
        for item in required
        if not item.evidence_refs
        or any(
            reference not in accepted_evidence
            or accepted_evidence[reference].todo_instance_id
            != item.todo_instance_id
            for reference in item.evidence_refs
        )
    )
    evidence = _evaluation(
        CompletionPredicateCode.REQUIRED_TODOS_FRESH_EVIDENCE,
        (
            PredicateResultState.SATISFIED
            if required and not missing_evidence
            else PredicateResultState.MISSING
        ),
        (
            "Every required Todo references fresh evidence from the frozen same-GameDay attempt lineage."
            if required and not missing_evidence
            else "Each required Todo needs evidence owned by its frozen same-GameDay attempt."
        ),
        todo_instance_ids=(item.todo_instance_id for item in missing_evidence),
        artifact_refs=(
            reference
            for item in missing_evidence
            for reference in item.evidence_refs
        ),
    )
    return present, completed, scope, game_day, evidence


def _review_evaluations(
    snapshot: CompletionContractSnapshot,
    accepted_evidence: Mapping[str, EvidenceArtifactFact],
) -> tuple[PredicateEvaluation, PredicateEvaluation, bool]:
    review = snapshot.agent_review
    current_attempt_id = (
        snapshot.current_attempt.run_attempt_id
        if snapshot.current_attempt is not None
        else None
    )
    if review is None:
        return (
            _evaluation(
                CompletionPredicateCode.AGENT_REVIEW_CURRENT_SCOPE,
                PredicateResultState.MISSING,
                "No completion review was supplied for the current attempt.",
            ),
            _evaluation(
                CompletionPredicateCode.AGENT_REVIEW_ACCEPTED,
                PredicateResultState.MISSING,
                "An accepted completion review is required.",
            ),
            False,
        )

    reviewed_evidence = [
        accepted_evidence.get(reference) for reference in review.artifact_refs
    ]
    has_current_attempt_evidence = any(
        item is not None and item.run_attempt_id == current_attempt_id
        for item in reviewed_evidence
    )
    terminal_fact_times = [
        item.completed_at
        for item in snapshot.todos
        if item.required
        and item.status == CompletionTodoState.COMPLETED
        and item.completed_at is not None
    ]
    if (
        snapshot.current_attempt is not None
        and snapshot.current_attempt.completed_at is not None
    ):
        terminal_fact_times.append(snapshot.current_attempt.completed_at)
    scope_valid = (
        review.game_id == snapshot.game_id
        and review.run_id == snapshot.run_id
        and review.run_attempt_id == current_attempt_id
        and review.game_day_key == snapshot.game_day.period_key
        and _inside_game_day(snapshot, review.reviewed_at)
        and bool(review.artifact_refs)
        and all(item is not None for item in reviewed_evidence)
        and has_current_attempt_evidence
        and all(
            review.reviewed_at >= item.captured_at
            for item in reviewed_evidence
            if item is not None
        )
        and all(review.reviewed_at >= item for item in terminal_fact_times)
    )
    scope = _evaluation(
        CompletionPredicateCode.AGENT_REVIEW_CURRENT_SCOPE,
        (
            PredicateResultState.SATISFIED
            if scope_valid
            else PredicateResultState.MISSING
        ),
        (
            "The completion review covers current-attempt, post-reset artifacts."
            if scope_valid
            else "The completion review is stale, out of scope or references stale artifacts."
        ),
        artifact_refs=review.artifact_refs,
    )

    if scope_valid and review.decision == AgentReviewDecision.ACCEPTED:
        accepted_state = PredicateResultState.SATISFIED
        message = "The current scoped completion review was accepted."
    elif scope_valid and review.decision == AgentReviewDecision.REJECTED:
        accepted_state = PredicateResultState.BLOCKED
        message = "The current scoped completion review rejected completion."
    else:
        accepted_state = PredicateResultState.MISSING
        message = "The current scoped completion review has not accepted completion."
    accepted = _evaluation(
        CompletionPredicateCode.AGENT_REVIEW_ACCEPTED,
        accepted_state,
        message,
        artifact_refs=review.artifact_refs,
    )
    return scope, accepted, scope_valid


def _todo_review_evaluation(
    snapshot: CompletionContractSnapshot,
    accepted_evidence: Mapping[str, EvidenceArtifactFact],
) -> PredicateEvaluation:
    """Require one artifact-owned semantic verdict per required Todo."""

    required_ids = {
        item.todo_instance_id for item in snapshot.todos if item.required
    }
    review = snapshot.agent_review
    if review is None:
        return _evaluation(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            PredicateResultState.MISSING,
            "No structured required-Todo review was supplied.",
            todo_instance_ids=sorted(required_ids),
        )

    reviews_by_todo = {
        item.todo_instance_id: item for item in review.todo_reviews
    }
    submitted_ids = set(reviews_by_todo)
    missing_ids = required_ids - submitted_ids
    foreign_ids = submitted_ids - required_ids
    if missing_ids or foreign_ids:
        return _evaluation(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            PredicateResultState.MISSING,
            "The completion review does not cover exactly the frozen required Todos.",
            todo_instance_ids=sorted(missing_ids | foreign_ids),
            artifact_refs=(
                reference
                for todo_id in foreign_ids
                for reference in reviews_by_todo[todo_id].artifact_refs
            ),
        )

    rejected = tuple(
        item
        for item in review.todo_reviews
        if item.verdict == TodoReviewVerdict.REJECTED
    )
    if rejected:
        return _evaluation(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            PredicateResultState.BLOCKED,
            "One or more required Todo reviews explicitly reject completion.",
            todo_instance_ids=(item.todo_instance_id for item in rejected),
            artifact_refs=(
                reference for item in rejected for reference in item.artifact_refs
            ),
        )

    unresolved = tuple(
        item
        for item in review.todo_reviews
        if item.verdict != TodoReviewVerdict.CONFIRMED
    )
    if unresolved:
        return _evaluation(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            PredicateResultState.MISSING,
            "One or more required Todo reviews still need semantic confirmation.",
            todo_instance_ids=(item.todo_instance_id for item in unresolved),
            artifact_refs=(
                reference
                for item in unresolved
                for reference in item.artifact_refs
            ),
        )

    invalid_reviews = []
    for item in review.todo_reviews:
        evidence = [accepted_evidence.get(ref) for ref in item.artifact_refs]
        if (
            not set(item.artifact_refs).issubset(review.artifact_refs)
            or any(value is None for value in evidence)
            or any(
                value is not None
                and (
                    value.todo_instance_id != item.todo_instance_id
                    or not value.raw
                    or not value.is_screenshot
                    or value.content_type.lower()
                    not in COMPLETION_REVIEW_EVIDENCE_CONTENT_TYPES
                )
                for value in evidence
            )
        ):
            invalid_reviews.append(item)
    if invalid_reviews:
        return _evaluation(
            CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
            PredicateResultState.BLOCKED,
            "A required Todo verdict references missing, foreign, or non-visual evidence.",
            todo_instance_ids=(item.todo_instance_id for item in invalid_reviews),
            artifact_refs=(
                reference
                for item in invalid_reviews
                for reference in item.artifact_refs
            ),
        )

    return _evaluation(
        CompletionPredicateCode.AGENT_REVIEW_REQUIRED_TODOS_CONFIRMED,
        PredicateResultState.SATISFIED,
        "Every frozen required Todo has a confirmed semantic review with owned visual evidence.",
        todo_instance_ids=sorted(required_ids),
        artifact_refs=(
            reference
            for item in review.todo_reviews
            for reference in item.artifact_refs
        ),
    )


def _rule_evaluation(
    rule: ReviewedArtifactPredicateRule,
    review_observations: Sequence[AgentPredicateObservation],
    review_artifact_refs: set[str],
    accepted_evidence: Mapping[str, EvidenceArtifactFact],
) -> PredicateEvaluation:
    candidates = tuple(
        item
        for item in review_observations
        if item.predicate_id == rule.observation_id
    )
    candidate_refs = _unique(
        reference for item in candidates for reference in item.artifact_refs
    )
    for observation in candidates:
        evidence = [accepted_evidence.get(item) for item in observation.artifact_refs]
        valid_artifacts = (
            set(observation.artifact_refs).issubset(review_artifact_refs)
            and all(item is not None for item in evidence)
            and all(
                item is not None
                and item.raw
                and item.is_screenshot
                and item.kind in rule.allowed_artifact_kinds
                for item in evidence
            )
        )
        if valid_artifacts and all(
            constraint.matches(observation.metrics)
            for constraint in rule.constraints
        ):
            return _evaluation(
                rule.code,
                PredicateResultState.SATISFIED,
                rule.satisfied_message,
                artifact_refs=observation.artifact_refs,
            )
    return _evaluation(
        rule.code,
        PredicateResultState.MISSING,
        rule.missing_message,
        artifact_refs=candidate_refs,
    )


def _promoted_adapter_proof_evaluation(
    snapshot: CompletionContractSnapshot,
    accepted_evidence: Mapping[str, EvidenceArtifactFact],
) -> PredicateEvaluation:
    """Require a typed allowlisted proof for Manager-authored reviews.

    A promoted package is execution authority, not general visual authority.
    Historical automatic reviews with an empty predicate list (or only a
    generic before/after pair) therefore fail closed even if their persisted
    decision says ``accepted``.
    """

    review = snapshot.agent_review
    if review is None or review.reviewer_principal_id != "manager:promoted-adapter":
        return _evaluation(
            CompletionPredicateCode.PROMOTED_ADAPTER_OPERATION_PROOF,
            PredicateResultState.SATISFIED,
            "Completion is not relying on a Manager-authored Adapter review.",
        )

    for observation in review.observations:
        if observation.predicate_id != (
            "ww-daily-activity-100-and-reward-claim-screenshots"
        ):
            continue
        metrics = observation.metrics
        if not (
            metrics.get("dailyActivityPoints") == 100
            and metrics.get("beforeClaimScreenshotPresent") is True
            and metrics.get("rawScreenshotPresent") is True
            and metrics.get("watermarkedScreenshotPresent") is True
        ):
            continue
        evidence = [accepted_evidence.get(ref) for ref in observation.artifact_refs]
        if any(item is None for item in evidence):
            continue
        kinds = {item.kind for item in evidence if item is not None}
        if {
            "game-ui-daily-reward-before",
            "game-ui-daily-reward-raw",
            "game-ui-daily-reward-watermarked",
            "game-ui-daily-activity-100",
        }.issubset(kinds):
            return _evaluation(
                CompletionPredicateCode.PROMOTED_ADAPTER_OPERATION_PROOF,
                PredicateResultState.SATISFIED,
                "The promoted Adapter supplied the registered WW operation proof.",
                artifact_refs=observation.artifact_refs,
            )
    return _evaluation(
        CompletionPredicateCode.PROMOTED_ADAPTER_OPERATION_PROOF,
        PredicateResultState.BLOCKED,
        "A Manager-authored Adapter review lacks a registered operation-specific semantic proof.",
        artifact_refs=(review.artifact_refs if review is not None else ()),
    )


def adjudicate_completion(
    snapshot: CompletionContractSnapshot,
    *,
    policies: CompletionPolicyRegistry = DEFAULT_COMPLETION_POLICIES,
) -> CompletionContractDecision:
    """Adjudicate one immutable snapshot without I/O or mutation."""

    policy = policies.resolve(snapshot.game_id)
    policy_valid = policy is not None and policy.cadence == snapshot.cadence
    evaluations: list[PredicateEvaluation] = [
        _evaluation(
            CompletionPredicateCode.POLICY_AVAILABLE,
            (
                PredicateResultState.SATISFIED
                if policy_valid
                else PredicateResultState.BLOCKED
            ),
            (
                "A matching completion policy is registered."
                if policy_valid
                else "No matching completion policy is registered for this game and cadence."
            ),
        )
    ]

    window_valid = policy_valid and policy is not None and _valid_policy_window(
        snapshot, policy
    )
    evaluations.append(
        _evaluation(
            CompletionPredicateCode.GAME_DAY_WINDOW,
            (
                PredicateResultState.SATISFIED
                if window_valid
                else PredicateResultState.BLOCKED
            ),
            (
                "The GameDay window matches the registered reset boundary."
                if window_valid
                else "The GameDay window does not match the registered reset boundary."
            ),
        )
    )
    evaluations.extend(_attempt_evaluations(snapshot))

    accepted_evidence, excluded_evidence = _classify_evidence(snapshot)
    integrity_failures = tuple(
        item
        for item in excluded_evidence
        if EvidenceExclusionReason.ENTITY_INTEGRITY_FAILED in item.reasons
    )
    duplicate_semantics = tuple(
        item
        for item in excluded_evidence
        if EvidenceExclusionReason.DUPLICATE_SEMANTIC_REUSE in item.reasons
    )
    evaluations.extend(
        (
            _evaluation(
                CompletionPredicateCode.EVIDENCE_ENTITY_INTEGRITY,
                (
                    PredicateResultState.BLOCKED
                    if integrity_failures
                    else PredicateResultState.SATISFIED
                ),
                (
                    "Every referenced evidence entity was re-opened and matched its sealed path, size, and SHA-256."
                    if not integrity_failures
                    else "Referenced evidence failed entity revalidation: "
                    + ", ".join(
                        f"{item.artifact_id}={item.integrity_reason_code or 'integrity_failed'}"
                        for item in integrity_failures
                    )
                ),
                artifact_refs=(item.artifact_id for item in integrity_failures),
            ),
            _evaluation(
                CompletionPredicateCode.EVIDENCE_SEMANTIC_UNIQUENESS,
                (
                    PredicateResultState.BLOCKED
                    if duplicate_semantics
                    else PredicateResultState.SATISFIED
                ),
                (
                    "No screenshot digest is reused across different Todo or evidence-kind semantics."
                    if not duplicate_semantics
                    else "Byte-identical screenshots were assigned to conflicting Todo or evidence-kind semantics."
                ),
                artifact_refs=(item.artifact_id for item in duplicate_semantics),
            ),
        )
    )
    evaluations.append(
        _evaluation(
            CompletionPredicateCode.FRESH_EVIDENCE,
            (
                PredicateResultState.SATISFIED
                if accepted_evidence
                else PredicateResultState.MISSING
            ),
            (
                "Fresh evidence exists after the current GameDay reset."
                if accepted_evidence
                else "No evidence belongs to the current run attempt after reset."
            ),
            artifact_refs=accepted_evidence,
        )
    )
    evaluations.extend(_todo_evaluations(snapshot, accepted_evidence))

    current_attempt_id = (
        snapshot.current_attempt.run_attempt_id
        if snapshot.current_attempt is not None
        else None
    )
    scoped_attempt_ids = _scoped_attempt_ids(snapshot)
    scoped_blockers = tuple(
        item
        for item in snapshot.blockers
        if item.active
        and item.game_id == snapshot.game_id
        and item.run_id == snapshot.run_id
        and item.run_attempt_id in scoped_attempt_ids
        and item.game_day_key == snapshot.game_day.period_key
    )
    evaluations.append(
        _evaluation(
            CompletionPredicateCode.ACTIVE_BLOCKERS_CLEAR,
            (
                PredicateResultState.BLOCKED
                if scoped_blockers
                else PredicateResultState.SATISFIED
            ),
            (
                "No active blocker applies to the current run attempt."
                if not scoped_blockers
                else "An active blocker applies to the current run attempt."
            ),
            artifact_refs=(
                reference
                for item in scoped_blockers
                for reference in item.artifact_refs
            ),
            todo_instance_ids=(
                item.todo_instance_id or "" for item in scoped_blockers
            ),
        )
    )

    review_scope, review_accepted, review_scope_valid = _review_evaluations(
        snapshot, accepted_evidence
    )
    evaluations.extend(
        (
            review_scope,
            review_accepted,
            _todo_review_evaluation(snapshot, accepted_evidence),
            _promoted_adapter_proof_evaluation(snapshot, accepted_evidence),
        )
    )

    if policy is not None:
        review = snapshot.agent_review
        observations = (
            review.observations
            if review is not None
            and review_scope_valid
            and review.decision == AgentReviewDecision.ACCEPTED
            else ()
        )
        review_artifact_refs = (
            set(review.artifact_refs)
            if review is not None and review_scope_valid
            else set()
        )
        evaluations.extend(
            _rule_evaluation(
                rule,
                observations,
                review_artifact_refs,
                accepted_evidence,
            )
            for rule in policy.predicate_rules
        )

    missing = tuple(
        item.code
        for item in evaluations
        if item.state == PredicateResultState.MISSING
    )
    blocking = tuple(
        item.code
        for item in evaluations
        if item.state == PredicateResultState.BLOCKED
    )
    if blocking:
        outcome = CompletionOutcome.BLOCKED
        message = "Completion is blocked by a current scoped contract predicate."
    elif missing:
        outcome = CompletionOutcome.REVIEW_REQUIRED
        message = "Completion requires additional current scoped evidence or review."
    else:
        outcome = CompletionOutcome.ACCEPTED_DONE
        message = "All generic and game-specific completion predicates are satisfied."

    all_artifact_refs = _unique(
        [item.artifact_id for item in snapshot.evidence]
        + [
            reference
            for item in snapshot.todos
            for reference in item.evidence_refs
        ]
        + [
            reference
            for item in snapshot.blockers
            for reference in item.artifact_refs
        ]
        + (
            list(snapshot.agent_review.artifact_refs)
            if snapshot.agent_review is not None
            else []
        )
        + (
            [
                reference
                for observation in snapshot.agent_review.observations
                for reference in observation.artifact_refs
            ]
            if snapshot.agent_review is not None
            else []
        )
    )
    screenshots = _unique(
        item.artifact_id for item in snapshot.evidence if item.is_screenshot
    )
    supporting = _unique(
        reference
        for item in evaluations
        if item.state == PredicateResultState.SATISFIED
        for reference in item.artifact_refs
    )
    policy_id, policy_version = (
        completion_policy_identity(policy) if policy is not None else ("", "")
    )
    return CompletionContractDecision(
        outcome=outcome,
        accepted_done=outcome == CompletionOutcome.ACCEPTED_DONE,
        policy_id=policy_id,
        policy_version=policy_version,
        game_id=snapshot.game_id,
        run_id=snapshot.run_id,
        run_attempt_id=current_attempt_id,
        attempt_lineage_ids=tuple(
            item.run_attempt_id for item in _attempt_lineage(snapshot)
        ),
        game_day_key=snapshot.game_day.period_key,
        evaluations=evaluations,
        missing_predicates=missing,
        blocking_predicates=blocking,
        blocker_ids=(item.blocker_id for item in scoped_blockers),
        artifact_refs=all_artifact_refs,
        screenshot_artifact_refs=screenshots,
        accepted_evidence_refs=tuple(accepted_evidence),
        current_attempt_evidence_refs=tuple(
            artifact_id
            for artifact_id, item in accepted_evidence.items()
            if item.run_attempt_id == current_attempt_id
        ),
        carried_evidence_refs=tuple(
            artifact_id
            for artifact_id, item in accepted_evidence.items()
            if item.run_attempt_id != current_attempt_id
        ),
        supporting_artifact_refs=supporting,
        excluded_evidence=excluded_evidence,
        review_id=(
            snapshot.agent_review.review_id
            if snapshot.agent_review is not None
            else None
        ),
        message=message,
    )
