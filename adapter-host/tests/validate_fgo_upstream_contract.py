#!/usr/bin/env python3
"""Static FGO upstream capability audit.

This script deliberately does not connect to ADB/BBchannel, launch an executable,
or send any input to the emulator.  It records why the current upstream tools are
not a safe selected-Todo execution binding.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


WATCHED_PROCESSES = {"bbchannel.exe", "dnplayer.exe"}
ANDROID = "{http://schemas.android.com/apk/res/android}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path) -> dict:
    value = json.loads(read(path))
    require(isinstance(value, dict), f"{path} must be a JSON object")
    return value


def process_snapshot() -> dict[str, set[int]]:
    result = subprocess.run(
        ["tasklist.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    snapshot = {name: set() for name in WATCHED_PROCESSES}
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2 or row[0].lower() not in snapshot:
            continue
        try:
            snapshot[row[0].lower()].add(int(row[1]))
        except ValueError:
            pass
    return snapshot


def git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git.exe", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
        text=True, encoding="utf-8", errors="strict", check=True,
    )
    commit = result.stdout.strip()
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), f"invalid Git commit for {root}")
    return commit


def audit_fga(root: Path) -> dict:
    manifest_path = root / "app/src/main/AndroidManifest.xml"
    battle_path = root / "scripts/src/main/java/io/github/fate_grand_automata/scripts/modules/Battle.kt"
    refill_path = root / "scripts/src/main/java/io/github/fate_grand_automata/scripts/modules/Refill.kt"
    auto_path = root / "scripts/src/main/java/io/github/fate_grand_automata/scripts/entrypoints/AutoBattle.kt"
    readme = read(root / "README.md")
    battle, refill, auto = read(battle_path), read(refill_path), read(auto_path)

    require("current screen" in readme and "PLAY button" in readme, "FGA screen-selected PLAY contract changed")
    require("shouldLimitRuns" in battle and "state.runs >=" in battle, "FGA exact run-limit guard is missing")
    require("ExitReason.LimitRuns(state.runs)" in battle, "FGA no longer exposes its internal completed-run count")
    require("ExitReason.APRanOut" in refill, "FGA AP-exhaustion stop is missing")
    require("class LimitRuns(val count: Int)" in auto, "FGA internal LimitRuns result changed")

    tree = ET.parse(manifest_path)
    application = tree.getroot().find("application")
    require(application is not None, "FGA Android application manifest is missing")
    components = []
    for tag in ("activity", "service", "receiver"):
        for item in application.findall(tag):
            actions = [node.get(ANDROID + "name", "") for node in item.findall("./intent-filter/action")]
            categories = [node.get(ANDROID + "name", "") for node in item.findall("./intent-filter/category")]
            components.append({
                "type": tag,
                "name": item.get(ANDROID + "name", ""),
                "exported": item.get(ANDROID + "exported"),
                "actions": actions,
                "categories": categories,
            })
    runner = next(item for item in components if item["name"] == ".runner.ScriptRunnerService")
    require(runner["exported"] != "true" and not runner["actions"], "FGA unexpectedly gained an exported runner API; re-review required")
    all_actions = {action for item in components for action in item["actions"]}
    all_categories = {category for item in components for category in item["categories"]}
    require("android.intent.action.VIEW" not in all_actions, "FGA gained a VIEW deep link; re-review required")
    require("android.intent.category.BROWSABLE" not in all_categories, "FGA gained a browsable deep link; re-review required")
    return {
        "commit": git_commit(root),
        "internalCapabilities": ["screen-detected-play", "limit-runs-with-count", "ap-ran-out-stop"],
        "externalSelectedTodoApi": False,
        "exportedRunner": False,
        "homeOrQuestNavigation": False,
        "dailyMissionClaim": False,
    }


def option_labels(value: object) -> list[str]:
    labels: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "label", "default"} and isinstance(child, str):
                labels.append(child)
            labels.extend(option_labels(child))
    elif isinstance(value, list):
        for child in value:
            labels.extend(option_labels(child))
    return labels


def audit_maafgo(root: Path) -> dict:
    options = load_json(root / "assets/options/迦勒底之门-副本类型.json")
    labels = option_labels(options)
    ap_labels = sorted({label for label in labels if re.search(r"(?:60|80)$", label)})
    require(ap_labels, "MaaFgo Chaldea Gate AP options were not found")
    require(not any(re.search(r"(?:^|\D)10(?:\D|$)", label) for label in labels), "MaaFgo now appears to contain a 10 AP route; re-review required")

    tasks = sorted(path.stem for path in (root / "assets/tasks").glob("*.json"))
    require(not any("御主任务" in name or "日常任务奖励" in name for name in tasks), "MaaFgo gained a daily mission task; re-review required")
    action = read(root / "agent/custom/bbc_action.py")
    require("set_run_times" in action and "start_battle" in action, "MaaFgo BBchannel bridge changed")
    require("'脚本停止' in popup_title" in action and "CustomAction.RunResult(success=True)" in action, "MaaFgo terminal mapping changed")
    require("completed_run_count" not in action and "completed_runs" not in action, "MaaFgo gained a structured completion count; re-review required")
    return {
        "commit": git_commit(root),
        "chaldeaGateApLabels": ap_labels,
        "hasTenApRoute": False,
        "hasDailyMissionClaimTask": False,
        "bbchannelSuccessUsesStopSignal": True,
        "structuredCompletedRunCount": False,
    }


def audit_bbchannel(root: Path) -> dict:
    server = root / "dist/BBchannel64/bbc_tcp_server.py"
    settings = root / "scripts_settings.json"
    source = read(server)
    config = load_json(settings)
    page = config.get("page0", {})
    require(page.get("server") == "CH", "BBchannel is not fixed to CN")
    require(page.get("clearAP") == 0 and page.get("allowOtherApple") == 0, "BBchannel AP recovery is not disabled")

    handler_match = re.search(r"HANDLERS\s*=\s*\{(?P<body>.*?)\n\s*\}", source, re.DOTALL)
    require(handler_match is not None, "BBchannel command registry is missing")
    handlers = sorted(set(re.findall(r"'([a-z_]+)'\s*:", handler_match.group("body"))))
    required = {"set_ap_recovery", "set_run_times", "start_battle", "get_status", "get_ui_status"}
    require(required.issubset(handlers), "BBchannel safe configuration/status commands changed")
    forbidden_missing = {"navigate_home", "select_quest", "claim_daily_missions", "get_completed_runs"}
    require(forbidden_missing.isdisjoint(handlers), "BBchannel gained a relevant command; re-review required")
    require('event_generate("<Button-1>"' in source, "BBchannel start_battle no longer maps to its current GUI button")
    require("'completed_runs'" not in source and "'completed_run_count'" not in source, "BBchannel gained a structured completion count; re-review required")
    return {
        "handlers": handlers,
        "safeConfiguration": {"server": "CH", "clearAP": 0, "allowOtherApple": 0},
        "canRequestThreeRuns": True,
        "canDisableApRecovery": True,
        "canNavigateHome": False,
        "canSelectTenApQuest": False,
        "canClaimDailyMissions": False,
        "structuredCompletedRunCount": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fga-root", type=Path, required=True)
    parser.add_argument("--maafgo-root", type=Path, required=True)
    parser.add_argument("--bbchannel-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    before = process_snapshot()
    fga = audit_fga(args.fga_root.resolve(strict=True))
    maafgo = audit_maafgo(args.maafgo_root.resolve(strict=True))
    bbchannel = audit_bbchannel(args.bbchannel_root.resolve(strict=True))
    after = process_snapshot()
    require(before == after, f"static audit changed watched processes: before={before}, after={after}")

    blockers = {
        "attach-home": ["no exported FGA runner/deep-link", "no authoritative home-scene result"],
        "three-10ap-quests": ["no 10 AP route in MaaFgo", "BBchannel starts only the current battle", "no structured completed-run count"],
        "daily-missions-4of4": ["no mission-panel navigation", "no structured 4/4 observation"],
        "claim-daily-missions": ["no daily-mission claim task or callable command", "no claim receipt"],
        "verify-reward-panel": ["no current-run raw mission-panel evidence contract"],
    }
    evidence = {
        "schemaVersion": 1,
        "suite": "fgo-upstream-selected-todo-static-audit",
        "status": "passed",
        "promotionDecision": "candidate-blocked",
        "executionReady": False,
        "gameStarted": False,
        "toolProcessStarted": False,
        "watchedProcessesUnchanged": {key: sorted(value) for key, value in before.items()},
        "upstreams": {"FGA": fga, "MaaFgo": maafgo, "BBchannel": bbchannel},
        "todoBlockers": blockers,
        "minimumPromotionGaps": [
            "typed home and exact 10 AP node navigation",
            "atomic configuration for exactly three runs with all AP recovery disabled",
            "structured actual completed-run count equal to three",
            "typed daily mission 4/4 observation and claim result",
            "same-run raw reward-panel evidence accepted by Manager",
        ],
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    args.evidence.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
