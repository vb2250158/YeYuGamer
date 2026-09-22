"""Heartbeat helper: decisive liveness probe for a pid list.

Settles "terminated zombie object" vs "process wedged mid-teardown":

* OpenProcess is tried with each access mask separately so ACCESS_DENIED can be
  attributed to the *right* right.
* GetExitCodeProcess == STILL_ACTIVE means the process has not exited.
* WaitForSingleObject needs SYNCHRONIZE; we use exactly that handle so the wait
  result is always meaningful (WAIT_OBJECT_0 == terminated, WAIT_TIMEOUT == not).
* Thread32First/Next gives the real thread count (0 == no threads left, i.e. the
  process body is gone even though the process object is still alive).
* GetProcessTimes exitTime non-zero confirms the exit was recorded.

Usage: hb-probe.py <pid> [<pid> ...]
"""
import ctypes
import ctypes.wintypes as w
import sys
import time

k = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPTHREAD = 0x4
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0x0
WAIT_TIMEOUT = 0x102

ACCESS = [
    ("QUERY_LIMITED_INFORMATION", PROCESS_QUERY_LIMITED_INFORMATION),
    ("SYNCHRONIZE", SYNCHRONIZE),
    ("TERMINATE", PROCESS_TERMINATE),
]


class FT(ctypes.Structure):
    _fields_ = [("low", w.DWORD), ("high", w.DWORD)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ThreadID", w.DWORD),
        ("th32OwnerProcessID", w.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", w.DWORD),
    ]


def ft_value(f):
    return (f.high << 32) | f.low


def filetime_str(f):
    v = ft_value(f)
    if not v:
        return "0"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((v - 116444736000000000) / 10_000_000.0))


def thread_count(pid):
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == -1:
        return None
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    n = 0
    ok = k.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            n += 1
        ok = k.Thread32Next(snap, ctypes.byref(te))
    k.CloseHandle(snap)
    return n


for arg in sys.argv[1:]:
    pid = int(arg)
    print("=== pid=%d ===" % pid)

    for label, mask in ACCESS:
        ctypes.set_last_error(0)
        h = k.OpenProcess(mask, False, pid)
        err = ctypes.get_last_error()
        print("  OpenProcess(%-26s) handle=%s err=%d" % (label, "ok" if h else "NULL", err))
        if h and label == "QUERY_LIMITED_INFORMATION":
            code = w.DWORD(0xDEADBEEF)
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            print("    GetExitCodeProcess ok=%s code=%d %s"
                  % (bool(ok), code.value,
                     "STILL_ACTIVE(not exited)" if code.value == STILL_ACTIVE else "=> exited"))
            c = FT(); e = FT(); kk = FT(); u = FT()
            ok = k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kk), ctypes.byref(u))
            if ok:
                print("    created=%s exitTime=%s" % (filetime_str(c), filetime_str(e)))
            k.CloseHandle(h)
        elif h and label == "SYNCHRONIZE":
            ctypes.set_last_error(0)
            r = k.WaitForSingleObject(h, 0)
            print("    WaitForSingleObject -> 0x%X %s" % (
                r,
                {WAIT_OBJECT_0: "TERMINATED(signaled)",
                 WAIT_TIMEOUT: "NOT-signaled(still alive)"}.get(r, "WAIT_FAILED/other")))
            k.CloseHandle(h)
        elif h:
            k.CloseHandle(h)

    print("  threads=%s" % thread_count(pid))
