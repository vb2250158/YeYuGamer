from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER_PATH = ROOT / "adapter-host" / "classic-runner" / "classic_tool_driver.py"


def load_driver():
    spec = importlib.util.spec_from_file_location("classic_tool_driver", DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    driver = load_driver()

    class FakeProcess:
        def __init__(self, pid: int, parent: "FakeProcess | None" = None) -> None:
            self.pid = pid
            self._parent = parent

        def parent(self) -> "FakeProcess | None":
            return self._parent

    root = FakeProcess(10)
    shim = FakeProcess(20, root)
    current = FakeProcess(30, shim)
    fake_processes = {10: root, 20: shim, 30: current}
    assert driver._process_lineage_pids(fake_processes.__getitem__, 30) == {10, 20, 30}
    assert set(driver.PGR_TASKS) == {
        "attach-home", "claim-serum", "dorm", "simulation-field",
        "maintainer-action", "claim-daily-tasks", "battle-pass-free-track",
    }
    assert set(driver.ZZZ_APPS) == {
        "coffee", "scratch-card", "trigrams-collection", "suibian-temple",
        "random-play", "charge-plan", "city-fund-free-claim", "engagement-reward",
    }
    assert driver.NIKKE_TREES == {
        "attach-lobby": "nikke_return_lobby.json",
        "outpost": "nikke_outpost.json",
        "dispatch-friend": "nikke_dispatch_friend.json",
    }
    assert "daily-shop" not in driver.NIKKE_TREES
    parsed_jsonc = driver._loads_jsonc(
        '{"url":"https://example.invalid/a//b",/* note */"items":[1,2,],// tail\n}'
    )
    assert parsed_jsonc == {"url": "https://example.invalid/a//b", "items": [1, 2]}

    with tempfile.TemporaryDirectory() as log_root:
        log_path = Path(log_root) / "gui.log"
        log_path.write_text("old generation\n", encoding="utf-8")
        checkpoint = driver._log_checkpoint(log_path)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("appended marker\n")
        assert driver._tail_since(log_path, checkpoint).splitlines() == ["appended marker"]
        log_path.write_text("rebuilt marker\n", encoding="utf-8")
        assert driver._tail_since(log_path, checkpoint).splitlines() == ["rebuilt marker"]

    progress = driver._parse_pgr_progress(
        "TASK_FLOW_STOP manual=False\n", ["claim-serum"]
    )
    assert progress.flow_done and progress.completed == [] and progress.failure is None
    progress = driver._parse_pgr_progress(
        "任务 '领取体力' 的执行信息\nWHEN_TASK_SUCCESS\nTASK_FLOW_STOP manual=False\n",
        ["claim-serum"],
    )
    assert progress.flow_done and progress.completed == ["claim-serum"] and progress.failure is None
    progress = driver._parse_pgr_progress(
        "任务 '宿舍任务' 的执行信息\n识别错误，返回主菜单\n严重错误\nWHEN_TASK_SUCCESS\n所有任务都已完成\n",
        ["dorm"],
    )
    assert (
        progress.flow_done
        and progress.completed == []
        and progress.failure is not None
        and progress.failure_operation == "dorm"
        and "严重错误" in progress.failure
    )
    progress = driver._parse_pgr_progress(
        "识别错误，返回主菜单\n任务 '领取体力' 的执行信息\nWHEN_TASK_SUCCESS\n",
        ["claim-serum"],
    )
    assert progress.completed == ["claim-serum"] and progress.failure is None
    progress = driver._parse_pgr_progress(
        "任务 '宿舍任务' 的执行信息\n识别错误，返回主菜单\nWHEN_TASK_SUCCESS\n"
        "任务 '拟战场域' 的执行信息\nWHEN_TASK_SUCCESS\n",
        ["dorm", "simulation-field"],
    )
    assert progress.completed == ["dorm", "simulation-field"] and progress.failure is None
    progress = driver._parse_pgr_progress(
        "任务 '宿舍任务' 的执行信息\n识别错误，返回主菜单\n"
        "任务 '拟战场域' 的执行信息\nWHEN_TASK_SUCCESS\n",
        ["dorm", "simulation-field"],
    )
    assert progress.completed == ["simulation-field"]
    assert "dorm" in progress.recovered_failures
    assert "without completing dorm" in progress.recovered_failures["dorm"]
    marker = "MPA recovery grace expired after 90s for dorm: 识别错误，返回主菜单"
    assert driver._pgr_abort_reason("dorm", "dorm", marker) == f"MPA failure marker: {marker}"
    cascaded = driver._pgr_abort_reason("simulation-field", "dorm", marker)
    assert cascaded.startswith("cascaded_from=dorm; MPA failure marker: MPA recovery grace expired")
    assert "not_attempted=simulation-field" in cascaded
    progress = driver._parse_pgr_progress(
        "任务 '宿舍任务' 的执行信息\n"
        + driver.PGR_DORM_NO_EFFECT_MARKER
        + "\nWHEN_TASK_SUCCESS\n所有任务都已完成\n",
        ["dorm"],
    )
    assert progress.completed == [] and progress.failure_operation == "dorm"
    assert progress.failure.startswith("dorm_interaction_no_effect:")
    assert progress.recoverable_operation is None
    assert progress.flow_done  # A later success/flow marker cannot erase failure.
    progress = driver._parse_pgr_progress(
        driver.PGR_DORM_NO_EFFECT_MARKER
        + "\n任务 '领取体力' 的执行信息\nWHEN_TASK_SUCCESS\n",
        ["claim-serum"],
    )
    assert progress.completed == ["claim-serum"] and progress.failure is None

    profile = driver._pgr_profile(
        {
            "tasks": [
                {
                    "name": "Controller",
                    "is_checked": True,
                    "task_option": {
                        "controller_type": "Win32-Window-Front",
                        "Win32-Window-Front": {
                            "win32_screencap_methods": 32,
                            "mouse_input_methods": 1,
                            "keyboard_input_methods": 1,
                        },
                    },
                },
                {"name": "Resource", "is_checked": True, "task_option": {"resource": "zh_CN"}},
                {"name": "领取体力", "is_checked": False, "task_option": {"_speedrun_state": {"dirty": True}}},
                {"name": "购买物品", "is_checked": True, "task_option": {}},
                {"name": "研发", "is_checked": True, "task_option": {}},
            ]
        },
        ["claim-serum"],
        "test",
    )
    checked = [item["name"] for item in profile["tasks"] if item["is_checked"]]
    assert checked == ["Controller", "Resource", "领取体力"]
    controller = profile["tasks"][0]["task_option"]
    assert controller["controller_type"] == "Win32-Window-Background"
    assert controller["Win32-Window-Background"]["win32_screencap_methods"] == 16
    assert controller["Win32-Window-Background"]["mouse_input_methods"] == 1
    assert controller["Win32-Window-Background"]["keyboard_input_methods"] == 1
    with tempfile.TemporaryDirectory() as root:
        tool_root = Path(root)
        config_root = Path(root) / "config"
        config_root.mkdir()
        config_path = config_root / "config.json"
        config_path.write_text(
            json.dumps({
                "Security": {"acknowledged_resource_runs": "[]"},
                "Update": {"auto_update": False},
                "Bundle": {"bundle_auto_update": False},
            }),
            encoding="utf-8",
        )
        (tool_root / "i18n" / "zh_cn").mkdir(parents=True)
        (tool_root / "interface.json").write_text(
            json.dumps({
                "name": "FOS",
                "label": "$project.label",
                "github": "https://github.com/overflow65537/MAA_Punish",
                "contact": "$contact",
            }),
            encoding="utf-8",
        )
        (tool_root / "i18n" / "zh_cn.json").write_text(
            json.dumps({
                "project.label": "法奥斯之矛",
                "contact": "./i18n/zh_cn/CONTACT.md",
            }),
            encoding="utf-8",
        )
        (tool_root / "i18n" / "zh_cn" / "CONTACT.md").write_text(
            "trusted contact", encoding="utf-8"
        )
        fingerprint = driver._acknowledge_pgr_trusted_resource(tool_root)
        acknowledged_config = json.loads(config_path.read_text(encoding="utf-8"))
        assert json.loads(acknowledged_config["Security"]["acknowledged_resource_runs"]) == [fingerprint]
        driver._enable_pgr_formal_updates(Path(root))
        update_config = json.loads(config_path.read_text(encoding="utf-8"))
        assert update_config["Update"]["auto_update"] is True
        assert update_config["Bundle"]["bundle_auto_update"] is True
        tasks_root = tool_root / "tasks"
        tasks_root.mkdir()
        launch_task_path = tasks_root / "进入游戏.json"
        launch_task_path.write_text(
            json.dumps({
                "task": [{
                    "name": "进入游戏",
                    "pipeline_override": {
                        "检查主界面2": {
                            "action": {
                                "type": "Custom",
                                "param": {"custom_action": "Notice"},
                            }
                        }
                    },
                }]
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        startup_root = tool_root / "resource" / "base" / "pipeline"
        startup_root.mkdir(parents=True)
        startup_path = startup_root / "Start_up.jsonc"
        startup = {
                name: {
                    "recognition": {
                        "type": "OCR",
                        "param": {"roi": [999, 231, 281, 187], "expected": "^战斗$"},
                    }
                }
                for name in ("检查主界面", "检查主界面2")
            }
        startup["关闭点击空白弹窗"] = {
            "recognition": {
                "type": "Or",
                "param": {"any_of": ["关闭公告", "关闭拟真围剿通知", "月卡续期"]},
            },
            "action": {"type": "Click", "param": {"target": [10, 10]}},
        }
        startup_path.write_text(
            json.dumps(startup, ensure_ascii=False),
            encoding="utf-8",
        )
        dorm_path = startup_root / "Dorm" / "Dorm_Task.jsonc"
        dorm_path.parent.mkdir(parents=True)
        dorm_path.write_text(
            json.dumps(
                {
                    "宿舍任务_领取": {
                        "next": [
                            "今日任务已完成",
                            "[JumpBack]一键领取_宿舍任务",
                            "[JumpBack]关闭奖励",
                            "[JumpBack]打开宿舍任务",
                            "[JumpBack]选择每日任务",
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        driver._patch_pgr_home_handoff(tool_root)
        first_dorm_patch = dorm_path.read_bytes()
        driver._patch_pgr_home_handoff(tool_root)
        assert dorm_path.read_bytes() == first_dorm_patch
        dorm = json.loads(first_dorm_patch)
        stroke = dorm["夜雨_宿舍任务_从抚摸按钮拖到头部"]
        assert stroke["recognition"]["param"]["expected"] == "来回抚摸"
        action = stroke["action"]
        assert action["type"] == "Swipe"
        gesture = action["param"]
        assert gesture["begin"] == [640, 650]
        assert gesture["end"][0] == [640, 170]
        assert len(gesture["end"]) == len(gesture["duration"]) == len(gesture["end_hold"])
        assert all(550 <= x <= 730 and 120 <= y <= 230 for x, y in gesture["end"])
        directions = [b[0] - a[0] for a, b in zip(gesture["end"], gesture["end"][1:])]
        assert all(a * b < 0 for a, b in zip(directions, directions[1:]))
        assert sum(gesture["duration"]) + sum(gesture["end_hold"]) <= 5000
        assert gesture["only_hover"] is False and gesture["contact"] == 0
        assert "repeat" not in stroke
        gate = dorm["夜雨_宿舍任务_互动后复核"]
        # Only the original daily-page proof is a non-returning success exit.
        assert [name for name in gate["next"] if not name.startswith("[JumpBack]")] == ["今日任务已完成"]
        assert gate["timeout"] == 15000
        assert gate["on_error"] == ["夜雨_宿舍任务_无效果截图"]
        failure_shot = dorm[gate["on_error"][0]]
        assert failure_shot["action"]["param"]["custom_action_param"]["type"] == "PGR_DormNoEffect"
        failure_stop = dorm[failure_shot["next"][0]]
        assert failure_stop["action"] == {"type": "StopTask"}
        assert failure_stop["focus"]["Node.Recognition.Succeeded"] == driver.PGR_DORM_NO_EFFECT_MARKER
        assert failure_stop["next"] == failure_stop["on_error"] == []
        assert "前往互动" not in json.dumps(gate, ensure_ascii=False)
        assert "主菜单" not in json.dumps(failure_shot, ensure_ascii=False)
        launch_task = json.loads(launch_task_path.read_text(encoding="utf-8"))
        assert launch_task["task"][0]["pipeline_override"]["检查主界面2"]["next"] == ["空任务"]
        title_override = launch_task["task"][0]["pipeline_override"]["进入游戏"]
        assert title_override["recognition"]["param"]["roi"] == [250, 550, 780, 160]
        assert any(
            "点击任意处" in pattern
            for pattern in title_override["recognition"]["param"]["expected"]
        )
        assert title_override["action"]["param"]["target"] == [640, 660]
        startup = json.loads(startup_path.read_text(encoding="utf-8"))
        assert startup["检查主界面"]["recognition"]["param"]["roi"] == [940, 220, 300, 220]
        assert startup["检查主界面2"]["recognition"]["param"]["roi"] == [940, 220, 300, 220]
        assert startup["关闭点击空白弹窗"]["action"]["param"]["target"] == [640, 680]
        stale_root = Path(root) / "profiles"
        stale_root.mkdir()
        (stale_root / "c_yeyu_old.json").write_text("{}", encoding="utf-8")
        (stale_root / "user-profile.json").write_text("{}", encoding="utf-8")
        driver._cleanup_stale_pgr_run_profiles(stale_root)
        assert not (stale_root / "c_yeyu_old.json").exists()
        assert (stale_root / "user-profile.json").exists()
        multi_path = Path(root) / "config" / "multi_config.json"
        original_multi = b'{"curr_config_id":"default","config_list":["default"]}\n'
        multi_path.write_bytes(original_multi)
        abandoned = config_root / "configs"
        abandoned.mkdir()
        (abandoned / "c_yeyu_abandoned.json").write_text("{}", encoding="utf-8")
        multi_path.write_text(
            json.dumps(
                {
                    "curr_config_id": "c_yeyu_abandoned",
                    "config_list": ["default", "stable-user", "c_yeyu_abandoned"],
                    "bundle": {"MPA": {"path": "./"}},
                }
            ),
            encoding="utf-8",
        )
        driver._normalize_pgr_profile_registry(Path(root), "default")
        normalized = json.loads(multi_path.read_text(encoding="utf-8"))
        assert normalized["curr_config_id"] == "default"
        assert normalized["config_list"] == ["default", "stable-user"]
        assert normalized["bundle"] == {"MPA": {"path": "./"}}
        driver._cleanup_stale_pgr_run_profiles(abandoned)
        assert not (abandoned / "c_yeyu_abandoned.json").exists()
        original_multi = multi_path.read_bytes()
        registration = driver._activate_pgr_run_profile(Path(root), "c_yeyu_test")
        active_multi = json.loads(multi_path.read_text(encoding="utf-8"))
        assert active_multi["curr_config_id"] == "c_yeyu_test"
        assert active_multi["config_list"] == ["default", "stable-user", "c_yeyu_test"]
        driver._restore_pgr_run_profile(registration)
        assert multi_path.read_bytes() == original_multi
    plan = driver._nikke_plan("outpost", "nikke_outpost.json", "selected")
    assert plan["root"]["type"] == "subtree"
    assert plan["root"]["params"] == {"tree_path": "nikke_outpost.json"}
    assert "nikke_daily_shop.json" not in json.dumps(plan)

    source = DRIVER_PATH.read_text(encoding="utf-8")
    assert 'launcher = tool_root / "OneDragon-Launcher.exe"' in source
    assert '[str(launcher), "--onedragon"]' in source
    assert "visible_windows(owned_pids(gui_started_at))" in source
    assert "protected_pids = _process_lineage_pids(psutil.Process, os.getpid())" in source
    assert 'if int(process.info["pid"]) in protected_pids:' in source
    assert "run_application(app_id" not in source
    assert "Behavior Tree Result success" in source
    assert 'str(gui_entry), "--task", "1", "--exit"' in source
    assert 'environment["YEYU_NIKKE_TREE_FOLDER"] = str(selected_root)' in source
    assert '"-m", "ok.cli"' not in source
    assert "MPA flow ended without this task's success marker" in source
    assert "process_exited_at" in source
    assert "without a complete task-flow marker" in source
    assert "_activate_pgr_run_profile(tool_root, config_id)" in source
    assert "_normalize_pgr_profile_registry(tool_root, base_path.stem)" in source
    assert "_restore_pgr_run_profile(registration)" in source
    assert "wait_for_formal_update()" in source
    assert "_tail_since(gui_log, log_start)" in source
    assert "YEYU_GAMER_PGR_RECOVERY_GRACE_SECONDS" in source
    assert "MPA recovered to main menu without completing" in source
    assert "_pgr_abort_reason(operation, cascade_from, progress.failure)" in source
    assert "_acknowledge_pgr_trusted_resource(tool_root)" in source
    assert "_patch_pgr_home_handoff(tool_root)" in source
    assert "pgr_resource_identity_requires_human_review" in source
    execution_source = source[
        source.index("    _normalize_pgr_profile_registry"):
        source.index("def run_zzz(")
    ]
    assert "wait_for_formal_relaunch_barrier()" not in execution_source
    assert "pgr_formal_update_handoff_lost" in source
    assert "formal_gui_windows" in source
    assert "ready-GUI direct-run emitted no task start within 120 seconds" in source
    assert "no_task_deadline = time.monotonic() + 120" in source
    assert "time.monotonic() >= no_task_deadline" in source
    assert '--force-restart --config-id \"{config_id}\"' in source
    assert '--reuse-existing --config-id \"{config_id}\" --direct-run' in source
    update_wait_source = source[
        source.index("    def wait_for_formal_update()"):
        source.index("    def wait_for_formal_relaunch_barrier()")
    ]
    assert update_wait_source.count("formal_started_at, formal_launch = shell_launch()") == 1
    assert "relaunches" not in update_wait_source
    pgr_run_source = source[
        source.index("    run_launch: _OwnedProcess | None = None"):
        source.index("def run_zzz(")
    ]
    assert "wait_for_execution_gui_ready(start_size, config_id, run_launch)" in pgr_run_source
    assert pgr_run_source.count("--direct-run") == 1
    assert "ShellExecuteExW" in source
    assert "SEE_MASK_NOCLOSEPROCESS" in source
    assert "include_launched" in source
    assert "WaitForSingleObject" in source
    assert "_OwnedProcess(pid, info.hProcess)" in source
    pgr_source = source[source.index("def run_pgr("):source.index("def run_zzz(")]
    assert "import psutil" not in pgr_source
    assert "EnumProcesses" in source
    assert 'psapi.EnumProcesses.restype = wintypes.BOOL' in source
    assert 'OpenProcess.restype = wintypes.HANDLE' in source
    assert "当前已是最新版本" in source
    assert source.index("wait_for_formal_update()") < source.index("config_id =")
    assert pgr_run_source.index("wait_for_execution_gui_ready(start_size, config_id, run_launch)") < pgr_run_source.index("_patch_pgr_home_handoff(tool_root)") < pgr_run_source.index("--reuse-existing")
    assert "pgr_home_ocr_identity_changed" in source
    assert "pgr_blank_popup_identity_changed" in source
    assert "expected_root in image.parents" in source
    assert "subprocess.Popen(\n            [str(mfw)" not in source
    zzz_source = source[source.index("def run_zzz("):source.index("def _nikke_plan(")]
    assert "fresh run record entered running" in zzz_source
    assert "terminal record arrived without observed running transition" in zzz_source
    assert "返回大世界 ] 执行成功 返回状态 大世界-普通" in zzz_source
    assert "OneDragon confirmed an operable normal-world frame" in zzz_source
    assert "if attach_pending:" in zzz_source
    assert "OneDragon formal GUI reached a visible ready state after its update check" not in zzz_source
    assert 'reset_record["run_status"] = 0' in zzz_source
    assert "subprocess.run(\n            [str(launcher), \"--onedragon\"]" not in zzz_source
    assert "OneDragon formal launcher selected app=" not in zzz_source
    runner_source = (ROOT / "adapter-host" / "classic-runner" / "Program.cs").read_text(encoding="utf-8")
    assert "Console.OutputEncoding = new UTF8Encoding(false);" in runner_source
    assert '"reason", ProtocolText(reason)' in runner_source
    assert "FileShare.ReadWrite | FileShare.Delete" in runner_source
    assert "Char.IsControl(character)" in runner_source
    assert '"battle-pass-free-track"' in runner_source
    assert '"city-fund-free-claim"' in runner_source
    assert "return exitCode;" in runner_source
    print(json.dumps({"status": "passed", "tests": 50, "dormGestureRegression": True, "gameStarted": False}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
