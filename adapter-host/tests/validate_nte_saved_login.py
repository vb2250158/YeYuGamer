"""Replay template specificity and one-click semantics; never starts a game."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1] / "nte-runner"
spec = importlib.util.spec_from_file_location("nte_saved_login_test", ROOT / "NteSavedLogin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LoginTests(unittest.TestCase):
    def test_preserves_template_input_without_post_click_capture_or_delay(self):
        with tempfile.TemporaryDirectory() as root:
            events, actions = [], []
            frame = np.zeros((4, 4, 3), dtype=np.uint8)
            def capture(*args):
                actions.append("capture")
                frame[:] += 1
                return frame
            def click(*args):
                actions.append("click")
                frame[:] = 99  # Official capture storage may be reused by input.
                return True
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"), emit=lambda *args: events.append(args))
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True, capture=capture, click=click)
            hook = module.SavedLoginConfirmation(reporter, popup)
            hook.matches = lambda _: True
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False), hwnd=SimpleNamespace(hwnd=1),
                log_info=lambda _: None, operate=lambda func, **kwargs: func(), sleep=lambda _: None)
            with patch.object(module.time, "sleep", side_effect=AssertionError("no screenshot delay")):
                self.assertTrue(hook.confirm(task))
            self.assertEqual(actions, ["capture", "capture", "click"])
            metadata = [json.loads(e[2].split(":", 1)[1]) for e in events if e[2].startswith("saved_login_capture_metadata:")]
            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0]["actionType"], "recognition")
            self.assertEqual(metadata[0]["frameSource"], "owned_popup_template_input")
            self.assertEqual(int(cv2.imread(str(Path(root) / metadata[0]["fileName"]))[0, 0, 0]), 2)

    def frame(self):
        gray = np.zeros((720, 1280), dtype=np.uint8)
        for name, box in module._BOXES.items():
            left, top, right, bottom = (v // 2 for v in box)
            gray[top:bottom, left:right] = cv2.imread(str(ROOT / "login-templates" / (name + ".png")), 0)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def test_requires_all_three_fixed_regions(self):
        matcher = module.SavedLoginConfirmation(None)
        frame = self.frame()
        self.assertTrue(matcher.matches(frame))
        for box in module._BOXES.values():
            altered = frame.copy()
            left, top, right, bottom = (v // 2 for v in box)
            altered[top:bottom, left:right] = 0
            self.assertFalse(matcher.matches(altered))
        self.assertFalse(matcher.matches(np.zeros_like(frame)))
        self.assertFalse(matcher.matches(np.zeros((720, 900, 3), dtype=np.uint8)))

    def test_invalid_template_does_not_enter_input_lock(self):
        frame = self.frame()
        invalid_templates = {
            "missing_or_undecodable": None,
            "empty": np.zeros((0, 0), dtype=np.uint8),
            "oversized": np.zeros((720, 1280), dtype=np.uint8),
            "wrong_channels": np.zeros((36, 157, 3), dtype=np.uint8),
        }
        for reason, template in invalid_templates.items():
            with self.subTest(reason=reason):
                popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                    capture=lambda *args: frame, click=lambda *args: self.fail("must not click"))
                hook = module.SavedLoginConfirmation(None, popup)
                task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
                    hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None,
                    operate=lambda *args, **kwargs: self.fail("invalid template entered input lock"))
                with patch.object(cv2, "imread", return_value=template):
                    self.assertFalse(hook.confirm(task))
                self.assertFalse(hook.attempted)

    def test_template_recognition_error_does_not_enter_input_lock(self):
        frame = self.frame()
        popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
            capture=lambda *args: frame, click=lambda *args: self.fail("must not click"))
        hook = module.SavedLoginConfirmation(None, popup)
        task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
            hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None,
            operate=lambda *args, **kwargs: self.fail("recognition failure entered input lock"))
        with patch.object(cv2, "matchTemplate", side_effect=cv2.error("invalid template input")):
            self.assertFalse(hook.confirm(task))
        self.assertFalse(hook.attempted)

    def test_nonfinite_match_score_is_not_confirmation(self):
        frame = self.frame()
        for score in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(score=score):
                hook = module.SavedLoginConfirmation(None)
                with patch.object(cv2, "matchTemplate", return_value=np.array([[score]])):
                    self.assertFalse(hook.matches(frame))

    def test_empty_or_invalid_capture_is_not_confirmation(self):
        hook = module.SavedLoginConfirmation(None)
        for frame in (np.empty((0, 1280, 3), dtype=np.uint8),
                      np.empty((720, 0, 3), dtype=np.uint8),
                      np.empty((720, 1280, 2), dtype=np.uint8)):
            with self.subTest(shape=frame.shape):
                self.assertFalse(hook.matches(frame))

    def test_one_click_preserves_official_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            events, clicks = [], []
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"), emit=lambda *args: events.append(args))
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                capture=lambda *args: self.frame(), click=lambda *args: clicks.append(args) or True)
            hook = module.SavedLoginConfirmation(reporter, popup)
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False), frame=self.frame(),
                hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None, sleep=lambda _: None,
                operate=lambda func, **kwargs: func(), next_frame=self.frame)
            self.assertTrue(hook.confirm(task))
            self.assertFalse(hook.confirm(task))
            self.assertEqual(len(clicks), 1)
            self.assertEqual(len([e for e in events if e[1] == "integration_evidence"]), 1)
            metadata = [json.loads(e[2].split(":", 1)[1]) for e in events
                        if e[1] == "integration_progress" and e[2].startswith("saved_login_capture_metadata:")]
            self.assertEqual([m["phase"] for m in metadata], ["template"])
            self.assertEqual(len({m["actionId"] for m in metadata}), 1)
            self.assertEqual([m["actionType"] for m in metadata], ["recognition"])
            self.assertTrue(all(m["window"] == "owned_NTEGame_popup" and m["capturedAt"] for m in metadata))
            for entry in (e for e in events if e[1] == "integration_evidence"):
                self.assertLess(next(i for i, e in enumerate(events) if e[1] == "integration_progress" and
                    e[2].startswith("saved_login_capture_metadata:") and
                    json.loads(e[2].split(":", 1)[1])["fileName"] == entry[2]), events.index(entry))
            self.assertFalse(any(e[1] == "completed" for e in events))

    def test_foreground_must_be_confirmed(self):
        popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: False)
        hook = module.SavedLoginConfirmation(None, popup)
        task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False), frame=self.frame(),
            hwnd=SimpleNamespace(hwnd=1))
        self.assertFalse(hook.confirm(task))
        self.assertFalse(hook.attempted)

    def test_unapplied_handoff_logs_actual_condition_once_without_progress(self):
        logs = []
        popup = SimpleNamespace(find=lambda _: None)
        hook = module.SavedLoginConfirmation(None, popup)
        task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
            hwnd=SimpleNamespace(hwnd=1), log_info=logs.append)
        self.assertFalse(hook.confirm(task))
        self.assertFalse(hook.confirm(task))
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[-1].endswith("no_unique_visible_owned_popup"))
        popup.find = lambda _: 2
        popup.foreground = lambda *args: False
        self.assertFalse(hook.confirm(task))
        self.assertEqual(len(logs), 2)
        self.assertTrue(logs[-1].endswith("owned_popup_not_foreground"))
        self.assertFalse(hook.attempted)

    def test_diagnostic_logger_failure_does_not_block_official_handoff(self):
        def failed_log(_):
            raise OSError("logger unavailable")
        hook = module.SavedLoginConfirmation(None, SimpleNamespace(find=lambda _: None))
        task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
            hwnd=SimpleNamespace(hwnd=1), log_info=failed_log)
        self.assertFalse(hook.confirm(task))

    def test_closed_popup_without_buffer_does_not_request_next_frame(self):
        with tempfile.TemporaryDirectory() as root:
            events = []
            frames = iter([self.frame(), self.frame(), None, None])
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                capture=lambda *args: next(frames), click=lambda *args: True)
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"),
                emit=lambda *args: events.append(args))
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
                hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None, sleep=lambda _: None,
                operate=lambda func, **kwargs: func(),
                executor=SimpleNamespace(nullable_frame=lambda: None),
                next_frame=lambda: self.fail("diagnostic capture must not advance official scene"))
            self.assertTrue(module.SavedLoginConfirmation(reporter, popup).confirm(task))
            self.assertFalse(any(e[2].startswith("saved_login_screenshot_failed:") for e in events))
            self.assertTrue(any(e[2].startswith("saved_login_click_returned:") for e in events))
            self.assertEqual(len([e for e in events if e[1] == "integration_evidence"]), 1)

    def test_capture_happens_inside_input_lock_and_click_exception_survives(self):
        with tempfile.TemporaryDirectory() as root:
            inside = [False]
            captures = []
            def capture(*args):
                self.assertTrue(inside[0])
                captures.append(1)
                return self.frame()
            def click(*args):
                raise RuntimeError("official input failed")
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                capture=capture, click=click)
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"), emit=lambda *args: None)
            hook = module.SavedLoginConfirmation(reporter, popup)
            def operate(func, **kwargs):
                inside[0] = True
                try:
                    return func()
                finally:
                    inside[0] = False
            # The initial template probe precedes the input lock; only action
            # captures must happen inside it.
            popup.capture = lambda *args: self.frame() if not inside[0] else capture(*args)
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
                hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None, operate=operate)
            with self.assertRaisesRegex(RuntimeError, "official input failed"):
                hook.confirm(task)
            self.assertEqual(len(captures), 1)

    def test_failed_screenshot_does_not_change_sent_click(self):
        with tempfile.TemporaryDirectory() as root:
            events, calls = [], []
            frames = iter([self.frame(), self.frame(), None, None])
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                capture=lambda *args: calls.append("capture") or next(frames),
                click=lambda *args: calls.append("click") or True)
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"), emit=lambda *args: events.append(args))
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
                hwnd=SimpleNamespace(hwnd=1), log_info=lambda _: None, sleep=lambda _: None,
                operate=lambda func, **kwargs: func())
            with patch.object(cv2, "imwrite", return_value=False):
                self.assertTrue(module.SavedLoginConfirmation(reporter, popup).confirm(task))
            self.assertEqual(calls, ["capture", "capture", "click"])
            metadata = [json.loads(e[2].split(":", 1)[1]) for e in events
                        if e[1] == "integration_progress" and e[2].startswith("saved_login_capture_metadata:")]
            self.assertEqual([m["phase"] for m in metadata], ["template"])
            self.assertIsNone(metadata[0]["fileName"])
            self.assertEqual(len([e for e in events if e[1] == "integration_evidence"]), 0)

    def test_focus_change_while_waiting_for_official_lock_does_not_click(self):
        with tempfile.TemporaryDirectory() as root:
            popup = SimpleNamespace(find=lambda _: 2, foreground=lambda *args: True,
                capture=lambda *args: self.frame(), click=lambda *args: self.fail("must not click"))
            reporter = SimpleNamespace(path=str(Path(root) / "stage.jsonl"), emit=lambda *args: None)
            hook = module.SavedLoginConfirmation(reporter, popup)
            def operate(func, **kwargs):
                popup.foreground = lambda *args: False
                return func()
            task = SimpleNamespace(scene=SimpleNamespace(logged_in=lambda: False),
                hwnd=SimpleNamespace(hwnd=1), operate=operate)
            self.assertFalse(hook.confirm(task))
            self.assertFalse(hook.attempted)


if __name__ == "__main__":
    unittest.main()
