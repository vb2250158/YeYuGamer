"""Capture a registered game window without accepting caller supplied window data."""

from __future__ import annotations

import ctypes
import os
import struct
import zlib
from ctypes import wintypes
from dataclasses import dataclass


class WindowCaptureError(RuntimeError):
    """The Manager could not produce a trustworthy game-window frame."""


@dataclass(frozen=True, slots=True)
class CapturedWindow:
    content: bytes
    hwnd: int
    pid: int
    process_name: str
    title: str
    width: int
    height: int
    method: str


@dataclass(frozen=True, slots=True)
class _WindowRule:
    process_names: frozenset[str]
    title_fragments: tuple[str, ...]
    capture_process_names: frozenset[str] | None = None


# This registry is deliberately Manager-owned. API callers provide only GameId and
# RunId, so an Agent cannot turn the screenshot capability into a desktop scraper.
_RULES: dict[str, _WindowRule] = {
    "StarRail": _WindowRule(frozenset({"starrail.exe"}), ("崩坏：星穹铁道", "Honkai: Star Rail")),
    "ZZZ": _WindowRule(frozenset({"zenlesszonezero.exe"}), ("绝区零", "Zenless Zone Zero")),
    "WW": _WindowRule(frozenset({"wuthering waves.exe", "client-win64-shipping.exe"}), ("鸣潮", "Wuthering Waves")),
    "PGR": _WindowRule(frozenset({"pgr.exe"}), ("战双帕弥什", "Punishing: Gray Raven")),
    "Endfield": _WindowRule(frozenset({"endfield.exe", "endfield-win64-shipping.exe"}), ("明日方舟：终末地", "Arknights: Endfield")),
    "GF2": _WindowRule(frozenset({"gf2_exilium.exe", "girlfrontline2.exe"}), ("少女前线2", "GIRLS' FRONTLINE 2")),
    "NIKKE": _WindowRule(frozenset({"nikke.exe"}), ("胜利女神", "GODDESS OF VICTORY: NIKKE")),
    "NTE": _WindowRule(
        frozenset({"ntegame.exe", "htgame.exe", "nte.exe", "neverness to everness.exe"}),
        ("异环", "Neverness to Everness"),
        frozenset({"htgame.exe", "nte.exe", "neverness to everness.exe"}),
    ),
    "FGO": _WindowRule(frozenset({"fategrandorder.exe"}), ("命运-冠位指定", "Fate/Grand Order")),
    "BD2": _WindowRule(frozenset({"browndust2.exe"}), ("棕色尘埃2", "BrownDust2")),
    "CZN": _WindowRule(frozenset({"chaoszeronightmare.exe"}), ("卡厄思梦境", "Chaos Zero Nightmare")),
    "BA": _WindowRule(frozenset({"bluearchive.exe"}), ("蔚蓝档案", "Blue Archive")),
}

# Android games are displayed by the fixed LDPlayer instance that Manager
# validated and launched.  A process name by itself is not enough to authorize
# capture because several emulator instances can coexist; callers must also
# provide the exact player PID reported by ldconsole ``list2`` for this run.
_LDPLAYER_CAPTURE_PROCESS_NAMES = frozenset(
    {"dnplayer.exe", "ldplayer.exe", "leidian.exe"}
)


def registered_game_process_names(game_id: str) -> frozenset[str]:
    """Return the Manager-owned process allowlist for one supported game."""

    rule = _RULES.get(game_id)
    if rule is None:
        return frozenset()
    return rule.process_names


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _encode_bgra_png(width: int, height: int, pixels: bytes) -> bytes:
    stride = width * 4
    rows: list[bytes] = []
    for row_index in range(height - 1, -1, -1):
        bgra = pixels[row_index * stride : (row_index + 1) * stride]
        rgba = bytearray(stride)
        rgba[0::4] = bgra[2::4]
        rgba[1::4] = bgra[1::4]
        rgba[2::4] = bgra[0::4]
        rgba[3::4] = b"\xff" * width
        rows.append(b"\x00" + bytes(rgba))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=6))
        + _png_chunk(b"IEND", b"")
    )


