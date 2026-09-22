"""Keep the current game day moving toward completion.

Runs from Windows Task Scheduler every five minutes.  It only does what a person
would do from the installed WebGUI: confirm the Manager is healthy, and when the
queue is idle while the current game day still has incomplete required todos, ask
the Manager for one more daily batch.

It also restarts the installed desktop host when the Manager is not answering.
Only the 04:00 daily used to start it, so any restart at another hour (a crash, or
``scripts\\heartbeat\\heal-stuck-clients.py`` rebuilding a clean process table)
left the machine with no Manager until the next 04:00 and the whole game day
stopped.  That recovery is bounded by a cooldown and a per-day cap.

Deliberately out of scope (they need judgement, not a timer):
  * never cancel or interrupt a running batch (a mid-batch cancel can void an
    already-passed contract -- 2026-09-15 lesson);
  * never release a human takeover or fabricate a completion;
  * never publish a release (the release needs the queue idle and is driven by
    the operator/agent);
  * never restart Windows (that is the stuck-client healer's own guarded job).

Every decision is appended to
``C:\\Projects\\YeYuGamer\\.cache\\logs\\daily-supervisor\\<date>.log``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

INSTALL_ROOT = Path(r"C:\Users\Admin\AppData\Local\Programs\YeYuGamer")
RUNTIME_ROOT = Path(r"C:\ProgramData\YeYuGamer\runtime")
LOG_ROOT = Path(r"C:\Projects\YeYuGamer\.cache\logs\daily-supervisor")
STATE_FILE = LOG_ROOT / "supervisor-state.json"
CLI = INSTALL_ROOT / "YeYuGamer.cmd"
TOKEN_FILE = RUNTIME_ROOT / "secrets" / "actors" / "cli.token"
BASE_URL = "http://127.0.0.1:8877/api/v1"

# A release needs an idle queue (the Manager refuses a safe stop while a run is
# active).  The release driver drops this flag while it works; standing down here
# keeps this heartbeat from stealing that window -- measured 2026-09-22 01:39:
# this heartbeat started a batch mid-release and broke the release.
RELEASE_FLAG = Path(r"C:\ProgramData\YeYuGamer\runtime\state\release-pending.flag")

# A game client that never finishes process teardown keeps its single-instance
# mutex, and only a machine restart clears it -- but that restart needs an idle
# queue, which this heartbeat would otherwise never leave: it starts a fresh
# batch every 5 minutes and each batch re-attempts exactly the stuck games.
# Measured 2026-09-22: PGR/GF2 were stuck from 11:46 and the healer deferred on
# "queue busy" on every tick, so the day could never be repaired.  The healer
# drops this flag to claim the window; stand down while it is fresh so the
# running batch can drain.  Freshness matters: a healer that stops running must
# not be able to fence the game day, so a stale flag is ignored.
HEAL_RESERVE_FLAG = RUNTIME_ROOT / "state" / "client-heal-pending.flag"
HEAL_RESERVE_FRESH_SECONDS = 1500

# A batch that keeps failing must not become an infinite restart loop.
MIN_SECONDS_BETWEEN_STARTS = 20 * 60
MAX_STARTS_PER_GAME_DAY = 8

# Manager recovery: the installed desktop host is what serves the API, and only
# the 04:00 daily used to start it.
HEALTH_URL = BASE_URL + "/health"
STARTER_SCRIPT = Path(r"C:\Projects\YeYuGamer\scripts\Start-YeYuGamer.ps1")
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
RECOVERY_STATE = LOG_ROOT / "manager-recovery.json"
MANAGER_START_COOLDOWN_SECONDS = 15 * 60
MAX_MANAGER_STARTS_PER_DAY = 12
MANAGER_STARTUP_TIMEOUT_SECONDS = 180


def log(message: str) -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + message
    with (LOG_ROOT / (time.strftime("%Y-%m-%d") + ".log")).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def snapshot() -> dict | None:
    request = urllib.request.Request(
        BASE_URL + "/snapshot",
        headers={"Authorization": "Bearer " + token(), "X-YeYu-Gamer-Actor": "cli"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        log("snapshot unreadable: %s: %s" % (type(error).__name__, error))
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def outstanding(games: dict, scope: list[str]) -> list[str]:
    """In-scope games whose required todos are not all completed yet."""

    pending: list[str] = []
    for game_id in scope:
        summary = games.get(game_id) or {}
        required = int(summary.get("requiredTotal") or 0)
        done = int(summary.get("requiredCompleted") or 0)
        if required and done < required:
            pending.append("%s:%s/%s" % (game_id, done, required))
    return pending


def heal_reserve_pending() -> bool:
    """True while the stuck-client healer is waiting for an idle queue."""

    try:
        age = time.time() - HEAL_RESERVE_FLAG.stat().st_mtime
    except OSError:
        return False
    return age <= HEAL_RESERVE_FRESH_SECONDS


def start_daily() -> str:
    key = "supervisor-" + time.strftime("%Y%m%d-%H%M")
    result = subprocess.run(
        [str(CLI), "--json", "start-daily", "--idempotency-key", key],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    text = (result.stdout or "") + (result.stderr or "")
    batch = ""
    try:
        batch = json.loads(text)["raw"]["result"]["batchId"]
    except Exception:
        batch = "(unparsed)"
    return "exit=%s batch=%s" % (result.returncode, batch or "(none)")


def manager_healthy(timeout: float = 3.0) -> bool:
    """Whether the installed desktop host answers ``/health`` with status ok."""

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_manager(max_attempts: int, timeout_seconds: int) -> str:
    """Start the installed desktop host when the Manager is not answering.

    ``Start-YeYuGamer.ps1`` starts the host unconditionally, so the health probe
    has to happen here first -- otherwise a healthy Manager would get a second
    instance competing for the API port.  Bounded by a cooldown and a per-day cap
    so a host that refuses to stay up cannot make this heartbeat spawn a process
    every five minutes.
    """

    if manager_healthy():
        return "healthy"

    now = time.time()
    state = {}
    try:
        state = json.loads(RECOVERY_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    day = time.strftime("%Y-%m-%d")
    if state.get("day") != day:
        state = {"day": day, "starts": 0, "lastAttemptAt": 0.0}
    if int(state.get("starts") or 0) >= MAX_MANAGER_STARTS_PER_DAY:
        return "budget exhausted (%d starts today)" % int(state.get("starts") or 0)
    if now - float(state.get("lastAttemptAt") or 0) < MANAGER_START_COOLDOWN_SECONDS:
        return "cooldown"

    state["starts"] = int(state.get("starts") or 0) + 1
    state["lastAttemptAt"] = now
    RECOVERY_STATE.parent.mkdir(parents=True, exist_ok=True)
    RECOVERY_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    log("the Manager is not answering; starting the installed desktop host (%d/%d today)"
        % (state["starts"], MAX_MANAGER_STARTS_PER_DAY))
    result = subprocess.run(
        [
            str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(STARTER_SCRIPT),
            "-NoOpenWebGui", "-TimeoutSeconds", str(timeout_seconds),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout_seconds + 60,
    )
    tail = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    detail = tail[-1] if tail else "(no output)"
    if manager_healthy():
        return "started (exit=%s)" % result.returncode
    return "start failed (exit=%s): %s" % (result.returncode, detail)


def main() -> int:
    if RELEASE_FLAG.exists():
        log("a release is pending; standing down so the release can take the idle queue")
        return 0

    if heal_reserve_pending():
        log("a stuck-client restart has claimed the next idle queue window; standing down")
        return 0

    payload = snapshot()
    if payload is None:
        outcome = ensure_manager(MAX_MANAGER_STARTS_PER_DAY, MANAGER_STARTUP_TIMEOUT_SECONDS)
        log("manager recovery: %s" % outcome)
        return 0 if outcome.startswith("started") else 1

    batch = payload.get("activeBatch")
    if batch:
        log("queue busy (%s, batch=%s); leaving it alone" % (batch.get("state"), batch.get("batchId")))
        return 0

    todo = payload.get("todo") or {}
    games = todo.get("games") or {}
    scope = list(todo.get("scopeGameIds") or [])
    if not scope:
        log("the Manager reports no in-scope games; nothing to do")
        return 0
    pending = outstanding(games, scope)
    if not pending:
        log("all in-scope required todos are complete for this game day")
        return 0

    state = load_state()
    game_day = str((payload.get("todo") or {}).get("gameDay") or "")
    if state.get("gameDay") != game_day:
        state = {"gameDay": game_day, "starts": 0, "lastStartAt": 0.0}
    now = time.time()
    if state["starts"] >= MAX_STARTS_PER_GAME_DAY:
        log("outstanding %s but the start budget (%d) is exhausted; needs a decision"
            % (";".join(pending), MAX_STARTS_PER_GAME_DAY))
        save_state(state)
        return 0
    if now - float(state.get("lastStartAt") or 0) < MIN_SECONDS_BETWEEN_STARTS:
        log("outstanding %s; waiting out the restart cooldown" % ";".join(pending))
        return 0

    log("outstanding %s -> requesting one daily batch" % ";".join(pending))
    outcome = start_daily()
    state["starts"] = int(state.get("starts") or 0) + 1
    state["lastStartAt"] = now
    save_state(state)
    log("start-daily %s (start %d/%d)" % (outcome, state["starts"], MAX_STARTS_PER_GAME_DAY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
