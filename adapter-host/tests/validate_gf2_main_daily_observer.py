"""Replay the main-branch GF2 DailyTask without starting the tool or game.

The run body and ``mail`` method below are the real official v1.2.72 source.
Only their window-facing collaborators are replaced on the instance.
"""
from __future__ import annotations

import importlib.util
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(r"C:\Game\YeYuGamerCandidates\gf2-official-main-4122e01\GF2\source")
COMMIT = "4122e01e8a69063ae37807f316f2ae8f80eeedb1"


def load_bridge():
    path = ROOT / "adapter-host" / "openkuro-runner" / "Gf2YeYuObserver.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("gf2_main_observer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def events(path: Path):
    # Image results are verified separately from the official method contract.
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [record for record in records if record["state"] not in {"step_artifact", "progress"}]


def config(**enabled):
    values = {
        "已确认启用游戏内全局自动功能": True,
        "生成汇总文件": False,
        "多账户模式": False,
        "社区每日": False,
        "邮件": False,
        "情报和战前补给": False,
        "闪耀星愿": False,
        "活动自律": False,
        "活动层": False,
        "公共区/调度室": False,
        "购买免费礼包": False,
        "自动刷体力": False,
        "竞技场": False,
        "班组": False,
        "领任务": False,
        "大月卡": False,
        "探索领取": False,
    }
    values.update(enabled)
    return values


def task_without_window_io(DailyTask, values, click):
    task = object.__new__(DailyTask)
    task.config = values
    task.box = SimpleNamespace(bottom_left=object(), bottom=object())
    task.info_set = lambda *args, **kwargs: None
    task.log_info = lambda *args, **kwargs: None
    task.is_adb = lambda: False
    task.click = click
    task.wait_click_ocr = lambda *args, **kwargs: None
    # Model an already-ready client with no resource-download screen. Exercise
    # the real method without capturing a window or injecting actual input.
    task.ocr = lambda *args, **kwargs: []
    task.wait_until = lambda predicate, **kwargs: bool(predicate())
    task.is_main = lambda **kwargs: True
    return task


