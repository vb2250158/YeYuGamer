"""Offline saved-account replay: fake OCR/windows/input only, no tool import."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parents[1] / "openkuro-runner" / "WwAccountYeYuBridge.py"
spec = importlib.util.spec_from_file_location("ww_account_bridge_replay", SOURCE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
A = "alpha****example.com"
B = "beta****example.com"


def box(name):
    return SimpleNamespace(name=name)


class FakeGame:
    def __init__(self, state="login", current=A, accounts=None, gate=None, menu=True, confirm=True, login=True):
        self.state = state
        self.current = current
        self.accounts = accounts or [A, B]
        self.gate = gate
        self.menu = menu
        self.confirm = confirm
        self.login = login
        self.actions = []
        self.logged_in = False
        self.on_click = None

    def next_frame(self):
        pass

    def ocr(self):
        if self.gate:
            return [box(self.gate)]
        if self.state == "login":
            return [box(self.current)] + ([box("登录")] if self.login else [])
        if self.state == "dropdown":
            return [box(name) for name in self.accounts]
        if self.state == "confirm":
            return [box("是否返回登录界面"), box("确认")]
        return []

    def in_team_and_world(self):
        return self.state == "world"

    def click(self, target):
        self.actions.append(("click", target.name))
        if self.on_click:
            self.on_click(target)
        if target.name == "登录":
            self.state = "world"
        elif target.name == "确认":
            self.state = "login"
        elif self.state == "login" and target.name == self.current:
            self.state = "dropdown"
        elif self.state == "dropdown":
            self.current = target.name
            self.state = "login"

    def send_key(self, key):
        self.actions.append(("key", key))
        self.state = "menu"

    def wait_feature(self, name, **kwargs):
        if not self.menu:
            raise RuntimeError("menu not observed")
        return box(name)

    def find_one(self, name, **kwargs):
        if name == "esc_setting":
            return box(name) if self.menu and self.state == "menu" else None
        return box("确认") if self.confirm and self.state == "confirm" else None

    def click_relative(self, x, y):
        self.actions.append(("relative", x, y))
        self.state = "confirm"

    def find_boxes(self, boxes, **kwargs):
        return boxes


def upstream_logout(task):
    # Exact formal upstream sequence; the facade validates each visible state.
    task.send_key("esc", after_sleep=1.5)
    task.wait_feature("esc_setting")
    task.click_relative(0.04, 0.96, after_sleep=1)
    task.click_confirm(timeout=10)
    task.find_account_drop_down()


class AccountReplay(unittest.TestCase):
    def switch(self, game, expected=B, report=None):
        events = []
        controller = bridge.AccountSwitch(game, expected, report or (lambda state, detail: events.append((state, detail))), upstream_logout, lambda _: None)
        return controller, events

    def test_selects_one_exact_saved_account_and_confirms_world_before_success(self):
        game = FakeGame()
        controller, events = self.switch(game)
        controller.run()
        self.assertEqual(game.actions, [("click", A), ("click", B), ("click", "登录")])
        self.assertTrue(controller.ready)
        self.assertEqual([state for state, _ in events], ["account_capture_before", "account_capture_list", "account_capture_selected", "account_capture_after", "account_verified"])
        self.assertIn("UID not verified", events[-1][1])

    def test_already_selected_label_still_checks_open_list_uniqueness(self):
        game = FakeGame(current=B)
        controller, _ = self.switch(game)
        controller.run()
        self.assertEqual(game.actions, [("click", B), ("click", B), ("click", "登录")])

    def test_world_uses_upstream_scene_anchored_logout_once(self):
        game = FakeGame(state="world")
        controller, _ = self.switch(game)
        controller.run()
        self.assertEqual(game.actions.count(("relative", 0.04, 0.96)), 1)
        self.assertEqual(game.actions.count(("click", "确认")), 1)
        self.assertEqual(game.actions.count(("click", "登录")), 1)

    def test_missing_or_duplicate_label_never_clicks_an_account(self):
        for accounts in ([A, "other****example.com"], [A, B, B]):
            with self.subTest(accounts=accounts):
                game = FakeGame(accounts=accounts)
                controller, _ = self.switch(game)
                with self.assertRaises(bridge.AccountHumanRequired):
                    controller.run()
                self.assertEqual(game.actions, [("click", A)])

    def test_ocr_similarity_is_not_account_identity(self):
        with self.assertRaises(bridge.AccountHumanRequired):
            bridge.exact_saved_account([box("name0****example.com")], "nameo****example.com")

    def test_credentials_terms_and_verification_never_receive_input(self):
        for gate in ("输入密码", "短信验证码", "请阅读并同意隐私政策", "+86", "扫码登录", "实名验证"):
            with self.subTest(gate=gate):
                game = FakeGame(gate=gate)
                controller, _ = self.switch(game)
                with self.assertRaises(bridge.AccountHumanRequired):
                    controller.run()
                self.assertEqual(game.actions, [])

    def test_passive_footer_and_alternate_login_tabs_do_not_block_saved_account(self):
        game = FakeGame()
        original_ocr = game.ocr
        game.ocr = lambda: original_ocr() + [box("隐私政策"), box("用户协议"), box("密码登录"), box("验证码登录")]
        controller, _ = self.switch(game)
        controller.run()
        self.assertTrue(controller.ready)
        self.assertEqual(game.actions[-1], ("click", "登录"))

    def test_active_consent_dialog_with_background_saved_account_blocks(self):
        game = FakeGame()
        original_ocr = game.ocr
        game.ocr = lambda: original_ocr() + [box("隐私政策"), box("同意")]
        controller, _ = self.switch(game)
        with self.assertRaises(bridge.AccountHumanRequired):
            controller.run()
        self.assertEqual(game.actions, [])

    def test_no_verified_login_button_has_no_coordinate_fallback(self):
        game = FakeGame(login=False)
        controller, _ = self.switch(game)
        with self.assertRaises(bridge.AccountHumanRequired):
            controller.run()
        self.assertEqual(game.actions, [])

    def test_missing_menu_anchor_prevents_logout_click(self):
        game = FakeGame(state="world", menu=False)
        controller, _ = self.switch(game)
        with self.assertRaises(RuntimeError):
            controller.run()
        self.assertEqual(game.actions, [("key", "esc")])

    def test_missing_confirmation_never_retries_logout(self):
        game = FakeGame(state="world", confirm=False)
        controller, _ = self.switch(game)
        with self.assertRaises(bridge.AccountHumanRequired):
            controller.run()
        self.assertEqual(game.actions, [("key", "esc"), ("relative", 0.04, 0.96)])

    def test_account_change_during_capture_handoff_prevents_login(self):
        game = FakeGame(current=B)
        def report(state, _):
            if state == "account_capture_selected":
                game.current = A
        controller, _ = self.switch(game, report=report)
        with self.assertRaises(bridge.AccountHumanRequired):
            controller.run()
        self.assertEqual(game.actions, [("click", B), ("click", B)])

    def test_unknown_scene_never_sends_escape_or_selects_account(self):
        game = FakeGame(state="unknown")
        controller, _ = self.switch(game)
        with self.assertRaises(bridge.AccountHumanRequired):
            controller.run()
        self.assertEqual(game.actions, [])

    def test_login_without_world_effect_never_reports_verified(self):
        game = FakeGame(current=B)
        controller, events = self.switch(game)
        original_click = game.click
        def ignored_click(target):
            original_click(target)
            if target.name == "登录":
                game.state = "waiting"
        game.click = ignored_click
        counter = iter(range(0, 1000, 10))
        with patch.object(bridge.time, "monotonic", side_effect=lambda: next(counter)):
            with self.assertRaises(bridge.AccountHumanRequired):
                controller.run()
        self.assertFalse(controller.ready)
        self.assertNotIn("account_verified", [state for state, _ in events])
        self.assertEqual(game.actions, [("click", B), ("click", B), ("click", "登录")])

    def test_legacy_run_does_not_import_or_patch_tool_classes(self):
        config = {"trigger_tasks": [["does.not.exist", "Unrelated"]]}
        with patch.object(bridge, "load_request", return_value=None):
            bridge.install(config, "unused.py")
        self.assertEqual(config["trigger_tasks"], [["does.not.exist", "Unrelated"]])

    def test_formal_install_gates_background_login_and_runs_daily_after_identity(self):
        calls = []
        class BaseTask:
            def sleep(self, seconds):
                pass
        class BaseWWTask(BaseTask):
            def sleep(self, seconds):
                calls.append("monthly-card-sleep")
        class DailyTask(FakeGame, BaseWWTask):
            def run(self):
                calls.append(("daily", self.current, self.state))
        class AutoLoginTask:
            def run(self):
                calls.append("unsafe-login")
        class AutoCombatTask:
            def run(self):
                calls.append("combat")
        request = {"accountSnapshot": {"label": "A", "saved_account_label": B}}
        fake_modules = {
            "ok": SimpleNamespace(BaseTask=BaseTask),
            "src.task.BaseWWTask": SimpleNamespace(BaseWWTask=BaseWWTask),
            "src.task.DailyTask": SimpleNamespace(DailyTask=DailyTask),
            "src.task.MultiAccountDailyTask": SimpleNamespace(MultiAccountDailyTask=SimpleNamespace(_switch_to_login=upstream_logout)),
            "fixture.AutoLogin": SimpleNamespace(AutoLoginTask=AutoLoginTask),
            "fixture.AutoCombat": SimpleNamespace(AutoCombatTask=AutoCombatTask),
        }
        events = []
        reporter = SimpleNamespace(terminal=False, write=lambda state, detail="": events.append(state))
        def tool_exit(code):
            raise SystemExit(code)
        with patch.dict(sys.modules, fake_modules), patch.object(bridge, "load_request", return_value=request), patch.object(bridge, "Reporter", return_value=reporter), patch.object(bridge.os, "_exit", side_effect=tool_exit):
            bridge.install({"trigger_tasks": [["fixture.AutoLogin", "AutoLoginTask"], ["fixture.AutoCombat", "AutoCombatTask"]]}, "unused.py")
            AutoLoginTask().run()
            AutoCombatTask().run()
            self.assertEqual(calls, [])
            with self.assertRaises(SystemExit) as exited:
                DailyTask(current=B).run()
            self.assertEqual(exited.exception.code, 0)
            self.assertEqual(calls, [("daily", B, "world")])
            self.assertEqual(events[-1], "account_verified")
            AutoLoginTask().run()
            AutoCombatTask().run()
            self.assertEqual(calls[-1], "combat")
            self.assertNotIn("unsafe-login", calls)

    def test_stale_or_cross_operation_sidecar_never_enters_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configs").mkdir()
            path = root / "configs" / "YeYuGamerRun.json"
            doc = {"schemaVersion": 1, "active": True, "createdAtUnix": 0,
                   "accountSnapshot": {"label": "A", "saved_account_label": B},
                   "stageFile": str(root / "stage.jsonl"), "accountOperation": "claim-mail", "selectedOperations": ["claim-mail"]}
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                bridge.load_request(root / "main.py")
            doc["createdAtUnix"] = bridge.time.time()
            doc["accountOperation"] = "spend-waveplates"
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                bridge.load_request(root / "main.py")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AccountReplay))
    print(json.dumps({"status": "passed" if result.wasSuccessful() else "failed", "cases": result.testsRun, "gameStarted": False, "toolStarted": False}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
