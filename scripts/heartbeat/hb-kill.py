"""Heartbeat helper: terminate leftover game processes via PROCESS_TERMINATE.

The launch path can leave same-named client processes behind; a new client then
refuses to start ("Another instance is already running").  ``taskkill`` fails on
these ("There is no running instance of the task") because it maps that message
onto an OpenProcess failure, so we open the process ourselves with an explicit
PROCESS_TERMINATE mask and report the real Win32 error.

Usage: hb-kill.py <pid> [<pid> ...]        (never kill a pid you did not verify)
"""
import ctypes
import sys

k = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x0

rc = 0
for arg in sys.argv[1:]:
    pid = int(arg)
    ctypes.set_last_error(0)
    h = k.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not h:
        print("pid=%-6d OpenProcess(TERMINATE) FAILED err=%d" % (pid, ctypes.get_last_error()))
        rc = 1
        continue
    ctypes.set_last_error(0)
    ok = k.TerminateProcess(h, 1)
    err = ctypes.get_last_error()
    if not ok:
        print("pid=%-6d TerminateProcess FAILED err=%d" % (pid, err))
        rc = 1
    else:
        wait = k.WaitForSingleObject(h, 5000)
        print("pid=%-6d TerminateProcess ok; wait=0x%X %s"
              % (pid, wait, "reaped" if wait == WAIT_OBJECT_0 else "still not signaled"))
    k.CloseHandle(h)

sys.exit(rc)
