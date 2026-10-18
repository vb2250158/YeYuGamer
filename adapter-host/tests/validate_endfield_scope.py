"""Feed compiled production overrides to the official plan and task runner."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace


def main():
    replay, adapter, source = sys.argv[1:]
    source = Path(source)
    plan_file = source / "src/tasks/onetime/DailyTask.py"
    tree = ast.parse(plan_file.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DailyTask")
    plan = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "build_task_plan")
    scope = {}
    exec(compile(ast.Module(body=[plan], type_ignores=[]), str(plan_file), "exec"), scope)
    ok = ModuleType("ok")
    ok.TaskDisabledException = type("TaskDisabledException", (Exception,), {})
    sys.modules["ok"] = ok
    runner_file = source / "src/tasks/daily/daily_task_runner.py"
    spec = importlib.util.spec_from_file_location("official_runner", runner_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operations = {
        "mail": "claim_mail", "spend-sanity": "battle",
        "delivery-commission": "delivery_send_others", "collect-credit": "collect_credit",
        "dijiang-harvest": "boat_claim_rewards", "claim-daily-reward": "claim_daily_rewards",
    }
    calls = []

    class Feature:
        def __getattr__(self, name):
            def execute(*args, **kwargs):
                calls.append(name)
            return execute

    task = SimpleNamespace(**{name: Feature() for name in (
        "daily_liaison", "daily_routine", "delivery", "daily_regional", "daily_shop", "daily_battle", "daily_demo")})
    task.box = SimpleNamespace(right=None)
    task.transfer_to_home_point = lambda **kwargs: calls.append("transfer_to_home_point")
    task.ensure_main = lambda: None
    task.send_key = lambda *args: None
    task.log_info = lambda *args, **kwargs: None
    task.tr = lambda value: value
    with tempfile.TemporaryDirectory() as directory:
        for selected in [list(operations), ["mail"], []]:
            calls.clear()
            task.config = {}
            items = scope["build_task_plan"](task)
            baseline = {entry[0]: True for entry in items}
            baseline.update({"⭐帝江号收菜": ["收集线索"], "⭐地区建设": ["买物资"],
                "⭐活动奖励": ["活动奖励"], "⭐帝江号一键存放": True, "⭐简易制作": True,
                "⭐执行外部命令": True, "体力本": "干员经验", "untouched-parameter": {"value": 7}})
            path = Path(directory) / "DailyTask.json"
            path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
            subprocess.run([replay, adapter, str(path), ",".join(selected)], check=True)
            task.config = json.loads(path.read_text(encoding="utf-8"))
            items.append(("⭐执行外部命令", lambda: calls.append("external_command")))
            runner = module.DailyTaskRunner(task, items)
            for item in items:
                runner.execute_task(*item)
            assert set(calls) == {operations[key] for key in selected}, (selected, calls)
            assert len(calls) == len(selected), calls
            assert task.config["untouched-parameter"] == {"value": 7}
            assert task.config["体力本"] == "干员经验"
            if "dijiang-harvest" in selected:
                assert task.config["⭐帝江号收菜"] == ["收集线索"]
    print(json.dumps({"passed": True, "officialPlanAndRunner": True, "cases": 3, "gameStarted": False}))


if __name__ == "__main__":
    main()
