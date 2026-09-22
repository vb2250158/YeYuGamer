"""Snapshot of live game-related processes: pid, name, working set, window, create time."""
import ctypes, subprocess, csv, io, datetime
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
k32.OpenProcess.argtypes=[wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype=wintypes.HANDLE
k32.CloseHandle.argtypes=[wintypes.HANDLE]
k32.GetExitCodeProcess.argtypes=[wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetExitCodeProcess.restype=wintypes.BOOL

class PMC(ctypes.Structure):
    _fields_=[("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD),
              ("PeakWorkingSetSize",ctypes.c_size_t),("WorkingSetSize",ctypes.c_size_t),
              ("QuotaPeakPagedPoolUsage",ctypes.c_size_t),("QuotaPagedPoolUsage",ctypes.c_size_t),
              ("QuotaPeakNonPagedPoolUsage",ctypes.c_size_t),("QuotaNonPagedPoolUsage",ctypes.c_size_t),
              ("PagefileUsage",ctypes.c_size_t),("PeakPagefileUsage",ctypes.c_size_t)]
k32.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindows = user32.EnumWindows
EnumWindows.argtypes=[ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
EnumWindows.restype=wintypes.BOOL
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes=[wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
IsWindowVisible = user32.IsWindowVisible
IsWindowVisible.argtypes=[wintypes.HWND]
IsIconic = user32.IsIconic
IsIconic.argtypes=[wintypes.HWND]

def windows():
    owned = {}
    def cb(hwnd, lparam):
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        vis = bool(IsWindowVisible(hwnd))
        icon = bool(IsIconic(hwnd))
        if pid.value:
            owned.setdefault(pid.value, []).append((vis, icon))
        return True
    EnumWindows(EnumWindows.__class__ and ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return owned

wins = windows()

def probe(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h: return None
    try:
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
        mem = k32.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        return (code.value if ok else None, pmc.WorkingSetSize if mem else None)
    finally:
        k32.CloseHandle(h)

rows = []
out = subprocess.run(["tasklist","/FO","CSV","/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
for row in csv.reader(io.StringIO(out)):
    if len(row) < 2: continue
    name = row[0]
    try: pid = int(row[1])
    except ValueError: continue
    rows.append((name, pid, row[4] if len(row) > 4 else ""))

keys = ("launcher","game","client","endfield","wuwa","wuthering","pgr","punishing",
        "starrail","zzz","zenless","gf2","nte","nikke","wegame","games.exe","updater")
print(f"{'pid':>7} {'exit':>5} {'memMB':>8} {'win':>10}  name")
for name, pid, mem in rows:
    low = name.casefold()
    if not any(k in low for k in keys): continue
    got = probe(pid)
    code = got[0] if got else None
    wsm = got[1] if got else None
    w = wins.get(pid, [])
    surf = ",".join(f"{'vis' if v else 'hid'}{'/min' if i else ''}" for v, i in w) or "-"
    print(f"{pid:>7} {str(code):>5} {(wsm or 0)//1024//1024:>8} {surf:>10}  {name}")
