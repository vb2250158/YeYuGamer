"""Replay the packaged WW source patch using condition-only fake game state.

Only DailyTask.run is compiled from the patched snapshot. No game/tool modules
are imported, no window is captured, and no installed source is modified.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace


NIGHTMARE = "farm-nightmare-daily-echo"
STAMINA = "spend-waveplates"


def load_run(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    task = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DailyTask")
    run = next(node for node in task.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    namespace = {
        "WWOneTimeTask": SimpleNamespace(run=lambda task: None),
        "ADDITIONAL_TASKS": "Additional Tasks to Run After Daily Task",
        "AUTO_FARM_NIGHTMARE_NEST": "auto-farm-nightmare",
    }
    exec(compile(ast.Module(body=[run], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["run"]


class DailyConditions:
    def __init__(self, *, selected, present=False, completed=False, used=180, ready=True, readable=True):
        self._yeyu_selected_operations = set(selected)
        self._last_nightmare_daily_present = present
        self._last_nightmare_daily_completed = completed
        self._last_daily_points = 100
        self.config = {"Farm Nightmare Nest for Daily Echo": True}
        self.used = used
        self.ready = ready
        self.readable = readable
        self.events = []

    def yeyu_operation_enabled(self, operation):
        return operation in self._yeyu_selected_operations

    def yeyu_stage(self, operation, state, detail=""):
        if self.yeyu_operation_enabled(operation):
            self.events.append((operation, state, detail))

    def yeyu_failed(self, operation, error):
        self.yeyu_stage(operation, "failed", str(error))

    def open_daily(self):
        if not self.readable:
            raise RuntimeError("daily condition cannot be read")
        return self.used, self.ready

    def validate_additional_tasks(self):
        pass

    def wait_feature(self, *args, **kwargs):
        return False

    def ensure_main(self, *args, **kwargs):
        pass

    def run_additional_tasks(self):
        pass

    def log_info(self, *args, **kwargs):
        pass


def assert_terminal_after_start(task, operation, terminal):
    events = [(state, detail) for op, state, detail in task.events if op == operation]
    assert [state for state, _ in events] == ["started", terminal], events
    assert "evaluating current" in events[0][1], events
    assert events[1][1], "condition terminal must preserve the upstream reason"


def main():
    run = load_run(Path(sys.argv[1]))
    cases = 0
    for present, completed, expected in (
        (False, False, "completed"),
        (True, True, "completed"),
        (True, False, "skipped"),
    ):
        task = DailyConditions(selected={NIGHTMARE, STAMINA}, present=present, completed=completed)
        run(task)
        assert_terminal_after_start(task, NIGHTMARE, expected)
        assert_terminal_after_start(task, STAMINA, "skipped")
        assert "nightmare_present=" in task.events[0][2]
        assert "used_stamina=180" in task.events[2][2]
        cases += 1

    task = DailyConditions(selected={STAMINA}, ready=False)
    run(task)
    assert_terminal_after_start(task, STAMINA, "skipped")
    assert not any(op == NIGHTMARE for op, _, _ in task.events)
    cases += 1

    task = DailyConditions(selected=set())
    run(task)
    assert task.events == [], task.events
    cases += 1

    task = DailyConditions(selected={NIGHTMARE, STAMINA}, readable=False)
    try:
        run(task)
        raise AssertionError("unknown daily conditions must fail")
    except RuntimeError as error:
        assert str(error) == "daily condition cannot be read"
    assert task.events == [], "an unreadable condition must not create a completed Todo"
    cases += 1
    print(json.dumps({"status": "passed", "cases": cases, "gameStarted": False, "toolStarted": False}))


if __name__ == "__main__":
    main()
