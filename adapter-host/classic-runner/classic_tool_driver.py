from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable


PGR_TASKS = {
    "attach-home": "进入游戏",
    "claim-serum": "领取体力",
    "dorm": "宿舍任务",
    "simulation-field": "拟战场域",
    "maintainer-action": "维系者行动",
    "claim-daily-tasks": "领取任务",
    "battle-pass-free-track": "战令",
}
PGR_DORM_NO_EFFECT_MARKER = "YEYU_PGR_DORM_INTERACTION_NO_EFFECT"

ZZZ_APPS = {
    "coffee": "coffee",
    "scratch-card": "scratch_card",
    "trigrams-collection": "trigrams_collection",
    "suibian-temple": "suibian_temple",
    "random-play": "random_play",
    "charge-plan": "charge_plan",
    "city-fund-free-claim": "city_fund",
    "engagement-reward": "engagement_reward",
}

NIKKE_TREES = {
    "attach-lobby": "nikke_return_lobby.json",
    "outpost": "nikke_outpost.json",
    "dispatch-friend": "nikke_dispatch_friend.json",
}


def _process_lineage_pids(
    process_factory: Callable[[int], Any],
    pid: int,
) -> set[int]:
    """Return the current process and every live ancestor PID.

    The ZZZ virtual-environment launcher can insert one or more Python shim
    processes above this driver.  They live under the same tool root as the
    formal GUI, so path-and-start-time cleanup must protect the complete
    driver lineage rather than only ``os.getpid()``.
    """

    lineage: set[int] = set()
    current = process_factory(pid)
    while current is not None:
        current_pid = int(current.pid)
        if current_pid in lineage:
            break
        lineage.add(current_pid)
        current = current.parent()
    return lineage


def _emit(stage_file: Path, operation: str, state: str, detail: str) -> None:
    record = {"operation": operation, "state": state, "detail": detail}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    for attempt in range(40):
        try:
            with stage_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


_JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.DOTALL)


def _loads_jsonc(raw: str) -> Any:
    without_comments = _JSONC_TOKEN.sub(
        lambda match: match.group(0) if match.group(0).startswith('"') else "",
        raw,
    )
    without_trailing_commas = re.sub(r",(?=\s*[}\]])", "", without_comments)
    return json.loads(without_trailing_commas)


def _selected_operations(game_id: str) -> list[str]:
    raw = os.environ.get("YEYU_GAMER_SELECTED_OPERATIONS", "")
    value = json.loads(raw)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError("selected_operations_invalid")
    allowed = {
        "PGR": set(PGR_TASKS),
        "ZZZ": {"attach-home", *ZZZ_APPS},
        "NIKKE": set(NIKKE_TREES),
    }[game_id]
    if len(value) != len(set(value)) or any(item not in allowed for item in value):
        raise ValueError("selected_operations_not_allowed")
    return value


