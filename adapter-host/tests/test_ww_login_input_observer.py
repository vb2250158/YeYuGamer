import importlib.util
from contextlib import contextmanager
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace


path = Path(__file__).resolve().parents[1] / "openkuro-runner" / "WwYeYuObserver.py"
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("ww_login_observer", path)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class FakeWin32:
    """Stateful Win32 boundary: no real focus, keyboard, cursor or mouse calls."""

    def __init__(self):
        self.events = []
        self.valid = {1, 2, 3, 9}
        self.pids = {1: 100, 2: 100, 3: 100, 9: 999}
        self.foreground = 9
        self.focused = False
        self.hit_hwnd = 3
        self.occluded_before_focus = False
        self.rect = (900, 700, 1700, 900)
        self.client_offset = (100, 200)
        self.after_focus = None
        self.after_sleep = None
        self.button_error = None
        self.gui = SimpleNamespace(
            IsWindow=self.is_window, GetForegroundWindow=self.get_foreground,
            WindowFromPoint=self.window_from_point, BringWindowToTop=self.raise_window,
            SetForegroundWindow=self.focus_window, GetWindowRect=self.window_rect,
            ClientToScreen=self.client_to_screen,
            GetAncestor=lambda hwnd, flag: hwnd,
            IsIconic=lambda hwnd: False,
            ShowWindow=lambda hwnd, flag: self.events.append(("restore", hwnd)),
        )
        self.process = SimpleNamespace(GetWindowThreadProcessId=self.window_pid)
        self.api = SimpleNamespace(SetCursorPos=self.set_cursor, mouse_event=self.mouse_event)
        self.user32 = SimpleNamespace(keybd_event=self.key_event)

    def is_window(self, hwnd):
        self.events.append(("valid", hwnd))
        return hwnd in self.valid

    def window_pid(self, hwnd):
        self.events.append(("pid", hwnd))
        return (1, self.pids.get(hwnd, 0))

    def get_foreground(self):
        self.events.append(("foreground", self.foreground))
        return self.foreground

    def window_from_point(self, point):
        self.events.append(("hit", point))
        return 9 if self.occluded_before_focus and not self.focused else self.hit_hwnd

    def raise_window(self, hwnd):
        self.events.append(("raise", hwnd))
        return True

    def focus_window(self, hwnd):
        self.events.append(("focus", hwnd))
        self.foreground = hwnd
        self.focused = True
        if self.after_focus:
            self.after_focus(self)
        return True

    def window_rect(self, hwnd):
        self.events.append(("rect", hwnd))
        return self.rect

    def client_to_screen(self, hwnd, point):
        self.events.append(("client", hwnd, point))
        return (point[0] + self.client_offset[0], point[1] + self.client_offset[1])

    def set_cursor(self, point):
        self.events.append(("cursor", point))

    def mouse_event(self, flag, *_):
        self.events.append(("button", flag))
        if self.button_error == flag:
            raise OSError("synthetic mouse API failure")

    def key_event(self, key, _scan, flags, _extra):
        self.events.append(("alt", key, flags))

    def sleep(self, duration):
        self.events.append(("sleep", duration))
        if self.after_sleep:
            self.after_sleep(self, duration)

    @property
    def buttons(self):
        return [event for event in self.events if event[0] == "button"]

    @contextmanager
    def installed(self):
        modules = {"win32gui": self.gui, "win32process": self.process, "win32api": self.api,
                   "win32con": SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4)}
        with patch.dict("sys.modules", modules), \
                patch("ctypes.windll", SimpleNamespace(user32=self.user32), create=True), \
                patch("time.sleep", self.sleep):
            yield self


