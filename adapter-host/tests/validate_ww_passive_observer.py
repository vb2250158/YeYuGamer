"""No-game replay for the OK-WW passive observer contract."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import types


ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT / "openkuro-runner" / "WwYeYuObserver.py"
OFFICIAL_DAILY = ROOT.parents[0] / "vendor-baselines" / "ok-wuthering-waves" / "src" / "task" / "DailyTask.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "openkuro-runner"))


def _module(name: str, attribute: str, value: object) -> None:
    module = types.ModuleType(name)
    setattr(module, attribute, value)
    sys.modules[name] = module


class BaseWWTask:
    def ensure_main(self, *args, **kwargs):
        return "main"


class DailyTask(BaseWWTask):
    def run(self):
        return "daily"

    def open_daily(self):
        return (0, False)

    def claim_daily(self):
        raise ValueError("upstream failure")

    def claim_mail(self):
        return "mail"

    def claim_battle_pass(self):
        return "battle-pass"

    def inspect_battle_pass_status(self):
        self.inspect_battle_pass_page('tasks')
        self.inspect_battle_pass_page('rewards')
        return self.status_result

    def inspect_battle_pass_page(self, page):
        self.page_calls.append(page)
        if page not in ('tasks', 'rewards'):
            raise self.page_error
        return self.page_results[page]


class NightmareNestTask:
    def run_capture_mode(self):
        return "nightmare"


class TacetTask:
    def farm_tacet(self, *args, **kwargs):
        return "tacet"


class ForgeryTask:
    def farm_forgery(self, *args, **kwargs):
        return "forgery"


class SimulationTask:
    def farm_simulation(self, *args, **kwargs):
        return "simulation"


def main() -> None:
    sys.modules["src"] = types.ModuleType("src")
    sys.modules["src.task"] = types.ModuleType("src.task")
    _module("src.task.DailyTask", "DailyTask", DailyTask)
    _module("src.task.BaseWWTask", "BaseWWTask", BaseWWTask)
    _module("src.task.NightmareNestTask", "NightmareNestTask", NightmareNestTask)
    _module("src.task.TacetTask", "TacetTask", TacetTask)
    _module("src.task.ForgeryTask", "ForgeryTask", ForgeryTask)
    _module("src.task.SimulationTask", "SimulationTask", SimulationTask)
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory) / "stage.jsonl"
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)
        # This is deliberately a directory: logging installation must fail open.
        os.environ["YEYU_GAMER_UPSTREAM_LOG_FILE"] = directory
        spec = importlib.util.spec_from_file_location("ww_observer_test", OBSERVER)
        observer = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(observer)
        observer.install()
        assert DailyTask().claim_mail() == "mail"
        try:
            DailyTask().claim_daily()
        except ValueError as error:
            assert str(error) == "upstream failure"
        else:
            raise AssertionError("observer swallowed the upstream exception")
        task = DailyTask()
        task.config = {'Battle Pass Status Inspection Only': True}
        task.page_calls = []
        task.status_result = object()
        task.page_results = {'tasks': object(), 'rewards': object()}
        task.page_error = ValueError('official-page-failure')
        assert task.inspect_battle_pass_status() is task.status_result
        assert task.page_calls == ['tasks', 'rewards']
        for page in ('other-page', ['invalid-page-type']):
            try:
                task.inspect_battle_pass_page(page)
            except ValueError as error:
                assert error is task.page_error
            else:
                raise AssertionError('page observation swallowed the official exception')
        records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        assert any(item["operation"] == "claim-mail" and item["state"] == "completed" for item in records)
        assert any(item["operation"] == "claim-daily-reward" and item["state"] == "failed" for item in records)
        bp_terminals = [item['state'] for item in records if item['operation'] == 'claim-battle-pass'
                        and item['state'] in {'started', 'completed', 'failed'}]
        assert bp_terminals == ['started', 'completed'], 'page scopes must not add task terminals'
    print("ww_passive_observer_ok")



def _official_daily_run():
    """Compile the audited upstream method itself, with window/tool dependencies replaced."""
    tree = ast.parse(OFFICIAL_DAILY.read_text(encoding="utf-8"), filename=str(OFFICIAL_DAILY))
    daily = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DailyTask")
    run = next(node for node in daily.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    namespace = {
        "WWOneTimeTask": types.SimpleNamespace(run=lambda _task: None),
        "TaskDisabledException": type("TaskDisabledException", (Exception,), {}),
        "ADDITIONAL_TASKS": "Additional Tasks to Run After Daily Task",
        "AUTO_FARM_NIGHTMARE_NEST": "Auto Farm all Nightmare Nest",
    }
    for class_name in ("FarmEchoTask", "ForgeryTask", "GardenTask", "MergeEchoTask", "NightmareNestTask", "TacetTask", "SimulationTask"):
        namespace[class_name] = type(class_name, (), {})
    exec(compile(ast.Module(body=[run], type_ignores=[]), str(OFFICIAL_DAILY), "exec"), namespace)
    return namespace["run"], namespace


def _condition_replay(observer, *, used: int, ready: bool, route: str, additional=(), fail_open: bool = False):
    upstream_run, namespace = _official_daily_run()

    class Base:
        def ensure_main(self, *_args, **_kwargs):
            self.calls.append("ensure-main")

    class Nightmare(namespace["NightmareNestTask"]):
        def __init__(self, calls): self.calls = calls
        def run(self): self.calls.append("nightmare-all")
        def run_capture_mode(self): self.calls.append("nightmare")

    class Forge(namespace["ForgeryTask"]):
        def __init__(self, calls): self.calls = calls
        def farm_forgery(self, *_args, **_kwargs): self.calls.append("stamina")

    class Tacet(namespace["TacetTask"]):
        def __init__(self, calls): self.calls = calls
        def farm_tacet(self, *_args, **_kwargs): self.calls.append("tacet")

    class Simulation(namespace["SimulationTask"]):
        def __init__(self, calls): self.calls = calls
        def farm_simulation(self, *_args, **_kwargs): self.calls.append("simulation")

    namespace["NightmareNestTask"] = Nightmare
    namespace["ForgeryTask"] = Forge
    namespace["TacetTask"] = Tacet
    namespace["SimulationTask"] = Simulation

    class ActualDaily(Base):
        run = upstream_run
        def __init__(self):
            self.calls = []
            self.support_tasks = ["Tacet Suppression", "Forgery Challenge", "Simulation Challenge"]
            self.config = {
                "Farm Nightmare Nest for Daily Echo": True,
                "Which to Farm": route,
                "Additional Tasks to Run After Daily Task": list(additional),
            }
            self.nightmare = Nightmare(self.calls)
            self.forge = Forge(self.calls)
            self.tacet = Tacet(self.calls)
            self.simulation = Simulation(self.calls)
        def validate_additional_tasks(self): pass
        def open_daily(self):
            if fail_open: raise ValueError("official-open-daily-failure")
            return used, ready
        def get_task_by_class(self, task_type):
            return {Nightmare: self.nightmare, Forge: self.forge, Tacet: self.tacet, Simulation: self.simulation}.get(task_type)
        def run_task_by_class(self, task_type):
            return self.get_task_by_class(task_type).run()
        def claim_daily(self): self.calls.append("claim-daily")
        def claim_mail(self): self.calls.append("claim-mail")
        def claim_battle_pass(self): self.calls.append("claim-battle-pass")
        def run_additional_tasks(self): self.calls.append("additional")
        def sleep(self, *_args, **_kwargs): pass
        def log_info(self, *_args, **_kwargs): pass
        def log_debug(self, *_args, **_kwargs): pass
        def log_error(self, *_args, **_kwargs): pass
        def screenshot(self, *_args, **_kwargs): pass

    modules = {
        "src.task.DailyTask": types.SimpleNamespace(DailyTask=ActualDaily),
        "src.task.BaseWWTask": types.SimpleNamespace(BaseWWTask=Base),
        "src.task.NightmareNestTask": types.SimpleNamespace(NightmareNestTask=Nightmare),
        "src.task.TacetTask": types.SimpleNamespace(TacetTask=Tacet),
        "src.task.ForgeryTask": types.SimpleNamespace(ForgeryTask=Forge),
        "src.task.SimulationTask": types.SimpleNamespace(SimulationTask=Simulation),
    }
    return ActualDaily, modules


def _condition_records(*, used: int, ready: bool, route: str, additional=(), fail_open: bool = False, profile_failure: bool = False):
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory) / "stage.jsonl"
        spec = importlib.util.spec_from_file_location("ww_observer_condition_test", OBSERVER)
        observer = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(observer)
        daily_type, modules = _condition_replay(
            observer,
            used=used,
            ready=ready,
            route=route,
            additional=additional,
            fail_open=fail_open,
        )
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"YEYU_GAMER_STAGE_FILE": str(stage)}, clear=False):
            observer.install()
            if profile_failure:
                with patch.object(observer.sys, "setprofile", side_effect=RuntimeError("observer profiler unavailable")):
                    result = daily_type().run()
            else:
                result = daily_type().run()
        records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        return result, records


def _assert_condition_replays() -> None:
    # These calls execute DailyTask.run parsed directly from the pinned official source.
    result, records = _condition_records(used=180, ready=True, route="Tacet Suppression")
    assert result is None
    skipped = {row["operation"] for row in records if row["state"] == "skipped"}
    assert skipped == {"farm-nightmare-daily-echo", "spend-waveplates"}, records
    assert not any(row["operation"] in skipped and row["state"] == "started" for row in records), records
    assert not any(row["operation"] in skipped and row["state"] == "completed" for row in records), records
    assert all("official task body not entered" in row["detail"] for row in records if row["state"] == "skipped")

    result, records = _condition_records(used=0, ready=False, route="Forgery Challenge")
    assert result is None
    assert not any(row["operation"] in {"farm-nightmare-daily-echo", "spend-waveplates"} and row["state"] == "skipped" for row in records), records
    assert any(row["operation"] == "farm-nightmare-daily-echo" and row["state"] == "completed" for row in records)
    assert any(row["operation"] == "spend-waveplates" and row["state"] == "completed" for row in records)

    # This executes the actual condition1 branch in the pinned DailyTask.run:
    # AUTO_FARM_NIGHTMARE_NEST calls NightmareNestTask.run(), not capture mode.
    result, records = _condition_records(
        used=180,
        ready=True,
        route="Tacet Suppression",
        additional=("Auto Farm all Nightmare Nest",),
    )
    assert result is None
    nightmare_completed = [
        row for row in records
        if row["operation"] == "farm-nightmare-daily-echo" and row["state"] == "completed"
    ]
    assert len(nightmare_completed) == 1, records
    assert nightmare_completed[0]["detail"] == "upstream:run:returned:None", records
    assert not any(
        row["operation"] == "farm-nightmare-daily-echo" and row["state"] == "skipped"
        for row in records
    ), records

    try:
        _condition_records(used=180, ready=True, route="Tacet Suppression", fail_open=True)
        raise AssertionError("official exception was swallowed")
    except ValueError as error:
        assert str(error) == "official-open-daily-failure"

    result, records = _condition_records(used=180, ready=True, route="Tacet Suppression", profile_failure=True)
    assert result is None
    assert not any(row["state"] == "skipped" for row in records), records



def _assert_multi_account_failure_is_observed():
    spec = importlib.util.spec_from_file_location("ww_multi_failure_observer", OBSERVER)
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    failure = RuntimeError("official account selection timed out")
    class Multi:
        def run(self):
            raise failure
    observer._observe_multi_account(Multi)
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory) / "stage.jsonl"
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(stage)
        try:
            Multi().run()
        except RuntimeError as caught:
            assert caught is failure
        else:
            raise AssertionError("official failure was swallowed")
        records = [json.loads(line) for line in stage.read_text(encoding="utf-8").splitlines()]
        root = [row for row in records if row["operation"] == "upstream-multi-account-task"]
        assert [row["state"] for row in root] == ["started", "failed"]
        assert "official account selection timed out" in root[-1]["detail"]
        assert observer._current_account["label"] is None


if __name__ == "__main__":
    main()
    _assert_condition_replays()
    _assert_multi_account_failure_is_observed()
    print("ww_passive_observer_condition_replay_ok")
