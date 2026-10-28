"""User-authorized saved-account confirmation missing in ok-nte v1.3.11.

This hook runs on the official task thread and holds its input lock. The HOTTA
popup has its own HWND, absent from the official base-game capture.
It never selects an account, enters credentials, or decides task completion.
"""

import json
import time
import uuid
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

_BOXES = {
    "header": (1120, 317, 1435, 390),
    "confirm": (985, 810, 1575, 895),
    "alternative": (985, 915, 1575, 1003),
}


class OwnedLoginPopup:
    """Only the visible NTEGame popup directly owned by the official game HWND."""

    def __init__(self, matcher=None):
        self.matcher = matcher

    def valid(self, hwnd, owner):
        import psutil
        import pywintypes
        import win32gui
        import win32process

        try:
            return (
                bool(owner)
                and win32gui.IsWindowVisible(hwnd)
                and win32gui.GetWindow(hwnd, 4) == owner
                and psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name().lower()
                == "ntegame.exe"
            )
        except (psutil.Error, OSError, pywintypes.error):
            return False

    def find(self, owner):
        import win32gui

        windows = []
        win32gui.EnumWindows(
            lambda hwnd, _: windows.append(hwnd) if self.valid(hwnd, owner) else None, None
        )
        if self.matcher is not None:
            if len(windows) > 8:
                return None
            windows = [hwnd for hwnd in windows if self.matcher(self.capture(hwnd, owner))]
        return windows[0] if len(windows) == 1 else None

    def capture(self, hwnd, owner):
        import pywintypes

        try:
            return self._capture_window(hwnd, owner)
        except (OSError, pywintypes.error):
            # The owned popup may close between enumeration and capture.
            # Missing diagnostic pixels must not fail the official task.
            return None

    def _capture_window(self, hwnd, owner):
        import ctypes

        import numpy as np
        import win32gui
        import win32ui

        if not self.valid(hwnd, owner):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        dc = win32gui.GetWindowDC(hwnd)
        source = win32ui.CreateDCFromHandle(dc)
        target = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            target.SelectObject(bitmap)
            if not ctypes.windll.user32.PrintWindow(hwnd, target.GetSafeHdc(), 2):
                return None
            return (
                np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8)
                .reshape(height, width, 4)[:, :, :3]
                .copy()
            )
        finally:
            target.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(hwnd, dc)
            win32gui.DeleteObject(bitmap.GetHandle())

    def foreground(self, hwnd, owner):
        import win32gui

        return self.valid(hwnd, owner) and win32gui.GetForegroundWindow() == hwnd

    def click(self, hwnd, owner, point=None):
        import win32api
        import win32con
        import win32gui

        if point is None or not all(isfinite(value) and 0 < value < 1 for value in point):
            return False
        if not self.foreground(hwnd, owner):
            return False
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win32api.SetCursorPos(
            (round(left + (right - left) * point[0]), round(top + (bottom - top) * point[1]))
        )
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True