class LoginInputObserverTests(unittest.TestCase):
    def make_types(self, error=None):
        calls = []

        class Input:
            def update_mouse_pos(self, x, y):
                calls.append((x, y))
                if error:
                    raise error
                return (y << 16) | x

        class Task:
            def wait_login(self):
                return Input().update_mouse_pos(30, 40)

        return Task, Input, calls

    def test_records_exact_official_result_without_extra_input(self):
        task, interaction, calls = self.make_types()
        observer._observe_login_input(task, interaction, lambda _, pos: str(pos))
        with patch.object(observer, "_emit") as emit:
            self.assertEqual(task().wait_login(), (40 << 16) | 30)
        self.assertEqual(calls, [(30, 40)])
        emit.assert_called_once_with("official-login-input", "observed", str((40 << 16) | 30))

    def test_non_login_input_is_not_observed(self):
        task, interaction, _ = self.make_types()
        observer._observe_login_input(task, interaction, lambda *_: "target")
        with patch.object(observer, "_emit") as emit:
            interaction().update_mouse_pos(1, 2)
        emit.assert_not_called()

    def test_original_exception_survives_and_context_is_released(self):
        error = ValueError("official input failure")
        task, interaction, _ = self.make_types(error)
        observer._observe_login_input(task, interaction, lambda *_: "target")
        with self.assertRaises(ValueError) as caught:
            task().wait_login()
        self.assertIs(caught.exception, error)
        self.assertFalse(observer._in_official_login.get())

    def test_diagnostic_failure_does_not_change_input_return(self):
        task, interaction, calls = self.make_types()
        def unavailable(*_):
            raise OSError("window disappeared")
        observer._observe_login_input(task, interaction, unavailable)
        self.assertEqual(task().wait_login(), (40 << 16) | 30)
        self.assertEqual(calls, [(30, 40)])

    def test_reinstall_does_not_duplicate_observer(self):
        task, interaction, calls = self.make_types()
        for _ in range(2):
            observer._observe_login_input(task, interaction, lambda *_: "target")
        with patch.object(observer, "_emit") as emit:
            task().wait_login()
        emit.assert_called_once()
        self.assertEqual(calls, [(30, 40)])

    def test_preparation_runs_after_target_resolution_without_repeating_input(self):
        task, interaction, calls = self.make_types()
        def prepare(_):
            self.assertEqual(calls, [(30, 40)])
            return "official foreground request"
        observer._observe_login_input(task, interaction, lambda *_: "target", prepare)
        with patch.object(observer, "_emit") as emit:
            self.assertEqual(task().wait_login(), (40 << 16) | 30)
        self.assertEqual(calls, [(30, 40)])
        self.assertEqual(emit.call_args_list[0].args, ("login-window-activation", "observed", "official foreground request"))

    def test_native_login_activation_is_scoped_and_leaves_input_upstream(self):
        for foreground, label, foreign_pid, expected in (
            (False, "登录", False, 1), (True, "登录", False, 0),
            (False, "Button2", False, 1), (False, "登录", True, 0),
        ):
            with self.subTest(foreground=foreground, label=label, foreign_pid=foreign_pid):
                calls = []
                window = SimpleNamespace(hwnd=1, is_foreground=lambda: foreground,
                                         bring_to_front=lambda: calls.append("activate") or True)
                gui = SimpleNamespace(GetClassName=lambda h: {3: "Button", 2: "#32770", 1: "UnrealWindow"}[h],
                                      GetWindowText=lambda _: label, GetParent=lambda h: {3: 2, 2: 1}.get(h))
                process = SimpleNamespace(GetWindowThreadProcessId=lambda h: (0, 200 if foreign_pid and h == 2 else 100))
                with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}):
                    result = observer._prepare_native_login_input(SimpleNamespace(hwnd=3, hwnd_window=window))
                self.assertEqual(len(calls), expected)
                if expected:
                    self.assertIn('"foregroundConfirmed": false', result)
                else:
                    self.assertIsNone(result)

    def test_activation_error_is_recorded_and_original_input_return_survives(self):
        task, interaction, calls = self.make_types()
        def unavailable(_):
            raise OSError("activation failed")
        observer._observe_login_input(task, interaction, lambda *_: "target", unavailable)
        with patch.object(observer, "_emit") as emit:
            self.assertEqual(task().wait_login(), (40 << 16) | 30)
        self.assertEqual(calls, [(30, 40)])
        self.assertEqual(emit.call_args_list[0].args[:2], ("login-window-activation", "failed"))


