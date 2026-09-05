"""Render immutable batch-seal snapshots into safe mail bodies.

The renderer deliberately accepts only the frozen Batch result document.  It
does not read current Todo state, scan directories, resolve paths, or perform
transport work.  This keeps a retry byte-for-byte tied to the same seal.

Layout of one round mail:

1. banner: game day, terminal outcome, completed / review / failed counts;
2. one section per game: status badge, reason, the frozen screenshots the
   seal whitelisted for that game (inline ``cid:`` images), the Todo rows;
3. technical detail: blockers/next actions and the typed completion
   contracts, for the reader who wants to see why a game is not accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Mapping


_STATUS_LABELS = {
    "completed": ("✅", "已完成"),
    "skipped": ("⏭️", "策略跳过"),
    "blocked": ("⛔", "堵塞"),
    "review_required": ("⚠️", "待复核"),
    "human_required": ("👤", "等待人工"),
    "in_progress": ("🔄", "进行中"),
    "pending": ("⬜", "未到达"),
    "failed": ("❌", "失败"),
    "cancelled": ("🚫", "已取消"),
}

_GAME_BADGES = {
    "accepted": ("✅", "完成", "accepted"),
    "failed": ("❌", "失败", "failed"),
    "not_started": ("⬜", "本轮未执行", "notstarted"),
    "review": ("⚠️", "待复核", "review"),
}

_BEIJING = timezone(timedelta(hours=8))
_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True, slots=True)
class RenderedBatchNotification:
    """Transport-neutral, deterministic rendering of one batch seal."""

    outcome: str
    subject: str
    text_body: str
    html_body: str


def _text(value: object, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _integer(value: object, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    return value if isinstance(value, int) else fallback


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mail_outcome(seal: Mapping[str, Any]) -> str:
    """Read the immutable Manager decision; never infer it in the renderer."""

    outcome = _text(seal.get("notificationOutcome"))
    if outcome not in {"completed", "blocked"}:
        raise ValueError("sealed batch lacks typed notificationOutcome")
    return outcome


def _game_sort_key(entry: tuple[str, Mapping[str, Any]]) -> tuple[int, str]:
    game_id, document = entry
    summary = _mapping(document.get("summary"))
    return _integer(summary.get("orderIndex"), 10_000), game_id


def _display_game_day(game_day: str) -> str:
    """Show the calendar game day; the raw scope key stays in the detail."""

    match = _DATE_PATTERN.search(game_day)
    return match.group(1) if match else game_day


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


def _contract_entries(sealed_result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    contract_snapshot = sealed_result.get("completionContracts")
    entries: dict[str, Mapping[str, Any]] = {}
    if isinstance(contract_snapshot, Mapping):
        for game_id, value in contract_snapshot.items():
            entries[str(game_id)] = _mapping(value)
    elif isinstance(contract_snapshot, list):
        for value in contract_snapshot:
            if isinstance(value, Mapping):
                entries[_text(value.get("gameId"), "unknown")] = value
    return entries


def _game_badge(
    game_id: str,
    coverage: Mapping[str, Mapping[str, Any]],
    contracts: Mapping[str, Mapping[str, Any]],
    outcome: str,
) -> tuple[str, str, str]:
    if outcome == "completed":
        return _GAME_BADGES["accepted"]
    entry = coverage.get(game_id, {})
    issues = [str(item) for item in entry.get("issues", [])] if isinstance(entry.get("issues"), list) else []
    if entry.get("acceptedDone") is True:
        return _GAME_BADGES["accepted"]
    if "failed_run" in issues:
        return _GAME_BADGES["failed"]
    if "not_started_run" in issues or "missing_completion_contract" in issues:
        return _GAME_BADGES["not_started"]
    contract = contracts.get(game_id, {})
    contract_status = _text(contract.get("outcome"), _text(contract.get("status")))
    if contract.get("acceptedDone") is True or contract_status == "accepted_done":
        return _GAME_BADGES["accepted"]
    return _GAME_BADGES["review"]


def render_batch_notification(
    *,
    game_day: str,
    batch_id: str,
    seal_version: int,
    sealed_result: Mapping[str, Any],
) -> RenderedBatchNotification:
    """Render one immutable seal without consulting mutable Manager state."""

    if not game_day.strip():
        raise ValueError("gameDay is required")
    if not batch_id.strip():
        raise ValueError("batchId is required")
    if seal_version < 1:
        raise ValueError("sealVersion must be positive")

    todo_snapshot = sealed_result.get("todoSnapshot")
    if not isinstance(todo_snapshot, Mapping):
        raise ValueError("sealed batch lacks todoSnapshot")

    outcome = _mail_outcome(sealed_result)
    display_day = _display_game_day(game_day)
    contracts = _contract_entries(sealed_result)
    coverage = {
        _text(item.get("gameId"), "unknown"): item
        for item in _items(sealed_result.get("completionCoverage"))
    }
    mail_screenshots = _mapping(sealed_result.get("mailScreenshotsByGame"))
    whitelisted = {
        str(item)
        for item in sealed_result.get("sealEvidenceArtifactIds", [])
        if isinstance(item, str)
    }

    snapshot_entries = [
        (str(game_id), _mapping(document))
        for game_id, document in todo_snapshot.items()
        if isinstance(game_id, str)
    ]
    snapshot_entries.sort(key=_game_sort_key)

    badges = {
        game_id: _game_badge(game_id, coverage, contracts, outcome)
        for game_id, _ in snapshot_entries
    }
    accepted_count = sum(1 for badge in badges.values() if badge[2] == "accepted")
    failed_count = sum(1 for badge in badges.values() if badge[2] == "failed")
    not_started_count = sum(1 for badge in badges.values() if badge[2] == "notstarted")
    review_count = len(badges) - accepted_count - failed_count - not_started_count
    total = len(badges)

    outcome_label = "全部完成" if outcome == "completed" else "完成受阻 / 待复核"
    summary_bits = [f"完成 {accepted_count}/{total}"]
    if review_count:
        summary_bits.append(f"待复核 {review_count}")
    if failed_count:
        summary_bits.append(f"失败 {failed_count}")
    if not_started_count:
        summary_bits.append(f"未开始 {not_started_count}")
    summary_line = " · ".join(summary_bits)
    subject = f"夜雨 Gamer 每日 {display_day} · {outcome_label} · {summary_line}"

    text_lines = [
        subject,
        f"游戏日：{display_day}（范围 {game_day}）",
        f"Batch：{batch_id}",
        f"Seal：{seal_version}",
        summary_line,
        "",
        "Todo 打勾是逐项事实，不替代每日完成证据合同。",
    ]
    html_sections: list[str] = []

    blocker_entries = _items(sealed_result.get("notificationBlockers"))
    blockers_by_game: dict[str, list[Mapping[str, Any]]] = {}
    for blocker in blocker_entries:
        blockers_by_game.setdefault(_text(blocker.get("gameId"), "unknown"), []).append(blocker)

    for game_id, document in snapshot_entries:
        summary = _mapping(document.get("summary"))
        instances = sorted(
            _items(document.get("instances")),
            key=lambda item: (
                _integer(item.get("orderIndex"), 10_000),
                _text(item.get("title")),
            ),
        )
        completed = _integer(summary.get("requiredCompleted"))
        required_total = _integer(summary.get("requiredTotal"))
        display_name = _text(summary.get("displayName"), game_id)
        icon, label, badge_class = badges[game_id]
        contract = contracts.get(game_id, {})
        game_blockers = blockers_by_game.get(game_id, [])
        reason = _text(contract.get("message"))
        if game_blockers:
            reason = _text(game_blockers[0].get("reason"), reason)
        if badge_class == "notstarted":
            # No GameRun of this batch executed the game; explain it from the
            # frozen Todo facts instead of the batch-level failure sentence.
            remaining = max(0, required_total - completed)
            reason = (
                "所有必做 Todo 已在早前完成，本轮跳过。"
                if required_total and remaining == 0
                else f"本轮没有为该游戏启动运行；仍有 {remaining} 项必做 Todo 未完成或待复核。"
            )
        missing = (
            [str(value) for value in contract.get("missingPredicates", [])]
            if isinstance(contract.get("missingPredicates"), list)
            else []
        )
        blocking = (
            [str(value) for value in contract.get("blockingPredicates", [])]
            if isinstance(contract.get("blockingPredicates"), list)
            else []
        )

        text_lines.extend(
            [
                "",
                f"{icon} {display_name} / {game_id} · {label}",
                f"Required Todo：{completed}/{required_total}",
            ]
        )
        if reason and badge_class != "accepted":
            text_lines.append(f"   原因：{reason}")
        if blocking:
            text_lines.append(f"   阻断：{', '.join(blocking)}")
        if missing and badge_class != "accepted":
            text_lines.append(f"   缺口：{', '.join(missing)}")

        frames = [
            item
            for item in _items(mail_screenshots.get(game_id))
            if _text(item.get("artifactId")) in whitelisted
        ]
        figure_rows: list[str] = []
        if frames:
            text_lines.append(f"   截图 {len(frames)} 张（见邮件内嵌图）：")
            for frame in frames:
                artifact_id = _text(frame.get("artifactId"))
                role = _text(frame.get("role"), "现场画面")
                operation = _text(frame.get("operation"))
                captured = _beijing_time(frame.get("capturedAt"))
                caption_bits = [role]
                if operation:
                    caption_bits.append(operation)
                if captured:
                    caption_bits.append(f"{captured} 北京时间")
                caption = " · ".join(caption_bits)
                text_lines.append(f"   - {caption} · evidence-{artifact_id}")
                figure_rows.append(
                    "<figure class=\"frame\">"
                    f"<img src=\"cid:evidence-{escape(artifact_id)}\" alt=\"{escape(caption)}\">"
                    f"<figcaption>{escape(caption)}</figcaption>"
                    "</figure>"
                )
        else:
            unavailable = next(
                (
                    _text(blocker.get("screenshotUnavailableReason"))
                    for blocker in game_blockers
                    if _text(blocker.get("screenshotUnavailableReason"))
                ),
                "",
            )
            note = "本轮没有可用的现场截图" + (f"：{unavailable}" if unavailable else "。")
            text_lines.append(f"   {note}")
            figure_rows.append(f"<p class=\"todo-missing\">{escape(note)}</p>")

        rows: list[str] = []
        for item in instances:
            status = _text(item.get("status"), "pending")
            todo_icon, todo_label = _STATUS_LABELS.get(status, ("❔", status or "unknown"))
            title = _text(item.get("title"), _text(item.get("operation"), "未命名 Todo"))
            attempts = _integer(item.get("attempts"))
            todo_reason = _text(item.get("reason"))
            difficulty = _text(item.get("automationDifficulty"), "unknown")
            dispatch = _text(item.get("dispatchDisposition"), "unknown")
            dispatch_reason = _text(item.get("dispatchReason"))
            latest_attempt = _mapping(item.get("latestTodoAttempt"))
            latest_assessment = _mapping(item.get("latestAutomationAssessment"))
            text_line = f"   {todo_icon} {title} · {todo_label} · 尝试 {attempts} · 难度 {difficulty}"
            if dispatch != "unknown":
                text_line += f" · 调度 {dispatch}"
            if todo_reason:
                text_line += f" · {todo_reason}"
            if dispatch_reason:
                text_line += f" · {dispatch_reason}"
            if latest_attempt:
                text_line += (
                    " · 最近尝试 "
                    f"{_text(latest_attempt.get('state'), 'unknown')}"
                    f"/{_text(latest_attempt.get('reasonCode'), 'none')}"
                )
            automatable_label = ""
            if latest_assessment:
                automatable = latest_assessment.get("automatable")
                automatable_label = (
                    "可自动化"
                    if automatable is True
                    else "不宜自动化"
                    if automatable is False
                    else "自动化可行性未知"
                )
                assessment_issue = _text(latest_assessment.get("issue"))
                assessment_recommendation = _text(latest_assessment.get("recommendation"))
                text_line += (
                    " · Agent 判断 "
                    f"{_text(latest_assessment.get('difficulty'), 'unknown')}"
                    f"/{automatable_label}"
                )
                if assessment_issue:
                    text_line += f" · 问题 {assessment_issue}"
                if assessment_recommendation:
                    text_line += f" · 建议 {assessment_recommendation}"
            text_lines.append(text_line)
            detail = (
                f"<span class=\"todo-meta\">{escape(todo_label)} · 尝试 {attempts} · "
                f"难度 {escape(difficulty)} · 调度 {escape(dispatch)}</span>"
            )
            if todo_reason:
                detail += f"<div class=\"todo-reason\">{escape(todo_reason)}</div>"
            if dispatch_reason:
                detail += f"<div class=\"todo-reason\">调度依据：{escape(dispatch_reason)}</div>"
            if latest_attempt:
                detail += (
                    "<div class=\"todo-attempt\">最近尝试："
                    f"{escape(_text(latest_attempt.get('state'), 'unknown'))} · "
                    f"{escape(_text(latest_attempt.get('reasonCode'), 'none'))} · "
                    f"{escape(_text(latest_attempt.get('reason')))}</div>"
                )
            if latest_assessment:
                detail += (
                    "<div class=\"todo-assessment\">Agent 判断："
                    f"{escape(_text(latest_assessment.get('difficulty'), 'unknown'))} · "
                    f"{escape(automatable_label)} · "
                    f"问题：{escape(_text(latest_assessment.get('issue')))} · "
                    f"建议：{escape(_text(latest_assessment.get('recommendation')))}</div>"
                )
            rows.append(
                "<li>"
                f"<span class=\"todo-icon\">{todo_icon}</span>"
                f"<strong>{escape(title)}</strong>{detail}"
                "</li>"
            )
        if not rows:
            rows.append("<li>本轮封口快照没有 Todo 项。</li>")

        reason_html = (
            f"<p class=\"reason\">{escape(reason)}</p>"
            if reason and badge_class != "accepted"
            else ""
        )
        predicate_html = ""
        if badge_class != "accepted" and (blocking or missing):
            predicate_html = "<p class=\"predicates\">"
            if blocking:
                predicate_html += f"阻断：{escape(', '.join(blocking))}"
            if blocking and missing:
                predicate_html += " · "
            if missing:
                predicate_html += f"缺口：{escape(', '.join(missing))}"
            predicate_html += "</p>"
        html_sections.append(
            f"<section class=\"game {badge_class}\">"
            f"<h2><span class=\"badge {badge_class}\">{icon} {escape(label)}</span> "
            f"{escape(display_name)} <small>{escape(game_id)}</small></h2>"
            f"<p class=\"progress\">Required Todo：{completed}/{required_total}</p>"
            f"{reason_html}{predicate_html}"
            f"<div class=\"frames\">{''.join(figure_rows)}</div>"
            f"<ul>{''.join(rows)}</ul>"
            "</section>"
        )

    if not snapshot_entries:
        text_lines.extend(["", "本轮封口快照没有选中游戏。"])
        html_sections.append("<section class=\"game\"><p>本轮封口快照没有选中游戏。</p></section>")

    text_lines.extend(["", "阻塞与下一步："])
    blocker_rows: list[str] = []
    if blocker_entries:
        for blocker in blocker_entries:
            game_id = _text(blocker.get("gameId"), "unknown")
            kind = _text(blocker.get("kind"), "technical")
            reason = _text(blocker.get("reason"), "未提供原因")
            next_action = _text(blocker.get("nextAction"), "等待 Agent 复核")
            screenshot_unavailable = _text(blocker.get("screenshotUnavailableReason"))
            line = f"- {game_id} · {kind} · {reason} · 下一步：{next_action}"
            if screenshot_unavailable:
                line += f" · 截图缺失：{screenshot_unavailable}"
            text_lines.append(line)
            blocker_rows.append(
                "<li>"
                f"<strong>{escape(game_id)}</strong> · {escape(kind)}"
                f"<div class=\"todo-reason\">{escape(reason)}</div>"
                f"<div class=\"todo-next\">下一步：{escape(next_action)}</div>"
                + (
                    "<div class=\"todo-missing\">"
                    f"截图缺失：{escape(screenshot_unavailable)}</div>"
                    if screenshot_unavailable
                    else ""
                )
                + "</li>"
            )
    else:
        text_lines.append("- 无。")
        blocker_rows.append("<li>无。</li>")

    text_lines.extend(["", "Completion Contract："])
    contract_rows: list[str] = []
    if contracts:
        for game_id in sorted(contracts):
            contract = contracts[game_id]
            status = _text(
                contract.get("outcome"),
                _text(contract.get("status"), "evidence_pending"),
            )
            version = _text(contract.get("contractVersion"), "completion-contract.v1")
            missing = (
                [str(value) for value in contract.get("missingPredicates", [])]
                if isinstance(contract.get("missingPredicates"), list)
                else []
            )
            line = f"- {game_id}: {status} · {version}"
            if missing:
                line += f" · 缺口 {', '.join(missing)}"
            text_lines.append(line)
            contract_rows.append(
                "<li>"
                f"<strong>{escape(game_id)}</strong> · {escape(status)} · {escape(version)}"
                + (f"<div class=\"todo-reason\">缺口：{escape(', '.join(missing))}</div>" if missing else "")
                + "</li>"
            )
    else:
        text_lines.append("- 本轮封口未提供完成合同快照；不能据此宣称完成。")
        contract_rows.append("<li>本轮封口未提供完成合同快照；不能据此宣称完成。</li>")

    html_sections.append(
        "<section class=\"detail\"><h2>阻塞与下一步</h2>"
        f"<ul>{''.join(blocker_rows)}</ul>"
        "<h2>Completion Contract</h2>"
        f"<ul>{''.join(contract_rows)}</ul></section>"
    )

    banner_class = "completed" if outcome == "completed" else "blocked"
    html_body = (
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
        "body{margin:0;background:#f3f6fa;color:#172033;font-family:'Microsoft YaHei',Arial,sans-serif}"
        ".card{max-width:860px;margin:20px auto;background:#fff;border:1px solid #dfe7f0;border-radius:14px;overflow:hidden}"
        ".banner{padding:22px 26px;color:#fff}.banner.completed{background:#176b4b}.banner.blocked{background:#8a4b13}"
        ".banner h1{margin:0 0 8px;font-size:21px}.banner p{margin:4px 0;font-size:13px}"
        ".notice{margin:18px 26px;padding:12px 14px;background:#eef5ff;border-radius:9px;font-size:13px}"
        ".game,.detail{padding:18px 26px;border-top:1px solid #e7edf4}.game h2,.detail h2{margin:0 0 7px;font-size:17px}"
        ".game h2 small{color:#708094;font-size:12px;font-weight:400}.progress{font-weight:700;margin:4px 0}"
        ".badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:13px;margin-right:6px;color:#fff}"
        ".badge.accepted{background:#176b4b}.badge.failed{background:#a13a2a}.badge.review{background:#a26a12}.badge.notstarted{background:#6b7785}"
        ".reason{margin:4px 0;color:#7d3e22;font-size:13px;white-space:pre-wrap}.predicates{margin:4px 0;color:#526b82;font-size:12px}"
        ".frames{margin:10px 0}.frame{margin:0 0 12px}.frame img{max-width:100%;border:1px solid #d5dee8;border-radius:8px;display:block}"
        ".frame figcaption{color:#607086;font-size:12px;margin-top:4px}"
        "ul{padding:0;list-style:none}li{padding:8px 0;border-top:1px solid #edf1f5}"
        ".todo-icon{margin-right:8px}.todo-meta{display:block;margin:4px 0 0 27px;color:#607086;font-size:12px}"
        ".todo-reason,.todo-next,.todo-missing,.todo-attempt,.todo-assessment{margin:4px 0 0 27px;font-size:12px;white-space:pre-wrap}"
        ".todo-reason,.todo-missing{color:#7d3e22}.todo-next{color:#365f8c}.todo-attempt{color:#526b82}.todo-assessment{color:#475f36}"
        ".detail{background:#f8fafc;font-size:13px}"
        ".foot{padding:15px 26px;background:#f8fafc;color:#69778a;font-size:12px}"
        "</style></head><body><main class=\"card\">"
        f"<header class=\"banner {banner_class}\"><h1>{escape(outcome_label)} · {escape(summary_line)}</h1>"
        f"<p>游戏日：{escape(display_day)}</p><p>Batch：{escape(batch_id)} · Seal：{seal_version} · 范围 {escape(game_day)}</p></header>"
        "<p class=\"notice\">Todo 打勾是逐项事实，不替代每日完成证据合同。截图为封口时冻结的本轮现场画面，邮件内为缩放副本，原图保留在本机证据目录。</p>"
        f"{''.join(html_sections)}"
        "<footer class=\"foot\">本邮件只使用批次封口时冻结的 Todo 与证据引用；发送重试不会重新运行游戏。</footer>"
        "</main></body></html>"
    )
    return RenderedBatchNotification(
        outcome=outcome,
        subject=subject,
        text_body="\n".join(text_lines),
        html_body=html_body,
    )
