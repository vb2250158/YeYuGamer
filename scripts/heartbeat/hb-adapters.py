"""Heartbeat helper: report and repair per-game Adapter execution packages.

Why this exists
---------------
``scripts/Install-YeYuGamer*Adapter.ps1`` deliberately installs an Adapter as an
**unpromoted candidate** (``promotion.status = "candidate"``,
``executionReady = false``) and the publish pipeline promotes it afterwards via
the Manager-owned ``POST /adapter-versions/{version}/promotion-requests``.  If
that pipeline is interrupted between install and promote (2026-09-23: a publish
killed externally), the previously promoted module is already gone and the game
has **no execution authority at all**.

Nothing notices: the batch planner simply marks every required Todo of that game
``unsupported`` (``execution_package_unpromoted``) and *defers the whole game*
from the daily batch, with no blocker, no review and no notification.  The game
silently drops out of the day.

This tool makes that condition visible and repairs it through the designed,
Manager-owned path -- and only when the on-disk candidate test evidence is
bound to the exact installed payload, so a stale or mismatched candidate can
never be promoted by accident.

Usage
-----
    hb-adapters.py status                    # read-only report for every installed module
    hb-adapters.py repair [--json]           # promote every enabled game left unpromoted
    hb-adapters.py promote <GameId> [--wait-seconds N]

``Invoke-YeYuGamerDailySupervisor.py`` calls ``repair`` on every idle-queue tick,
so an interrupted release can no longer cost a game its whole day.

``promote`` uses the typed API (idempotency key + current stateVersion).  The
Manager refuses promotion while any execution is in flight, and the 5-minute
``DailySupervisor`` claims every idle queue, so a bare retry can be starved all
day.  With ``--wait-seconds`` the tool claims the next idle window exactly the
way ``Publish-YeYuGamerLocalRelease.ps1`` does -- by writing
``runtime/state/release-pending.flag`` (``DailySupervisor`` stands down while it
exists) -- and removes it again on every exit path, so it can never fence a
game day shut.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RUNTIME = pathlib.Path(r"C:\ProgramData\YeYuGamer\runtime")
MODULES_ROOT = RUNTIME / "adapters" / "game-modules"
EVIDENCE_ROOT = RUNTIME / "adapters" / "promotion-evidence"
RELEASE_FLAG = RUNTIME / "state" / "release-pending.flag"
TOKEN = (RUNTIME / "secrets" / "actors" / "cli.token").read_text(encoding="utf-8").strip()
BASE = "http://127.0.0.1:8877/api/v1"


def call(method: str, path: str, body: dict | None = None, idem: str | None = None, expect: int | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + TOKEN, "X-YeYu-Gamer-Actor": "cli"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem
    if expect is not None:
        headers["X-Expected-State-Version"] = str(expect)
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def snapshot() -> dict:
    status, payload = call("GET", "/snapshot")
    if status != 200:
        raise SystemExit("snapshot failed: %s %s" % (status, payload))
    return payload


def installed_modules() -> list[dict]:
    """Live per-game module directories (never the ``.previous-*``/``.*`` backups)."""
    out: list[dict] = []
    if not MODULES_ROOT.is_dir():
        return out
    for entry in sorted(MODULES_ROOT.iterdir()):
        if not entry.is_dir() or entry.name.startswith(".") or ".previous-" in entry.name:
            continue
        manifest_path = entry / "install-manifest.json"
        if not manifest_path.is_file():
            continue
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        promotion = document.get("promotion") or {}
        games = [str(value) for value in (document.get("supportedGameIds") or [])]
        out.append(
            {
                "module": entry.name,
                "gameIds": games,
                "packageVersion": document.get("packageVersion"),
                "buildId": document.get("buildId"),
                "installedAt": document.get("installedAt"),
                "promotionStatus": promotion.get("status"),
                "executionReady": document.get("executionReady"),
                "receiptFile": promotion.get("receiptFile") or "",
                "payloadDigest": promotion.get("payloadDigest"),
                "path": entry,
            }
        )
    return out


def candidate_test_evidence(game_id: str) -> dict | None:
    path = EVIDENCE_ROOT / game_id.lower() / "candidate-test-evidence.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def evidence_binding(module: dict, game_id: str) -> tuple[bool, str]:
    """The candidate may only be promoted when its evidence is bound to it."""
    evidence = candidate_test_evidence(game_id)
    if evidence is None:
        return False, "candidate test evidence is missing"
    if str(evidence.get("status")) != "passed" or evidence.get("passed") is not True:
        return False, "candidate test evidence did not pass"
    if str(evidence.get("packageVersion")) != str(module["packageVersion"]):
        return False, "candidate evidence packageVersion differs from the installed candidate"
    if str(evidence.get("buildId")) != str(module["buildId"]):
        return False, "candidate evidence buildId differs from the installed candidate"
    if str(evidence.get("payloadDigest")) != str(module["payloadDigest"]):
        return False, "candidate evidence payloadDigest differs from the installed candidate"
    if [str(value) for value in (evidence.get("supportedGameIds") or [])] != [game_id]:
        return False, "candidate evidence game scope differs from the installed candidate"
    return True, "evidence is bound to the installed candidate"


def report() -> int:
    snap = snapshot()
    enabled = {
        str(game["gameId"]): bool(game.get("enabled"))
        for game in snap.get("games", [])
    }
    modules = installed_modules()
    by_game = {game: module for module in modules for game in module["gameIds"]}
    bad: list[str] = []
    print("game       enabled module                   version                  status      ready receipt")
    for game_id in sorted(by_game, key=lambda value: (not enabled.get(value), value)):
        module = by_game[game_id]
        flag = enabled.get(game_id)
        print(
            f"{game_id:10} {str(flag):7} {module['module']:24} "
            f"{str(module['packageVersion']):24} {str(module['promotionStatus']):11} "
            f"{str(module['executionReady']):5} {bool(module['receiptFile'])}"
        )
        if flag and not (module["promotionStatus"] == "promoted" and module["executionReady"]):
            bound, why = evidence_binding(module, game_id)
            bad.append(game_id)
            print(f"           ^ ENABLED GAME WITHOUT EXECUTION AUTHORITY -> {why}")
    for game_id in sorted(enabled):
        if enabled[game_id] and game_id not in by_game:
            bad.append(game_id)
            print(f"{game_id:10} True    <no installed game module>")
    print()
    if bad:
        print("AT_RISK:", " ".join(bad), "(these games will be deferred out of every daily batch)")
        return 3
    print("OK: every enabled game has a promoted execution package")
    return 0


def _busy_conflict(status: int, payload: object) -> bool:
    if status != 409:
        return False
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "execution is active" in text or "adapter_busy" in text


def promote(game_id: str, wait_seconds: int = 0) -> int:
    """Promote one enabled game's installed candidate. Returns a process exit code."""

    modules = {game: module for module in installed_modules() for game in module["gameIds"]}
    module = modules.get(game_id)
    if module is None:
        print(f"{game_id}: no installed execution package")
        return 4
    if module["promotionStatus"] == "promoted" and module["executionReady"]:
        print(f"{game_id}: already promoted ({module['packageVersion']})")
        return 0
    bound, why = evidence_binding(module, game_id)
    if not bound:
        print(f"REFUSED: {game_id} candidate is not promotable -- {why}")
        return 4
    if module["promotionStatus"] != "candidate":
        print(f"REFUSED: {game_id} module is not an installed candidate ({module['promotionStatus']})")
        return 4
    version = urllib.parse.quote(str(module["packageVersion"]), safe="")
    deadline = time.monotonic() + max(0, wait_seconds)
    claimed = False
    attempt = 0
    try:
        while True:
            attempt += 1
            snap = snapshot()
            state_version = int(snap["stateVersion"])
            status, payload = call(
                "POST",
                "/adapter-versions/%s/promotion-requests" % version,
                {
                    "adapterId": "legacy-" + game_id.lower(),
                    "targetStage": "promoted",
                    "reason": "unattended repair: publish left the installed candidate unpromoted",
                    "requestedBy": "cli",
                },
                idem="hb-promote-%s-%s-%d" % (game_id.lower(), module["buildId"], attempt),
                expect=state_version,
            )
            if status in (200, 201, 202):
                break
            if _busy_conflict(status, payload) and time.monotonic() < deadline:
                if not claimed:
                    RELEASE_FLAG.write_text(
                        "adapter promotion in progress %s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        encoding="utf-8",
                    )
                    claimed = True
                    print("claimed the next idle window (release-pending.flag); waiting")
                time.sleep(45)
                continue
            if _busy_conflict(status, payload):
                print("deferred: an execution is active; the Manager refuses promotion until the queue is idle")
                return 6
            print("status", status)
            print(json.dumps(payload, ensure_ascii=False)[:900] if isinstance(payload, dict) else str(payload)[:900])
            return 5
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:900] if isinstance(payload, dict) else str(payload)[:900])
    finally:
        if claimed and RELEASE_FLAG.exists():
            RELEASE_FLAG.unlink()
            print("released the idle window claim (release-pending.flag removed)")
    after = {game: item for item in installed_modules() for game in item["gameIds"]}.get(game_id)
    ok = bool(after) and after["promotionStatus"] == "promoted" and after["executionReady"] is True
    print(f"{game_id}: now promoted={ok} ({after['packageVersion'] if after else 'missing'})")
    return 0 if ok else 5