class NativeLoginClickHandoffTests(unittest.TestCase):
    """The official click on the game's saved-login control needs real input."""

    @contextmanager
    def official_login(self):
        token = observer._in_official_login.set(True)
        try:
            yield
        finally:
            observer._in_official_login.reset(token)

    def make_interaction(self, target=3, foreign_pid=False, click_error=None):
        class Input:
            def __init__(self):
                self._dynamic_target_hwnd = 0
                self.hwnd_window = SimpleNamespace(hwnd=1)
                self.calls = []

            @property
            def hwnd(self):
                return self._dynamic_target_hwnd or 1

            def update_mouse_pos(self, x, y):
                self.calls.append(("move", x, y))
                self._dynamic_target_hwnd = target
                return (y << 16) | x

            def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key="left"):
                self.calls.append(("click", x, y, key))
                if click_error is not None:
                    raise click_error
                return "official-click-result"

        gui = SimpleNamespace(
            GetClassName=lambda h: {3: "Button", 2: "#32770", 1: "UnrealWindow"}.get(h, "Static"),
            GetParent=lambda h: {3: 2, 2: 1}.get(h),
            GetWindowRect=lambda _: (900, 700, 1700, 900),
        )
        process = SimpleNamespace(
            GetWindowThreadProcessId=lambda h: (0, 300 if foreign_pid and h == 2 else 100))
        return Input, gui, process

    def test_native_login_click_is_delivered_once_as_real_input(self):
        Input, gui, process = self.make_interaction()
        observer._observe_native_login_click(Input)
        interaction = Input()
        with self.official_login(), patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue, \
                patch.object(observer, "_emit") as emit:
            self.assertIsNone(interaction.click(10, 20))
        issue.assert_called_once()
        self.assertEqual(issue.call_args.args[0],
                         {"hwnd": 3, "dialog": 2, "pid": 100, "kind": "native-dialog", "x": 1300, "y": 800})
        self.assertEqual(interaction.calls, [("move", 10, 20)])
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "issued"))

    def test_native_dialog_outside_login_flow_keeps_original_click(self):
        Input, gui, process = self.make_interaction()
        result = object()
        with patch.object(Input, "click", autospec=True, return_value=result) as original:
            observer._observe_native_login_click(Input)
            interaction = Input()
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click") as issue, \
                    patch.object(observer, "_emit") as emit:
                actual = interaction.click(10, 20, move_back=True, name="official-control",
                                           down_time=0.25, move=False, key="left")
        self.assertIs(actual, result)
        original.assert_called_once_with(interaction, 10, 20, move_back=True, name="official-control",
                                         down_time=0.25, move=False, key="left")
        self.assertEqual(interaction.calls, [], "ordinary input must not resolve the target twice")
        issue.assert_not_called()
        emit.assert_not_called()

    def test_native_dialog_outside_login_flow_preserves_original_exception(self):
        failure = ValueError("official non-login click failure")
        Input, gui, process = self.make_interaction(click_error=failure)
        observer._observe_native_login_click(Input)
        interaction = Input()
        with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue:
            with self.assertRaises(ValueError) as caught:
                interaction.click(10, 20)
        self.assertIs(caught.exception, failure)
        self.assertEqual(interaction.calls, [("click", 10, 20, "left")])
        issue.assert_not_called()

    def test_official_wait_login_scopes_native_dialog_handoff(self):
        Input, gui, process = self.make_interaction()
        interaction = Input()
        class Task:
            def wait_login(self):
                return interaction.click(10, 20)
        observer._observe_login_input(Task, Input, lambda *_: "official native dialog")
        observer._observe_native_login_click(Input)
        with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue, \
                patch.object(observer, "_emit"):
            self.assertIsNone(Task().wait_login())
        issue.assert_called_once()
        self.assertEqual(interaction.calls, [("move", 10, 20)])
        self.assertFalse(observer._in_official_login.get())

    def test_non_native_target_keeps_the_original_click(self):
        Input, gui, process = self.make_interaction(target=1)
        observer._observe_native_login_click(Input)
        interaction = Input()
        with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue, \
                patch.object(observer, "_emit") as emit:
            self.assertEqual(interaction.click(10, 20), "official-click-result")
        issue.assert_not_called()
        emit.assert_not_called()
        self.assertEqual(interaction.calls, [("click", 10, 20, "left")])

    def test_foreign_dialog_is_not_clicked(self):
        Input, gui, process = self.make_interaction(foreign_pid=True)
        observer._observe_native_login_click(Input)
        interaction = Input()
        with self.official_login(), patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue, \
                patch.object(observer, "_emit") as emit:
            self.assertEqual(interaction.click(10, 20), "official-click-result")
        issue.assert_not_called()
        emit.assert_not_called()

    def test_in_client_login_click_is_delivered_once_as_real_input(self):
        """The SDK login page resolves to the game window itself, not a dialog."""
        Input, gui, process = self.make_interaction(target=1)
        gui.ClientToScreen = lambda hwnd, point: (point[0] + 100, point[1] + 200)
        observer._observe_native_login_click(Input)
        interaction = Input()
        token = observer._in_official_login.set(True)
        try:
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click") as issue, \
                    patch.object(observer, "_emit") as emit:
                self.assertIsNone(interaction.click(10, 20))
        finally:
            observer._in_official_login.reset(token)
        issue.assert_called_once()
        self.assertEqual(issue.call_args.args[0],
                         {"hwnd": 1, "dialog": 1, "pid": 100, "kind": "game-window",
                          "clientPoint": (10, 20), "x": 110, "y": 220})
        self.assertEqual(interaction.calls, [("move", 10, 20)])
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "issued"))

    def test_in_client_click_outside_login_flow_keeps_the_original_click(self):
        Input, gui, process = self.make_interaction(target=1)
        gui.ClientToScreen = lambda hwnd, point: point
        observer._observe_native_login_click(Input)
        interaction = Input()
        with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue:
            self.assertEqual(interaction.click(10, 20), "official-click-result")
        issue.assert_not_called()

    def test_non_left_click_keeps_the_original_click(self):
        Input, gui, process = self.make_interaction()
        observer._observe_native_login_click(Input)
        interaction = Input()
        with self.official_login(), patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue:
            self.assertEqual(interaction.click(10, 20, key="right"), "official-click-result")
        issue.assert_not_called()

    def test_reinstall_does_not_duplicate_click_handoff(self):
        Input, gui, process = self.make_interaction()
        for _ in range(2):
            observer._observe_native_login_click(Input)
        interaction = Input()
        with self.official_login(), patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                patch.object(observer, "_issue_physical_click") as issue:
            interaction.click(10, 20)
        issue.assert_called_once()

    def test_in_client_account_selection_click_is_delivered_once_as_real_input(self):
        """The official account-list click needs real input, like saved login."""
        Input, gui, process = self.make_interaction(target=1)
        gui.ClientToScreen = lambda hwnd, point: (point[0] + 100, point[1] + 200)
        observer._observe_native_login_click(Input)
        interaction = Input()
        token = observer._in_official_account_selection.set(True)
        try:
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click") as issue, \
                    patch.object(observer, "_emit") as emit:
                self.assertIsNone(interaction.click(1168, 845))
        finally:
            observer._in_official_account_selection.reset(token)
        issue.assert_called_once()
        self.assertEqual(issue.call_args.args[0],
                         {"hwnd": 1, "dialog": 1, "pid": 100, "kind": "game-window",
                          "clientPoint": (1168, 845), "x": 1268, "y": 1045})
        self.assertEqual(interaction.calls, [("move", 1168, 845)])
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "issued"))

    def test_account_selection_click_resolves_against_the_official_target_hwnd(self):
        """The in-client account page resolves to an hwnd the strict predicate rejects."""
        Input, gui, process = self.make_interaction(target=2)
        gui.ClientToScreen = lambda hwnd, point: (point[0] + 100, point[1] + 200)
        gui.IsWindow = lambda hwnd: bool(hwnd)
        observer._observe_native_login_click(Input)
        interaction = Input()
        token = observer._in_official_account_selection.set(True)
        try:
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click") as issue, \
                    patch.object(observer, "_emit") as emit:
                self.assertIsNone(interaction.click(1168, 845))
        finally:
            observer._in_official_account_selection.reset(token)
        issue.assert_called_once()
        self.assertEqual(issue.call_args.args[0],
                         {"hwnd": 2, "dialog": 2, "pid": 100, "kind": "game-window-fallback",
                          "clientPoint": (1168, 845), "x": 1268, "y": 1045})
        self.assertEqual(interaction.calls, [("move", 1168, 845)])
        self.assertEqual(emit.call_args_list[0].args[:2], ("ww-login-window-click", "diagnostic"))
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "issued"))

    def test_account_selection_scope_covers_only_the_account_list_click(self):
        """Only the official _click_account_in_list call is scoped, once."""
        class Multi:
            def __init__(self):
                self.seen = []

            def _click_account_in_list(self):
                self.seen.append(observer._in_official_account_selection.get())
                return "next-account"

            def _select_and_login_account(self):
                self.seen.append(("outside", observer._in_official_account_selection.get()))
                return self._click_account_in_list()

        for _ in range(2):
            observer._observe_account_selection(Multi)
        multi = Multi()
        self.assertEqual(multi._select_and_login_account(), "next-account")
        self.assertEqual(multi.seen, [("outside", False), True])
        self.assertFalse(observer._in_official_account_selection.get())

    def test_account_selection_handoff_failure_keeps_the_official_click(self):
        """A failed handoff must not swallow the official account click."""
        Input, gui, process = self.make_interaction(target=1)
        gui.ClientToScreen = lambda hwnd, point: point
        observer._observe_native_login_click(Input)
        interaction = Input()
        token = observer._in_official_account_selection.set(True)
        try:
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click", side_effect=RuntimeError("busy")), \
                    patch.object(observer, "_emit") as emit:
                self.assertEqual(interaction.click(10, 20), "official-click-result")
        finally:
            observer._in_official_account_selection.reset(token)
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "failed"))
        self.assertEqual(interaction.calls, [("move", 10, 20), ("click", 10, 20, "left")])

    def test_saved_login_handoff_failure_preserves_the_official_click_return(self):
        """A refused handoff must preserve the official saved-login input path."""
        Input, gui, process = self.make_interaction(target=1)
        gui.ClientToScreen = lambda hwnd, point: point
        observer._observe_native_login_click(Input)
        interaction = Input()
        token = observer._in_official_login.set(True)
        try:
            with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                    patch.object(observer, "_issue_physical_click", side_effect=RuntimeError("native_login_point_foreign")), \
                    patch.object(observer, "_emit") as emit:
                self.assertEqual(interaction.click(10, 20), "official-click-result")
        finally:
            observer._in_official_login.reset(token)
        self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "failed"))
        self.assertIn("native_login_point_foreign", emit.call_args.args[2])
        self.assertEqual(interaction.calls, [("move", 10, 20), ("click", 10, 20, "left")])

    def test_failed_handoff_preserves_the_original_click_exception(self):
        """Fail-open means the original exception, never the observer exception."""
        for scope in (observer._in_official_login, observer._in_official_account_selection):
            with self.subTest(scope=scope.name):
                original_error = ValueError("official saved-login failure")
                Input, gui, process = self.make_interaction(target=1, click_error=original_error)
                gui.ClientToScreen = lambda hwnd, point: point
                observer._observe_native_login_click(Input)
                interaction = Input()
                token = scope.set(True)
                try:
                    with patch.dict("sys.modules", {"win32gui": gui, "win32process": process}), \
                            patch.object(observer, "_issue_physical_click", side_effect=RuntimeError("native_login_window_changed")), \
                            patch.object(observer, "_emit") as emit:
                        with self.assertRaises(ValueError) as caught:
                            interaction.click(10, 20)
                finally:
                    scope.reset(token)
                self.assertIs(caught.exception, original_error)
                self.assertEqual(interaction.calls, [("move", 10, 20), ("click", 10, 20, "left")])
                self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "failed"))
                self.assertIn("native_login_window_changed", emit.call_args.args[2])

    def test_mouse_down_or_up_failure_never_falls_back_to_a_second_official_click(self):
        for failed_flag in (2, 4):
            with self.subTest(failed_flag=failed_flag):
                Input, gui, process = self.make_interaction()
                observer._observe_native_login_click(Input)
                interaction = Input()
                fake = FakeWin32()
                fake.gui.GetClassName = gui.GetClassName
                fake.gui.GetParent = gui.GetParent
                fake.button_error = failed_flag
                with self.official_login(), fake.installed(), patch.object(observer, "_emit") as emit:
                    self.assertIsNone(interaction.click(10, 20))
                self.assertEqual(interaction.calls, [("move", 10, 20)])
                self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])
                self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "failed"))
                self.assertIn("native_login_input", emit.call_args.args[2])

    def test_issued_log_reports_the_real_activated_point_for_native_and_client_targets(self):
        """The observable issued coordinate must match the mouse position."""
        for target_hwnd, actual_point in ((3, (2100, 1100)), (1, (510, 620))):
            with self.subTest(target_hwnd=target_hwnd):
                Input, gui, process = self.make_interaction(target=target_hwnd)
                observer._observe_native_login_click(Input)
                interaction = Input()
                fake = FakeWin32()
                fake.gui.GetClassName = gui.GetClassName
                fake.gui.GetParent = gui.GetParent
                fake.hit_hwnd = target_hwnd
                def moved_on_focus(current):
                    current.rect = (2000, 1000, 2200, 1200)
                    current.client_offset = (500, 600)
                fake.after_focus = moved_on_focus
                token = observer._in_official_login.set(True)
                try:
                    with fake.installed(), patch.object(observer, "_emit") as emit:
                        self.assertIsNone(interaction.click(10, 20))
                finally:
                    observer._in_official_login.reset(token)
                self.assertEqual(interaction.calls, [("move", 10, 20)])
                self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])
                self.assertIn(("cursor", actual_point), fake.events)
                self.assertEqual(emit.call_args.args[:2], ("ww-login-window-click", "issued"))
                self.assertIn("at ({},{})".format(*actual_point), emit.call_args.args[2])
                self.assertNotIn("at (1300,800)", emit.call_args.args[2])
                self.assertNotIn("at (110,220)", emit.call_args.args[2])


