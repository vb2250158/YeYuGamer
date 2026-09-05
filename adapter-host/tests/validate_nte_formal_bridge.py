from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


OPERATIONS = [
    "attach-world",
    "claim-mail",
    "inspect-daily-progress",
    "spend-urban-vitality",
    "claim-daily-reward",
    "claim-period-reward",
]
ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "adapter-host" / "nte-runner" / "Program.cs"


def validate_transport() -> dict[str, object]:
    windows = Path(os.environ["WINDIR"])
    compiler = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    gac = windows / "Microsoft.NET" / "assembly" / "GAC_MSIL"
    references = [
        gac / name / "v4.0_4.0.0.0__31bf3856ad364e35" / f"{name}.dll"
        for name in ("UIAutomationClient", "UIAutomationTypes")
    ]
    with tempfile.TemporaryDirectory(prefix="nte-transport-") as temporary:
        executable = Path(temporary) / "NteTransportTests.exe"
        subprocess.run(
            [str(compiler), "/nologo", "/target:exe", "/main:NteTransportTests",
             "/r:System.Web.Extensions.dll", *(f"/r:{path}" for path in references),
             f"/out:{executable}", str(RUNNER), str(Path(__file__).with_name("NteTransportTests.cs"))],
            check=True, capture_output=True, text=True, timeout=30,
        )
        completed = subprocess.run(
            [str(executable)], check=True, capture_output=True, text=True, timeout=10,
        )
        report = json.loads(completed.stdout)
        assert report["passed"] and report["cases"] == 9
        return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    runner = RUNNER.read_text(encoding="utf-8")
    assert "IsInteractiveFormalWindow" in runner
    assert "visible && interactive && ready" in runner
    assert "splash_not_ready:ok_nte_formal_gui_never_became_interactive" in runner
    assert '"splash_not_ready"' in runner
    assert 'message.StartsWith(code + ":", StringComparison.Ordinal)' in runner
    assert "AutomationElement.ControlTypeProperty" in runner
    assert "PostMessage(process.MainWindowHandle, 0x0010" in runner
    assert "process.CloseMainWindow()" not in runner
    transport = validate_transport()

    anomaly_module = types.ModuleType("src.tasks.AnomalyTask")

    class AnomalyTask:
        TASK_NAME = "异常收容"
        TASK_ABILITY = "异能升级材料"
        TASK_ARC = "弧盘突破材料"
        TASK_CONSOLE = "空幕"
        CONF_TASK_TYPE = "副本目标"
        CONF_EXP_TARGET = "经验目标"
        CONF_ABILITY_ID = "异能材料"
        CONF_ARC_ID = "弧盘材料"
        CONF_CONSOLE_ID = "空幕材料"

    anomaly_module.AnomalyTask = AnomalyTask

    daily_module = types.ModuleType("src.tasks.DailyTask")

    class DailyTask:
        CONF_TASK = "副本类型"
        CONF_CLAIM_MAIL = "领取邮件"
        CONF_COMPLETE_DAILY = "完成每日活跃度"
        CONF_CLAIM_ACTIVITY = "领取活跃度奖励"
        CONF_CLAIM_BP = "领取环期任务奖励"
        CONF_COFFEE_TASK = "一咖舍任务"
        CONF_CINEMA_DATE = "影院约会"
        CONF_FOUNTAIN_SIGN = "喷泉签到"
        CONF_FURNITURE = "异象家具"
        CONF_GIFT = "羁遇赠礼"
        DAILY_STAMINA_TARGET = "目标消耗体力"
        TASK_NONE = "不执行"

        def __init__(self) -> None:
            self.config = {"preserved": "yes"}
            self.calls: list[str] = []

        def ensure_main(self): self.calls.append("ensure_main")
        def claim_mail(self): self.calls.append("claim_mail")
        def check_activity(self): self.calls.append("check_activity")
        def complete_daily_activities(self): self.calls.append("complete_daily_activities")
        def claim_activity_rewards(self): self.calls.append("claim_activity_rewards")
        def claim_battle_pass_rewards(self): self.calls.append("claim_battle_pass_rewards")
        def _print_result(self): self.calls.append("print_result")
        def log_info(self, *_args, **_kwargs): pass
        def do_run(self): raise AssertionError("upstream do_run must be replaced only in the run process")

    daily_module.DailyTask = DailyTask
    sys.modules["src"] = types.ModuleType("src")
    sys.modules["src.tasks"] = types.ModuleType("src.tasks")
    sys.modules["src.tasks.AnomalyTask"] = anomaly_module
    sys.modules["src.tasks.DailyTask"] = daily_module

    spec = importlib.util.spec_from_file_location("NteYeYuBridge", args.bridge)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / "stage.jsonl"
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)
        os.environ["YEYU_GAMER_SELECTED_OPERATIONS"] = json.dumps(OPERATIONS)
        os.environ["YEYU_GAMER_NTE_PROFILE"] = json.dumps(
            {
                "anomalyTaskType": "异能升级材料",
                "expRewardTarget": "甲硬币",
                "materialIndex": 5,
                "staminaTarget": 200,
                "autoCycleSubTask": False,
                "coffeeMode": "不执行",
            },
            ensure_ascii=False,
        )
        bridge.install()
        task = DailyTask()
        original = task.config
        task.do_run()
        assert task.config is original and task.config == {"preserved": "yes"}
        events = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        assert [event["operation"] for event in events[::2]] == OPERATIONS
        assert all(event["state"] == "started" for event in events[::2])
        assert all(event["state"] == "completed" and event["detail"] for event in events[1::2])
        assert set(task.calls) >= {
            "claim_mail", "check_activity", "complete_daily_activities",
            "claim_activity_rewards", "claim_battle_pass_rewards",
        }

    report = {
        "schemaVersion": 1,
        "suite": "nte-formal-run-scoped-bridge",
        "passed": True,
        "gameStarted": False,
        "formalGuiStarted": False,
        "splashReadinessGate": True,
        "transport": transport,
        "operations": OPERATIONS,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(report, separators=(",", ":")))


if __name__ == "__main__":
    main()
