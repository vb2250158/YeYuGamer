"""Build the Manager capability catalog from one typed definition source."""

from __future__ import annotations

import hashlib
from typing import Any

from ..domain.models import CapabilityDefinition


def build_capability_registry(
    execution_ready: bool,
) -> dict[str, CapabilityDefinition]:
    """Return the stable capability definitions for one Manager process."""

    definitions = [
        CapabilityDefinition(
            capability_id="system.snapshot.read",
            display_name="读取 Manager 快照",
            description="读取版本一致的系统快照，不改变状态。",
            risk="observe_only",
            enabled=True,
            requires_idempotency_key=False,
            input_schema={"type": "object", "additionalProperties": False},
        ),
        CapabilityDefinition(
            capability_id="game.daily.plan",
            display_name="规划单游戏每日",
            description="创建单个白名单游戏的计划，不启动旧执行器。",
            risk="controlled_write",
            enabled=True,
            requires_idempotency_key=True,
            input_schema={
                "type": "object",
                "required": ["gameId"],
                "properties": {"gameId": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        CapabilityDefinition(
            capability_id="game.daily.run",
            display_name="运行单游戏每日",
            description="通过本机哈希固定的兼容 Adapter 运行一个白名单 GameId。",
            risk="routine_action",
            enabled=execution_ready,
            requires_idempotency_key=True,
            input_schema={
                "type": "object",
                "required": ["gameId"],
                "properties": {"gameId": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        CapabilityDefinition(
            capability_id="batch.daily.plan",
            display_name="规划每日批次",
            description="按 Manager 配置创建批次计划，不自动启动游戏。",
            risk="controlled_write",
            enabled=True,
            requires_idempotency_key=True,
            input_schema={
                "type": "object",
                "properties": {
                    "gameIds": {"type": "array", "items": {"type": "string"}}
                },
                "additionalProperties": False,
            },
        ),
    ]
    extra_specs: list[
        tuple[
            str,
            str,
            str,
            str,
            bool,
            dict[str, Any],
            tuple[str, ...],
        ]
    ] = [
        (
            "batch.daily.run",
            "运行每日批次",
            "通过串行执行器运行每日批次；仅已晋级并有当前绑定的游戏会进入执行队列。",
            "routine_action",
            execution_ready,
            {"gameIds": {"type": "array", "items": {"type": "string"}}},
            (),
        ),
        (
            "game.weekly.plan",
            "规划单游戏周常",
            "建立独立周常运行计划。",
            "controlled_write",
            True,
            {"gameId": {"type": "string"}, "weeklyId": {"type": "string"}},
            ("gameId", "weeklyId"),
        ),
        (
            "game.weekly.run",
            "运行单游戏周常",
            "通过已晋级 Adapter 运行周常。",
            "routine_action",
            False,
            {"gameId": {"type": "string"}, "weeklyId": {"type": "string"}},
            ("gameId", "weeklyId"),
        ),
        (
            "run.resume.request",
            "请求恢复同一运行",
            "只恢复既有 run，不创建第二批次。",
            "routine_action",
            False,
            {"runId": {"type": "string"}, "reason": {"type": "string"}},
            (),
        ),
        (
            "run.takeover.request",
            "申请人工接管",
            "为目标 run 建立人工处置请求。该 capability 绑定尚未完成，请使用 typed run-control API。",
            "controlled_write",
            False,
            {"runId": {"type": "string"}, "reason": {"type": "string"}},
            (),
        ),
        (
            "run.takeover.release",
            "释放人工接管",
            "释放接管后要求重新观察。该 capability 绑定尚未完成，请使用 typed run-control API。",
            "controlled_write",
            False,
            {"runId": {"type": "string"}, "reason": {"type": "string"}},
            (),
        ),
        (
            "observation.capture.request",
            "截取游戏窗口",
            "由 Manager 截取与 GameId 和 GameRun 匹配的白名单游戏窗口，并登记 opaque PNG artifact。",
            "routine_action",
            True,
            {"gameId": {"type": "string"}, "runId": {"type": "string"}},
            ("gameId", "runId"),
        ),
        (
            "checkpoint.record",
            "记录证据检查点",
            "只接受关联 Observation 和 artifact 的检查点。",
            "controlled_write",
            False,
            {
                "runId": {"type": "string"},
                "artifactIds": {"type": "array", "items": {"type": "string"}},
            },
            (),
        ),
        (
            "evidence.review.submit",
            "提交证据复核",
            "复核 artifact，不直接宣称游戏完成。该 capability 绑定尚未完成，请使用 typed evidence API。",
            "controlled_write",
            False,
            {"artifactId": {"type": "string"}, "verdict": {"type": "string"}},
            (),
        ),
        (
            "incident.repair.create",
            "创建维修会话",
            "针对聚合 Incident 建立隔离维修会话。该 capability 绑定尚未完成，请使用 typed incident API。",
            "controlled_write",
            False,
            {"incidentId": {"type": "string"}, "reason": {"type": "string"}},
            (),
        ),
        (
            "repair.verification.request",
            "请求维修复验",
            "维修复验 capability 尚未绑定。",
            "controlled_write",
            False,
            {"repairSessionId": {"type": "string"}},
            (),
        ),
        (
            "adapter.replay.request",
            "请求 Adapter Replay",
            "固定 fixture Replay 尚未实现。",
            "controlled_write",
            False,
            {"adapterId": {"type": "string"}},
            (),
        ),
        (
            "adapter.shadow.request",
            "请求 Adapter Shadow",
            "无输入 Shadow Host 尚未实现。",
            "controlled_write",
            False,
            {"adapterId": {"type": "string"}},
            (),
        ),
        (
            "adapter.canary.request",
            "运行 Adapter 诊断 Canary",
            "由 Manager-owned Adapter Host 校验注册、固定入口和哈希；不启动 Adapter 或游戏。",
            "approval_required",
            True,
            {"adapterId": {"type": "string"}},
            ("adapterId",),
        ),
        (
            "adapter.promotion.request",
            "请求 Adapter 晋级",
            "晋级 capability 尚未绑定，请使用 typed governance API。",
            "approval_required",
            False,
            {"adapterId": {"type": "string"}, "versionId": {"type": "string"}},
            (),
        ),
        (
            "adapter.rollback.request",
            "请求 Adapter 回滚",
            "回滚 capability 尚未绑定，请使用 typed governance API。",
            "approval_required",
            False,
            {"adapterId": {"type": "string"}, "versionId": {"type": "string"}},
            (),
        ),
        (
            "diagnostics.bundle.create",
            "生成诊断包",
            "诊断包 capability 尚未绑定，请使用 typed diagnostics API。",
            "controlled_write",
            False,
            {"runId": {"type": "string"}, "batchId": {"type": "string"}},
            (),
        ),
        (
            "config.validate",
            "验证配置草稿",
            "配置验证 capability 尚未绑定。",
            "controlled_write",
            False,
            {"config": {"type": "object"}},
            (),
        ),
        (
            "config.rollback",
            "请求配置回滚",
            "回滚到 Manager 账本中的已知版本。",
            "approval_required",
            False,
            {"version": {"type": "string"}},
            (),
        ),
        (
            "notification.draft",
            "生成通知草稿",
            "通知草稿去重与模板实现尚未完成。",
            "controlled_write",
            False,
            {"milestoneId": {"type": "string"}},
            (),
        ),
        (
            "notification.send",
            "发送通知",
            "经明确策略和收件人绑定投递通知。",
            "approval_required",
            False,
            {"notificationId": {"type": "string"}},
            (),
        ),
    ]
    for (
        capability_id,
        display_name,
        description,
        risk,
        enabled,
        properties,
        required,
    ) in extra_specs:
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            input_schema["required"] = list(required)
        definitions.append(
            CapabilityDefinition(
                capability_id=capability_id,
                display_name=display_name,
                description=description,
                risk=risk,
                enabled=enabled,
                requires_idempotency_key=True,
                input_schema=input_schema,
            )
        )

    enriched: list[CapabilityDefinition] = []
    implemented_capabilities = {
        "system.snapshot.read",
        "game.daily.plan",
        "game.daily.run",
        "batch.daily.plan",
        "batch.daily.run",
        "game.weekly.plan",
        "game.weekly.run",
        "adapter.canary.request",
        "observation.capture.request",
    }
    for definition in definitions:
        implementation_hash = hashlib.sha256(
            f"{definition.capability_id}@{definition.version}:manager-v1".encode()
        ).hexdigest()
        enriched.append(
            definition.model_copy(
                update={
                    "output_schema": {"type": "object", "additionalProperties": True},
                    "policy": {
                        "managerOnly": True,
                        "implementationStatus": (
                            "implemented"
                            if definition.capability_id in implemented_capabilities
                            else "disabled-unimplemented"
                        ),
                        "legacyExecutionGate": definition.capability_id.endswith(".run"),
                        "startsGame": definition.capability_id.endswith(".run"),
                        "diagnosticOnly": (
                            definition.capability_id == "adapter.canary.request"
                        ),
                    },
                    "pre_evidence": (
                        ["fresh-observation"]
                        if definition.risk in {"routine_action", "approval_required"}
                        else []
                    ),
                    "post_evidence": (
                        ["action-receipt", "fresh-observation"]
                        if definition.risk in {"routine_action", "approval_required"}
                        else ["command-receipt"]
                    ),
                    "implementation_hash": implementation_hash,
                }
            )
        )
    return {definition.capability_id: definition for definition in enriched}
