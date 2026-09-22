"""Heartbeat helper: clear stuck game clients by restarting the machine.

Why this exists
---------------
A game client that never completes process teardown keeps its single-instance
mutex (measured on this installation 2026-09-22: PGR -> ``comkurogameharukuro``,
GF2 -> ``ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default``).  The
kernel releases a mutex only when its owner is destroyed, so the game can never
launch again -- every later attempt sees "another instance is already running"
and exits within a second having written nothing.  Measured with ``handle64``:
the mutex has exactly one holder, the stuck client itself; its remaining threads
sit in an uninterruptible kernel wait with zero CPU time (not spinning), which is
why nothing in user mode can free it:

  * ``taskkill /F``                      -> "There is no running instance"
  * ``TerminateProcess``                 -> err=5 (STATUS_PROCESS_IS_TERMINATING)
  * external ``TerminateThread``         -> succeeds, changes nothing
  * restarting WerSvc / killing audiodg  -> no effect
  * restarting the Manager               -> no effect

A restart is the only recovery, and it has to be unattended, so this script only
fires when a restart is both needed and safe:

  1. an *enabled* game still has required todos left in the current game day,
  2. that game's client mutex is held by an enumerated-but-not-live leftover,
  3. the daily queue is idle (no active batch, no controller lease),
  4. no release is pending and no other heartbeat holds the wake lock,
  5. the console has been idle for a while (nobody is using the machine),
  6. auto-logon is configured -- otherwise a restart would strand the machine at
     the lock screen and the daily would never run again,
  7. cooldown / per-day cap have not been reached.

Because guard 3 can never come true on its own while the DailySupervisor keeps
starting a fresh batch every 5 minutes, this script first *reserves* the window:
it drops ``client-heal-pending.flag``, the supervisor stands down while the flag
is fresh, and the next tick finds an idle queue (see RESERVE_FLAG below).

Usage
-----
  heal-stuck-clients.py            decide and act
  heal-stuck-clients.py --dry-run  decide and report, never restart
  heal-stuck-clients.py --selftest read-only proof that every guard sees real state
  heal-stuck-clients.py --force    skip the per-day cap (still honours 1-5)

Exit codes: 0 healthy, 3 deferred (queue busy / observation aborted),
4 refused, 5 restarted.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import datetime
import json
import os
import pathlib
import platform
import subprocess
import sys
import time
import urllib.request

RUNTIME = pathlib.Path(r"C:\ProgramData\YeYuGamer\runtime")
API = "http://127.0.0.1:8877/api/v1"
HEAL_LOG = RUNTIME / "logs" / "stuck-client-heal.log"
HEAL_STATE = RUNTIME / "state" / "stuck-client-heal.json"
RELEASE_FLAG = RUNTIME / "state" / "release-pending.flag"

# The restart needs an idle queue, but the DailySupervisor's own retry loop keeps
# the queue busy: it starts a fresh batch every 5 minutes, every batch re-attempts
# the very games whose clients are stuck, and the gap between one batch finishing
# and the next starting is shorter than PRE_RESTART_OBSERVE_SECONDS.  Measured
# 2026-09-22: PGR/GF2 stayed stuck from 11:46 and this script logged
# "deferred: queue busy" on every tick -- the one action that could unblock the
# game day could never be taken (and every doomed batch left one more
# unrecoverable leftover behind).  So claim the window first: drop this flag, let
# the supervisor stand down while it is fresh, and restart from the next tick that
# finds an idle queue.  The flag is self-expiring, so a healer that stops running
# can never fence the game day -- the supervisor resumes on its own.
RESERVE_FLAG = RUNTIME / "state" / "client-heal-pending.flag"
RESERVE_FRESH_SECONDS = 1500

HEARTBEAT_LOCK = pathlib.Path(r"C:\Projects\YeYuGamer\.cache\heartbeat.lock")
# A heartbeat that dies without releasing the wake lock must not fence restarts
# for the rest of the uptime; the lock is only meaningful while it is fresh
# (same 25-minute rule the heartbeat itself uses for takeover).
HEARTBEAT_LOCK_STALE_SECONDS = 1500

# Evidence-based registry: only games measured on this installation, paired with
# the client image names that appear in the leftover entries.
CLIENTS = {
    "PGR": ("comkurogameharukuro", ("pgr.exe",)),
    "GF2": (
        "ilium-GF2-Game-GF2-Exilium-exe-SingleInstanceMutex-Default",
        ("gf2_exilium.exe", "girlfrontline2.exe"),
    ),
}

CONSOLE_IDLE_SECONDS = 180
COOLDOWN_SECONDS = 3600
MAX_RESTARTS_PER_GAME_DAY = 2

# The queue can go from "idle" to "a batch is starting" at any moment: the
# DailySupervisor ticks every 5 minutes and starts a daily whenever it finds an
# idle queue.  Restarting into that batch would kill a live run and leave the
# game day fenced, so the restart is observed and revocable:
#   * watch the queue for PRE_RESTART_OBSERVE_SECONDS before committing,
#   * then issue a delayed shutdown and revoke it with ``shutdown /a`` if a
#     batch shows up before the machine goes down.
PRE_RESTART_OBSERVE_SECONDS = 120
POST_RESTART_OBSERVE_SECONDS = 50
SHUTDOWN_DELAY_SECONDS = 60

ERROR_ALREADY_EXISTS = 183
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "%s %s" % (stamp, message)
    print(line)
    try:
        HEAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HEAL_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def snapshot() -> dict:
    token = (RUNTIME / "secrets" / "actors" / "cli.token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        API + "/snapshot",
        headers={"Authorization": "Bearer " + token, "X-YeYu-Gamer-Actor": "cli"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def mutex_is_held(name: str) -> bool:
    """Non-destructive probe: create the mutex and look at ERROR_ALREADY_EXISTS."""

    kernel32.CreateMutexW.restype = w.HANDLE
    kernel32.CreateMutexW.argtypes = (w.LPVOID, w.BOOL, w.LPCWSTR)
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return error == ERROR_ALREADY_EXISTS


def listed_not_live(names: tuple[str, ...]) -> dict[int, str]:
    """Enumerated processes matching ``names`` whose exit code is no longer STILL_ACTIVE."""

    wanted = {n.casefold() for n in names}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    found: list[tuple[int, str]] = []
    ok = kernel32.Process32First(snap, ctypes.byref(entry))
    while ok:
        name = entry.szExeFile.decode("mbcs", "replace")
        if name.casefold() in wanted:
            found.append((entry.th32ProcessID, name))
        ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)

    out: dict[int, str] = {}
    for pid, name in found:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        code = w.DWORD(0xDEADBEEF)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        if code.value != STILL_ACTIVE:
            out[pid] = name
    return out


def console_idle_seconds() -> int | None:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return max(0, (kernel32.GetTickCount() - info.dwTime) // 1000)


def computer_name() -> str:
    """The machine name, as Winlogon needs it for ``DefaultDomainName``.

    Deliberately *not* read from the Winlogon key: ``COMPUTERNAME`` is an
    environment variable, and reading it as a registry value silently returns
    nothing -- which made an earlier version of the auto-logon guard refuse
    every restart (caught by the read-only self-test on 2026-09-22).
    """

    for name in ("COMPUTERNAME", "HOSTNAME"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return platform.node().strip()


def auto_logon_reason() -> str | None:
    """Return ``None`` when auto-logon looks usable, otherwise why it does not.

    A guard, not a proof: we cannot try the logon without restarting.  So check
    everything that is known to break it -- a missing ``DefaultPassword`` value
    (the key must exist even when the account has a blank password), a wrong
    ``DefaultUserName``/``DefaultDomainName`` (the domain must be the machine
    name or ``.`` for a local account), or a disabled ``AutoAdminLogon``.

    Getting this wrong is the worst failure mode in the whole daily: the machine
    would come back to a lock screen, no interactive session would ever exist,
    and every hourly/5-minute task in the pipeline (all ``InteractiveToken``)
    would silently stop.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon",
        ) as key:
            def value(name: str) -> str | None:
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    return None

            if (value("AutoAdminLogon") or "").strip() != "1":
                return "AutoAdminLogon is not 1"
            user = (value("DefaultUserName") or "").strip()
            if not user:
                return "DefaultUserName is missing"
            domain = (value("DefaultDomainName") or "").strip()
            computer = computer_name()
            if domain.casefold() not in {"", ".", computer.casefold()}:
                return "DefaultDomainName %r is neither the machine name %r nor '.'" % (
                    domain,
                    computer,
                )
            if value("DefaultPassword") is None:
                return "DefaultPassword key is missing (autologon needs it even when empty)"
            # A broken Userinit/Shell signs the session straight back out, which
            # looks exactly like a failed auto-logon from the outside.
            userinit = (value("Userinit") or "").casefold()
            if "userinit.exe" not in userinit:
                return "Userinit is %r (the session would sign straight back out)" % userinit
            shell = (value("Shell") or "").casefold()
            if "explorer.exe" not in shell:
                return "Shell is %r (no desktop would come up)" % shell
    except OSError as error:
        return "cannot read Winlogon keys: %s" % error
    return None