class PhysicalLoginClickBoundaryTests(unittest.TestCase):
    @staticmethod
    def native_target():
        return {"hwnd": 3, "dialog": 2, "pid": 100, "kind": "native-dialog", "x": 1300, "y": 800}

    @staticmethod
    def client_target():
        return {"hwnd": 1, "dialog": 1, "pid": 100, "kind": "game-window",
                "clientPoint": (10, 20), "x": 110, "y": 220}

    def assert_refused(self, fake, reason, target=None):
        with fake.installed(), self.assertRaises(RuntimeError) as caught:
            observer._issue_physical_click(target or self.native_target())
        self.assertEqual(str(caught.exception).split()[0], reason)
        self.assertEqual(fake.buttons, [])

    def test_invalid_or_foreign_target_is_refused_before_activation(self):
        for hwnd in (3, 2):
            for changed in ("invalid", "foreign"):
                with self.subTest(hwnd=hwnd, changed=changed):
                    fake = FakeWin32()
                    if changed == "invalid":
                        fake.valid.remove(hwnd)
                    else:
                        fake.pids[hwnd] = 999
                    self.assert_refused(fake, "native_login_window_changed")
                    self.assertFalse(any(event[0] in {"alt", "raise", "focus", "cursor"} for event in fake.events))

    def test_occluded_valid_window_is_raised_before_point_ownership_check(self):
        """A foreground overlay must not prevent a valid game window activation."""
        fake = FakeWin32()
        fake.occluded_before_focus = True
        with fake.installed():
            observer._issue_physical_click(self.native_target())
        raised = fake.events.index(("raise", 2))
        focused = fake.events.index(("focus", 2))
        first_hit = next(index for index, event in enumerate(fake.events) if event[0] == "hit")
        for hwnd in (3, 2):
            self.assertLess(fake.events.index(("valid", hwnd)), raised)
            self.assertLess(fake.events.index(("pid", hwnd)), raised)
        self.assertLess(raised, focused)
        self.assertLess(focused, first_hit)
        self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])

    def test_minimized_game_owner_is_restored_before_popup_and_point_validation(self):
        fake = FakeWin32()
        fake.gui.GetAncestor = lambda hwnd, flag: 1
        fake.gui.IsIconic = lambda hwnd: hwnd == 1
        fake.occluded_before_focus = True
        with fake.installed():
            observer._issue_physical_click(self.native_target())
        restore = fake.events.index(("restore", 1))
        root_focus = fake.events.index(("focus", 1))
        popup_focus = fake.events.index(("focus", 2))
        first_hit = next(i for i, event in enumerate(fake.events) if event[0] == "hit")
        self.assertLess(restore, root_focus)
        self.assertLess(root_focus, popup_focus)
        self.assertLess(popup_focus, first_hit)
        self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])

    def test_foreign_root_owner_cannot_receive_restore_or_input(self):
        fake = FakeWin32()
        fake.gui.GetAncestor = lambda hwnd, flag: 9
        fake.gui.IsIconic = lambda hwnd: True
        self.assert_refused(fake, "native_login_root_owner_foreign")
        self.assertFalse(any(event[0] in {"restore", "focus", "alt"} for event in fake.events))

    def test_native_button_centre_is_recomputed_after_focus(self):
        fake = FakeWin32()
        fake.after_focus = lambda current: setattr(current, "rect", (2000, 1000, 2200, 1200))
        with fake.installed():
            observer._issue_physical_click(self.native_target())
        self.assertIn(("cursor", (2100, 1100)), fake.events)
        self.assertNotIn(("cursor", (1300, 800)), fake.events)
        first_rect = next(index for index, event in enumerate(fake.events) if event[0] == "rect")
        self.assertLess(fake.events.index(("focus", 2)), first_rect)
        self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])

    def test_game_client_point_is_recomputed_after_focus(self):
        fake = FakeWin32()
        fake.hit_hwnd = 1
        fake.after_focus = lambda current: setattr(current, "client_offset", (500, 600))
        with fake.installed():
            observer._issue_physical_click(self.client_target())
        self.assertIn(("cursor", (510, 620)), fake.events)
        self.assertNotIn(("cursor", (110, 220)), fake.events)
        first_client = next(index for index, event in enumerate(fake.events) if event[0] == "client")
        self.assertLess(fake.events.index(("focus", 1)), first_client)
        self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])

    def test_foreign_point_after_focus_is_refused_before_cursor_or_mouse(self):
        fake = FakeWin32()
        fake.hit_hwnd = 9
        self.assert_refused(fake, "native_login_point_foreign")
        self.assertIn(("focus", 2), fake.events)
        self.assertFalse(any(event[0] == "cursor" for event in fake.events))

    def test_focus_stolen_after_cursor_wait_is_refused_before_mouse_down(self):
        fake = FakeWin32()
        fake.after_sleep = lambda current, duration: setattr(current, "foreground", 9) if duration == 0.15 else None
        self.assert_refused(fake, "native_login_foreground_foreign")
        self.assertIn(("cursor", (1300, 800)), fake.events)

    def test_point_covered_after_cursor_wait_is_refused_before_mouse_down(self):
        fake = FakeWin32()
        fake.after_sleep = lambda current, duration: setattr(current, "hit_hwnd", 9) if duration == 0.15 else None
        self.assert_refused(fake, "native_login_point_foreign")
        self.assertIn(("cursor", (1300, 800)), fake.events)

    def test_window_lost_or_pid_reused_during_either_wait_is_refused(self):
        for duration in (0.1, 0.15):
            for hwnd in (3, 2):
                for changed in ("invalid", "foreign"):
                    with self.subTest(duration=duration, hwnd=hwnd, changed=changed):
                        fake = FakeWin32()
                        def change(current, waited):
                            if waited == duration:
                                if changed == "invalid":
                                    current.valid.discard(hwnd)
                                else:
                                    current.pids[hwnd] = 999
                        fake.after_sleep = change
                        self.assert_refused(fake, "native_login_window_changed")

    def test_button_or_client_window_move_during_cursor_wait_is_refused(self):
        for kind in ("native-dialog", "game-window"):
            with self.subTest(kind=kind):
                fake = FakeWin32()
                target = self.native_target() if kind == "native-dialog" else self.client_target()
                fake.hit_hwnd = target["hwnd"]
                def move(current, duration):
                    if duration == 0.15:
                        current.rect = (2000, 1000, 2200, 1200)
                        current.client_offset = (500, 600)
                fake.after_sleep = move
                self.assert_refused(fake, "native_login_point_changed", target)

    def test_mouse_api_failure_marks_input_started_and_cannot_be_retried(self):
        for flag in (2, 4):
            with self.subTest(flag=flag):
                fake = FakeWin32()
                fake.button_error = flag
                with fake.installed(), self.assertRaises(observer._PhysicalClickError) as caught:
                    observer._issue_physical_click(self.native_target())
                self.assertTrue(caught.exception.input_started)
                # A down API error can still mean input reached the OS. The
                # one release attempt avoids leaving the button held down.
                self.assertEqual(fake.buttons, [("button", 2), ("button", 4)])


if __name__ == "__main__":
    unittest.main()
