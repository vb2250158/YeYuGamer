import ctypes, ctypes.wintypes as w

u = ctypes.windll.user32
EnumWindows = u.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
GetWindowTextW = u.GetWindowTextW
GetWindowTextLengthW = u.GetWindowTextLengthW
IsWindowVisible = u.IsWindowVisible
GetWindowThreadProcessId = u.GetWindowThreadProcessId
IsIconic = u.IsIconic

rows = []
def cb(hwnd, lparam):
    pid = ctypes.c_ulong()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    n = GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value
    vis = bool(IsWindowVisible(hwnd))
    icon = bool(IsIconic(hwnd))
    if title or vis:
        rows.append((pid.value, hwnd, vis, icon, title))
    return True

EnumWindows(EnumWindowsProc(cb), None)

import subprocess
print("visible/ttiled top-level windows: %d" % len(rows))
visrows = [r for r in rows if r[2]]
print("-- visible: %d --" % len(visrows))
for pid, hwnd, vis, icon, title in sorted(visrows, key=lambda r: r[0]):
    print("  pid=%-7s hwnd=0x%08x minimized=%-5s title=%r" % (pid, hwnd, icon, title[:70]))
print("-- hidden but titled (first 25) --")
hid = [r for r in rows if not r[2] and r[4]]
for pid, hwnd, vis, icon, title in hid[:25]:
    print("  pid=%-7s hwnd=0x%08x title=%r" % (pid, hwnd, title[:70]))
