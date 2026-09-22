"""Heartbeat helper: single-instance mutex probe + last-resort thread reaper.

Why this exists
---------------
A game client that fails to complete process teardown keeps its single-instance
mutex in ``\\Sessions\\1\\BaseNamedObjects`` (PGR: ``comkurogameharukuro``,
GF2: ``ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default``).  The kernel
only releases a mutex when the owning process is destroyed, so every later launch
sees "already running" and exits instantly (observed: PGR exits 0 within seconds,
writes no log at all).  ``TerminateProcess`` on such a process fails with
ERROR_ACCESS_DENIED (the status is really STATUS_PROCESS_IS_TERMINATING), but the
process still has enumerable threads -- so try terminating those instead.

Modes
-----
  probe <mutex-name> [<mutex-name> ...]   report whether each mutex already exists
  threads <pid> [<pid> ...]               list threads of each pid
  reap <pid> [<pid> ...]                  TerminateThread every thread of each pid

Probing is read-only.  ``reap`` is destructive and must only be pointed at pids
that were verified as leftover/dying instances of a client we own.
"""
import ctypes
import ctypes.wintypes as w
import sys

k = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPTHREAD = 0x4
THREAD_TERMINATE = 0x0001
THREAD_SUSPEND_RESUME = 0x0002
ERROR_ALREADY_EXISTS = 183

MUTEX_ALL_ACCESS = 0x001F0001


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


def thread_ids(pid):
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


def probe(name):
    ctypes.set_last_error(0)
    h = k.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        return "CREATE FAILED err=%d" % err
    k.CloseHandle(h)
    return "HELD (already exists)" if err == ERROR_ALREADY_EXISTS else "free (not held)"


mode = sys.argv[1]

if mode == "probe":
    for name in sys.argv[2:]:
        print("  %-62s %s" % (name, probe(name)))
elif mode == "threads":
    for arg in sys.argv[2:]:
        print("  pid=%s threads=%s" % (arg, thread_ids(int(arg))))
elif mode == "reap":
    for arg in sys.argv[2:]:
        pid = int(arg)
        for tid in thread_ids(pid):
            ctypes.set_last_error(0)
            h = k.OpenThread(THREAD_TERMINATE | THREAD_SUSPEND_RESUME, False, tid)
            if not h:
                print("  pid=%s tid=%s OpenThread FAILED err=%d" % (pid, tid, ctypes.get_last_error()))
                continue
            ctypes.set_last_error(0)
            if k.TerminateThread(h, 1):
                print("  pid=%s tid=%s TerminateThread ok" % (pid, tid))
            else:
                print("  pid=%s tid=%s TerminateThread FAILED err=%d" % (pid, tid, ctypes.get_last_error()))
            k.CloseHandle(h)
else:
    print(__doc__)
    sys.exit(2)
