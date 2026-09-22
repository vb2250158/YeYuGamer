"""报告当前前台窗口的持有者，并判定它是否"无响应"。

为什么需要它：游戏启动器要靠 SetForegroundWindow 抢前台。Windows 的前台锁规则是
"非前台进程不许抢"，而**更狠的一条**是：如果当前前台窗口所属的线程处于挂起/不泵消息
的状态，别的进程连切换都做不到（AttachThreadInput/激活会失败），表现为启动器报
`not-ready:foreground-not-acquired`（2026-09-22 Endfield 实测）。

用法：
    python hb-foreground.py            # 只看当前前台窗口
    python hb-foreground.py --all      # 再看一遍所有可见窗口按 z 序（前 15 个）
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000


def _text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _image_name(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"<open-process err={ctypes.get_last_error()}>"
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return f"<query err={ctypes.get_last_error()}>"
    finally:
        kernel32.CloseHandle(handle)


def _thread_responsive(hwnd: int) -> tuple[bool, int]:
    """WM_NULL + SMTO_ABORTIFHUNG：返回 (是否响应, SendMessageTimeout 的返回码)。"""
    result = ctypes.c_size_t(0)
    code = user32.SendMessageTimeoutW(
        hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 1500, ctypes.byref(result)
    )
    return bool(code), int(code)


def describe(hwnd: int, label: str) -> None:
    pid = wintypes.DWORD(0)
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    responsive, code = _thread_responsive(hwnd)
    print(
        f"{label}: hwnd=0x{hwnd:08x} pid={pid.value} tid={tid} "
        f"image={_image_name(pid.value)} responsive={responsive} (send-timeout={code})"
    )
    print(f"    class={_class(hwnd)!r} title={_text(hwnd)!r} "
          f"hung={bool(user32.IsHungAppWindow(hwnd))} "
          f"visible={bool(user32.IsWindowVisible(hwnd))} "
          f"minimized={bool(user32.IsIconic(hwnd))}")


def main(argv: list[str]) -> int:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        print("foreground: <none>")
    else:
        describe(hwnd, "foreground")

    if "--all" in argv:
        print("-- z-order (top 15 visible) --")
        current = user32.GetTopWindow(None)
        shown = 0
        while current and shown < 15:
            if user32.IsWindowVisible(current):
                shown += 1
                describe(current, f"#{shown}")
            current = user32.GetWindow(current, 2)  # GW_HWNDNEXT
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
