"""Pure Todo-plan classification for Manager batch and run execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class BatchPlanningDecision:
    """One immutable classification of the current Manager Todo plans."""

    candidate_game_ids: tuple[str, ...]
    unresolved_game_ids: tuple[str, ...]
    executable_game_ids: tuple[str, ...]
    deferred_game_ids: tuple[str, ...]
    skipped_completed_game_ids: tuple[str, ...]

    @property
    def has_unresolved_todos(self) -> bool:
        return bool(self.unresolved_game_ids)

    @property
    def has_executable_binding(self) -> bool:
        return bool(self.executable_game_ids)

    def execution_unavailable_details(
        self, todo_plans: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Return a small public reason document for an execute rejection."""

        return {
            "candidateGameIds": list(dict.fromkeys(str(todo_plans[key].get("gameId", key)) for key in self.candidate_game_ids)),
            "deferredGameIds": list(dict.fromkeys(str(todo_plans[key].get("gameId", key)) for key in self.deferred_game_ids)),
            "games": [
                _unavailable_game_details(game_id, todo_plans[game_id])
                for game_id in self.deferred_game_ids
            ],
        }


def classify_batch_todo_plans(
    candidate_game_ids: Sequence[str],
    todo_plans: Mapping[str, Mapping[str, Any]],
) -> BatchPlanningDecision:
    """Classify games without mutating the supplied Manager projections."""

    candidate = tuple(dict.fromkeys(str(game_id) for game_id in candidate_game_ids))
    unresolved = tuple(
        game_id
        for game_id in candidate
        if int(todo_plans[game_id]["requiredRemaining"]) > 0
    )
    executable = tuple(
        game_id
        for game_id in unresolved
        if bool(todo_plans[game_id].get("executableTodoInstanceIds"))
    )
    executable_set = set(executable)
    unresolved_set = set(unresolved)
    return BatchPlanningDecision(
        candidate_game_ids=candidate,
        unresolved_game_ids=unresolved,
        executable_game_ids=executable,
        deferred_game_ids=tuple(
            game_id for game_id in unresolved if game_id not in executable_set
        ),
        skipped_completed_game_ids=tuple(
            game_id for game_id in candidate if game_id not in unresolved_set
        ),
    )


def _unavailable_game_details(
    game_id: str, plan: Mapping[str, Any]
) -> dict[str, Any]:
    deferred_ids = _strings(plan.get("deferredTodoInstanceIds"))
    deferred_reasons = plan.get("deferredReasons")
    reason_codes: list[str] = []
    reasons: list[str] = []
    if isinstance(deferred_reasons, Mapping):
        for todo_instance_id in deferred_ids:
            item = deferred_reasons.get(todo_instance_id)
            if not isinstance(item, Mapping):
                continue
            _append_unique(reason_codes, item.get("code"))
            _append_unique(reasons, item.get("reason"))

    runtime = plan.get("runtimeBinding")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    runtime_status = str(runtime.get("status") or "unavailable")
    bindings = runtime.get("bindings")
    binding_count = (
        len(bindings)
        if isinstance(bindings, Sequence)
        and not isinstance(bindings, (str, bytes, bytearray))
        else 0
    )
    reason_code = (
        reason_codes[0]
        if reason_codes
        else runtime_status
        if runtime_status != "promoted"
        else "no_verified_executable_binding"
    )
    reason = (
        reasons[0]
        if reasons
        else "No selected Todo has a verified executable binding."
    )
    return {
        "gameId": plan.get("gameId", game_id),
        **({"accountId": plan["accountId"]} if "accountId" in plan else {}),
        "reasonCode": reason_code,
        "reason": reason,
        "reasonCodes": reason_codes or [reason_code],
        "deferredTodoInstanceIds": deferred_ids,
        "runtimeBinding": {
            "status": runtime_status,
            "manifestVerified": runtime.get("manifestVerified") is True,
            "bindingCount": binding_count,
        },
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)
