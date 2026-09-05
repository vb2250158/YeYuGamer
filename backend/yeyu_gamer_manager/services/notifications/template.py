"""Compact email and collapsible offline report from one immutable Batch seal.

This renderer never reads live state, files or transport settings. The report
is frozen with the mail; transport embeds only resolver-verified image bytes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Mapping

from ..account_scopes import DEFAULT_ACCOUNT_ID, account_target_id
from .screenshots import REWARD_KINDS, is_reward_capture

_STATUS_LABELS = {
    "completed": "已完成", "skipped": "已跳过", "blocked": "受阻",
    "review_required": "待验收", "human_required": "需人工处理",
    "in_progress": "进行中", "pending": "未执行", "failed": "失败", "cancelled": "已取消",
}
_BADGES = {
    "accepted": ("已验收", "#18704a"), "human": ("需人工处理", "#97551c"),
    "failed": ("执行失败", "#ac3939"), "notstarted": ("本轮未执行", "#697585"),
    "review": ("待验收", "#97551c"), "blocked": ("执行受阻", "#97551c"),
    "cancelled": ("已取消", "#697585"),
}
_BEIJING = timezone(timedelta(hours=8))
_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")
_P = 'style="margin:6px 0;font-size:14px;line-height:1.65;overflow-wrap:anywhere"'


@dataclass(frozen=True, slots=True)
class RenderedBatchNotification:
    outcome: str
    subject: str
    text_body: str
    html_body: str
    report_html: str = ""


def _text(value: object, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _integer(value: object, fallback: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _target_id(item: Mapping[str, Any], fallback: str = "unknown") -> str:
    return account_target_id(_text(item.get("gameId"), fallback), _text(item.get("accountId"), DEFAULT_ACCOUNT_ID))


def _contract_entries(seal: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    contracts = seal.get("completionContracts")
    if isinstance(contracts, Mapping):
        return {_target_id(_mapping(value), str(key)): _mapping(value) for key, value in contracts.items()}
    return {_target_id(item): item for item in _items(contracts)}


def _beijing_time(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_BEIJING).strftime("%m-%d %H:%M:%S")


def _badge(coverage: Mapping[str, Any], contract: Mapping[str, Any], outcome: str,
           target: Mapping[str, Any]) -> str:
    # Consume Manager decisions; checked Todos alone never imply acceptance.
    if outcome == "completed" or coverage.get("acceptedDone") is True:
        return "accepted"
    issues = coverage.get("issues", [])
    status = _text(contract.get("outcome"), _text(contract.get("status")))
    run_state = _text(coverage.get("runState"), _text(target.get("state")))
    if run_state == "human_required":
        return "human"
    if "failed_run" in issues or run_state == "failed":
        return "failed"
    if "not_started_run" in issues or "missing_completion_contract" in issues:
        return "notstarted"
    if run_state == "cancelled":
        return "cancelled"
    if not coverage and (contract.get("acceptedDone") is True or status == "accepted_done"):
        return "accepted"
    if status == "blocked":
        return "blocked"
    return "review"


def _frames(seal: Mapping[str, Any], game_id: str, target_id: str, contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A game-level list alone cannot establish a screenshot's account scope."""
    whitelist = set(seal.get("sealEvidenceArtifactIds", []))
    contract_refs = set(contract.get("screenshotArtifactRefs", []))
    frames, seen = [], set()
    for frame in _items(_mapping(seal.get("mailScreenshotsByGame")).get(game_id)):
        artifact_id = _text(frame.get("artifactId"))
        if artifact_id not in whitelist or artifact_id not in contract_refs or artifact_id in seen:
            continue
        if frame.get("targetId") and frame["targetId"] != target_id:
            continue
        if "accountId" in frame and _target_id(frame, game_id) != target_id:
            continue
        if frame.get("runId") and frame["runId"] != contract.get("runId"):
            continue
        seen.add(artifact_id)
        frames.append(frame)
    return frames


