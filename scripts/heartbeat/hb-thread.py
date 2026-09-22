"""Heartbeat helper: measure and clear thread suspend counts for stuck clients.

A client that never finishes terminating keeps its single-instance mutex (PGR:
``comkurogameharukuro``, GF2: ``ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default``)
enumerable forever, which makes every later launch die instantly.  The handle
dump proved the mutex has exactly one holder -- the stuck client itself -- so the
only way it is ever released is if that client's handle table finally closes,
which cannot happen while one of its threads refuses to exit.  A thread that was
*suspended* by something else (game boosters and remote-play helpers suspend
background processes) is the one case that is still reachable from user mode.

``SuspendThread`` returns the thread's previous suspend count, and the paired
``ResumeThread`` puts it back, so the two calls together measure the count
without changing it.

Modes
-----
  state  <pid> [<pid> ...]   suspend count of every thread (read-only, net zero)
  resume <pid> [<pid> ...]   drive every thread's suspend count down to zero
"""
import ctypes
import ctypes.wintypes as w
import sys

k = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPTHREAD = 0x4
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_LIMITED_INFORMATION = 0x0800


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


def thread_ids(pid: int) -> list[int]:
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == -1:
        return []
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    out = []
    ok = k.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k.Thread32Next(snap, ctypes.byref(te))
    k.CloseHandle(snap)
    return out


def suspend_count(tid: int) -> int | None:
    h = k.OpenThread(THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not h:
        return None
    previous = k.SuspendThread(h)          # -1 (0xFFFFFFFF) on failure
    if previous == 0xFFFFFFFF:
        k.CloseHandle(h)
        return None
    k.ResumeThread(h)                      # restores the count we just raised
    k.CloseHandle(h)
    return int(previous)


mode = sys.argv[1]
rc = 0

if mode == "state":
    for arg in sys.argv[2:]:
        pid = int(arg)
        tids = thread_ids(pid)
        if not tids:
            print("  pid=%-6d no enumerable threads" % pid)
            continue
        for tid in tids:
            count = suspend_count(tid)
            verdict = "unknown(unopenable)" if count is None else (
                "SUSPENDED (count=%d)" % count if count > 0 else "running (count=0)"
            )
            print("  pid=%-6d tid=%-6d %s" % (pid, tid, verdict))
elif mode == "resume":
    for arg in sys.argv[2:]:
        pid = int(arg)
        for tid in thread_ids(pid):
            h = k.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
            if not h:
                print("  pid=%-6d tid=%-6d OpenThread FAILED err=%d" % (pid, tid, ctypes.get_last_error()))
                rc = 1
                continue
            count = 0
            previous = k.ResumeThread(h)
            while previous > 0:
                count += 1
                previous = k.ResumeThread(h)
            k.CloseHandle(h)
            print("  pid=%-6d tid=%-6d resumed out of %d suspend(s)" % (pid, tid, count))
else:
    print(__doc__)
    sys.exit(2)

sys.exit(rc)
