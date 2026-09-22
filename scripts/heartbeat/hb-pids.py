"""Heartbeat helper: report pid, start time and liveness for named processes."""
import ctypes
import ctypes.wintypes as w
import re
import sys
import time

k = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class FT(ctypes.Structure):
    _fields_ = [("low", w.DWORD), ("high", w.DWORD)]


def creation_time(pid):
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, None
    c = FT()
    e = FT()
    kk = FT()
    u = FT()
    ok = k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kk), ctypes.byref(u))
    code = w.DWORD()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    if not ok:
        return None, code.value
    value = (c.high << 32) | c.low
    return (value - 116444736000000000) / 10_000_000.0, code.value


snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
e = PE32()
e.dwSize = ctypes.sizeof(PE32)
rows = []
ok = k.Process32First(snap, ctypes.byref(e))
while ok:
    rows.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile.decode("mbcs", "replace")))
    ok = k.Process32Next(snap, ctypes.byref(e))
k.CloseHandle(snap)

pattern = re.compile(sys.argv[1], re.I)
print("pattern:", sys.argv[1])
for pid, ppid, name in rows:
    if pattern.search(name):
        created, code = creation_time(pid)
        stamp = time.strftime("%H:%M:%S", time.localtime(created)) if created else "?"
        live = "LIVE" if code == STILL_ACTIVE else ("exit=%s" % code)
        print("  pid=%s %s ppid=%s started=%s %s" % (pid, name, ppid, stamp, live))