class WindowsGameWindowCapture:
    """Resolve and capture only windows admitted by the fixed GameId registry."""

    def capture(
        self,
        game_id: str,
        *,
        watermark_text: str | None = None,
        allowed_pids: frozenset[int] | None = None,
    ) -> CapturedWindow:
        if os.name != "nt":
            raise WindowCaptureError("game-window capture is available only on Windows")
        rule = _RULES.get(game_id)
        if rule is None:
            raise WindowCaptureError(f"no screenshot window binding is registered for GameId {game_id}")
        capture_names = rule.capture_process_names or rule.process_names
        capture_rule = _WindowRule(capture_names, rule.title_fragments)
        if allowed_pids:
            if game_id not in {"FGO", "BD2", "CZN"}:
                raise WindowCaptureError(
                    "a PID-bound emulator capture is not registered for this GameId"
                )
            capture_rule = _WindowRule(_LDPLAYER_CAPTURE_PROCESS_NAMES, ())
        return self._capture_windows(
            capture_rule,
            watermark_text=watermark_text,
            allowed_pids=allowed_pids,
        )

    def capture_processes(
        self,
        process_names: frozenset[str],
        *,
        allowed_pids: frozenset[int] | None = None,
        watermark_text: str | None = None,
    ) -> CapturedWindow:
        """Capture the largest visible window of Manager-launched launcher/game PIDs.

        Launch-phase evidence must show the official launcher (update progress,
        blank web surface, login prompt) before a registered game window exists.
        The caller is the Manager's own launcher, which supplies both the exact
        process-name allowlist it launched and the PIDs it observed; API callers
        never reach this method.
        """

        if os.name != "nt":
            raise WindowCaptureError("game-window capture is available only on Windows")
        normalized = frozenset(name.casefold() for name in process_names if name)
        if not normalized:
            raise WindowCaptureError("launch capture requires at least one process name")
        return self._capture_windows(
            _WindowRule(normalized, ()),
            watermark_text=watermark_text,
            allowed_pids=allowed_pids,
        )

    @staticmethod
    def _capture_windows(
        rule: _WindowRule,
        *,
        watermark_text: str | None = None,
        allowed_pids: frozenset[int] | None = None,
    ) -> CapturedWindow:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        window_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [window_callback, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        user32.FillRect.restype = ctypes.c_int
        user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        user32.DrawTextW.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.LPVOID, wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
        gdi32.CreateFontW.restype = wintypes.HFONT
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetBkMode.restype = ctypes.c_int
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        gdi32.SetTextColor.restype = wintypes.COLORREF

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        candidates: list[tuple[int, int, str, str, int, int]] = []

        def process_name(pid: int) -> str:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return ""
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return ""
                return os.path.basename(buffer.value).lower()
            finally:
                kernel32.CloseHandle(handle)

        @window_callback
        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0 or length > 1000:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            pid = int(pid_value.value)
            if allowed_pids is not None and pid not in allowed_pids:
                return True
            name = process_name(pid)
            if name not in rule.process_names or (
                rule.title_fragments
                and not any(
                    fragment.casefold() in title.casefold()
                    for fragment in rule.title_fragments
                )
            ):
                return True
            rect = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if 64 <= width <= 16384 and 64 <= height <= 16384 and width * height <= 67_108_864:
                candidates.append((int(hwnd), pid, name, title, width, height))
            return True

        if not user32.EnumWindows(visit, 0):
            raise WindowCaptureError("Windows window enumeration failed")
        if not candidates:
            raise WindowCaptureError("no visible, non-minimized registered game window was found")
        hwnd, pid, name, title, width, height = max(candidates, key=lambda item: item[4] * item[5])

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        source_dc = user32.GetDC(hwnd)
        memory_dc = gdi32.CreateCompatibleDC(source_dc)
        bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            rendered = bool(user32.PrintWindow(hwnd, memory_dc, 2))
            method = "PrintWindow"
            if not rendered:
                SRCCOPY = 0x00CC0020
                rendered = bool(gdi32.BitBlt(memory_dc, 0, 0, width, height, source_dc, 0, 0, SRCCOPY))
                method = "BitBlt"
            if not rendered:
                raise WindowCaptureError("the registered game window could not be rendered")
            if watermark_text:
                # Draw the timestamp into the captured bitmap itself.  The
                # source window remains untouched and the derivative cannot be
                # confused with the raw pre-step frame.
                bar_height = max(34, min(64, height // 14))
                bar = wintypes.RECT(0, height - bar_height, width, height)
                brush = gdi32.CreateSolidBrush(0x00000000)
                font = gdi32.CreateFontW(
                    -max(18, min(34, bar_height // 2)), 0, 0, 0, 700,
                    0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI",
                )
                previous_font = gdi32.SelectObject(memory_dc, font) if font else 0
                try:
                    if brush:
                        user32.FillRect(memory_dc, ctypes.byref(bar), brush)
                    gdi32.SetBkMode(memory_dc, 1)  # TRANSPARENT
                    gdi32.SetTextColor(memory_dc, 0x00FFFFFF)
                    text_rect = wintypes.RECT(12, height - bar_height, width - 12, height)
                    user32.DrawTextW(
                        memory_dc,
                        watermark_text,
                        -1,
                        ctypes.byref(text_rect),
                        0x00000020 | 0x00000004 | 0x00000002,
                    )  # SINGLELINE | VCENTER | RIGHT
                finally:
                    if previous_font:
                        gdi32.SelectObject(memory_dc, previous_font)
                    if font:
                        gdi32.DeleteObject(font)
                    if brush:
                        gdi32.DeleteObject(brush)
            info = BITMAPINFO(BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, height, 1, 32, 0, width * height * 4, 0, 0, 0, 0))
            buffer = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0) != height:
                raise WindowCaptureError("the game-window bitmap could not be read")
            content = _encode_bgra_png(width, height, buffer.raw)
        finally:
            if previous:
                gdi32.SelectObject(memory_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            if source_dc:
                user32.ReleaseDC(hwnd, source_dc)
        return CapturedWindow(content, hwnd, pid, name, title, width, height, method)