def main():
    global SOURCE, COMMIT
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    SOURCE = args.source.resolve()
    record = json.loads((SOURCE.parent / "runtime-preparation.json").read_text(encoding="utf-8-sig"))
    COMMIT = record["source"]["commit"]
    assert SOURCE.is_dir(), f"main candidate missing: {SOURCE}"
    sys.path.insert(0, str(SOURCE))
    from src.tasks.DailyTask import DailyTask

    bridge = load_bridge()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        stage = root / "stage.jsonl"
        stage.write_text("", encoding="utf-8")
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = json.dumps(["attach-home", "mail"])
        bridge.install()

        # DailyTask.run and DailyTask.mail are real main-branch methods here.
        task = task_without_window_io(DailyTask, config(**{"邮件": True}), lambda *args, **kwargs: None)
        DailyTask.run(task)
        if hasattr(task, "daily_runner"):
            assert task.daily_runner.final_summary["status"] == "完成"
            assert "邮件" in task.daily_runner.task_status["success"]
        observed = events(stage)
        assert [(item["operation"], item["state"]) for item in observed] == [
            ("attach-home", "started"),
            ("attach-home", "completed"),
            ("mail", "started"),
            ("mail", "completed"),
        ]

        # Observer reporting does not replace the exception raised by the
        # actual mail method’s click collaborator.
        class OriginalClickError(RuntimeError):
            pass

        stage.write_text("", encoding="utf-8")
        task = task_without_window_io(
            DailyTask,
            config(**{"邮件": True}),
            lambda *args, **kwargs: (_ for _ in ()).throw(OriginalClickError("fixture click failure")),
        )
        try:
            DailyTask.run(task)
            raise AssertionError("real DailyTask.mail exception was swallowed")
        except OriginalClickError as error:
            assert str(error) == "fixture click failure"
        if hasattr(task, "daily_runner"):
            assert task.daily_runner.final_summary["status"] == "异常结束"
            assert task.daily_runner.final_summary["exception"] == "fixture click failure"
        assert [(item["operation"], item["state"]) for item in events(stage)] == [
            ("attach-home", "started"),
            ("attach-home", "completed"),
            ("mail", "started"),
            ("mail", "failed"),
        ]

        # A stage destination that cannot be opened is telemetry-only.  The
        # same real run and mail method still return normally.
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(root / "missing" / "stage.jsonl")
        task = task_without_window_io(DailyTask, config(**{"邮件": True}), lambda *args, **kwargs: None)
        assert DailyTask.run(task) is None
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)

        # This invokes the unmodified official DailyTask.run selection loop.
        # The subclass only replaces the selected window actions with markers.
        calls = []

        class SelectionProbe(DailyTask):
            def ensure_main(self, *args, **kwargs):
                calls.append("ensure_main")

            def activities(self):
                calls.append("activities")

            def star_wish(self):
                calls.append("star_wish")

        probe = object.__new__(SelectionProbe)
        probe.config = config(**{"情报和战前补给": True, "闪耀星愿": True})
        probe.log_info = lambda *args, **kwargs: None
        DailyTask.run(probe)
        assert calls.count("activities") == 1 and calls.count("star_wish") == 1

        calls.clear()
        probe.config = config()
        DailyTask.run(probe)
        assert "activities" not in calls and "star_wish" not in calls

        # The four main-branch methods are wrapped themselves, rather than
        # inferred from a successful root DailyTask.  Their official normal
        # return is None, which must remain a normal method terminal.
        stage.write_text("", encoding="utf-8")
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = json.dumps(
            ["activity-supply", "star-wish", "event-battle", "exploration-claim"]
        )
        task = task_without_window_io(DailyTask, config(), lambda *args, **kwargs: None)
        task.box = SimpleNamespace(
            _activities=object(), top_right=object(), bottom_left=object(), bottom_right=object(), left=object(), bottom=object()
        )
        task.box_of_screen = lambda *args, **kwargs: object()
        if hasattr(task, '_collect_supply_activities'):
            task._executor = SimpleNamespace(device_manager=SimpleNamespace(hwnd_window=SimpleNamespace(hwnd=0)))
            task._open_supply_activity = lambda name: True
            task._claim_supply_activity = lambda name: '已领取'
            activity_body = DailyTask.activities
            while hasattr(activity_body, '__wrapped__'):
                activity_body = activity_body.__wrapped__
            # Replace only window ownership and UI actions; the current
            # official collector still owns its return value and loop.
            with mock.patch.dict(activity_body.__globals__, {'run_in_window': lambda hwnd, callback: callback()}):
                DailyTask.activities(task)
        else:
            DailyTask.activities(task)
        DailyTask.star_wish(task)
        DailyTask.activity(task)
        DailyTask.explore_claim(task)
        assert [(item["operation"], item["state"]) for item in events(stage)] == [
            ("activity-supply", "started"), ("activity-supply", "completed"),
            ("star-wish", "started"), ("star-wish", "completed"),
            ("event-battle", "started"), ("event-battle", "completed"),
            ("exploration-claim", "started"), ("exploration-claim", "completed"),
        ]

        # Older DailyTask releases lack these main-branch methods.  Their
        # absence is rejected by runner preflight, not an install-time task
        # attempt. Existing method wrappers must still be installed.
        stage.write_text("", encoding="utf-8")
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = json.dumps(["activity-supply", "event-battle"])

        class LegacyDailyTask:
            def mail(self):
                return None

        bridge._install_methods(LegacyDailyTask)
        assert callable(LegacyDailyTask.mail)
        assert events(stage) == []
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = json.dumps(["mail"])
        assert LegacyDailyTask().mail() is None
        assert [(item["operation"], item["state"]) for item in events(stage)] == [
            ("mail", "started"), ("mail", "completed"),
        ]

    from validate_official_task_exit import verify_failure_exit
    verify_failure_exit(bridge._install_failure_exit)

    print(json.dumps({"status": "passed", "sourceCommit": COMMIT, "cases": 16, "qtDispatcherExitVerified": True, "toolStarted": False, "gameStarted": False}))


if __name__ == "__main__":
    main()