def enabled_game_ids() -> list[str]:
    snap = snapshot()
    return sorted(
        str(game["gameId"]) for game in snap.get("games", []) if game.get("enabled")
    )


def repair(wait_seconds: int = 0, as_json: bool = False) -> int:
    """Promote every enabled game that is installed-but-unpromoted.

    This is what an interrupted release leaves behind, and the condition is
    otherwise invisible: the planner just defers the game.  Called by the
    five-minute supervisor on an idle queue so the next planned batch already
    contains the repaired game.

    Returns 0 when nothing needed repair or every repair succeeded, 3 when a game
    still needs repair but an execution is in flight, 4 when at least one game
    cannot be repaired (missing/mismatched evidence, or a failed promotion).
    """

    modules = {game: module for module in installed_modules() for game in module["gameIds"]}
    outcomes: list[dict] = []
    blocked = False
    deferred = False
    for game_id in enabled_game_ids():
        module = modules.get(game_id)
        if module is None:
            outcomes.append({"gameId": game_id, "outcome": "no_installed_package"})
            blocked = True
            continue
        if module["promotionStatus"] == "promoted" and module["executionReady"]:
            continue
        bound, why = evidence_binding(module, game_id)
        if not bound:
            outcomes.append({"gameId": game_id, "outcome": "not_repairable", "detail": why})
            blocked = True
            continue
        if module["promotionStatus"] != "candidate":
            outcomes.append(
                {
                    "gameId": game_id,
                    "outcome": "not_repairable",
                    "detail": "module is %s, not an installed candidate" % module["promotionStatus"],
                }
            )
            blocked = True
            continue
        code = promote(game_id, wait_seconds=wait_seconds)
        if code == 0:
            outcomes.append(
                {
                    "gameId": game_id,
                    "outcome": "promoted",
                    "packageVersion": module["packageVersion"],
                }
            )
        elif code == 6:
            outcomes.append({"gameId": game_id, "outcome": "deferred_busy"})
            deferred = True
        else:
            outcomes.append(
                {
                    "gameId": game_id,
                    "outcome": "promotion_failed",
                    "packageVersion": module["packageVersion"],
                }
            )
            blocked = True
    if as_json:
        print(json.dumps({"schemaVersion": 1, "repairs": outcomes}, ensure_ascii=False))
    else:
        print("repairs:", outcomes or "none needed")
    if blocked:
        return 4
    return 3 if deferred else 0


if __name__ == "__main__":
    args = [value for value in sys.argv[1:]]
    wait = 0
    if "--wait-seconds" in args:
        index = args.index("--wait-seconds")
        try:
            wait = int(args[index + 1])
        except (IndexError, ValueError):
            raise SystemExit("--wait-seconds requires an integer")
        del args[index : index + 2]
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    mode = args[0] if args else "status"
    if mode == "status":
        raise SystemExit(report())
    if mode == "repair":
        raise SystemExit(repair(wait_seconds=wait, as_json=as_json))
    if mode == "promote":
        if len(args) < 2:
            raise SystemExit("promote requires a GameId")
        raise SystemExit(promote(args[1], wait_seconds=wait))
    raise SystemExit("unknown mode: %s" % mode)
