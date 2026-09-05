"""Pure Manager-owned Todo dispatch policy.

The WebGUI renders this projection verbatim.  Adapter availability, risk,
resume safety and blocker state are business facts and therefore never belong
in presentation code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DISPATCH_DISPOSITIONS = frozenset(
    {
        "eligible",
        "completed_skip",
        "terminal_skip",
        "deferred_review",
        "deferred_human",
        "deferred_forbidden",
        "reconcile_required",
        "unsupported",
    }
)


HUMAN_TAKEOVER_RELEASED_REOBSERVE_REASON = (
    "human takeover released; fresh observation/rebind is required"
)


@dataclass(frozen=True, slots=True)
class TodoDispatchFacts:
    status: str
    risk: str
    todo_definition_id: str
    operation: str
    adapter_capability_ref: str | None
    runtime: Mapping[str, Any]
    binding: Mapping[str, Any] | None
    current_reason: str = ""
    latest_attempt: Mapping[str, Any] | None = None
    active_blocker: Mapping[str, Any] | None = None


def _availability(
    *,
    execute: bool = False,
    resume: bool = False,
    review: bool = False,
    release_human: bool = False,
    submit_manual_evidence: bool = False,
    next_action: str,
) -> dict[str, object]:
    return {
        "execute": execute,
        "resume": resume,
        "review": review,
        "releaseHuman": release_human,
        "submitManualEvidence": submit_manual_evidence,
        "nextAction": next_action,
    }


def _projection(
    disposition: str,
    reason: str,
    *,
    code: str,
    availability: dict[str, object],
) -> dict[str, object]:
    if disposition not in DISPATCH_DISPOSITIONS:
        raise ValueError("unknown Todo dispatch disposition")
    return {
        "dispatchDisposition": disposition,
        "dispatchReasonCode": code,
        "dispatchReason": reason,
        "actionAvailability": availability,
    }


def evaluate_todo_dispatch(facts: TodoDispatchFacts) -> dict[str, object]:
    """Return the sole queue/action projection for one frozen Todo instance."""

    status = facts.status
    blocker = facts.active_blocker or {}
    blocker_kind = str(blocker.get("kind") or "")
    if status == "completed":
        return _projection(
            "completed_skip",
            "Todo 已有 Manager 完成事实，后续批次跳过。",
            code="already_completed",
            availability=_availability(next_action="无需操作"),
        )
    if status == "skipped":
        return _projection(
            "terminal_skip",
            "Todo 已按策略终止，不进入执行队列。",
            code="terminal_skip",
            availability=_availability(next_action="无需操作"),
        )
    if facts.risk == "forbidden" or blocker_kind == "forbidden":
        return _projection(
            "deferred_forbidden",
            "动作被安全策略禁止自动执行。",
            code="forbidden_by_policy",
            availability=_availability(
                review=True, next_action="仅查看策略与证据，不执行"
            ),
        )
    if status == "human_required" or blocker_kind == "human_required":
        return _projection(
            "deferred_human",
            "需要人工完成登录、验证或明确接管后再对账。",
            code="human_resolution_required",
            availability=_availability(
                review=True,
                release_human=True,
                next_action="人工处理后提交显式 release，并重新观察",
            ),
        )
    # A user may start a new daily batch after a tool reports a *retryable*
    # routine failure.  Keep the earlier attempt and its diagnostics intact,
    # but let the newly selected Todo enter a fresh Manager attempt.  This is
    # deliberately narrower than a generic "review means retry": human
    # takeover, forbidden actions, non-retryable reviews, and every blocker
    # except a retryable diagnostic review remain non-executable below.
    retryable_review_blocker = (
        blocker_kind == "review_required"
        and bool(blocker.get("retryable") is True)
    )
    latest_reason_code = str(
        (facts.latest_attempt or {}).get("reasonCode")
        or (facts.latest_attempt or {}).get("reason_code")
        or ""
    )
    latest_reason = str((facts.latest_attempt or {}).get("reason") or "")
    review_reason_code = latest_reason_code or str(blocker.get("code") or "")
    review_reason = latest_reason or str(blocker.get("reason") or "")
    # Older OpenKuro Adapter releases incorrectly marked these fixed formal
    # GUI bootstrap failures non-retryable. They are neither human review nor
    # a policy decision: a newly requested *full* batch can safely restart the
    # formal GUI lifecycle from the beginning. Keep the allowlist narrow so a
    # generic evidence review never becomes executable.
    legacy_retryable_formal_gui_review = (
        status == "review_required"
        and blocker_kind in {"", "review_required"}
        and review_reason_code == "upstream_stage_unverified"
        and review_reason.startswith(
            (
                "formal_gui_exited_before_update_ready",
                "formal_gui_exited_before_inner_gui",
                "formal_gui_inner_bind_timeout",
                "formal_gui_update_check_timeout",
            )
        )
    )
    retryable_routine_review = (
        status == "review_required"
        and (
            not blocker
            or retryable_review_blocker
            or legacy_retryable_formal_gui_review
        )
        and (
            bool(
                facts.latest_attempt
                and facts.latest_attempt.get("retryable") is True
            )
            or legacy_retryable_formal_gui_review
        )
        and facts.risk in {"routine_action", "observe_only"}
    )
    # Releasing a human takeover never authorizes the old GameRun to resume.
    # It does, however, establish a narrow, durable state from which the next
    # explicitly requested full daily batch must re-run the session entry Todo
    # and collect a fresh game observation.  Without this case the release
    # leaves attach-home permanently deferred while later operations run.
    released_human_gate_reobserve = (
        status == "review_required"
        and not blocker
        and facts.current_reason == HUMAN_TAKEOVER_RELEASED_REOBSERVE_REASON
        and facts.risk in {"routine_action", "observe_only"}
    )
    if retryable_routine_review or released_human_gate_reobserve:
        status = "pending"
    # A previous retryable review blocker may outlive a later batch-cancel
    # attempt which is recorded as a non-retryable blocked outcome.  That
    # combination is still a routine fresh-batch retry: the active blocker is
    # the durable diagnostic fact, while the newest attempt merely records
    # that its old RunAttempt was already sealed.  Do not let the review check
    # below shadow the blocked retry policy which intentionally accepts this
    # exact case.
    retryable_blocked_review = (
        status == "blocked"
        and facts.risk in {"routine_action", "observe_only"}
        and blocker_kind == "review_required"
        and bool(blocker.get("retryable") is True)
    )
    if (
        status == "review_required" or blocker_kind == "review_required"
    ) and not retryable_routine_review and not retryable_blocked_review:
        return _projection(
            "deferred_review",
            "当前事实不足或风险需要人工复核。",
            code="review_resolution_required",
            availability=_availability(
                review=True,
                submit_manual_evidence=True,
                next_action="补充证据并完成复核",
            ),
        )
    # A user starting a new daily batch has explicitly asked to run the
    # selected routine Todo again. A prior transport interruption remains in
    # the audit ledger, but must not silently exclude that selected Todo.
    # Human-required and policy-blocked items returned above never reach here.
    retryable_execution_blocker = (
        blocker_kind in {"contract_invariant", "review_required"}
        and bool(blocker.get("retryable") is True)
    )
    retryable_routine_block = (
        status == "blocked"
        and facts.risk in {"routine_action", "observe_only"}
        and (
            not blocker
            # This projection is for a newly requested full batch, not a
            # same-run resume.  An earlier active diagnostic blocker marked
            # retryable remains audit evidence, but the newest attempt may be
            # a non-retryable *batch cancellation*.  The retryable blocker is
            # sufficient to admit a fresh run; completion is still fenced to
            # the new run and cannot bypass a blocker from that run.
            or retryable_execution_blocker
        )
    )
    if retryable_routine_block:
        status = "pending"
    if status == "in_progress":
        retryable = bool(
            facts.latest_attempt and facts.latest_attempt.get("retryable") is True
        )
        supports_resume = bool(
            facts.binding and facts.binding.get("supportsResume") is True
        )
        can_resume = retryable and supports_resume and not blocker
        return _projection(
            "reconcile_required",
            (
                "Todo 属于既有 GameRun；必须先做 same-run 对账，不能由新批次重跑。"
                if can_resume
                else "Todo 的中断状态缺少可安全续跑事实，需要重新观察或复核。"
            ),
            code=("same_run_resume_required" if can_resume else "resume_facts_incomplete"),
            availability=_availability(
                resume=can_resume,
                review=True,
                next_action=(
                    "在原 GameRun 提交 resume 请求"
                    if can_resume
                    else "重新观察并补齐 receipt/checkpoint/blocker 事实"
                ),
            ),
        )
    if status != "pending":
        return _projection(
            "unsupported",
            "Todo 状态不在 Manager 已知调度状态机中。",
            code=f"todo_state_{status}",
            availability=_availability(
                review=True, next_action="检查 Todo 状态与定义版本"
            ),
        )
    if facts.risk not in {"routine_action", "observe_only"}:
        return _projection(
            "deferred_review",
            "Todo 风险不在自动执行白名单。",
            code="risk_not_executable",
            availability=_availability(
                review=True,
                submit_manual_evidence=True,
                next_action="人工确认目标、成本和证据",
            ),
        )
    if facts.adapter_capability_ref is None:
        return _projection(
            "unsupported",
            "Todo 尚未绑定 typed Adapter capability。",
            code="capability_unbound",
            availability=_availability(
                review=True, next_action="实现并晋级对应 Adapter capability"
            ),
        )
    if not bool(facts.runtime.get("manifestVerified")):
        code = str(facts.runtime.get("status") or "manifest_unverified")
        return _projection(
            "unsupported",
            "当前没有已验证并晋级的执行 manifest。",
            code=code,
            availability=_availability(
                review=True, next_action="完成 Adapter 隔离验证与晋级"
            ),
        )
    binding = facts.binding
    if binding is None:
        return _projection(
            "unsupported",
            "Todo operation 没有晋级 runtime binding。",
            code="operation_not_promoted",
            availability=_availability(
                review=True, next_action="登记并晋级 operation binding"
            ),
        )
    if facts.todo_definition_id not in {
        str(value) for value in binding.get("todoDefinitionIds", [])
    }:
        return _projection(
            "unsupported",
            "Runtime binding 未声明当前冻结 Todo definition。",
            code="todo_definition_not_promoted",
            availability=_availability(
                review=True, next_action="更新 Adapter manifest 的 definition allowlist"
            ),
        )
    if facts.adapter_capability_ref not in {
        str(value) for value in binding.get("adapterCapabilityRefs", [])
    }:
        return _projection(
            "unsupported",
            "Runtime binding 未声明当前 capability 版本。",
            code="capability_not_promoted",
            availability=_availability(
                review=True, next_action="更新并重新晋级 capability binding"
            ),
        )
    if str(binding.get("risk")) != facts.risk:
        return _projection(
            "unsupported",
            "Runtime binding 风险与冻结 Todo 风险不一致。",
            code="risk_binding_mismatch",
            availability=_availability(
                review=True, next_action="修正风险声明并重新晋级"
            ),
        )
    return _projection(
        "eligible",
        "Manager 已验证 Todo 状态、风险、capability 与晋级 binding。",
        code="eligible_pending",
        availability=_availability(execute=True, next_action="等待队列调度"),
    )