def load_state() -> dict:
    try:
        return json.loads(HEAL_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    HEAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    HEAL_STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def selftest() -> int:
    """Read-only proof that every guard is wired to real machine state.

    Worth running after touching this file: on 2026-09-22 a typo in the
    auto-logon check made the guard refuse *every* restart while still looking
    healthy in code review -- only this self-test exposed it.
    """

    print("== mutex / leftover detection ==")
    for game, (mutex, names) in CLIENTS.items():
        print("  %-4s mutex_held=%s leftovers=%s"
              % (game, mutex_is_held(mutex), listed_not_live(names)))
    idle = console_idle_seconds()
    print("== guards ==")
    print("  console_idle_seconds = %s (need >= %s)" % (idle, CONSOLE_IDLE_SECONDS))
    print("  auto_logon_reason    = %s (need None)" % auto_logon_reason())
    print("  computer_name        = %r" % computer_name())
    print("  release flag exists  = %s" % RELEASE_FLAG.exists())
    print("  reserve flag         = %s (age=%s, fresh<=%s)"
          % (RESERVE_FLAG.exists(), reserve_age_seconds(), RESERVE_FRESH_SECONDS))
    print("  wake lock            = %s (age=%s, blocking while fresh<=%s)"
          % (HEARTBEAT_LOCK.exists(), heartbeat_lock_age(), HEARTBEAT_LOCK_STALE_SECONDS))
    try:
        snap = snapshot()
    except Exception as error:  # noqa: BLE001
        print("  snapshot             = UNAVAILABLE (%s)" % error)
        return 1
    print("  activeBatch          = %s" % ((snap.get("activeBatch") or {}).get("state"),))
    runtime = {g.get("gameId"): g for g in (snap.get("games") or [])}
    todos = (snap.get("todo") or {}).get("games") or {}
    for game in CLIENTS:
        summary = todos.get(game) or {}
        remaining = (summary.get("requiredTotal") or 0) - (summary.get("requiredCompleted") or 0)
        print("  %-4s enabled=%s required_remaining=%s runtimeState=%s"
              % (game, (runtime.get(game) or {}).get("enabled"), remaining,
                 (runtime.get(game) or {}).get("runtimeState")))
    print("== decision ==")
    return 0


def queue_idle(snap: dict) -> bool:
    batch = snap.get("activeBatch") or {}
    leases = (snap.get("executionControl") or {}).get("activeControllerLeaseCount") or 0
    return not batch and not leases


def wait_for_idle_queue(seconds: int, interval: int) -> bool:
    """Watch the queue for ``seconds``; return False as soon as it goes busy."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            if not queue_idle(snapshot()):
                return False
        except Exception as error:  # noqa: BLE001 - a busy Manager must not abort a heal
            log("observe: snapshot failed (%s); still watching" % error)
    return True


def write_reserve(games: list[str]) -> None:
    """Claim the next idle queue window for the restart.

    Refreshing the flag on every tick is what keeps the claim alive; the
    supervisor only honours it while the file is fresh.
    """

    RESERVE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": "restart to clear stuck client mutex for %s" % ",".join(games),
        "pid": os.getpid(),
        "writtenAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    RESERVE_FLAG.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_reserve() -> None:
    """Release the claim (called whenever a restart is no longer needed)."""

    try:
        RESERVE_FLAG.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:  # noqa: BLE001 - a stale claim expires by itself
        log("could not clear the reserve flag: %s" % error)


def reserve_age_seconds() -> float | None:
    try:
        return time.time() - RESERVE_FLAG.stat().st_mtime
    except OSError:
        return None


def heartbeat_lock_age() -> float:
    """Seconds since the wake lock was taken; a missing lock counts as ancient."""

    try:
        return time.time() - HEARTBEAT_LOCK.stat().st_mtime
    except OSError:
        return float("inf")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    no_observe = "--no-observe" in sys.argv

    if "--selftest" in sys.argv:
        selftest()
        return 0

    try:
        snap = snapshot()
    except Exception as error:  # noqa: BLE001 - a missing Manager must not crash the task
        log("ERROR cannot read the Manager snapshot: %s" % error)
        return 1

    runtime = {g.get("gameId"): g for g in (snap.get("games") or [])}
    todos = (snap.get("todo") or {}).get("games") or {}

    stuck: list[dict] = []
    for game_id, (mutex, names) in CLIENTS.items():
        if not (runtime.get(game_id) or {}).get("enabled"):
            continue
        summary = todos.get(game_id) or {}
        remaining = (summary.get("requiredTotal") or 0) - (summary.get("requiredCompleted") or 0)
        if remaining <= 0:
            continue
        if not mutex_is_held(mutex):
            continue
        leftovers = listed_not_live(names)
        if not leftovers:
            continue
        stuck.append({"gameId": game_id, "mutex": mutex, "remaining": remaining,
                      "leftovers": {str(k): v for k, v in leftovers.items()}})

    if not stuck:
        # Nothing left to heal: drop any claim so the supervisor resumes.
        clear_reserve()
        log("healthy: no enabled game is blocked by a stuck client instance")
        return 0

    log("stuck clients: %s" % json.dumps(stuck, ensure_ascii=False))
    busy = not queue_idle(snap)
    blockers = []
    if RELEASE_FLAG.exists():
        blockers.append("release pending")
    if HEARTBEAT_LOCK.exists() and heartbeat_lock_age() <= HEARTBEAT_LOCK_STALE_SECONDS:
        blockers.append("heartbeat wake lock held")
    idle = console_idle_seconds()
    if idle is not None and idle < CONSOLE_IDLE_SECONDS:
        blockers.append("console active %ss ago" % idle)
    logon_problem = auto_logon_reason()
    if logon_problem:
        blockers.append("auto-logon unusable -> %s" % logon_problem)

    state = load_state()
    now = datetime.datetime.now()
    last = state.get("lastRestartAt")
    if last:
        try:
            elapsed = (now - datetime.datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            elapsed = COOLDOWN_SECONDS + 1
        if elapsed < COOLDOWN_SECONDS:
            blockers.append("cooldown (%ds left)" % int(COOLDOWN_SECONDS - elapsed))

    game_day = snap.get("gameDay") or now.strftime("%Y-%m-%d")
    counts = (state.get("restarts") or {}).get(game_day) or {}
    if not force:
        over = [s["gameId"] for s in stuck if (counts.get(s["gameId"]) or 0) >= MAX_RESTARTS_PER_GAME_DAY]
        if over and len(over) == len(stuck):
            blockers.append("per-day cap reached for %s" % ",".join(over))

    if blockers:
        log("refused: %s" % "; ".join(blockers))
        return 4

    if dry_run:
        log("dry-run: would restart to clear %s (queue %s)"
            % (",".join(s["gameId"] for s in stuck), "busy" if busy else "idle"))
        return 5

    # Everything but the queue is ready.  Claim the window *before* waiting for
    # it: the supervisor stands down while this flag is fresh, so the running
    # batch can drain without a fresh one taking its place (see RESERVE_FLAG).
    write_reserve([s["gameId"] for s in stuck])
    if busy:
        log("reserved the next idle queue window (%s); deferring until the batch drains"
            % RESERVE_FLAG.name)
        return 3

    # The window between "queue idle" and "shutdown committed" is exactly when the
    # 5-minute supervisor may start a fresh daily; restarting into it would kill a
    # live run.  Keep watching, and give up quietly if the queue fills up.
    if not no_observe:
        log("observing the queue for %ss before committing" % PRE_RESTART_OBSERVE_SECONDS)
        if not wait_for_idle_queue(PRE_RESTART_OBSERVE_SECONDS, 15):
            log("aborted: a batch started during the observation window")
            return 3

    previous_state = json.loads(json.dumps(state)) if state else {}
    counts = dict(counts)
    for item in stuck:
        counts[item["gameId"]] = (counts.get(item["gameId"]) or 0) + 1
    state.setdefault("restarts", {})[game_day] = counts
    state["lastRestartAt"] = now.isoformat(timespec="seconds")
    state["lastReason"] = stuck
    save_state(state)

    log("restarting: %s (%ss delay, forced; queue idle, console idle %ss)"
        % (",".join(s["gameId"] for s in stuck), SHUTDOWN_DELAY_SECONDS, idle))
    result = subprocess.run(
        ["shutdown", "/r", "/t", str(SHUTDOWN_DELAY_SECONDS), "/f", "/c",
         "YeYuGamer: clearing stuck game client instances to restore the daily"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("ERROR shutdown failed rc=%s out=%s err=%s"
            % (result.returncode, result.stdout.strip(), result.stderr.strip()))
        save_state(previous_state)
        return 1

    if not no_observe:
        # Last chance: if a batch appeared after we scheduled the restart, revoke it.
        for _ in range(max(1, POST_RESTART_OBSERVE_SECONDS // 5)):
            time.sleep(5)
            try:
                if not queue_idle(snapshot()):
                    abort = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
                    log("revoked the scheduled restart: a batch started (shutdown /a rc=%s)"
                        % abort.returncode)
                    save_state(previous_state)
                    return 3
            except Exception as error:  # noqa: BLE001
                log("observe: snapshot failed (%s); still watching" % error)

    return 5


if __name__ == "__main__":
    sys.exit(main())
