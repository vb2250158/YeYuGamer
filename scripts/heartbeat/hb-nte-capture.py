"""NTE "启动器窗口抓帧全黑" 的判别工具（只读）。

背景：2026-09-23 定案——NTE 的 `launcher_button color 0.0` / `windows_graphics:no frame for 10 sec`
不是窗口或 WGC 的问题：同一个 hwnd、重叠时间里，**新建的抓帧会话能正常拿到帧**，而那次 attempt
里长驻的会话 45 分钟拿不到。详见 `docs/daily-workflow.md` §7 2026-09-23 08:0x 条。

这个脚本把那个判决性实验固化成一条命令，供以后遇到 NTE 全黑时先判别"会话坏了"还是"窗口/环境坏了"。

用法（**必须用上游工具自己的 venv 解释器**，因为要 import 上游的 `ok` 包）：

    TB=$(python -c "import json;print(json.load(open(r'C:\\ProgramData\\YeYuGamer\\runtime\\adapters\\game-modules\\nte\\tool-binding.json'))['tool']['root'])")
    "$TB/.venv/Scripts/python.exe" scripts/heartbeat/hb-nte-capture.py

退出码：0 = 新 WGC 会话能拿到帧（⇒ 缺陷在那一个 attempt 的会话里，重试/快速失败才是解法）；
        3 = 新会话也拿不到帧（⇒ 这次是窗口/环境层，按 playbook §7 走）；
        4 = 没找到启动器窗口（游戏没起/已退出）；5 = 缺少依赖，只打了窗口信息。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import pathlib
import sys
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
user32.SetProcessDPIAware()

TOOL_BINDING = pathlib.Path(
    r"C:\ProgramData\YeYuGamer\runtime\adapters\game-modules\nte\tool-binding.json"
)
DEFAULT_TITLE = "异环启动器"

BLACKNESS = 8  # 平均像素低于它就当"全黑"


def find_windows(title_sub: str):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_sub in buf.value:
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


def window_info(hwnd: int) -> dict:
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cloaked = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except Exception:
        pass
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import win32gui

        cls = win32gui.GetClassName(hwnd)
    except Exception:
        cls = "?"
    return {
        "hwnd": hwnd,
        "pid": pid.value,
        "class": cls,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "cloaked": cloaked.value,
        "rect": (rect.left, rect.top, rect.right, rect.bottom),
        "size": (rect.right - rect.left, rect.bottom - rect.top),
    }


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def _stats(bits, width: int, height: int) -> dict:
    import numpy as np

    raw = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte * (width * 4 * height))).contents
    arr = np.frombuffer(bytes(raw), dtype=np.uint8).reshape(height, width, 4)
    bgr = arr[:, :, :3].astype("float32")
    return {
        "mean": round(float(bgr.mean()), 2),
        "max": int(bgr.max()),
        "nonblack_ratio": round(float((bgr.max(axis=2) > 12).mean()), 4),
    }


def print_window(hwnd: int, flag: int) -> dict | None:
    """PrintWindow 取像素。flag 2 = PW_RENDERFULLCONTENT，0 = 旧语义。"""
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(memdc, bitmap)
    bmi = _BitmapInfoHeader()
    bmi.biSize = ctypes.sizeof(_BitmapInfoHeader)
    bmi.biWidth = width
    bmi.biHeight = -height
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(width * 4 * height)
    bits = ctypes.cast(buf, ctypes.c_void_p)
    try:
        user32.PrintWindow(hwnd, memdc, flag)
        gdi32.GetDIBits(memdc, bitmap, 0, height, bits, ctypes.byref(bmi), 0)
        return _stats(bits, width, height)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)


def wgc_probe(hwnd: int, size: tuple[int, int], attempts: int = 20, pause: float = 0.15) -> dict:
    """用上游自己的抓帧类开一个**全新会话**，看能否拿到帧。"""
    import threading

    from ok.device.capture_methods.windows_graphics import WindowsGraphicsCaptureMethod

    class StubWindow:
        def __init__(self, handle, width, height):
            self.hwnd = handle
            self.exists = True
            self.width = width
            self.height = height
            self.capture_target_signature = ("hb-nte-capture", handle)
            self.app_exit_event = threading.Event()

    capture = WindowsGraphicsCaptureMethod(StubWindow(hwnd, *size))
    frames = 0
    first = None
    try:
        for _ in range(attempts):
            frame = capture.do_get_frame()
            if frame is not None:
                frames += 1
                if first is None:
                    bgr = frame[:, :, :3].astype("float32")
                    first = {
                        "shape": list(frame.shape),
                        "mean": round(float(bgr.mean()), 2),
                        "max": int(bgr.max()),
                        "nonblack_ratio": round(float((bgr.max(axis=2) > 12).mean()), 4),
                    }
            time.sleep(pause)
        connected = capture.connected()
    finally:
        capture.close()
    return {"frames": frames, "attempts": attempts, "first": first, "connected": connected}


def main() -> int:
    parser = argparse.ArgumentParser(description="NTE 启动器窗口抓帧判别（只读）")
    parser.add_argument("--title", default=DEFAULT_TITLE, help=f"启动器窗口标题子串，默认 {DEFAULT_TITLE!r}")
    parser.add_argument("--attempts", type=int, default=20, help="WGC 取帧次数，默认 20")
    args = parser.parse_args()

    if TOOL_BINDING.exists():
        try:
            binding = json.loads(TOOL_BINDING.read_text(encoding="utf-8"))
            print(f"binding   : nte activeToolVersion={binding['upstream'].get('activeToolVersion')} "
                  f"root={binding['tool'].get('root')}")
        except Exception as error:
            print(f"binding   : 读取失败 {error}")

    windows = find_windows(args.title)
    if not windows:
        print(f"RESULT    : 未找到标题含 {args.title!r} 的窗口（游戏没起或已退出）")
        return 4

    for hwnd, title in windows:
        info = window_info(hwnd)
        print(f"window    : {title!r} hwnd={info['hwnd']}(={hex(info['hwnd'])}) pid={info['pid']} "
              f"class={info['class']} visible={info['visible']} minimized={info['minimized']} "
              f"cloaked={info['cloaked']} rect={info['rect']} size={info['size']}")

    hwnd, title = windows[0]
    info = window_info(hwnd)
    for flag, label in ((2, "PW_RENDERFULLCONTENT"), (0, "PW_legacy")):
        try:
            stats = print_window(hwnd, flag)
            verdict = "全黑" if stats and stats["mean"] < BLACKNESS else "有内容"
            print(f"printwin  : {label} {verdict} {stats}")
        except Exception as error:
            print(f"printwin  : {label} 失败 {error}")

    try:
        result = wgc_probe(hwnd, info["size"], attempts=args.attempts)
    except ImportError as error:
        print(f"wgc       : 无法 import 上游 ok 包（{error}）——请用上游工具自己 venv 的解释器运行")
        return 5
    except Exception as error:
        print(f"wgc       : 探测异常 {type(error).__name__}: {error}")
        return 5

    print(f"wgc       : 新会话 {result['frames']}/{result['attempts']} 次拿到帧 "
          f"connected={result['connected']} 首帧={result['first']}")
    if result["frames"] > 0 and (result["first"] or {}).get("mean", 0) >= BLACKNESS:
        print("RESULT    : 新会话正常 ⇒ 缺陷在**那次 attempt 的长驻会话**里（重试/快速失败才是解法）；"
              "别再查窗口/图形层/提权")
        return 0
    if result["frames"] > 0:
        print("RESULT    : 新会话能拿到帧但内容全黑 ⇒ 窗口自身没渲染（按 playbook §7 走）")
        return 3
    print("RESULT    : 新会话也拿不到帧 ⇒ 这次是窗口/环境层（按 playbook §7 走）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
