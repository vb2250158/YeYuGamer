"""Offline geometry/ownership regressions; no Windows calls or game startup."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1] / "nte-runner"
SPEC = importlib.util.spec_from_file_location("saved_login_geometry", ROOT / "NteSavedLogin.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def frame(scale=1, offset=(0, 0), size=(720, 1280)):
    result = np.zeros((*size, 3), dtype=np.uint8)
    for name, box in module._BOXES.items():
        template = cv2.imread(str(ROOT / "login-templates" / (name + ".png")))
        height, width = template.shape[:2]
        template = cv2.resize(template, (round(width * scale), round(height * scale)))
        left = round(box[0] // 2 * scale) + offset[0]
        top = round(box[1] // 2 * scale) + offset[1]
        result[top : top + template.shape[0], left : left + template.shape[1]] = template
    return result


class GeometryTests(unittest.TestCase):
    def test_translated_layout_matches_without_stretching_window(self):
        self.assertTrue(module.SavedLoginConfirmation(None).matches(frame(offset=(40, -30))))

    def test_tight_non_widescreen_popup_matches(self):
        self.assertTrue(module.SavedLoginConfirmation(None).matches(frame()[138:521, 472:808]))

    def test_common_dpi_scales_match(self):
        for scale in (0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.35, 1.5, 1.75, 2, 3):
            with self.subTest(scale=scale):
                image = frame(scale, size=(round(720 * scale), round(1280 * scale)))
                self.assertTrue(module.SavedLoginConfirmation(None).matches(image))

    def test_missing_or_rearranged_regions_fail_closed(self):
        image = frame()
        image[400:450, 490:790] = 0
        self.assertFalse(module.SavedLoginConfirmation(None).matches(image))
        image = frame()
        image[158:195, 560:717] = 0
        header = cv2.imread(str(ROOT / "login-templates/header.png"))
        image[20:57, 20:177] = header
        self.assertFalse(module.SavedLoginConfirmation(None).matches(image))

    def test_duplicate_matching_layouts_are_ambiguous(self):
        image = np.hstack((frame(), frame()))
        self.assertFalse(module.SavedLoginConfirmation(None).matches(image))

    def test_uniform_fake_templates_fail_closed(self):
        matcher = module.SavedLoginConfirmation(None)
        matcher.templates = {
            name: np.zeros((box[3] // 2 - box[1] // 2, box[2] // 2 - box[0] // 2), np.uint8)
            for name, box in module._BOXES.items()
        }
        self.assertFalse(matcher.matches(frame()))

    def test_selects_unique_matching_owned_popup_among_other_owned_windows(self):
        matcher = module.SavedLoginConfirmation(None)
        popup = module.OwnedLoginPopup(matcher.matches)
        popup.valid = lambda hwnd, owner: owner == 1 and hwnd in (2, 3)
        popup.capture = lambda hwnd, owner: (
            frame() if hwnd == 3 else np.zeros((720, 1280, 3), np.uint8)
        )
        gui = SimpleNamespace(
            EnumWindows=lambda callback, value: [callback(hwnd, value) for hwnd in (2, 3, 4)]
        )
        with patch.dict(sys.modules, win32gui=gui):
            self.assertEqual(popup.find(1), 3)
            popup.capture = lambda *args: frame()
            self.assertIsNone(popup.find(1))

    def test_qt_version_change_does_not_replace_ownership_and_process_checks(self):
        gui = SimpleNamespace(
            IsWindowVisible=lambda _: True,
            GetWindow=lambda *args: 1,
            GetClassName=lambda _: "Qt692QWindowToolSaveBitsOwnDC",
        )
        process = SimpleNamespace(
            Error=OSError, Process=lambda _: SimpleNamespace(name=lambda: "NTEGame.exe")
        )
        modules = dict(
            win32gui=gui,
            psutil=process,
            pywintypes=SimpleNamespace(error=OSError),
            win32process=SimpleNamespace(GetWindowThreadProcessId=lambda _: (10, 20)),
        )
        with patch.dict(sys.modules, modules):
            popup = module.OwnedLoginPopup()
            self.assertTrue(popup.valid(2, 1))
            self.assertFalse(popup.valid(2, 99))
            process.Process = lambda _: SimpleNamespace(name=lambda: "other.exe")
            self.assertFalse(popup.valid(2, 1))

    def test_click_uses_detected_button_not_old_canvas_fraction(self):
        matcher = module.SavedLoginConfirmation(None)
        image = frame()[138:521, 472:808]
        self.assertTrue(matcher.matches(image))
        popup = module.OwnedLoginPopup()
        popup.foreground = lambda *args: True
        cursor = Mock()
        gui = SimpleNamespace(GetWindowRect=lambda _: (100, 200, 436, 583))
        api = SimpleNamespace(SetCursorPos=cursor, mouse_event=Mock())
        with patch.dict(
            sys.modules,
            win32gui=gui,
            win32api=api,
            win32con=SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4),
        ):
            self.assertTrue(popup.click(2, 1, matcher.confirm_point))
        actual = cursor.call_args.args[0]
        self.assertLess(abs(actual[0] - 268), 4)
        self.assertLess(abs(actual[1] - 488), 4)

    def test_missing_click_target_cannot_send_input(self):
        popup = module.OwnedLoginPopup()
        with patch.dict(
            sys.modules,
            win32gui=SimpleNamespace(),
            win32api=SimpleNamespace(),
            win32con=SimpleNamespace(),
        ):
            for point in (None, (float("nan"), 0.5), (0.5, 1.1)):
                self.assertFalse(popup.click(2, 1, point))


if __name__ == "__main__":
    unittest.main()