def _reward_frame(frames: list[Mapping[str, Any]], contract: Mapping[str, Any],
                  game_id: str, instances: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    accepted = set(contract.get("acceptedEvidenceRefs", []))
    rewards = [frame for frame in frames if is_reward_capture(frame, game_id, instances)]
    rewards.sort(key=lambda frame: (_text(frame.get("capturedAt")), _text(frame.get("artifactId"))), reverse=True)
    rewards.sort(key=lambda frame: (frame.get("artifactId") not in accepted,
                                  REWARD_KINDS.index(frame["kind"]) if frame["kind"] in REWARD_KINDS else len(REWARD_KINDS)))
    return rewards[0] if rewards else {}


def _figure(frame: Mapping[str, Any], caption: str) -> str:
    return (
        '<div style="margin:10px 0 4px">'
        f'<img src="cid:evidence-{escape(_text(frame.get("artifactId")))}" alt="{escape(caption)}" width="592" '
        'style="display:block;width:100%;max-width:592px;height:auto;border:0;border-radius:6px">'
        f'<p style="margin:4px 0;color:#6b7582;font-size:12px">{escape(caption)}</p></div>'
    )


def _short_attention(badge: str, reason: str, completed: int, total: int) -> str:
    if badge == "accepted":
        return ""
    if badge == "notstarted":
        return "本轮未启动；已有事项记录见附件。"
    if reason and re.search(r"[\u4e00-\u9fff]", reason):
        line = " ".join(reason.split())
        return line if len(line) <= 100 else line[:99] + "…（详见附件）"
    if badge == "human":
        return "当前账号已暂停，请在 YeYu Gamer 中查看现场并处理。"
    if badge == "failed":
        return "本轮执行失败，请在 YeYu Gamer 中查看失败步骤。"
    if badge == "cancelled":
        return "本轮已取消，已执行事项仍保留在详细记录中。"
    if badge == "blocked":
        return "本轮执行受阻，处理原因见附件。"
    if total and completed == total:
        return "步骤已执行完，领取结果仍待验收。"
    return "每日尚未验收；未完成事项和处理原因见附件。"


def _details(target_id: str, instances: list[Mapping[str, Any]], contract: Mapping[str, Any],
             blockers: list[Mapping[str, Any]], frames: list[Mapping[str, Any]], main_frame: Mapping[str, Any]) -> str:
    rows = []
    for item in instances:
        title = _text(item.get("title"), _text(item.get("operation"), "未命名事项"))
        status = _text(item.get("status"), "pending")
        meta = [f"尝试 {_integer(item.get('attempts'))}"]
        for key, label in (("reason", "原因"), ("automationDifficulty", "难度"),
                           ("dispatchDisposition", "调度"), ("dispatchReason", "调度依据")):
            if _text(item.get(key)):
                meta.append(f"{label}：{item[key]}")
        for key, label in (("latestTodoAttempt", "最近尝试"), ("latestAutomationAssessment", "自动化判断")):
            value = _mapping(item.get(key))
            if value:
                meta.append(label + "：" + " · ".join(f"{k}={v}" for k, v in value.items()))
        rows.append('<li style="padding:7px 0;border-bottom:1px solid #e6eaf0">'
                    f'<strong>{escape(title)}</strong> · {escape(_STATUS_LABELS.get(status, status))}'
                    f'<div style="color:#697585;font-size:12px">{escape(" · ".join(meta))}</div></li>')
    parts = ['<details style="margin-top:10px;border-top:1px solid #e6eaf0;padding-top:8px">',
             '<summary style="cursor:pointer;color:#516477;font-size:13px">事项、其他截图与诊断详情</summary>',
             '<ul style="list-style:none;padding:0;margin:8px 0">',
             "".join(rows) or "<li>本轮没有事项记录。</li>", "</ul>"]
    for frame in frames:
        if frame.get("artifactId") != main_frame.get("artifactId"):
            caption = " · ".join(filter(None, (_text(frame.get("role"), "现场画面"),
                                             _text(frame.get("operation")), _beijing_time(frame.get("capturedAt")))))
            parts.append(_figure(frame, caption))
    for blocker in blockers:
        for key, label in (("reason", "原因"), ("nextAction", "下一步"), ("screenshotUnavailableReason", "截图缺失原因")):
            if _text(blocker.get(key)):
                parts.append(f'<p {_P}>{label}：{escape(blocker[key])}</p>')
    status = _text(contract.get("outcome"), _text(contract.get("status"), "未提供合同"))
    parts.append(f'<p {_P}>Completion Contract · {escape(target_id)}: {escape(status)}</p>')
    for key, label in (("contractVersion", "合同版本"), ("runId", "Run"), ("runAttemptId", "Attempt"),
                       ("message", "判定说明"), ("missingPredicates", "缺口"), ("blockingPredicates", "阻断"),
                       ("acceptedEvidenceRefs", "已纳入证据")):
        value = contract.get(key)
        if value:
            text = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
            parts.append(f'<p {_P}>{label}：{escape(text)}</p>')
    return "".join(parts) + "</details>"


def _document(title: str, summary: str, day: str, sections: list[str], footer: str) -> str:
    # Inline core styles and one table column survive email client sanitizers.
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><style>details[open]>summary{{margin-bottom:8px}}'
        '@media(max-width:480px){.pad{padding:14px!important}.wrap{border-radius:0!important}}'
        '</style></head><body style="margin:0;background:#f4f6f8;color:#243142;'
        'font-family:Arial,\'Microsoft YaHei\',sans-serif;overflow-wrap:anywhere">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr><td align="center">'
        '<table class="wrap" role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        'style="max-width:640px;background:#fff;border-radius:10px">'
        '<tr><td class="pad" style="padding:18px 24px;border-bottom:2px solid #243f53">'
        f'<p style="margin:0 0 4px;font-size:12px;color:#697585">夜雨 Gamer · {escape(day)}</p>'
        f'<h1 style="margin:0;font-size:21px;line-height:1.4">{escape(title)}</h1>'
        f'<p style="margin:6px 0 0;font-size:13px;color:#516477">{escape(summary)}</p></td></tr>'
        f'{"".join(sections)}<tr><td class="pad" style="padding:12px 24px;background:#f7f9fb;'
        f'color:#697585;font-size:12px;line-height:1.6">{footer}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def render_batch_notification(*, game_day: str, batch_id: str, seal_version: int,
                              sealed_result: Mapping[str, Any]) -> RenderedBatchNotification:
    if not game_day.strip():
        raise ValueError("gameDay is required")
    if not batch_id.strip():
        raise ValueError("batchId is required")
    if seal_version < 1:
        raise ValueError("sealVersion must be positive")
    snapshot = sealed_result.get("todoSnapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("sealed batch lacks todoSnapshot")
    outcome = _text(sealed_result.get("notificationOutcome"))
    if outcome not in {"completed", "blocked"}:
        raise ValueError("sealed batch lacks typed notificationOutcome")
    match = _DATE_PATTERN.search(game_day)
    day = match.group(1) if match else game_day
    contracts = _contract_entries(sealed_result)
    coverage = {_target_id(item): item for item in _items(sealed_result.get("completionCoverage"))}
    targets = {_target_id(item): item for item in _items(sealed_result.get("accountTargets"))}
    blockers: dict[str, list[Mapping[str, Any]]] = {}
    for blocker in _items(sealed_result.get("notificationBlockers")):
        blockers.setdefault(_target_id(blocker), []).append(blocker)
    entries = [(str(key), _mapping(value)) for key, value in snapshot.items()]
    # Stable sorting retains the frozen order of accounts within a game.
    entries.sort(key=lambda entry: _integer(_mapping(entry[1].get("summary")).get("orderIndex"), 10_000))
    badges = {key: _badge(coverage.get(key, {}), contracts.get(key, {}), outcome, targets.get(key, {})) for key, _ in entries}
    accepted_count = sum(badge == "accepted" for badge in badges.values())
    game_count = len({_text(doc.get("gameId"), key) for key, doc in entries})
    scope = f"{game_count} 款游戏" + (f" · {len(entries)} 个账号任务" if game_count != len(entries) else "")
    summary_line = f"{scope} · 已验收 {accepted_count}/{len(entries)}"
    if len(entries) != accepted_count:
        summary_line += f" · 需关注 {len(entries) - accepted_count}"
    title = "每日全部完成" if outcome == "completed" else "每日验收 · 有待处理"
    subject = f"夜雨 Gamer 每日 {day} · {'全部完成' if outcome == 'completed' else '完成受阻 / 待验收'} · 已验收 {accepted_count}/{len(entries)}"
    text_lines = [subject, summary_line, ""]
    sections, report_sections = [], []
    for target_id, document in entries:
        summary = _mapping(document.get("summary"))
        game_id = _text(document.get("gameId"), target_id)
        target = targets.get(target_id, {})
        name = _text(summary.get("displayName"), game_id)
        if _text(target.get("accountLabel")) and target.get("accountLabel") != "当前账号":
            name += f" · {target['accountLabel']}"
        contract = contracts.get(target_id, {})
        badge = badges[target_id]
        label, color = _BADGES[badge]
        instances = sorted(_items(document.get("instances")), key=lambda item: _integer(item.get("orderIndex"), 10_000))
        done_titles = [_text(item.get("title"), _text(item.get("operation"), "未命名事项"))
                       for item in instances if item.get("status") == "completed"]
        completed, total = _integer(summary.get("requiredCompleted")), _integer(summary.get("requiredTotal"))
        done_label = "已完成" if badge == "accepted" else "已执行"
        done_line = f"{done_label}：" + ("、".join(done_titles) if done_titles else "暂无已完成事项")
        game_blockers = blockers.get(target_id, [])
        reason = next((_text(item.get("reason")) for item in game_blockers if _text(item.get("reason"))), _text(contract.get("message")))
        attention = _short_attention(badge, reason, completed, total)
        frames = _frames(sealed_result, game_id, target_id, contract)
        main_frame = _reward_frame(frames, contract, game_id, instances)
        if main_frame:
            reviewed = badge == "accepted" and main_frame.get("artifactId") in contract.get("acceptedEvidenceRefs", [])
            caption = "领取截图" + (" · 已验收" if reviewed else " · 待复核")
            captured = _beijing_time(main_frame.get("capturedAt"))
            if captured:
                caption += f" · {captured} 北京时间"
            figure, screenshot_note = _figure(main_frame, caption), caption + "（见邮件图片）"
        else:
            screenshot_note = "尚无领取截图" + ("；现场画面见附件。" if frames else "。")
            figure = f'<p style="margin:8px 0 0;color:#697585;font-size:12px">{escape(screenshot_note)}</p>'
        content = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td>'
            f'<h2 style="margin:0;font-size:17px;line-height:1.5">{escape(name)}</h2></td>'
            f'<td align="right" style="color:{color};font-size:13px;white-space:nowrap;padding-left:10px">{label}</td></tr></table>'
            f'<p {_P}>{escape(done_line)}</p>'
            f'<p style="margin:4px 0;color:#697585;font-size:12px">必做事项 {completed}/{total}</p>'
            + (f'<p style="margin:6px 0;color:{color};font-size:13px;line-height:1.6">{escape(attention)}</p>' if attention else "") + figure
        )
        start, end = '<tr><td class="pad" style="padding:16px 24px;border-bottom:1px solid #e6eaf0">', '</td></tr>'
        sections.append(start + content + end)
        report_sections.append(start + content + _details(target_id, instances, contract, game_blockers, frames, main_frame) + end)
        text_lines.extend([f"{name} · {label}", done_line, f"必做事项 {completed}/{total}"])
        if attention:
            text_lines.append(attention)
        text_lines.extend([screenshot_note, ""])
    if not entries:
        empty = '<tr><td class="pad" style="padding:16px 24px">本轮没有选中游戏。</td></tr>'
        sections.append(empty)
        report_sections.append(empty)
        text_lines.append("本轮没有选中游戏。")
    footer = '详细事项、其他截图和诊断记录见附件 <strong>daily-report.html</strong>，下载后用浏览器打开可逐项展开。'
    text_lines.append("详细事项、其他截图和诊断记录见附件 daily-report.html（下载后用浏览器打开）。")
    report_footer = (
        '<details><summary style="cursor:pointer">批次与证据说明</summary>'
        f'<p>Batch：{escape(batch_id)} · Seal：{seal_version} · 范围：{escape(game_day)}</p>'
        '<p>Todo 打勾是逐项事实，不替代每日完成证据合同。本报告使用封口时冻结的事项与证据；'
        '图片为邮件缩放副本，原图由本机 Manager 保留。发送重试不会重新运行游戏。</p></details>'
    )
    return RenderedBatchNotification(
        outcome=outcome, subject=subject, text_body="\n".join(text_lines),
        html_body=_document(title, summary_line, day, sections, footer),
        report_html=_document(title, summary_line, day, report_sections, report_footer),
    )
