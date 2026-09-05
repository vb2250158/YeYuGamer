"""Manager-composed Todo reset, query, projection, and planning service.

This service shares the Manager-owned store and Adapter Host. It keeps no
business-state copy: every query and mutation still goes through the same
SqliteStore instance that ManagerService owns.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.models import (
    AutomationAssessmentRecord,
    GameIntegrationOperationRecord,
    GameIntegrationPage,
    GameIntegrationParameterRecord,
    GameIntegrationRecord,
    TodoAttemptRecord,
    TodoDefinitionRecord,
    TodoInstanceRecord,
    utc_now,
)
from ..domain.todos import todo_instance_id, todo_period
from ..store.sqlite_store import SqliteStore
from .adapter_host import ManagerAdapterHost
from .current_completion import project_current_game_completion
from .integration_catalog import (
    candidate_for,
    integration_game_ids,
    registration_for,
)
from .manager_errors import ManagerConflict, ManagerValidation
from .todo_catalog import catalog as todo_catalog
from .todo_dispatch import TodoDispatchFacts, evaluate_todo_dispatch


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


class ManagerTodosService:
    """Todo subservice composed by the sole Manager control plane."""

    def __init__(
        self,
        *,
        store: SqliteStore,
        adapter_host: ManagerAdapterHost,
        projection_history: Callable[
            [], tuple[list[dict[str, Any]], list[dict[str, Any]]]
        ],
        automation_assessment_record: Callable[
            [dict[str, Any]], AutomationAssessmentRecord
        ],
    ) -> None:
        self.store = store
        self.adapter_host = adapter_host
        self._projection_history = projection_history
        self._automation_assessment_record = automation_assessment_record

    def _next_todo_reset_boundary(self, at: datetime) -> datetime | None:
        candidates = self._todo_instance_candidates(at=at)
        boundaries = [
            datetime.fromisoformat(str(item["period_ends_at"]))
            for item in candidates
        ]
        if not boundaries:
            return None
        return min(boundaries).astimezone(timezone.utc)

    def _reconcile_todo_reset_boundary(self, at: datetime) -> None:
        candidates = self._todo_instance_candidates(at=at)
        result = self.store.reconcile_todo_instances(
            candidates,
            # A clock boundary creates the newly-current period; it must not
            # explicitly reset still-current weekly Todo instances every day.
            # The destructive current-period reset intent is reserved for the
            # typed operator reset endpoint.
            intent="reconcile",
            requested_by="manager-scheduler",
            reason="configured-period-boundary",
        )
        self.store.set_metadata(
            "todo.last_scheduled_reset",
            {
                "at": at.astimezone(timezone.utc).isoformat(),
                "createdTodoInstanceIds": result["created_todo_instance_ids"],
            },
        )

    def _record_todo_reset_scheduler_error(self, error: Exception) -> None:
        self.store.set_metadata(
            "todo.reset_scheduler_health",
            {
                "state": "failed-retrying",
                "errorClass": type(error).__name__,
                "at": utc_now().isoformat(),
            },
        )
        self.store.append_event(
            "todo.reset-scheduler-failed",
            "todo-period",
            str(uuid.uuid4()),
            {"errorClass": type(error).__name__, "retrying": True},
        )

    def _validated_todo_game_ids(self, game_ids: list[str] | None) -> list[str]:
        known = [str(game["game_id"]) for game in self.store.list_games()]
        if game_ids is None:
            return known
        unknown = [game_id for game_id in game_ids if game_id not in known]
        if unknown:
            raise ManagerValidation(f"unknown GameId: {', '.join(unknown)}")
        if len(game_ids) != len(set(game_ids)):
            raise ManagerValidation("gameIds contains duplicates")
        return list(game_ids)

    def _todo_reset_policy_document(self) -> dict[str, Any]:
        configured = self.store.get_config()["values"].get("todo_reset_policy")
        if isinstance(configured, dict):
            return {
                "timezone": str(configured.get("timezone") or "Asia/Shanghai"),
                "time": str(configured.get("time") or "04:00"),
                "week_start_day": str(
                    configured.get("week_start_day") or "Monday"
                ),
                "per_game": dict(configured.get("per_game") or {}),
                "per_definition": dict(configured.get("per_definition") or {}),
            }
        return {
            "timezone": "Asia/Shanghai",
            "time": "04:00",
            "week_start_day": "Monday",
            "per_game": {},
            "per_definition": {},
        }

    def _configured_todo_reset_rule(
        self, definition: dict[str, Any]
    ) -> dict[str, Any]:
        policy = self._todo_reset_policy_document()
        cadence = str(definition["cadence"])
        rule: dict[str, Any] = {
            "timezone": policy["timezone"],
            "time": policy["time"],
            "cadence": cadence,
        }
        if cadence == "weekly":
            rule["weekStartDay"] = policy["week_start_day"]

        # Definition-scoped exceptions (FGO currently uses 00:00) remain in
        # force unless a narrower runtime override explicitly replaces them.
        definition_rule = dict(definition.get("reset_rule") or {})
        if definition_rule.get("timezone") not in {None, "Asia/Shanghai"}:
            rule["timezone"] = definition_rule["timezone"]
        if definition_rule.get("time") not in {None, "04:00"}:
            rule["time"] = definition_rule["time"]
        if cadence == "weekly" and definition_rule.get("weekStartDay") not in {
            None,
            "Monday",
        }:
            rule["weekStartDay"] = definition_rule["weekStartDay"]

        for override in (
            policy["per_game"].get(definition["game_id"], {}),
            policy["per_definition"].get(definition["todo_definition_id"], {}),
        ):
            if not isinstance(override, dict):
                continue
            if override.get("timezone") is not None:
                rule["timezone"] = override["timezone"]
            if override.get("time") is not None:
                rule["time"] = override["time"]
            if cadence == "weekly" and override.get("week_start_day") is not None:
                rule["weekStartDay"] = override["week_start_day"]
        return rule

    def _todo_instance_candidates(
        self,
        game_ids: list[str] | None = None,
        cadence: str | None = None,
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        selected = set(self._validated_todo_game_ids(game_ids))
        instant = at or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        existing_by_definition: dict[str, dict[str, Any]] = {}
        for existing in self.store.list_todo_instances(
            cadence=cadence, limit=10000
        ):
            if existing["game_id"] not in selected:
                continue
            existing_by_definition.setdefault(
                existing["todo_definition_id"], existing
            )
        definitions = self.store.list_todo_definitions(cadence=cadence)
        candidates: list[dict[str, Any]] = []
        for definition in definitions:
            if definition["game_id"] not in selected:
                continue
            configured_rule = self._configured_todo_reset_rule(definition)
            active_instance = existing_by_definition.get(
                definition["todo_definition_id"]
            )
            effective_rule = configured_rule
            policy_change_deferred = False
            if active_instance is not None:
                active_start = datetime.fromisoformat(
                    str(active_instance["period_starts_at"])
                )
                active_end = datetime.fromisoformat(
                    str(active_instance["period_ends_at"])
                )
                if active_start <= instant < active_end:
                    effective_rule = dict(active_instance["reset_rule"])
                    policy_change_deferred = effective_rule != configured_rule
            period = todo_period(effective_rule, instant)
            definition_snapshot = {
                key: value
                for key, value in definition.items()
                if key
                not in {
                    "active",
                    "updated_at",
                    "initial_status",
                    "initial_reason",
                }
            }
            definition_snapshot["reset_rule"] = dict(effective_rule)
            candidates.append(
                {
                    "todo_instance_id": todo_instance_id(
                        definition["todo_definition_id"], period["periodKey"]
                    ),
                    "todo_definition_id": definition["todo_definition_id"],
                    "game_id": definition["game_id"],
                    "cadence": definition["cadence"],
                    "period_key": period["periodKey"],
                    "period_starts_at": period["startsAt"],
                    "period_ends_at": period["endsAt"],
                    "definition_snapshot": definition_snapshot,
                    "status": definition["initial_status"],
                    "reason": definition["initial_reason"],
                    "effective_reset_rule": dict(effective_rule),
                    "next_period_reset_rule": dict(configured_rule),
                    "policy_change_deferred": policy_change_deferred,
                }
            )
        return candidates

    def list_todo_definitions(
        self, game_id: str | None = None, cadence: str | None = None
    ) -> list[TodoDefinitionRecord]:
        if game_id is not None:
            self._validated_todo_game_ids([game_id])
        return [
            TodoDefinitionRecord.model_validate(item)
            for item in self.store.list_todo_definitions(
                game_id=game_id, cadence=cadence
            )
        ]

    def list_game_integrations(self) -> GameIntegrationPage:
        """Project registered tool stages against Manager-owned Todo definitions.

        Registration is intentionally distinct from the installed Adapter's
        promotion.  It exposes a missing mapping to the control panel without
        claiming that the tool can start or that a Todo is complete.
        """

        configured_game_ids = [
            str(item["game_id"]) for item in self.store.list_games()
        ]
        catalog_definitions = todo_catalog()
        persisted_definitions = self.list_todo_definitions()
        definitions = {
            (item.game_id, item.operation): {
                "game_id": item.game_id,
                "operation": item.operation,
                "todo_definition_id": item.todo_definition_id,
                "title": item.title,
                "source_refs": item.source_refs,
                "automation_state": item.automation_state,
                "adapter_capability_ref": item.adapter_capability_ref,
            }
            for item in persisted_definitions
            if item.cadence == "daily"
        }
        for item in catalog_definitions:
            if item["cadence"] != "daily":
                continue
            definitions.setdefault(
                (str(item["game_id"]), str(item["operation"])), item
            )
        known_game_ids = list(
            dict.fromkeys(
                [
                    *configured_game_ids,
                    *(item.game_id for item in persisted_definitions),
                    *(str(item["game_id"]) for item in catalog_definitions),
                    *integration_game_ids(),
                ]
            )
        )
        records: list[GameIntegrationRecord] = []
        for game_id in known_game_ids:
            registration = registration_for(game_id)
            if registration is None:
                candidate = candidate_for(game_id)
                if candidate is not None:
                    records.append(
                        GameIntegrationRecord(
                            game_id=game_id,
                            tool_name=candidate.tool_name,
                            source=candidate.source,
                            mapping_status="incomplete",
                            mapping_note=candidate.mapping_note,
                            parameters=[
                                GameIntegrationParameterRecord(
                                    key=item.key,
                                    label=item.label,
                                    value_type=item.value_type,
                                    dispatch_status=item.dispatch_status,
                                    options=list(item.options),
                                    minimum=item.minimum,
                                    maximum=item.maximum,
                                    note=item.note,
                                )
                                for item in candidate.parameters
                            ],
                        )
                    )
                    continue
                records.append(
                    GameIntegrationRecord(
                        game_id=game_id,
                        mapping_status="not_registered",
                        mapping_note=(
                            "该游戏尚未登记真实工具阶段；Todo 仅保留为 Manager 目录，"
                            "不会因页面勾选变成工具调用。"
                        ),
                    )
                )
                continue
            missing = [
                operation
                for operation in registration.daily_operations
                if (game_id, operation) not in definitions
            ]
            registered_operations = set(registration.daily_operations)
            omitted_tool_operations = [
                item["operation"]
                for item in definitions.values()
                if item["game_id"] == game_id
                and item["automation_state"] == "source-tool-declared-unbound"
                and item["adapter_capability_ref"] is not None
                and item["operation"] not in registered_operations
            ]
            operations = [
                definitions[(game_id, operation)]
                for operation in registration.daily_operations
                if (game_id, operation) in definitions
            ]
            records.append(
                GameIntegrationRecord(
                    game_id=game_id,
                    integration_id=registration.integration_id,
                    tool_name=registration.tool_name,
                    source=registration.source,
                    entry_operation=registration.entry_operation,
                    mapping_status=(
                        "incomplete"
                        if missing or omitted_tool_operations
                        else "registered"
                    ),
                    mapping_note=(
                        "缺少 Todo 映射：" + "、".join(missing)
                        if missing
                        else "工具已声明但 Integration 遗漏："
                        + "、".join(omitted_tool_operations)
                        if omitted_tool_operations
                        else "真实阶段已登记；实际执行仍须通过已晋级的 Adapter binding 与同轮证据复核。"
                    ),
                    operations=[
                        GameIntegrationOperationRecord(
                            operation=item["operation"],
                            todo_definition_id=item["todo_definition_id"],
                            title=item["title"],
                            source_refs=item["source_refs"],
                        )
                        for item in operations
                    ],
                    parameters=[
                        GameIntegrationParameterRecord(
                            key=item.key,
                            label=item.label,
                            value_type=item.value_type,
                            dispatch_status=item.dispatch_status,
                            options=list(item.options),
                            minimum=item.minimum,
                            maximum=item.maximum,
                            note=item.note,
                        )
                        for item in registration.parameters
                    ],
                )
            )
        return GameIntegrationPage(items=records, total=len(records))

    def get_todo_definition(
        self, todo_definition_id: str
    ) -> TodoDefinitionRecord:
        return TodoDefinitionRecord.model_validate(
            self.store.get_todo_definition(todo_definition_id)
        )

    def list_todo_instances(
        self,
        *,
        game_id: str | None = None,
        cadence: str | None = None,
        period_key: str | None = None,
        status: str | None = None,
        current: bool = False,
        limit: int = 1000,
    ) -> list[TodoInstanceRecord]:
        if game_id is not None:
            self._validated_todo_game_ids([game_id])
        records = self.store.list_todo_instances(
            game_id=game_id,
            cadence=cadence,
            period_key=period_key,
            status=status,
            limit=limit,
        )
        if current:
            candidate_ids = {
                item["todo_instance_id"]
                for item in self._todo_instance_candidates(
                    [game_id] if game_id is not None else None, cadence
                )
            }
            records = [
                record
                for record in records
                if record["todo_instance_id"] in candidate_ids
            ]
        runtime_cache: dict[str, dict[str, Any]] = {}
        return [
            self._todo_instance_record(item, runtime_cache=runtime_cache)
            for item in records
        ]

    def get_todo_instance(self, todo_instance_id_value: str) -> TodoInstanceRecord:
        return self._todo_instance_record(
            self.store.get_todo_instance(todo_instance_id_value),
            runtime_cache={},
        )

    def _todo_instance_record(
        self,
        record: dict[str, Any],
        *,
        runtime_cache: dict[str, dict[str, Any]],
    ) -> TodoInstanceRecord:
        game_id = str(record["game_id"])
        runtime = runtime_cache.get(game_id)
        if runtime is None:
            runtime = self.adapter_host.execution_bindings(game_id)
            runtime_cache[game_id] = runtime
        binding = next(
            (
                item
                for item in runtime.get("bindings", [])
                if isinstance(item, dict)
                and item.get("operation") == record["operation"]
            ),
            None,
        )
        attempts = self.store.list_todo_attempts(
            todo_instance_id=str(record["todo_instance_id"]), limit=1
        )
        blockers = self.store.list_todo_blockers(
            todo_instance_id=str(record["todo_instance_id"]),
            active_only=True,
            limit=1,
        )
        projection = evaluate_todo_dispatch(
            TodoDispatchFacts(
                status=str(record["status"]),
                current_reason=str(record.get("reason") or ""),
                risk=str(record["risk"]),
                todo_definition_id=str(record["todo_definition_id"]),
                operation=str(record["operation"]),
                adapter_capability_ref=(
                    str(record["adapter_capability_ref"])
                    if record.get("adapter_capability_ref") is not None
                    else None
                ),
                runtime=runtime,
                binding=binding,
                latest_attempt=attempts[0] if attempts else None,
                active_blocker=blockers[0] if blockers else None,
            )
        )
        return TodoInstanceRecord.model_validate({**record, **projection})

    def _latest_automation_assessments(
        self, todo_instance_ids: set[str]
    ) -> dict[str, AutomationAssessmentRecord]:
        """Load the newest immutable Agent assessment for each requested Todo."""

        if not todo_instance_ids:
            return {}
        result: dict[str, AutomationAssessmentRecord] = {}
        for resource in self.store.list_resources("automation-assessment", 10000):
            record = self._automation_assessment_record(resource)
            if (
                record.todo_instance_id in todo_instance_ids
                and record.todo_instance_id not in result
            ):
                result[record.todo_instance_id] = record
            if len(result) == len(todo_instance_ids):
                break
        return result

    @staticmethod
    def _todo_blocker_projection(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "blockerId": item["blocker_id"],
            "runId": item["run_id"],
            "runAttemptId": item["run_attempt_id"],
            "todoInstanceId": item["todo_instance_id"],
            "todoAttemptId": item["todo_attempt_id"],
            "gameDayKey": item["game_day_key"],
            "kind": item["kind"],
            "code": item["code"],
            "state": item["state"],
            "revision": item["revision"],
            "retryable": item["retryable"],
            "reason": item["reason"],
            "artifactRefs": list(item["artifact_refs"]),
            "raisedAt": item["raised_at"],
            "transitionedAt": item["transitioned_at"],
        }

    def _todo_operational_context(
        self,
        item: TodoInstanceRecord,
        *,
        assessments: dict[str, AutomationAssessmentRecord],
        include_execution_facts: bool,
    ) -> dict[str, Any]:
        attempts = self.store.list_todo_attempts(
            todo_instance_id=item.todo_instance_id, limit=1
        )
        blockers = self.store.list_todo_blockers(
            todo_instance_id=item.todo_instance_id,
            active_only=True,
            limit=1,
        )
        assessment = assessments.get(item.todo_instance_id)
        context: dict[str, Any] = {
            "dispatchDisposition": item.dispatch_disposition,
            "dispatchReasonCode": item.dispatch_reason_code,
            "dispatchReason": item.dispatch_reason,
            "actionAvailability": item.action_availability.model_dump(
                mode="json", by_alias=True
            ),
            "latestTodoAttempt": (
                _dump(TodoAttemptRecord.model_validate(attempts[0]))
                if attempts
                else None
            ),
            "activeBlocker": (
                self._todo_blocker_projection(blockers[0]) if blockers else None
            ),
            "latestAutomationAssessment": (
                _dump(assessment) if assessment is not None else None
            ),
        }
        if include_execution_facts:
            context["recentExecutionFacts"] = self.store.list_execution_control_facts(
                todo_instance_id=item.todo_instance_id,
                limit=20,
            )
        return context

    def _typed_agent_todo_snapshot(
        self,
        *,
        todo_items: list[TodoInstanceRecord],
        plans: dict[str, dict[str, Any]],
        cadence: str,
        include_execution_facts: bool,
    ) -> dict[str, Any]:
        """Freeze the exact Todo targets an Agent decision may diagnose."""

        ordered = sorted(
            todo_items,
            key=lambda item: (item.game_id, item.order_index, item.todo_instance_id),
        )
        target_ids = [item.todo_instance_id for item in ordered]
        if len(target_ids) != len(set(target_ids)):
            raise ManagerConflict("Agent Todo snapshot contains duplicate targets")
        scope_document = {
            "schemaVersion": 1,
            "cadence": cadence,
            "items": [
                {
                    "todoInstanceId": item.todo_instance_id,
                    "todoDefinitionId": item.todo_definition_id,
                    "definitionVersion": item.definition_version,
                    "catalogVersion": item.catalog_version,
                    "sourceHash": item.source_hash,
                    "gameId": item.game_id,
                    "periodKey": item.period_key,
                    "periodStartsAt": item.period_starts_at.isoformat(),
                    "periodEndsAt": item.period_ends_at.isoformat(),
                    "resetRule": dict(item.reset_rule),
                }
                for item in ordered
            ],
        }
        scope_fingerprint = hashlib.sha256(
            json.dumps(
                scope_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assessments = self._latest_automation_assessments(set(target_ids))
        return {
            "schemaVersion": 1,
            "cadence": cadence,
            "scopeFingerprint": scope_fingerprint,
            "targetTodoInstanceIds": target_ids,
            "games": plans,
            "items": [
                {
                    "todoInstanceId": item.todo_instance_id,
                    "todoDefinitionId": item.todo_definition_id,
                    "definitionVersion": item.definition_version,
                    "catalogVersion": item.catalog_version,
                    "sourceHash": item.source_hash,
                    "gameId": item.game_id,
                    "operation": item.operation,
                    "title": item.title,
                    "category": item.category,
                    "orderIndex": item.order_index,
                    "required": item.required,
                    "status": item.status,
                    "attempts": item.attempts,
                    "risk": item.risk,
                    "automationDifficulty": item.automation_difficulty,
                    "automationState": item.automation_state,
                    "periodKey": item.period_key,
                    "periodStartsAt": item.period_starts_at.isoformat(),
                    "periodEndsAt": item.period_ends_at.isoformat(),
                    "resetRule": dict(item.reset_rule),
                    "reason": item.reason,
                    "evidenceRefs": list(item.evidence_refs),
                    "completedAt": (
                        item.completed_at.isoformat()
                        if item.completed_at is not None
                        else None
                    ),
                    **self._todo_operational_context(
                        item,
                        assessments=assessments,
                        include_execution_facts=include_execution_facts,
                    ),
                }
                for item in ordered
            ],
        }

    def todo_summary(self, game_id: str, cadence: str) -> dict[str, Any]:
        items = self.list_todo_instances(
            game_id=game_id, cadence=cadence, current=True, limit=1000
        )
        return self._todo_summary_from_items(items, cadence)

    def _selected_todo_scope_items(
        self,
        *,
        game_id: str,
        cadence: str,
        all_items: list[TodoInstanceRecord],
    ) -> list[TodoInstanceRecord]:
        """Return the Manager-owned completion scope for one game.

        Game cards and batch planning must project acceptance from the same
        selected Todo set. Optional/forbidden catalog rows remain visible in
        the full Today summary, but they cannot change the scope fingerprint
        after a selected batch has sealed successfully.
        """

        configured_selection = self.store.get_config()["values"].get(
            "daily_todo_selection", {}
        )
        configured_definition_ids = configured_selection.get(game_id)
        selected_definition_ids = (
            set(configured_definition_ids)
            if cadence == "daily" and isinstance(configured_definition_ids, list)
            else {
                item.todo_definition_id for item in all_items if item.required
            }
        )
        return [
            item
            for item in all_items
            if item.todo_definition_id in selected_definition_ids
        ]

    @staticmethod
    def _todo_summary_from_items(
        items: list[TodoInstanceRecord], cadence: str
    ) -> dict[str, Any]:
        counts = {
            status: sum(1 for item in items if item.status == status)
            for status in (
                "pending",
                "in_progress",
                "completed",
                "skipped",
                "blocked",
                "review_required",
                "human_required",
            )
        }
        required_items = [item for item in items if item.required]
        outstanding = [item for item in required_items if item.status != "completed"]
        difficult = [
            item
            for item in outstanding
            if item.automation_difficulty in {"high", "unknown"}
            or item.status in {"blocked", "review_required", "human_required"}
        ]
        progress_percent = (
            round((len(required_items) - len(outstanding)) * 100 / len(required_items), 2)
            if required_items
            else 0.0
        )
        next_reset_at = min(
            (item.period_ends_at for item in items), default=None
        )
        period_keys = sorted({item.period_key for item in items})
        period_starts_at = sorted(
            {item.period_starts_at.isoformat() for item in items}
        )
        period_ends_at = sorted(
            {item.period_ends_at.isoformat() for item in items}
        )
        reset_rules = sorted(
            {
                json.dumps(
                    item.reset_rule,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for item in items
            }
        )
        scope_document = {
            "schemaVersion": 1,
            "cadence": cadence,
            "instances": [
                {
                    "todoInstanceId": item.todo_instance_id,
                    "todoDefinitionId": item.todo_definition_id,
                    "definitionVersion": item.definition_version,
                    "sourceHash": item.source_hash,
                    "periodKey": item.period_key,
                    "periodStartsAt": item.period_starts_at.isoformat(),
                    "periodEndsAt": item.period_ends_at.isoformat(),
                    "resetRule": item.reset_rule,
                }
                for item in sorted(items, key=lambda value: value.todo_instance_id)
            ],
        }
        scope_fingerprint = hashlib.sha256(
            json.dumps(
                scope_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        scope_key = (
            f"{cadence}:{period_keys[0]}:{scope_fingerprint[:12]}"
            if len(period_keys) == 1 and len(period_starts_at) == 1 and len(period_ends_at) == 1
            else f"{cadence}:mixed:{scope_fingerprint[:16]}"
        )
        return {
            "cadence": cadence,
            "scopeKey": scope_key,
            "scopeFingerprint": scope_fingerprint,
            "periodKeys": period_keys,
            "periodStartsAt": period_starts_at,
            "periodEndsAt": period_ends_at,
            "resetRules": [json.loads(value) for value in reset_rules],
            "definitionCount": len(items),
            "requiredTotal": len(required_items),
            "requiredCompleted": len(required_items) - len(outstanding),
            "requiredRemaining": len(outstanding),
            "allRequiredCompleted": bool(required_items) and not outstanding,
            "progress": {
                "completed": len(required_items) - len(outstanding),
                "total": len(required_items),
                "percent": progress_percent,
            },
            "nextResetAt": (
                next_reset_at.isoformat() if next_reset_at is not None else None
            ),
            "counts": counts,
            "outstandingTodoInstanceIds": [item.todo_instance_id for item in outstanding],
            "unresolvedRequiredTodoIds": [
                item.todo_instance_id for item in outstanding
            ],
            "difficultOperations": [
                {
                    "todoInstanceId": item.todo_instance_id,
                    "operation": item.operation,
                    "title": item.title,
                    "status": item.status,
                    "automationDifficulty": item.automation_difficulty,
                    "automationState": item.automation_state,
                    "reason": item.reason,
                }
                for item in difficult
            ],
        }

    def todo_overview(self) -> dict[str, Any]:
        games = self.store.list_games()
        per_game: dict[str, dict[str, Any]] = {}
        for game in games:
            game_id = str(game["game_id"])
            current_items = self.list_todo_instances(
                game_id=game_id, cadence="daily", current=True, limit=1000
            )
            # Today is an execution projection, not a catalog inventory.  Use
            # the same selected completion scope as game cards and Batch
            # planning so optional/unselected rows cannot inflate its totals.
            selected_items = self._selected_todo_scope_items(
                game_id=game_id,
                cadence="daily",
                all_items=current_items,
            )
            per_game[game_id] = self._todo_summary_from_items(
                selected_items, "daily"
            )
        scope_game_ids = [
            str(game["game_id"]) for game in games if bool(game.get("enabled"))
        ]
        scope_document = {
            "schemaVersion": 1,
            "cadence": "daily",
            "games": [
                {
                    "gameId": game_id,
                    "scopeFingerprint": per_game[game_id]["scopeFingerprint"],
                }
                for game_id in scope_game_ids
            ],
        }
        scope_fingerprint = hashlib.sha256(
            json.dumps(
                scope_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reset_policy = self._todo_reset_policy_document()
        scoped_summaries = [per_game[game_id] for game_id in scope_game_ids]
        return {
            "defaultResetRule": {
                "timezone": reset_policy["timezone"],
                "time": reset_policy["time"],
                "cadence": "daily",
            },
            "scopeKey": f"daily:manager:{scope_fingerprint[:16]}",
            "scopeFingerprint": scope_fingerprint,
            "scopeGameIds": scope_game_ids,
            "games": per_game,
            "requiredTotal": sum(item["requiredTotal"] for item in scoped_summaries),
            "requiredCompleted": sum(
                item["requiredCompleted"] for item in scoped_summaries
            ),
            "requiredRemaining": sum(
                item["requiredRemaining"] for item in scoped_summaries
            ),
            "blocked": sum(item["counts"]["blocked"] for item in scoped_summaries),
            "reviewRequired": sum(
                item["counts"]["review_required"] for item in scoped_summaries
            ),
            "humanRequired": sum(
                item["counts"]["human_required"] for item in scoped_summaries
            ),
        }

    def _todo_plans_for_games(
        self, game_ids: list[str], cadence: str
    ) -> dict[str, dict[str, Any]]:
        plans: dict[str, dict[str, Any]] = {}
        sealed_batches = (
            self._projection_history()[1] if cadence == "daily" else []
        )
        for game_id in game_ids:
            all_items = self.list_todo_instances(
                game_id=game_id, cadence=cadence, current=True, limit=1000
            )
            items = self._selected_todo_scope_items(
                game_id=game_id,
                cadence=cadence,
                all_items=all_items,
            )
            selected_definition_ids = {
                item.todo_definition_id for item in items
            }
            pending = [item for item in items if item.status != "completed"]
            completed = [item for item in items if item.status == "completed"]
            scope = self._todo_summary_from_items(items, cadence)
            current_completion = None
            if cadence == "daily" and scope.get("periodKeys"):
                current_completion = project_current_game_completion(
                    game_id=game_id,
                    current_scope=scope,
                    sealed_batches=sealed_batches,
                    invalidated_at=self.store.latest_todo_reset_at(
                        {item.todo_instance_id for item in items}
                    ),
                )
            # Completed Todo facts remain immutable until the explicit reset
            # action is used. Evidence review and execution are separate
            # phases: lacking accepted_done must not silently replay actions
            # that may spend stamina or claim one-time rewards again.
            completion_recovery_replay: list[TodoInstanceRecord] = []
            recovery_replay_ids: set[str] = set()
            unresolved = list(pending)
            runtime = self.adapter_host.execution_bindings(game_id)
            bindings = {
                str(binding["operation"]): binding
                for binding in runtime.get("bindings", [])
                if isinstance(binding, dict) and isinstance(binding.get("operation"), str)
            }
            executable: list[TodoInstanceRecord] = []
            deferred_reasons: dict[str, dict[str, str]] = {}
            executable_bindings: dict[str, dict[str, Any]] = {}
            session_reentry: list[TodoInstanceRecord] = []
            # A completed session Todo is a durable GameDay fact, but it is
            # also the per-process prerequisite that establishes the newly
            # launched client's usable home/world state.  Re-enter it only
            # when this game still has selected work to do; an otherwise
            # completed game must remain skipped by a fresh batch.
            if pending:
                for item in completed:
                    if item.todo_instance_id in recovery_replay_ids:
                        continue
                    binding = bindings.get(item.operation)
                    if (
                        binding is not None
                        and str(binding.get("actionClass")) == "session"
                        and item.risk in {"routine_action", "observe_only"}
                        and item.adapter_capability_ref is not None
                    ):
                        session_reentry.append(item)
                        executable_bindings[item.todo_instance_id] = {
                            "operation": item.operation,
                            "timeoutSeconds": int(binding.get("timeoutSeconds", 1)),
                            "requiredEvidenceKinds": list(
                                binding.get("requiredEvidenceKinds", [])
                            ),
                            "supportsResume": bool(binding.get("supportsResume")),
                            "resume": False,
                            "sessionReentry": True,
                        }
            executable.extend(session_reentry)
            for item in unresolved:
                completion_replay = item.todo_instance_id in recovery_replay_ids
                if completion_replay and (
                    item.risk not in {"routine_action", "observe_only"}
                    or item.adapter_capability_ref is None
                ):
                    deferred_reasons[item.todo_instance_id] = {
                        "code": "completion_recovery_not_automatable",
                        "reason": "当前完成恢复项不在可自动执行的安全范围内。",
                    }
                    continue
                if not completion_replay and item.dispatch_disposition != "eligible":
                    deferred_reasons[item.todo_instance_id] = {
                        "code": item.dispatch_reason_code,
                        "reason": item.dispatch_reason,
                    }
                    continue
                binding = bindings.get(item.operation)
                if binding is None:
                    # The dispatch projection and the manifest are re-read
                    # independently.  If they changed between those reads,
                    # keep this Todo out of the batch rather than letting a
                    # stale selection abort unrelated, eligible work.
                    deferred_reasons[item.todo_instance_id] = {
                        "code": "operation_not_promoted",
                        "reason": "Todo operation 没有晋级 runtime binding。",
                    }
                    continue
                executable.append(item)
                executable_bindings[item.todo_instance_id] = {
                    "operation": item.operation,
                    "timeoutSeconds": int(binding.get("timeoutSeconds", 1)),
                    "requiredEvidenceKinds": list(
                        binding.get("requiredEvidenceKinds", [])
                    ),
                    "supportsResume": bool(binding.get("supportsResume")),
                    "resume": False,
                    **(
                        {"completionRecoveryReplay": True}
                        if completion_replay
                        else {}
                    ),
                }
            executable_ids = {
                candidate.todo_instance_id for candidate in executable
            }
            plans[game_id] = {
                "gameId": game_id,
                "cadence": cadence,
                "selectedTodoDefinitionIds": sorted(selected_definition_ids),
                "periodKeys": sorted({item.period_key for item in items}),
                "periodStartsAt": list(scope["periodStartsAt"]),
                "periodEndsAt": list(scope["periodEndsAt"]),
                "scopeKey": scope["scopeKey"],
                "scopeFingerprint": scope["scopeFingerprint"],
                "completionTodoInstanceIds": [
                    item.todo_instance_id for item in items
                ],
                "completionTodoScope": {
                    "schemaVersion": 1,
                    "scopeKey": scope["scopeKey"],
                    "scopeFingerprint": scope["scopeFingerprint"],
                    "periodKeys": list(scope["periodKeys"]),
                    "periodStartsAt": list(scope["periodStartsAt"]),
                    "periodEndsAt": list(scope["periodEndsAt"]),
                    "items": [
                        {
                            "todoInstanceId": item.todo_instance_id,
                            "todoDefinitionId": item.todo_definition_id,
                            "definitionVersion": item.definition_version,
                            "catalogVersion": item.catalog_version,
                            "sourceHash": item.source_hash,
                            "periodKey": item.period_key,
                            "periodStartsAt": item.period_starts_at.isoformat(),
                            "periodEndsAt": item.period_ends_at.isoformat(),
                            "required": item.required,
                            "risk": item.risk,
                            "statusAtBatchCreation": item.status,
                            "dispatchDispositionAtBatchCreation": (
                                item.dispatch_disposition
                            ),
                        }
                        for item in items
                    ],
                },
                "todoInstanceIds": [item.todo_instance_id for item in unresolved],
                "unresolvedRequiredTodoIds": [
                    item.todo_instance_id for item in unresolved
                ],
                "executableTodoInstanceIds": [
                    item.todo_instance_id for item in executable
                ],
                "sessionReentryTodoInstanceIds": [
                    item.todo_instance_id for item in session_reentry
                ],
                "completionRecoveryReplayTodoInstanceIds": [
                    item.todo_instance_id for item in completion_recovery_replay
                ],
                "deferredTodoInstanceIds": [
                    item.todo_instance_id
                    for item in unresolved
                    if item.todo_instance_id not in executable_ids
                ],
                "deferredReasons": deferred_reasons,
                "dispatch": {
                    item.todo_instance_id: {
                        "disposition": item.dispatch_disposition,
                        "reasonCode": item.dispatch_reason_code,
                        "reason": item.dispatch_reason,
                        "actionAvailability": item.action_availability.model_dump(
                            mode="json", by_alias=True
                        ),
                    }
                    for item in items
                },
                "executableBindings": executable_bindings,
                "runtimeBinding": runtime,
                "completedTodoInstanceIds": [
                    item.todo_instance_id for item in completed
                ],
                "skippedCompletedCount": max(
                    0,
                    len(completed)
                    - len(session_reentry)
                    - len(completion_recovery_replay),
                ),
                "requiredRemaining": len(unresolved),
                "currentCompletion": (
                    {
                        "status": str(current_completion.status),
                        "batchId": current_completion.batch_id,
                        "sealVersion": current_completion.seal_version,
                        "reasonCode": current_completion.reason_code,
                        "decisionId": current_completion.decision_id,
                        "reviewId": current_completion.review_id,
                    }
                    if current_completion is not None
                    else {
                        "status": "none",
                        "reasonCode": "current_todo_scope_has_no_period",
                    }
                ),
                "blockedTodoInstanceIds": [
                    item.todo_instance_id
                    for item in pending
                    if item.status == "blocked"
                ],
                "reviewRequiredTodoInstanceIds": [
                    item.todo_instance_id
                    for item in pending
                    if item.status == "review_required"
                ],
                "humanRequiredTodoInstanceIds": [
                    item.todo_instance_id
                    for item in pending
                    if item.status == "human_required"
                ],
                "reconcileTodoInstanceIds": [
                    item.todo_instance_id
                    for item in pending
                    if item.dispatch_disposition == "reconcile_required"
                ],
            }
        return plans

    @staticmethod
    def _batch_todo_scope(
        todo_plans: dict[str, dict[str, Any]], game_ids: list[str]
    ) -> dict[str, Any]:
        document = {
            "schemaVersion": 1,
            "games": [
                {
                    "gameId": game_id,
                    "scopeKey": todo_plans[game_id]["scopeKey"],
                    "scopeFingerprint": todo_plans[game_id]["scopeFingerprint"],
                    "periodKeys": list(todo_plans[game_id]["periodKeys"]),
                    "periodStartsAt": list(todo_plans[game_id]["periodStartsAt"]),
                    "periodEndsAt": list(todo_plans[game_id]["periodEndsAt"]),
                    "completionTodoInstanceIds": list(
                        todo_plans[game_id]["completionTodoInstanceIds"]
                    ),
                    "completionTodoScope": dict(
                        todo_plans[game_id]["completionTodoScope"]
                    ),
                }
                for game_id in game_ids
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        period_keys = sorted(
            {
                str(value)
                for game_id in game_ids
                for value in todo_plans[game_id]["periodKeys"]
            }
        )
        return {
            **document,
            "scopeFingerprint": fingerprint,
            "scopeKey": (
                f"batch:{period_keys[0]}:{fingerprint[:12]}"
                if len(period_keys) == 1
                else f"batch:mixed:{fingerprint[:16]}"
            ),
        }