def _tail_from(path: Path, start: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(min(start, path.stat().st_size))
        return stream.read().decode("utf-8", errors="replace")


def _log_checkpoint(path: Path) -> tuple[int, bytes]:
    """Bind a read cursor to the current log generation.

    Several formal GUI updaters truncate and rebuild ``gui.log`` in place.
    A byte offset alone then points at the end of the new file and silently
    misses every fresh lifecycle marker. The stable prefix distinguishes an
    append from a replacement without treating stale prior-run text as fresh.
    """

    if not path.is_file():
        return 0, b""
    with path.open("rb") as stream:
        prefix = stream.read(512)
    return path.stat().st_size, prefix


def _tail_since(path: Path, checkpoint: tuple[int, bytes]) -> str:
    if not path.is_file():
        return ""
    start, original_prefix = checkpoint
    with path.open("rb") as stream:
        current_prefix = stream.read(len(original_prefix))
    current_size = path.stat().st_size
    effective_start = start if current_size >= start and current_prefix == original_prefix else 0
    return _tail_from(path, effective_start)


def _pgr_profile(base: dict[str, Any], selected: list[str], config_id: str) -> dict[str, Any]:
    profile = json.loads(json.dumps(base, ensure_ascii=False))
    profile["item_id"] = config_id
    profile["name"] = f"YeYu Gamer selected daily {config_id}"
    selected_names = {PGR_TASKS[item] for item in selected}
    automation_names = set(PGR_TASKS.values())
    seen: set[str] = set()
    for task in profile.get("tasks", []):
        name = task.get("name")
        if name == "Controller":
            # ScreenDC is a foreground-desktop capture method.  When Codex or
            # the formal FOS window is in front it records that window while
            # claiming to be attached to PGR.  Generate an isolated run profile
            # that uses PrintWindow for stable capture.  Keep the upstream
            # Seize input method: the current PGR client accepts its real
            # window input, while SendMessageWithWindowPos can report a
            # successful click without changing the game UI.
            task["is_checked"] = True
            options = task.get("task_option")
            if not isinstance(options, dict):
                raise RuntimeError("pgr_controller_options_missing")
            foreground = options.get("Win32-Window-Front")
            if not isinstance(foreground, dict):
                raise RuntimeError("pgr_foreground_controller_missing")
            background = json.loads(json.dumps(foreground, ensure_ascii=False))
            background["win32_screencap_methods"] = 16  # PrintWindow
            background["mouse_input_methods"] = 1  # Seize
            background["keyboard_input_methods"] = 1
            options["controller_type"] = "Win32-Window-Background"
            options["Win32-Window-Background"] = background
            continue
        if name == "Resource":
            task["is_checked"] = True
            continue
        if name in selected_names:
            task["is_checked"] = True
            seen.add(name)
            options = task.get("task_option")
            if isinstance(options, dict):
                options.pop("_speedrun_state", None)
                speedrun = options.get("_speedrun_config")
                if isinstance(speedrun, dict):
                    speedrun["enabled"] = False
        elif name in automation_names or name not in {"Controller", "Resource"}:
            task["is_checked"] = False
    missing = selected_names - seen
    if missing:
        raise RuntimeError(f"pgr_task_definition_missing:{sorted(missing)}")
    return profile


class PgrProgress:
    def __init__(
        self,
        completed: list[str],
        flow_done: bool,
        failure: str | None,
        failure_operation: str | None,
        recovered_failures: dict[str, str],
        recoverable_operation: str | None,
        recoverable_marker: str | None,
    ) -> None:
        self.completed = completed
        self.flow_done = flow_done
        self.failure = failure
        self.failure_operation = failure_operation
        self.recovered_failures = recovered_failures
        self.recoverable_operation = recoverable_operation
        self.recoverable_marker = recoverable_marker


def _parse_pgr_progress(text: str, selected: list[str]) -> PgrProgress:
    task_to_operation = {name: operation for operation, name in PGR_TASKS.items() if operation in selected}
    completed: list[str] = []
    current: str | None = None
    flow_done = False
    failure: str | None = None
    failure_operation: str | None = None
    recovered_failures: dict[str, str] = {}
    recoverable_operation: str | None = None
    recoverable_marker: str | None = None
    start_pattern = re.compile(r"任务 '([^']+)' 的执行信息")
    recoverable_pattern = re.compile(r"(识别错误|識別錯誤).*(返回主菜单|返回主菜單)")
    fatal_pattern = re.compile(r"严重错误|嚴重錯誤|Traceback|Exception", re.IGNORECASE)
    task_failed_pattern = re.compile(r"任务.*失败|任務.*失敗|Task.*Failed", re.IGNORECASE)
    current_failed = False
    for line in text.splitlines():
        match = start_pattern.search(line)
        if match:
            if recoverable_operation and recoverable_operation not in completed:
                recovered_failures.setdefault(
                    recoverable_operation,
                    f"MPA recovered to main menu without completing {recoverable_operation}: {recoverable_marker}",
                )
                current_failed = False
            current = task_to_operation.get(match.group(1))
            recoverable_operation = None
            recoverable_marker = None
            current_failed = False
            continue
        if current == "dorm" and PGR_DORM_NO_EFFECT_MARKER in line:
            failure = "dorm_interaction_no_effect: original dorm task completion was not observed"
            failure_operation = current
            current_failed = True
            recoverable_operation = None
            recoverable_marker = None
            continue
        if current and recoverable_pattern.search(line):
            recoverable_operation = current
            recoverable_marker = line[-400:]
            current_failed = True
            continue
        if current and fatal_pattern.search(line):
            failure = line[-400:]
            failure_operation = current
            current_failed = True
        elif current and task_failed_pattern.search(line):
            recovered_failures.setdefault(current, "MPA task failure marker: " + line[-400:])
            current_failed = True
        if current and "WHEN_TASK_SUCCESS" in line and failure_operation != current and (
            not current_failed or recoverable_operation == current
        ):
            if current not in completed:
                completed.append(current)
            if recoverable_operation == current:
                recoverable_operation = None
                recoverable_marker = None
            current = None
        if "TASK_FLOW_STOP manual=False" in line or "所有任务都已完成" in line or "所有任務都已完成" in line:
            if recoverable_operation and recoverable_operation not in completed:
                recovered_failures.setdefault(
                    recoverable_operation,
                    f"MPA recovered to main menu without completing {recoverable_operation}: {recoverable_marker}",
                )
                recoverable_operation = None
                recoverable_marker = None
            flow_done = True
    return PgrProgress(
        completed,
        flow_done,
        failure,
        failure_operation,
        recovered_failures,
        recoverable_operation,
        recoverable_marker,
    )


def _parse_pgr_started_operations(text: str, selected: list[str]) -> list[str]:
    """Return fixed operations in the order MPA actually entered them.

    Do not announce every selected Todo when the tool process starts: Manager
    uses the structured start boundary to capture that individual step's
    before-frame.  Emitting starts up front made all screenshots point at the
    same initial screen and could not prove any per-step transition.
    """

    task_to_operation = {
        name: operation
        for operation, name in PGR_TASKS.items()
        if operation in selected
    }
    result: list[str] = []
    for match in re.finditer(r"任务 '([^']+)' 的执行信息", text):
        operation = task_to_operation.get(match.group(1))
        if operation and operation not in result:
            result.append(operation)
    return result


def _env_positive_int(name: str, default: int, minimum: int = 1, maximum: int = 3600) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value


def _pgr_abort_reason(operation: str, cascade_from: str, marker: str) -> str:
    if operation == cascade_from:
        return f"MPA failure marker: {marker}"
    return f"cascaded_from={cascade_from}; MPA failure marker: {marker}; not_attempted={operation}"


def _patch_pgr_home_handoff(tool_root: Path) -> None:
    """Keep FOS' launch task on the actionable home UI.

    The current MAA_Punish launch definition recognizes the home screen and
    then tries ``切换主界面状态`` before ending the task.  On the current PGR
    client that button opens the full-screen character interaction, so every
    following daily task clicks into the animation instead of its menu item.
    Apply a narrow post-update compatibility patch to the trusted formal tool:
    preserve its Notice action but end at ``空任务`` once home is recognized.
    """

    task_path = tool_root / "tasks" / "进入游戏.json"
    if not task_path.is_file():
        raise RuntimeError("pgr_launch_task_definition_missing")
    document = json.loads(task_path.read_text(encoding="utf-8"))
    tasks = document.get("task")
    if not isinstance(tasks, list):
        raise RuntimeError("pgr_launch_task_definition_invalid")
    launch = next((item for item in tasks if isinstance(item, dict) and item.get("name") == "进入游戏"), None)
    override = launch.get("pipeline_override") if isinstance(launch, dict) else None
    home = override.get("检查主界面2") if isinstance(override, dict) else None
    action = home.get("action") if isinstance(home, dict) else None
    param = action.get("param") if isinstance(action, dict) else None
    if (
        not isinstance(param, dict)
        or action.get("type") != "Custom"
        or param.get("custom_action") != "Notice"
    ):
        raise RuntimeError("pgr_launch_home_handoff_identity_changed")
    if "next" in home and home.get("next") not in (
        None,
        ["切换主界面状态"],
        ["空任务"],
    ):
        raise RuntimeError("pgr_launch_home_handoff_requires_review")
    home["next"] = ["空任务"]

    # PGR 4.7.10 replaced the small top-left age prompt used by the upstream
    # Start_up pipeline with a centred "点击任意处进入游戏" prompt.  Keep this
    # compatibility override in the formal task document so it is applied only
    # after FOS has completed its visible update/relaunch, and so a future
    # upstream node change remains reviewable instead of being patched blindly.
    launch_override = override.get("进入游戏")
    if launch_override is not None and not isinstance(launch_override, dict):
        raise RuntimeError("pgr_launch_title_override_requires_review")
    if launch_override is None:
        launch_override = {}
        override["进入游戏"] = launch_override
    launch_override["recognition"] = {
        "type": "OCR",
        "param": {
            "roi": [250, 550, 780, 160],
            "expected": [".*进入游戏.*", ".*点击任意处.*"],
        },
    }
    launch_override["action"] = {
        "type": "Click",
        "param": {"target": [640, 660]},
    }
    temporary = task_path.with_name(task_path.name + ".yeyu.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    os.replace(temporary, task_path)

    # The same PGR update also shifted the home-screen "战斗" label left of
    # MPA's old x=999 crop.  Patch only the two known upstream OCR nodes and
    # only while their identity still matches the audited definition.  This is
    # deliberately applied after the visible FOS updater so each formal run
    # repairs an update that restores the old resource file.
    start_up_path = tool_root / "resource" / "base" / "pipeline" / "Start_up.jsonc"
    if not start_up_path.is_file():
        raise RuntimeError("pgr_startup_pipeline_missing")
    start_up = _loads_jsonc(start_up_path.read_text(encoding="utf-8"))
    old_roi = [999, 231, 281, 187]
    current_roi = [940, 220, 300, 220]
    for node_name in ("检查主界面", "检查主界面2"):
        node = start_up.get(node_name)
        recognition = node.get("recognition") if isinstance(node, dict) else None
        recognition_param = recognition.get("param") if isinstance(recognition, dict) else None
        if (
            recognition.get("type") != "OCR"
            or not isinstance(recognition_param, dict)
            or recognition_param.get("expected") != "^战斗$"
            or recognition_param.get("roi") not in (old_roi, current_roi)
        ):
            raise RuntimeError("pgr_home_ocr_identity_changed")
        recognition_param["roi"] = current_roi
    popup = start_up.get("关闭点击空白弹窗")
    popup_recognition = popup.get("recognition") if isinstance(popup, dict) else None
    popup_action = popup.get("action") if isinstance(popup, dict) else None
    popup_action_param = popup_action.get("param") if isinstance(popup_action, dict) else None
    popup_expected = popup_recognition.get("param", {}).get("any_of") if isinstance(popup_recognition, dict) else None
    if (
        popup_recognition.get("type") != "Or"
        or popup_action.get("type") != "Click"
        or not isinstance(popup_action_param, dict)
        or popup_action_param.get("target") not in ([10, 10], [640, 680])
        or not isinstance(popup_expected, list)
        or "关闭公告" not in popup_expected
    ):
        raise RuntimeError("pgr_blank_popup_identity_changed")
    popup_action_param["target"] = [640, 680]
    start_up_temporary = start_up_path.with_name(start_up_path.name + ".yeyu.tmp")
    start_up_temporary.write_text(
        json.dumps(start_up, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    os.replace(start_up_temporary, start_up_path)

    # The current PGR dorm daily can leave one safe interaction objective after
    # MAA_Punish has completed its event/dispatch routine.  Upstream currently
    # waits only for "今日任务已完成" or "一键领取", so the visible active "前往"
    # button times out even though it leads to the cost-free companion petting
    # objective.  Inject this audited branch after the formal updater has run;
    # the original task page remains the verifier, and the branch cannot fire
    # unless the active lower-row button is actually recognized.  "前往" lands
    # on the dorm base map, not inside the room, so enter the visible first-room
    # card before interacting with its companion.
    dorm_path = (
        tool_root
        / "resource"
        / "base"
        / "pipeline"
        / "Dorm"
        / "Dorm_Task.jsonc"
    )
    if not dorm_path.is_file():
        raise RuntimeError("pgr_dorm_pipeline_missing")
    dorm = _loads_jsonc(dorm_path.read_text(encoding="utf-8"))
    claim = dorm.get("宿舍任务_领取")
    next_nodes = claim.get("next") if isinstance(claim, dict) else None
    expected_next = [
        "今日任务已完成",
        "[JumpBack]一键领取_宿舍任务",
        "[JumpBack]关闭奖励",
        "[JumpBack]打开宿舍任务",
        "[JumpBack]选择每日任务",
    ]
    patched_next = expected_next[:-1] + [
        "[JumpBack]夜雨_宿舍任务_前往互动",
        expected_next[-1],
    ]
    if next_nodes not in (expected_next, patched_next):
        raise RuntimeError("pgr_dorm_claim_identity_changed")
    claim["timeout"] = 90000
    claim["next"] = patched_next

    dorm["夜雨_宿舍任务_前往互动"] = {
        "recognition": {
            "type": "OCR",
            "param": {
                "roi": [1000, 220, 270, 260],
                "expected": "^前往$",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 3000,
        "next": ["夜雨_宿舍任务_进入一号宿舍"],
    }
    dorm["夜雨_宿舍任务_进入一号宿舍"] = {
        "action": {
            "type": "Click",
            "param": {"target": [620, 565]},
        },
        "post_delay": 3500,
        "next": ["夜雨_宿舍任务_房间截图"],
    }
    dorm["夜雨_宿舍任务_房间截图"] = {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "ScreenShot",
                "custom_action_param": {"type": "PGR_DormRoomBefore"},
            },
        },
        "post_delay": 500,
        "next": ["夜雨_宿舍任务_选择右侧伙伴1"],
    }
    # The residents move around the room, so clicking a world-space position is
    # inherently flaky.  The dorm UI provides fixed companion portraits on the
    # right edge; selecting one opens a stable quick-action menu.
    portrait_rows = (310, 410, 510)
    for portrait_index, portrait_y in enumerate(portrait_rows, start=1):
        select_name = f"夜雨_宿舍任务_选择右侧伙伴{portrait_index}"
        capture_name = f"夜雨_宿舍任务_伙伴快捷菜单截图{portrait_index}"
        fallback = (
            f"夜雨_宿舍任务_选择右侧伙伴{portrait_index + 1}"
            if portrait_index < len(portrait_rows)
            else "夜雨_宿舍任务_点击房间伙伴1"
        )
        dorm[select_name] = {
            "action": {
                "type": "Click",
                "param": {"target": [1214, portrait_y]},
            },
            "post_delay": 700,
            "next": [capture_name],
        }
        dorm[capture_name] = {
            "action": {
                "type": "Custom",
                "param": {
                    "custom_action": "ScreenShot",
                    "custom_action_param": {
                        "type": f"PGR_DormQuickMenu{portrait_index}"
                    },
                },
            },
            "post_delay": 200,
            "next": ["夜雨_宿舍任务_进入抚摸", fallback],
        }

    # Newer room layouts do not always expose the portrait-side action even
    # though the portrait click is accepted.  Bound the fallback to harmless
    # room-space locations where residents commonly stand or use furniture;
    # after every click OCR gets the first chance to take the petting action.
    room_targets = (
        [885, 220],  # couch
        [680, 350],  # bed
        [530, 230],  # back wall / door
        [540, 300],
        [325, 545],
        [760, 520],
    )
    for target_index, target in enumerate(room_targets, start=1):
        click_name = f"夜雨_宿舍任务_点击房间伙伴{target_index}"
        next_name = (
            f"夜雨_宿舍任务_点击房间伙伴{target_index + 1}"
            if target_index < len(room_targets)
            else "夜雨_宿舍任务_未找到可抚摸伙伴"
        )
        dorm[click_name] = {
            "action": {"type": "Click", "param": {"target": target}},
            "post_delay": 350,
            "next": ["夜雨_宿舍任务_进入抚摸", next_name],
        }
    dorm["夜雨_宿舍任务_未找到可抚摸伙伴"] = {
        "timeout": 3000,
        "next": ["夜雨_宿舍任务_进入抚摸"],
    }
    dorm["夜雨_宿舍任务_进入抚摸"] = {
        "recognition": {
            "type": "OCR",
            "param": {
                "roi": [850, 120, 420, 520],
                "expected": "(抚摸|摸头|爱抚)",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 1500,
        "next": ["夜雨_宿舍任务_互动界面截图"],
    }
    dorm["夜雨_宿舍任务_互动界面截图"] = {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "ScreenShot",
                "custom_action_param": {"type": "PGR_DormInteractionBefore"},
            },
        },
        "post_delay": 500,
        "next": ["夜雨_宿舍任务_启动抚摸"],
    }
    dorm["夜雨_宿舍任务_启动抚摸"] = {
        "recognition": {
            "type": "OCR",
            "param": {
                "roi": [540, 570, 200, 150],
                "expected": "^抚摸$",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 900,
        "next": ["夜雨_宿舍任务_抚摸启动后截图"],
    }
    dorm["夜雨_宿舍任务_抚摸启动后截图"] = {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "ScreenShot",
                "custom_action_param": {"type": "PGR_DormPettingActive"},
            },
        },
        "post_delay": 200,
        "next": ["夜雨_宿舍任务_从抚摸按钮拖到头部"],
    }
    # The dedicated petting screen says "来回抚摸". A single line from the
    # blue control to the head left the observed daily at 0/1. MaaFramework
    # v5.12.3 Swipe.end accepts waypoints without lifting between segments;
    # retain one press and add bounded lateral strokes in the observed head
    # region. Only the original task-page completion gate can finish the branch.
    dorm["夜雨_宿舍任务_从抚摸按钮拖到头部"] = {
        "recognition": {
            "type": "OCR",
            "param": {"roi": [0, 200, 240, 140], "expected": "来回抚摸"},
        },
        "action": {
            "type": "Swipe",
            "param": {
                "begin": [640, 650],
                "end": [[640, 170], [590, 170], [690, 170], [590, 170], [690, 170], [640, 170]],
                "duration": [1200, 350, 700, 700, 700, 350],
                "end_hold": [0, 0, 0, 0, 0, 300],
                "only_hover": False,
                "contact": 0,
            },
        },
        "post_delay": 1500,
        "next": ["夜雨_宿舍任务_互动后截图"],
    }
    dorm["夜雨_宿舍任务_互动后截图"] = {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "ScreenShot",
                "custom_action_param": {"type": "PGR_DormInteractionAfter"},
            },
        },
        "post_delay": 500,
        "next": ["夜雨_宿舍任务_互动后返回"],
    }
    dorm["夜雨_宿舍任务_互动后返回"] = {
        "recognition": {
            "type": "OCR",
            "param": {
                "roi": [20, 0, 180, 90],
                "expected": "返回",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 1800,
        "next": ["夜雨_宿舍任务_返回宿舍大厅"],
    }
    dorm["夜雨_宿舍任务_返回宿舍大厅"] = {
        "recognition": {
            "type": "OCR",
            "param": {
                "roi": [20, 0, 180, 90],
                "expected": "返回",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 2500,
        "next": ["夜雨_宿舍任务_重新打开任务"],
    }
    dorm["夜雨_宿舍任务_重新打开任务"] = {
        "recognition": {
            "type": "TemplateMatch",
            "param": {
                "roi": [1181, 82, 56, 56],
                "template": "宿舍委托/宿舍任务_1181_82_55_55__1125_32_155_155.png",
            },
        },
        "action": {"type": "Click"},
        "post_delay": 1200,
        "next": ["夜雨_宿舍任务_互动后复核"],
    }
    dorm["夜雨_宿舍任务_互动后复核"] = {
        "timeout": 15000,
        "next": [
            "今日任务已完成",
            "[JumpBack]一键领取_宿舍任务",
            "[JumpBack]关闭奖励",
            "[JumpBack]打开宿舍任务",
        ],
        "on_error": ["夜雨_宿舍任务_无效果截图"],
    }
    # A missing petting prompt, failed input, or missing return-page match
    # preserves the current scene instead of using the global main-menu retry.
    for name in (
        "夜雨_宿舍任务_抚摸启动后截图",
        "夜雨_宿舍任务_从抚摸按钮拖到头部",
        "夜雨_宿舍任务_互动后截图",
        "夜雨_宿舍任务_互动后返回",
        "夜雨_宿舍任务_返回宿舍大厅",
        "夜雨_宿舍任务_重新打开任务",
    ):
        dorm[name]["timeout"] = 15000
        dorm[name]["on_error"] = ["夜雨_宿舍任务_无效果截图"]
    dorm["夜雨_宿舍任务_无效果截图"] = {
        "action": {
            "type": "Custom",
            "param": {
                "custom_action": "ScreenShot",
                "custom_action_param": {"type": "PGR_DormNoEffect"},
            },
        },
        "next": ["夜雨_宿舍任务_无效果停止"],
        "on_error": ["夜雨_宿舍任务_无效果停止"],
    }
    dorm["夜雨_宿舍任务_无效果停止"] = {
        "focus": {"Node.Recognition.Succeeded": PGR_DORM_NO_EFFECT_MARKER},
        "action": {"type": "StopTask"},
        "next": [],
        "on_error": [],
    }
    dorm_temporary = dorm_path.with_name(dorm_path.name + ".yeyu.tmp")
    dorm_temporary.write_text(
        json.dumps(dorm, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    os.replace(dorm_temporary, dorm_path)


def _enable_pgr_formal_updates(tool_root: Path) -> None:
    """Enable the update gates consumed by the visible FOS GUI entry."""
    config_path = tool_root / "config" / "config.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    update = document.get("Update")
    bundle = document.get("Bundle")
    if not isinstance(update, dict) or not isinstance(bundle, dict):
        raise RuntimeError("pgr_formal_update_config_missing")
    update["auto_update"] = True
    bundle["bundle_auto_update"] = True
    temporary = config_path.with_name("config.json.yeyu.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, config_path)


def _acknowledge_pgr_trusted_resource(tool_root: Path) -> str:
    """Acknowledge only the pinned official MAA_Punish resource identity.

    MFW pauses the first run of every unseen resource identity behind a safety
    dialog.  A background-selected daily cannot answer that dialog.  YeYu may
    persist the same fingerprint as the GUI only after the resource advertises
    the exact allowlisted upstream repository; any identity drift remains a
    human review stop instead of becoming a generic auto-confirm.
    """

    interface_path = tool_root / "interface.json"
    translations_path = tool_root / "i18n" / "zh_cn.json"
    config_path = tool_root / "config" / "config.json"
    interface = json.loads(interface_path.read_text(encoding="utf-8"))
    translations = json.loads(translations_path.read_text(encoding="utf-8"))

    def translated(value: Any, *, expand_file: bool = False) -> str:
        text = str(value or "").strip()
        if text.startswith("$"):
            text = str(translations.get(text[1:], text) or "").strip()
        if expand_file and text and not any(char in text for char in ("\n", "\r", "\x00")):
            candidate = Path(text)
            if not candidate.is_absolute():
                candidate = tool_root / candidate
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
        return text

    name = translated(interface.get("label"))
    if not name or name.startswith("$"):
        name = translated(interface.get("title"))
    if not name or name.startswith("$"):
        name = translated(interface.get("name"))
    github = translated(interface.get("github") or interface.get("url"))
    contact = translated(interface.get("contact"), expand_file=True)
    if not re.fullmatch(
        r"https://github\.com/overflow65537/MAA_Punish(?:\.git)?/?",
        github,
        re.IGNORECASE,
    ):
        raise RuntimeError("pgr_resource_identity_requires_human_review")
    if name not in {"法奥斯之矛", "FOS"}:
        raise RuntimeError("pgr_resource_name_requires_human_review")

    payload = json.dumps(
        {"contact": contact, "github": github, "name": name},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    document = json.loads(config_path.read_text(encoding="utf-8"))
    security = document.get("Security")
    if not isinstance(security, dict):
        raise RuntimeError("pgr_resource_acknowledgement_store_missing")
    try:
        acknowledged = json.loads(str(security.get("acknowledged_resource_runs") or "[]"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("pgr_resource_acknowledgement_store_invalid") from exc
    if not isinstance(acknowledged, list) or any(not isinstance(item, str) for item in acknowledged):
        raise RuntimeError("pgr_resource_acknowledgement_store_invalid")
    acknowledged = [item for item in acknowledged if item != fingerprint]
    acknowledged.append(fingerprint)
    security["acknowledged_resource_runs"] = json.dumps(
        acknowledged[-200:], ensure_ascii=False
    )
    temporary = config_path.with_name("config.json.yeyu.resource-ack.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, config_path)
    return fingerprint


def _cleanup_stale_pgr_run_profiles(config_root: Path) -> None:
    for path in config_root.glob("c_yeyu_*.json"):
        if path.is_file() and not path.is_symlink():
            path.unlink()


def _normalize_pgr_profile_registry(tool_root: Path, base_config_id: str) -> None:
    """Remove abandoned YeYu run registrations before opening formal FOS.

    Cooperative batch cancellation can terminate the isolated Adapter before
    its ``finally`` block restores multi_config.json.  The next formal update
    GUI must never bootstrap from that ephemeral selected-task profile: it can
    stall during MainWindow initialization.  Recover to the fixed upstream
    base profile atomically, while retaining every non-YeYu profile entry.
    """

    path = tool_root / "config" / "multi_config.json"
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    config_list = document.get("config_list")
    if not isinstance(config_list, list) or any(
        not isinstance(item, str) for item in config_list
    ):
        raise RuntimeError("pgr_multi_config_invalid")
    stable = [item for item in config_list if not item.startswith("c_yeyu_")]
    if base_config_id not in stable:
        stable.append(base_config_id)
    document["config_list"] = stable
    document["curr_config_id"] = base_config_id
    temporary = path.with_name("multi_config.json.yeyu.normalize.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _activate_pgr_run_profile(tool_root: Path, config_id: str) -> tuple[Path, bytes]:
    """Register one ephemeral profile so FOS cannot fall back to the default."""
    path = tool_root / "config" / "multi_config.json"
    original = path.read_bytes()
    document = json.loads(original.decode("utf-8-sig"))
    config_list = document.get("config_list")
    if not isinstance(config_list, list) or any(not isinstance(item, str) for item in config_list):
        raise RuntimeError("pgr_multi_config_invalid")
    document["config_list"] = [item for item in config_list if item != config_id] + [config_id]
    document["curr_config_id"] = config_id
    temporary = path.with_name("multi_config.json.yeyu.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path, original


def _restore_pgr_run_profile(registration: tuple[Path, bytes] | None) -> None:
    if registration is None:
        return
    path, original = registration
    temporary = path.with_name("multi_config.json.yeyu.restore.tmp")
    temporary.write_bytes(original)
    os.replace(temporary, path)


def run_pgr(tool_root: Path, selected: list[str], stage_file: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OwnedProcess:
        """Small stdlib-only process handle used by the packaged adapter.

        The adapter runs with the configured system Python, so optional site
        packages such as psutil are deliberately not part of the runtime
        contract.  Keep the process surface to the three operations required
        by the fixed formal-GUI lifecycle.
        """

        def __init__(self, pid: int, handle: int | None = None) -> None:
            self.pid = pid
            self.handle = handle

        def is_running(self) -> bool:
            if self.handle:
                return kernel32.WaitForSingleObject(self.handle, 0) == 258  # WAIT_TIMEOUT
            handle = kernel32.OpenProcess(0x00100000, False, self.pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)

        def terminate(self) -> None:
            if self.handle:
                kernel32.TerminateProcess(self.handle, 1)
                return
            handle = kernel32.OpenProcess(0x0001, False, self.pid)
            if not handle:
                return
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)

        def close(self) -> None:
            if self.handle:
                kernel32.CloseHandle(self.handle)
                self.handle = None

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcesses.restype = wintypes.BOOL

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    def process_details() -> list[tuple[int, str]]:
        capacity = 4096
        process_ids = (wintypes.DWORD * capacity)()
        bytes_used = wintypes.DWORD()
        if not psapi.EnumProcesses(process_ids, ctypes.sizeof(process_ids), ctypes.byref(bytes_used)):
            return []
        result: list[tuple[int, str]] = []
        for pid in process_ids[: bytes_used.value // ctypes.sizeof(wintypes.DWORD)]:
            if not pid:
                continue
            handle = kernel32.OpenProcess(0x00100000, False, int(pid))
            if not handle:
                continue
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    result.append((int(pid), buffer.value))
            finally:
                kernel32.CloseHandle(handle)
        return result

    base_path = tool_root / "config" / "configs" / "c_d300db28e6bd482b947ce83c5521c567.json"
    # Current MPA bundles the MFW application as FOS.exe.  Older bundles used
    # MFW.exe, so accept only those two fixed product entry points.
    mfw = next((item for item in (tool_root / "FOS.exe", tool_root / "MFW.exe") if item.is_file()), None)
    gui_log = tool_root / "debug" / "gui.log"
    if not base_path.is_file() or mfw is None:
        raise FileNotFoundError("pgr_tool_files_missing")

    def owned_processes(paths: set[Path], started_at: float) -> list[_OwnedProcess]:
        expected = {str(path.resolve()).casefold() for path in paths if path.is_file()}
        result: list[_OwnedProcess] = []
        for pid, executable in process_details():
            try:
                if str(Path(executable).resolve()).casefold() in expected:
                    result.append(_OwnedProcess(pid))
            except OSError:
                continue
        return result

    def visible_windows(pids: set[int]) -> list[int]:
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in pids and ctypes.windll.user32.IsWindowVisible(hwnd):
                handles.append(int(hwnd))
            return True

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return handles

    def formal_gui_windows() -> list[int]:
        """Find the visible PGR formal GUI even across an elevated updater restart.

        The updater starts the replacement FOS process itself.  That process is
        not the ShellExecute process we own, and Windows can deny a lower
        integrity Adapter access to its executable path.  The product title is
        still available through EnumWindows, so use the fixed PGR title only;
        never infer ownership from an arbitrary foreground window.
        """

        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, title, len(title))
            if "法奥斯之矛" in title.value:
                handles.append(int(hwnd))
            return True

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return handles

    def shell_launch(arguments: str = "") -> tuple[float, _OwnedProcess]:
        started_at = time.time()
        # Use the same visible Windows Shell entry as a user double-click.  In
        # particular, do not start the PyInstaller/Qt GUI as a hidden console
        # child: its updater is designed around the formal GUI lifecycle.
        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "open"
        info.lpFile = str(mfw)
        info.lpParameters = arguments or ""
        info.lpDirectory = str(tool_root)
        info.nShow = 1
        if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
            raise OSError(ctypes.get_last_error(), "pgr_formal_shell_launch_failed")
        pid = int(kernel32.GetProcessId(info.hProcess))
        if pid <= 0:
            kernel32.CloseHandle(info.hProcess)
            raise OSError("pgr_formal_shell_process_missing")
        return started_at, _OwnedProcess(pid, info.hProcess)

    def include_launched(processes: list[_OwnedProcess], launched: _OwnedProcess) -> list[_OwnedProcess]:
        if launched.is_running() and all(process.pid != launched.pid for process in processes):
            processes.append(launched)
        return processes

    def stop_formal_gui(processes: list[_OwnedProcess]) -> None:
        handles = list(
            dict.fromkeys(
                visible_windows({process.pid for process in processes})
                + formal_gui_windows()
            )
        )
        for hwnd in handles:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            living = [process for process in processes if process.is_running()]
            living_windows = [
                hwnd
                for hwnd in handles
                if ctypes.windll.user32.IsWindow(hwnd)
                and ctypes.windll.user32.IsWindowVisible(hwnd)
            ]
            if not living and not living_windows:
                for process in processes:
                    process.close()
                return
            time.sleep(0.25)
        for process in processes:
            if process.is_running():
                process.terminate()
            process.close()
        if any(
            ctypes.windll.user32.IsWindow(hwnd)
            and ctypes.windll.user32.IsWindowVisible(hwnd)
            for hwnd in handles
        ):
            raise RuntimeError("pgr_formal_gui_close_failed")

    def wait_for_formal_update() -> None:
        updater_paths = {
            tool_root / "MFWUpdater.exe",
            tool_root / "MFWUpdater1.exe",
        }
        log_start = _log_checkpoint(gui_log)
        formal_started_at, formal_launch = shell_launch()
        dead_since: float | None = None
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            text = _tail_since(gui_log, log_start)
            formal_paths = {tool_root / "FOS.exe", tool_root / "MFW.exe"}
            formal = include_launched(owned_processes(formal_paths, formal_started_at), formal_launch)
            updaters = owned_processes(updater_paths, formal_started_at)
            handles = list(
                dict.fromkeys(
                    visible_windows({process.pid for process in formal})
                    + formal_gui_windows()
                )
            )
            update_ready = (
                "当前已是最新版本" in text
                or "无需下载" in text
                or "更新成功完成" in text
            )
            if update_ready and handles and not updaters:
                # Keep the checked, visible formal instance alive. FOS can be
                # configured to minimize to its tray when it receives
                # WM_CLOSE, so a close/relaunch sequence can leave an opaque
                # single-instance owner behind. The execution launch below
                # uses FOS' own --force-restart path, which sends its typed IPC
                # shutdown request and waits for the instance lock before the
                # same formal GUI entry continues.
                formal_launch.close()
                return
            failure_line = next(
                (
                    line[-500:]
                    for line in reversed(text.splitlines())
                    if re.search(r"更新.*失败|下载.*失败|校验.*失败|Traceback", line, re.IGNORECASE)
                ),
                None,
            )
            if failure_line:
                raise RuntimeError(f"pgr_formal_update_failed:{failure_line}")
            if formal or updaters or handles:
                dead_since = None
            else:
                dead_since = dead_since or time.monotonic()
                if time.monotonic() - dead_since >= 30:
                    update_handoff = (
                        "下载完成" in text
                        or "准备重启" in text
                        or "launch_updater_process" in text
                        or "启动更新程序" in text
                    )
                    if update_handoff:
                        # The updater is the sole owner of replacement and of
                        # the formal GUI restart.  Starting another FOS here can
                        # create two simultaneous downloads and leave its
                        # runtime files locked during the next update attempt.
                        raise RuntimeError("pgr_formal_update_handoff_lost")
                    raise RuntimeError("pgr_formal_gui_exited_before_update_completed")
            time.sleep(0.5)
        raise TimeoutError("pgr_formal_update_timeout")

    def wait_for_formal_relaunch_barrier() -> None:
        """Wait until the updater GUI generation has completely quiesced.

        FOS can signal its process handle before Qt has released its windows,
        single-instance objects and final gui.log writer.  Relaunching the
        formal entry at that instant produces a responsive-looking process
        which never reaches MainWindow initialization.  Require both the
        fixed product processes/windows to be absent and gui.log to remain
        unchanged for five continuous seconds before starting the execution
        generation.
        """

        formal_paths = {
            mfw,
            tool_root / "FOS.exe",
            tool_root / "MFW.exe",
            tool_root / "MFWUpdater.exe",
            tool_root / "MFWUpdater1.exe",
        }
        quiet_since: float | None = None
        last_log_signature: tuple[int, int] | None = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            processes = owned_processes(formal_paths, 0.0)
            handles = list(
                dict.fromkeys(
                    visible_windows({process.pid for process in processes})
                    + formal_gui_windows()
                )
            )
            log_signature = (
                (gui_log.stat().st_size, gui_log.stat().st_mtime_ns)
                if gui_log.is_file()
                else (0, 0)
            )
            now = time.monotonic()
            if not processes and not handles:
                if quiet_since is None or log_signature != last_log_signature:
                    quiet_since = now
                last_log_signature = log_signature
                if now - quiet_since >= 5:
                    return
            else:
                quiet_since = None
                last_log_signature = log_signature
            time.sleep(0.25)
        raise TimeoutError("pgr_formal_gui_relaunch_barrier_timeout")

    def wait_for_execution_gui_ready(
        start_size: tuple[int, bytes],
        config_id: str,
        process: _OwnedProcess,
    ) -> None:
        """Wait for the visible GUI and profile switch to become idle.

        MFW 4.9 accepts an IPC ``run`` command before its task list has
        finished rebuilding.  Sending ``--direct-run`` in the first process
        launch can therefore deadlock the GUI on its splash image.  Open the
        formal entry with the isolated profile first, wait for MainWindow and
        the switch_config command to complete, then send direct-run through a
        second invocation of the same formal entry (which forwards to the
        ready single instance).
        """

        deadline = time.monotonic() + 90
        stable_since: float | None = None
        last_signature: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            text = _tail_since(gui_log, start_size)
            signature = (
                (gui_log.stat().st_size, gui_log.stat().st_mtime_ns)
                if gui_log.is_file()
                else (0, 0)
            )
            profile_ready = (
                config_id in text
                and "command=switch_config" in text
                and "IPC completed" in text
            )
            main_ready = "主界面初始化完成" in text
            windows_ready = bool(
                visible_windows({process.pid}) + formal_gui_windows()
            )
            now = time.monotonic()
            if main_ready and profile_ready and windows_ready:
                if stable_since is None or signature != last_signature:
                    stable_since = now
                if now - stable_since >= 3:
                    return
            else:
                stable_since = None
            last_signature = signature
            if not process.is_running() and not formal_gui_windows():
                raise RuntimeError("pgr_execution_gui_exited_before_ready")
            time.sleep(0.25)
        raise TimeoutError("pgr_execution_gui_ready_timeout")

    _normalize_pgr_profile_registry(tool_root, base_path.stem)
    _cleanup_stale_pgr_run_profiles(base_path.parent)
    _acknowledge_pgr_trusted_resource(tool_root)
    _enable_pgr_formal_updates(tool_root)
    wait_for_formal_update()

    # The updater can replace the whole application/config tree.  Resolve and
    # read the profile only after the formal GUI has proved the update phase is
    # finished.
    base_path = tool_root / "config" / "configs" / "c_d300db28e6bd482b947ce83c5521c567.json"
    mfw = next((item for item in (tool_root / "FOS.exe", tool_root / "MFW.exe") if item.is_file()), None)
    if not base_path.is_file() or mfw is None:
        raise FileNotFoundError("pgr_tool_files_missing_after_update")
    _cleanup_stale_pgr_run_profiles(base_path.parent)
    config_id = "c_yeyu_" + uuid.uuid4().hex
    config_path = base_path.parent / f"{config_id}.json"
    profile = _pgr_profile(json.loads(base_path.read_text(encoding="utf-8")), selected, config_id)
    config_path.write_text(json.dumps(profile, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    registration = _activate_pgr_run_profile(tool_root, config_id)
    start_size = _log_checkpoint(gui_log)
    run_launch: _OwnedProcess | None = None
    try:
        # Restart through FOS' formal entry, but do not ask it to run while
        # Qt is still rebuilding the task list.  MFW accepts the IPC command
        # before that rebuild is safe and can then deadlock in interface
        # refresh.  Wait for the restarted GUI/profile to become idle first,
        # then use a second formal invocation to forward ``run`` to the ready
        # single instance.
        run_started_at, run_launch = shell_launch(
            f'--force-restart --config-id "{config_id}"'
        )
        wait_for_execution_gui_ready(start_size, config_id, run_launch)
        # The formal force-restart performs a second resource/update refresh
        # which can restore the upstream task JSON.  Apply the compatibility
        # override only after that visible GUI generation is fully ready; the
        # following direct-run reloads the current bundle before execution.
        _patch_pgr_home_handoff(tool_root)
        _, direct_launch = shell_launch(
            f'--reuse-existing --config-id "{config_id}" --direct-run'
        )
        direct_launch.close()
        no_task_deadline = time.monotonic() + 120
        deadline = time.monotonic() + 1800
        recovery_grace = _env_positive_int("YEYU_GAMER_PGR_RECOVERY_GRACE_SECONDS", 90, 5, 900)
        recovery_started_at: float | None = None
        recovery_key: tuple[str, str] | None = None
        last_completed: list[str] = []
        last_terminal: list[str] = []
        last_started: list[str] = []
        process_exited_at: float | None = None
        task_started = False
        while time.monotonic() < deadline:
            execution_text = _tail_since(gui_log, start_size)
            started = _parse_pgr_started_operations(execution_text, selected)
            progress = _parse_pgr_progress(execution_text, selected)
            task_started = task_started or " 的执行信息" in execution_text
            for operation in started:
                if operation not in last_started:
                    _emit(
                        stage_file,
                        operation,
                        "started",
                        f"MPA entered fixed task={PGR_TASKS[operation]}",
                    )
                    last_started.append(operation)
            for operation, reason in progress.recovered_failures.items():
                if operation not in last_terminal:
                    _emit(stage_file, operation, "failed", reason)
                    last_terminal.append(operation)
                if recovery_key and recovery_key[0] == operation:
                    recovery_key = None
                    recovery_started_at = None
            if progress.recoverable_operation and progress.recoverable_marker:
                key = (progress.recoverable_operation, progress.recoverable_marker)
                if key != recovery_key:
                    recovery_key = key
                    recovery_started_at = time.monotonic()
                elif recovery_started_at is not None and time.monotonic() - recovery_started_at >= recovery_grace:
                    progress.failure = (
                        f"MPA recovery grace expired after {recovery_grace}s for "
                        f"{progress.recoverable_operation}: {progress.recoverable_marker}"
                    )
                    progress.failure_operation = progress.recoverable_operation
            elif recovery_key:
                recovery_key = None
                recovery_started_at = None
            privacy_gate = (
                "我已详细阅读并同意隐私政策" in execution_text
                and "用户协议" in execution_text
            )
            if privacy_gate:
                for operation in selected:
                    if operation not in last_terminal:
                        _emit(
                            stage_file,
                            operation,
                            "human_required",
                            "PGR login/privacy agreement requires the operator; game client must be preserved",
                        )
                return 0
            if progress.failure:
                cascade_from = progress.failure_operation or "unknown"
                for operation in selected:
                    if operation not in last_terminal:
                        reason = _pgr_abort_reason(operation, cascade_from, progress.failure)
                        _emit(stage_file, operation, "failed", reason)
                        last_terminal.append(operation)
                return 20
            for operation in progress.completed:
                if operation not in last_terminal:
                    _emit(stage_file, operation, "completed", f"MPA emitted task success for operation={operation}")
                    last_completed.append(operation)
                    last_terminal.append(operation)
            if progress.flow_done:
                missing = [operation for operation in selected if operation not in last_terminal]
                for operation in missing:
                    _emit(stage_file, operation, "failed", "MPA flow ended without this task's success marker")
                    last_terminal.append(operation)
                return 0 if not missing else 22
            if not task_started and time.monotonic() >= no_task_deadline:
                for operation in selected:
                    if operation not in last_terminal:
                        _emit(
                            stage_file,
                            operation,
                            "failed",
                            "MPA ready-GUI direct-run emitted no task start within 120 seconds",
                        )
                        last_terminal.append(operation)
                return 25
            processes = include_launched(owned_processes({mfw}, run_started_at), run_launch)
            if not processes:
                # The formal GUI entry can append its final log record shortly
                # after the process handle becomes signalled.  Give that write a
                # small bounded grace, then fail instead of occupying the whole
                # Manager timeout with no live automation tool.
                process_exited_at = process_exited_at or time.monotonic()
                if time.monotonic() - process_exited_at >= 5:
                    missing = [operation for operation in selected if operation not in last_terminal]
                    for operation in missing:
                        _emit(
                            stage_file,
                            operation,
                            "failed",
                            "MPA formal GUI exited without a complete task-flow marker",
                        )
                        last_terminal.append(operation)
                    return 24
            else:
                process_exited_at = None
            time.sleep(0.1)
        for operation in selected:
            if operation not in last_terminal:
                _emit(stage_file, operation, "failed", "MPA selected-task run timed out")
        return 23
    finally:
        if run_launch is not None:
            run_launch.close()
        _restore_pgr_run_profile(registration)
        try:
            config_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_zzz(tool_root: Path, selected: list[str], stage_file: Path) -> int:
    import ctypes
    from ctypes import wintypes

    import psutil
    import yaml

    launcher = tool_root / "OneDragon-Launcher.exe"
    group_path = tool_root / "config" / "01" / "one_dragon" / "_group.yml"
    record_root = tool_root / "config" / "01" / "app_run_record"
    if not launcher.is_file() or not group_path.is_file() or not record_root.is_dir():
        raise FileNotFoundError("zzz_formal_gui_files_missing")

    protected_pids = _process_lineage_pids(psutil.Process, os.getpid())

    def owned_pids(started_at: float) -> set[int]:
        result: set[int] = set()
        expected = launcher.resolve()
        expected_root = tool_root.resolve()
        for process in psutil.process_iter(("pid", "exe", "create_time")):
            try:
                if int(process.info["pid"]) in protected_pids:
                    continue
                exe = process.info.get("exe")
                created = float(process.info.get("create_time") or 0)
                if not exe or created < started_at - 2:
                    continue
                image = Path(exe).resolve()
                if image == expected or expected_root in image.parents:
                    result.add(int(process.info["pid"]))
            except (OSError, psutil.Error):
                continue
        return result

    def visible_windows(pids: set[int]) -> list[int]:
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in pids and ctypes.windll.user32.IsWindowVisible(hwnd):
                handles.append(int(hwnd))
            return True

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return handles

    def close_windows(handles: list[int]) -> None:
        for hwnd in handles:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

    original_group = group_path.read_bytes()
    gui_process: subprocess.Popen[bytes] | None = None
    run_process: subprocess.Popen[bytes] | None = None
    run_output: Any | None = None
    terminal_operations: set[str] = set()
    try:
        if "attach-home" in selected:
            _emit(stage_file, "attach-home", "started", "OneDragon formal GUI update/startup entry")

        gui_started_at = time.time()
        gui_process = subprocess.Popen(
            [str(launcher)],
            cwd=tool_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        gui_handles: list[int] = []
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            gui_handles = visible_windows(owned_pids(gui_started_at))
            if gui_handles:
                break
            if gui_process.poll() not in (None, 0):
                raise RuntimeError(f"zzz_formal_gui_failed:{gui_process.returncode}")
            time.sleep(0.25)
        if not gui_handles:
            raise TimeoutError("zzz_formal_gui_update_timeout")
        close_windows(gui_handles)
        try:
            gui_process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            gui_process.terminate()
            gui_process.wait(timeout=10)
        close_deadline = time.monotonic() + 30
        remaining = owned_pids(gui_started_at)
        while remaining and time.monotonic() < close_deadline:
            close_windows(visible_windows(remaining))
            time.sleep(0.5)
            remaining = owned_pids(gui_started_at)
        if remaining:
            for pid in remaining:
                try:
                    psutil.Process(pid).terminate()
                except (OSError, psutil.Error):
                    pass
            tracked = []
            for pid in remaining:
                try:
                    tracked.append(psutil.Process(pid))
                except (OSError, psutil.Error):
                    pass
            _, alive = psutil.wait_procs(tracked, timeout=10)
            for process in alive:
                try:
                    process.kill()
                except (OSError, psutil.Error):
                    pass

        selected_app_ids = [ZZZ_APPS[item] for item in selected if item != "attach-home"]
        # A visible updater/GUI is not evidence that the game has reached an
        # operable world frame.  This runner needs an in-game application to
        # produce the normal-world readiness marker.
        if not selected_app_ids:
            if "attach-home" in selected:
                _emit(
                    stage_file,
                    "attach-home",
                    "failed",
                    "OneDragon formal GUI was visible, but no selected in-game app could prove an operable normal-world frame",
                )
                terminal_operations.add("attach-home")
                return 20
            return 0
        parsed_group = yaml.safe_load(original_group.decode("utf-8"))
        app_list = parsed_group.get("app_list") if isinstance(parsed_group, dict) else None
        if not isinstance(app_list, list):
            raise ValueError("zzz_group_config_invalid")
        known = {item.get("app_id") for item in app_list if isinstance(item, dict)}
        if any(app_id not in known for app_id in selected_app_ids):
            raise ValueError("zzz_selected_app_missing")
        for item in app_list:
            if isinstance(item, dict):
                item["enabled"] = item.get("app_id") in selected_app_ids
        temporary = group_path.with_name("_group.yml.yeyu.tmp")
        temporary.write_text(yaml.safe_dump(parsed_group, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(temporary, group_path)

        baseline: dict[str, dict[str, Any]] = {}
        for app_id in selected_app_ids:
            record_path = record_root / f"{app_id}.yml"
            baseline[app_id] = yaml.safe_load(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}

        # A Manager-side reset means the selected Todo must really execute
        # again.  OneDragon otherwise skips an app whose private daily record
        # is already successful, which would manufacture a new evidence pair
        # around no game interaction.  Reset only the selected app records;
        # preserve every app-specific field and let the formal runner write
        # the new current-day lifecycle.
        for app_id, prior in baseline.items():
            if not isinstance(prior, dict) or not prior:
                continue
            reset_record = dict(prior)
            reset_record["run_status"] = 0
            record_path = record_root / f"{app_id}.yml"
            temporary_record = record_path.with_name(f".{record_path.name}.yeyu-{uuid.uuid4().hex}.tmp")
            temporary_record.write_text(
                yaml.safe_dump(reset_record, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            os.replace(temporary_record, record_path)

        operation_by_app = {
            ZZZ_APPS[operation]: operation
            for operation in selected
            if operation != "attach-home"
        }
        started_operations: set[str] = set()
        failed = False

        run_started_at = time.time()
        log_path = tool_root / ".log" / "log.txt"
        log_checkpoint = _log_checkpoint(log_path)
        attach_pending = "attach-home" in selected
        run_output = tempfile.TemporaryFile()
        run_process = subprocess.Popen(
            [str(launcher), "--onedragon"],
            cwd=tool_root,
            stdin=subprocess.DEVNULL,
            stdout=run_output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        timed_out = False
        deadline = time.monotonic() + 1800
        while True:
            if attach_pending:
                fresh_log = _tail_since(log_path, log_checkpoint)
                if "返回大世界 ] 执行成功 返回状态 大世界-普通" in fresh_log:
                    _emit(
                        stage_file,
                        "attach-home",
                        "completed",
                        "OneDragon confirmed an operable normal-world frame after the formal GUI/update gate",
                    )
                    terminal_operations.add("attach-home")
                    attach_pending = False

            # Do not emit any application start before attach-home is proven.
            # Otherwise the Manager can capture a 'before' frame while the
            # game is still on a legal warning or loading screen.
            if attach_pending:
                returncode = run_process.poll()
                if returncode is not None:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    run_process.terminate()
                    try:
                        run_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        run_process.kill()
                        run_process.wait(timeout=10)
                    returncode = run_process.returncode
                    break
                time.sleep(0.05)
                continue

            for app_id, operation in operation_by_app.items():
                if operation in terminal_operations:
                    continue
                record_path = record_root / f"{app_id}.yml"
                try:
                    current = yaml.safe_load(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
                except (OSError, UnicodeError, yaml.YAMLError):
                    # The tool can replace its YAML between our existence
                    # check and read.  A later poll observes the atomic state.
                    continue
                status = int(current.get("run_status", 0)) if isinstance(current, dict) else 0
                recorded_at = float(current.get("run_time_float", 0)) if isinstance(current, dict) else 0
                fresh = recorded_at >= run_started_at - 2
                if status == 3 and fresh and operation not in started_operations:
                    _emit(stage_file, operation, "started", f"OneDragon app={app_id}; fresh run record entered running")
                    started_operations.add(operation)
                elif status in (1, 2) and fresh:
                    if operation not in started_operations:
                        _emit(
                            stage_file,
                            operation,
                            "failed",
                            f"OneDragon app={app_id}; terminal record arrived without observed running transition",
                        )
                        failed = True
                    elif status == 1:
                        _emit(stage_file, operation, "completed", f"OneDragon app={app_id}; fresh run record success")
                    else:
                        _emit(stage_file, operation, "failed", f"OneDragon app={app_id}; fresh run record failed")
                        failed = True
                    terminal_operations.add(operation)

            returncode = run_process.poll()
            if returncode is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                run_process.terminate()
                try:
                    run_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    run_process.kill()
                    run_process.wait(timeout=10)
                returncode = run_process.returncode
                break
            time.sleep(0.05)

        if attach_pending:
            _emit(
                stage_file,
                "attach-home",
                "failed",
                "OneDragon run ended without an operable normal-world readiness marker",
            )
            terminal_operations.add("attach-home")
            failed = True

        for app_id, operation in operation_by_app.items():
            if operation in terminal_operations:
                continue
            record_path = record_root / f"{app_id}.yml"
            try:
                current = yaml.safe_load(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
            except (OSError, UnicodeError, yaml.YAMLError):
                current = {}
            status = int(current.get("run_status", 0)) if isinstance(current, dict) else 0
            recorded_at = float(current.get("run_time_float", 0)) if isinstance(current, dict) else 0
            detail = (
                "OneDragon formal selected daily timed out"
                if timed_out
                else f"OneDragon app={app_id}; exit={returncode}; runStatus={status}; runTime={recorded_at}; missing terminal transition"
            )
            _emit(stage_file, operation, "failed", detail)
            terminal_operations.add(operation)
            failed = True
        if timed_out:
            return 23
        return 0 if returncode == 0 and not failed else 20
    except Exception as error:
        for operation in selected:
            if operation not in terminal_operations:
                _emit(
                    stage_file,
                    operation,
                    "failed",
                    f"OneDragon formal selected daily setup failed: {type(error).__name__}: {error}",
                )
        return 24
    finally:
        if run_process is not None and run_process.poll() is None:
            run_process.terminate()
            try:
                run_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                run_process.kill()
        if run_output is not None:
            run_output.close()
        if gui_process is not None and gui_process.poll() is None:
            gui_process.terminate()
            try:
                gui_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                gui_process.kill()
        restore = group_path.with_name("_group.yml.yeyu.restore")
        restore.write_bytes(original_group)
        os.replace(restore, group_path)


def _nikke_plan(operation: str, source_tree: str, alias: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "uid": f"yeyu-selected-{uuid.uuid4().hex}",
        "name": f"YeYu Gamer selected {operation}",
        "aliases": [alias],
        "visibleInTaskList": True,
        "blackboard": {},
        "root": {
            "uid": "selected-root",
            "type": "subtree",
            "name": f"selected {operation}",
            "params": {"tree_path": source_tree},
            "children": [],
        },
    }


def run_nikke(tool_root: Path, selected: list[str], stage_file: Path) -> int:
    tree_root = tool_root / "ok_tasks" / "trees"
    gui_entry = tool_root / "run_nikke_gui.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not tree_root.is_dir() or not gui_entry.is_file() or not pythonw.is_file():
        raise FileNotFoundError("nikke_behavior_tree_files_missing")
    failed = False
    for operation in selected:
        source_tree = NIKKE_TREES[operation]
        if not (tree_root / source_tree).is_file():
            raise FileNotFoundError(f"nikke_tree_missing:{source_tree}")
        selected_root = stage_file.parent / f"nikke-selected-{uuid.uuid4().hex}"
        selected_root.mkdir(parents=False, exist_ok=False)
        plan_path = selected_root / "selected.json"
        plan = json.loads((tree_root / source_tree).read_text(encoding="utf-8"))
        plan["visibleInTaskList"] = True
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        _emit(stage_file, operation, "started", f"fixed NIKKE subtree={source_tree}")
        try:
            environment = os.environ.copy()
            environment["YEYU_NIKKE_TREE_FOLDER"] = str(selected_root)
            process = subprocess.Popen(
                [str(pythonw), str(gui_entry), "--task", "1", "--exit"],
                cwd=tool_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=environment,
            )
            deadline = time.monotonic() + 1200
            game_missing_since: float | None = None
            game_lost = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    raise subprocess.TimeoutExpired(process.args, 1200)
                observed = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq nikke.exe", "/FO", "CSV", "/NH"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                game_present = observed.returncode == 0 and '"nikke.exe"' in observed.stdout.lower()
                if game_present:
                    game_missing_since = None
                else:
                    game_missing_since = game_missing_since or time.monotonic()
                    if time.monotonic() - game_missing_since >= 10:
                        game_lost = True
                        process.kill()
                        break
                time.sleep(1)
            output, _ = process.communicate(timeout=10)
            output = output or ""
            returncode = process.returncode if process.returncode is not None else -1
            success = not game_lost and returncode == 0 and "Behavior Tree Result success" in output and not any(
                marker in output
                for marker in ("Behavior Tree Result failure", "Node Status error", "Traceback (most recent call last)")
            )
            excerpt = " | ".join(line[-300:] for line in output.splitlines()[-8:])
            loss = "; gameWindowLost=true" if game_lost else ""
            _emit(stage_file, operation, "completed" if success else "failed", f"formalGui=run_nikke_gui.py; subtree={source_tree}; exit={returncode}{loss}; tail={excerpt}")
            if not success:
                failed = True
                break
        except subprocess.TimeoutExpired:
            _emit(stage_file, operation, "failed", f"subtree={source_tree}; timeout")
            failed = True
            break
        finally:
            shutil.rmtree(selected_root, ignore_errors=True)
    return 0 if not failed else 20


def _self_test() -> int:
    base = {
        "item_id": "base",
        "name": "base",
        "tasks": [
            {"name": "进入游戏", "item_id": "a", "is_checked": True, "task_option": {"_speedrun_state": {"x": 1}}},
            {"name": "领取体力", "item_id": "b", "is_checked": True, "task_option": {}},
            {"name": "结束游戏", "item_id": "unsafe", "is_checked": True, "task_option": {}},
        ],
    }
    profile = _pgr_profile(base, ["claim-serum"], "runtime")
    checked = [item["name"] for item in profile["tasks"] if item.get("is_checked")]
    assert checked == ["领取体力"]
    progress = _parse_pgr_progress(
        "任务 '领取体力' 的执行信息\n跳过通知 WHEN_TASK_SUCCESS (4)\nTASK_FLOW_STOP manual=False",
        ["claim-serum"],
    )
    assert progress.completed == ["claim-serum"] and progress.flow_done and progress.failure is None
    progress = _parse_pgr_progress("TASK_FLOW_STOP manual=False", ["claim-serum"])
    assert progress.completed == [] and progress.flow_done
    plan = _nikke_plan("outpost", "nikke_outpost.json", "alias")
    assert plan["root"]["params"]["tree_path"] == "nikke_outpost.json"
    assert set(ZZZ_APPS.values()) == {"coffee", "scratch_card", "trigrams_collection", "suibian_temple", "random_play", "charge_plan", "city_fund", "engagement_reward"}
    print(json.dumps({"status": "passed", "gameStarted": False, "tests": 5}, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", choices=("PGR", "ZZZ", "NIKKE"))
    parser.add_argument("--tool-root")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    if not args.game_id or not args.tool_root:
        parser.error("--game-id and --tool-root are required")
    stage_raw = os.environ.get("YEYU_GAMER_STAGE_FILE")
    if not stage_raw:
        raise RuntimeError("stage_file_missing")
    stage_file = Path(stage_raw).resolve()
    selected = _selected_operations(args.game_id)
    tool_root = Path(args.tool_root).resolve()
    if args.game_id == "PGR":
        return run_pgr(tool_root, selected, stage_file)
    if args.game_id == "ZZZ":
        return run_zzz(tool_root, selected, stage_file)
    return run_nikke(tool_root, selected, stage_file)


if __name__ == "__main__":
    raise SystemExit(main())