class SavedLoginConfirmation:
    def __init__(self, reporter, popup=None):
        self.reporter = reporter
        self.popup = popup if popup is not None else OwnedLoginPopup(self.matches)
        self.attempted = False
        self.templates = None
        self.reported_waits = set()
        self.confirm_point = None
        self.matched_regions = None

    def _wait(self, task, reason):
        # Diagnostic facts only: repeated login polling is not task progress.
        # Use the original logger, retaining the official wait_login owner.
        if reason not in self.reported_waits:
            self.reported_waits.add(reason)
            try:
                task.log_info("YeYuGamer saved-account login handoff not applied: " + reason)
            except Exception:
                pass  # Observation must not prevent the original wait_login.
        return False

    def matches(self, frame):
        import cv2

        self.confirm_point = None
        self.matched_regions = None
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
            return False
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return False
        try:
            reduction = min(1, 1280 / max(width, height))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if reduction < 1:
                gray = cv2.resize(
                    gray,
                    (round(width * reduction), round(height * reduction)),
                    interpolation=cv2.INTER_AREA,
                )
            gray = cv2.GaussianBlur(gray, (5, 5), 1)
            if self.templates is None:
                root = Path(__file__).with_name("login-templates")
                self.templates = {
                    name: cv2.imread(str(root / (name + ".png")), cv2.IMREAD_GRAYSCALE)
                    for name in _BOXES
                }
            for name, box in _BOXES.items():
                left, top, right, bottom = (value // 2 for value in box)
                template = self.templates[name]
                if template is None or template.shape != (bottom - top, right - left):
                    return False
                if template.std() < 1:
                    return False
            layouts = []
            for step in range(20, 121):
                scale = step / 40
                scaled = {
                    name: cv2.GaussianBlur(
                        cv2.resize(
                            template,
                            (round(template.shape[1] * scale), round(template.shape[0] * scale)),
                        ),
                        (5, 5),
                        1,
                    )
                    for name, template in self.templates.items()
                }
                header = scaled["header"]
                if header.shape[0] > gray.shape[0] or header.shape[1] > gray.shape[1]:
                    continue
                scores = cv2.matchTemplate(gray, header[3:-3, 3:-3], cv2.TM_CCOEFF_NORMED)
                for _ in range(8):
                    _, score, _, anchor = cv2.minMaxLoc(scores)
                    if not isfinite(score) or score < 0.97:
                        break
                    anchor_x, anchor_y = anchor
                    radius = max(4, header.shape[0] // 2)
                    scores[
                        max(0, anchor_y - radius) : anchor_y + radius + 1,
                        max(0, anchor_x - radius) : anchor_x + radius + 1,
                    ] = -1
                    anchor_x -= 3
                    anchor_y -= 3
                    regions = {
                        "header": (
                            anchor_x,
                            anchor_y,
                            anchor_x + header.shape[1],
                            anchor_y + header.shape[0],
                        )
                    }
                    for name in ("confirm", "alternative"):
                        box = _BOXES[name]
                        expected_x = anchor_x + round(
                            (box[0] // 2 - _BOXES["header"][0] // 2) * scale
                        )
                        expected_y = anchor_y + round(
                            (box[1] // 2 - _BOXES["header"][1] // 2) * scale
                        )
                        template = scaled[name]
                        left, top = max(0, expected_x - 1), max(0, expected_y - 1)
                        right = min(gray.shape[1], expected_x + template.shape[1] + 1)
                        bottom = min(gray.shape[0], expected_y + template.shape[0] + 1)
                        template = template[3:-3, 3:-3]
                        region = gray[top:bottom, left:right]
                        if (
                            region.shape[0] < template.shape[0]
                            or region.shape[1] < template.shape[1]
                        ):
                            break
                        _, score, _, offset = cv2.minMaxLoc(
                            cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
                        )
                        if not isfinite(score) or score < 0.97:
                            break
                        regions[name] = (
                            left + offset[0] - 3,
                            top + offset[1] - 3,
                            left + offset[0] + template.shape[1] + 3,
                            top + offset[1] + template.shape[0] + 3,
                        )
                    if len(regions) != 3:
                        continue
                    if any(
                        left < 0 or top < 0 or right > gray.shape[1] or bottom > gray.shape[0]
                        for left, top, right, bottom in regions.values()
                    ):
                        continue
                    button = regions["confirm"]
                    center = ((button[0] + button[2]) / 2, (button[1] + button[3]) / 2)
                    if any(
                        abs(center[0] - previous[0]) < 8 and abs(center[1] - previous[1]) < 8
                        for previous, _ in layouts
                    ):
                        continue
                    layouts.append((center, regions))
                    if len(layouts) > 1:
                        return False
                else:
                    return False
            if len(layouts) != 1:
                return False
            center, regions = layouts[0]
            self.confirm_point = (center[0] / gray.shape[1], center[1] / gray.shape[0])
            self.matched_regions = {
                name: tuple(round(value / reduction) for value in box)
                for name, box in regions.items()
            }
        except cv2.error:
            # Optional recognition failure must leave the official login path
            # available. Input-lock and click exceptions are not caught here.
            return False
        return True

    def _emit(self, kind, detail):
        try:
            self.reporter.emit("attach-world", kind, detail)
        except Exception:
            pass

    def _capture(self, frame, phase, timing, action_id):
        name = "nte-login-" + phase + "-" + action_id + ".png"
        detail = dict(
            timing, actionId=action_id, phase=phase, fileName=name if frame is not None else None
        )
        detail.update(
            actionType="recognition",
            method="SavedLoginConfirmation.matches",
            frameSource="owned_popup_template_input",
            window="owned_NTEGame_popup",
            matched=True,
            regions=self.matched_regions,
            regionSpace="owned_popup_pixels",
            accountOwnership="unconfirmed",
        )
        try:
            import cv2

            if frame is None:
                raise ValueError("fresh_popup_frame_unavailable")
            path = Path(self.reporter.path).parent / name
            if not cv2.imwrite(str(path), frame):
                raise OSError("image encoder returned false")
        except Exception as error:
            detail["fileName"] = None
            detail["error"] = type(error).__name__
            self._emit("integration_progress", "saved_login_screenshot_failed:" + phase)
        self._emit(
            "integration_progress",
            "saved_login_capture_metadata:" + json.dumps(detail, sort_keys=True),
        )
        if detail["fileName"] is not None:
            self._emit("integration_evidence", name)

    def confirm(self, task):
        if self.attempted or task.scene.logged_in():
            return False
        owner = task.hwnd.hwnd
        hwnd = self.popup.find(owner)
        if hwnd is None:
            return self._wait(task, "no_unique_visible_owned_popup")
        if not self.popup.foreground(hwnd, owner):
            return self._wait(task, "owned_popup_not_foreground")
        frame = self.popup.capture(hwnd, owner)
        if frame is None:
            return self._wait(task, "owned_popup_capture_unavailable")
        if not self.matches(frame):
            return self._wait(task, "owned_popup_does_not_match_saved_account_template")
        action_id = uuid.uuid4().hex
        captures = []

        def recognition_frame():
            requested_wall = time.time_ns()
            requested_mono = time.monotonic_ns()
            try:
                fresh_frame = self.popup.capture(hwnd, owner)
            except Exception:
                fresh_frame = None
            captured_wall = time.time_ns()
            captured_mono = time.monotonic_ns()
            timing = {
                "requestedAtNs": requested_wall,
                "capturedAtNs": captured_wall,
                "capturedAt": datetime.fromtimestamp(
                    captured_wall / 1_000_000_000, timezone.utc
                ).isoformat(),
                "requestedMonotonicNs": requested_mono,
                "capturedMonotonicNs": captured_mono,
            }
            return fresh_frame, timing

        def click_confirm():
            # Revalidate inside the official input lock; user focus may change
            # between recognition and lock acquisition.
            if not self.popup.foreground(hwnd, owner):
                return False
            fresh, timing = recognition_frame()
            if not self.matches(fresh):
                return False
            # Preserve the exact input that passed the owned-popup matcher.
            # Encoding is deferred until after the official input lock releases.
            captures.append((fresh.copy(), "template", timing))
            self.attempted = True
            self._emit(
                "integration_progress",
                "saved_login_confirm_requested:user_authorized_template_v1:owned_NTEGame_popup",
            )
            try:
                task.log_info(
                    "YeYuGamer saved-account login confirmation: "
                    "user-authorized template v1, owned NTEGame popup"
                )
            except Exception:
                pass
            sent = self.popup.click(hwnd, owner, self.confirm_point)
            if not sent:
                return False
            return True

        try:
            sent = task.operate(click_confirm, block=True, restore_cursor=True)
        finally:
            for captured in captures:
                self._capture(*captured, action_id)
        if not sent:
            if self.attempted:
                self._emit(
                    "integration_progress", "saved_login_click_not_sent:popup_foreground_changed"
                )
            return False
        task.sleep(1)
        self._emit(
            "integration_progress", "saved_login_click_returned:waiting_for_official_ensure_main"
        )
        return True
