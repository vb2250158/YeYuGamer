"""Replay login ownership and results; no game/window input is performed."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adapter-host/openkuro-runner'))
spec = importlib.util.spec_from_file_location(
    "endfield_bridge", ROOT / "adapter-host/openkuro-runner/EndfieldYeYuBridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def main():
    captures = []
    bridge._login_capture = lambda task, phase, kind: captures.append(phase)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "stages.jsonl"
        os.environ["YEYU_GAMER_STAGE_FILE"] = str(path)

        class Task:
            def __init__(self, texts=("长时间没有操作自动登出", "确认")):
                self.texts = texts
                self.calls = []
                self.executor = SimpleNamespace(nullable_frame=lambda: object())
                self.original_result = False
                self.error = None
                self.click_error = None

            def log_info(self, text):
                pass

            def ocr(self, *, frame, match):
                self.calls.append("ocr")
                return [SimpleNamespace(y=index * 100, height=20, name=text)
                        for index, text in enumerate(self.texts) if match.search(text)]

            def click(self, button):
                self.calls.append("click")
                if self.click_error:
                    raise self.click_error

            def wait_login(self):
                self.calls.append("official_login")
                return self.original_result

            def ensure_main(self):
                self.wait_login()
                self.wait_login()
                if self.error:
                    raise self.error
                return None

        bridge._install_login_handoff(Task)
        for texts in (("确认",), ("其他登出提示", "确认"),
                      ("长时间没有操作自动登出", "确认", "确认"),
                      ("确认", "长时间没有操作自动登出")):
            task = Task(texts)
            assert task.wait_login() is False
            assert "click" not in task.calls
        task = Task()
        task.original_result = True
        assert task.wait_login() is True and task.calls == ["official_login"]
        task = Task()
        task.executor.nullable_frame = lambda: None
        assert task.wait_login() is False and task.calls == ["official_login"]

        task = Task()
        assert task.wait_login() is False
        assert task._yeyu_idle_logout == "pending"
        assert task.calls[0] == "official_login" and task.calls.count("click") == 1
        assert task.ensure_main() is None
        assert task._yeyu_idle_logout == "completed" and task.calls.count("click") == 1
        assert captures == ["before", "after"]
        records = [json.loads(json.loads(line)["detail"]) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [item["metrics"]["integrationStatus"] for item in records] == ["pending", "pending", "completed"]

        for click_failure in (False, True):
            task = Task()
            error = RuntimeError("official failure")
            if click_failure:
                task.click_error = error
            else:
                task.error = error
            try:
                task.ensure_main()
            except RuntimeError as caught:
                assert caught is error
            else:
                raise AssertionError("official exception swallowed")
            assert task._yeyu_idle_logout == "failed"
            assert task.calls.count("click") == 1
            task.wait_login()
            assert task.calls.count("click") == 1
        bridge._install_login_handoff(Task, "title_login")
        for texts in (("点击任意位置继续", "点击任意位置继续"), ("请点击任意位置继续",), ("点击开始",)):
            task = Task(texts)
            task.wait_login()
            assert "click" not in task.calls
        task = Task(("点击任意位置继续",))
        task._logged_in = True
        task.wait_login()
        assert "click" not in task.calls
        task = Task(("点击任意位置继续",))
        task.wait_login()
        assert task._yeyu_title_login == "pending" and task.calls.count("click") == 1
        task.ensure_main()
        assert task._yeyu_title_login == "completed" and task.calls.count("click") == 1
        task = Task(("点击任意位置继续",))
        task.error = RuntimeError("world still unavailable")
        try:
            task.ensure_main()
        except RuntimeError as caught:
            assert caught is task.error
        else:
            raise AssertionError("official world failure swallowed")
        task.wait_login()
        assert task._yeyu_title_login == "failed" and task.calls.count("click") == 1
        # Native clicks invalidate the executor buffer. The outer title hook
        # must not act on the frame preceding the idle-dialog confirmation.
        task = Task(("长时间没有操作自动登出", "确认", "点击任意位置继续"))
        def click(button):
            task.calls.append("click")
            task.executor.nullable_frame = lambda: None
        task.click = click
        task.wait_login()
        assert task.calls.count("click") == 1 and not hasattr(task, "_yeyu_title_login")
    print(json.dumps({"status": "passed", "gameStarted": False, "inputPerformed": False, "titleLogin": True}))


if __name__ == "__main__":
    main()
