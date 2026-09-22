"""Wake lock for the unattended heartbeat (see ``docs/heartbeat-playbook.md`` §1).

Two heartbeats running at once is not hypothetical: on 2026-09-23 one killed a
publish that the other had started, and one left ``release-pending.flag`` behind
so ``DailySupervisor`` stood down for two hours.  The old recipe -- test whether
``.cache/heartbeat.lock`` exists, then ``echo $$`` into it -- is not mutual
exclusion: both sides can pass the test before either writes.

What this does instead:

* ``take`` creates the lock with ``O_CREAT | O_EXCL``, so exactly one caller
  wins; the loser exits 3 and should return quietly.
* Only **age** decides staleness (25 minutes, the old recipe's rule).  The pid in
  the file is a record of who took it, not an ownership test: on this host every
  heartbeat step runs in its own short-lived process, so "the pid is gone" is
  true almost immediately and would make the lock meaningless.
* ``release`` removes the lock unconditionally; call it only when you took it.
  A crashed heartbeat therefore fences the stuck-client healer (guard 4) for at
  most 25 minutes -- ``heal-stuck-clients.py`` ignores an older lock.

Exit codes: 0 taken/released/status printed, 3 already held by a fresh lock.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

LOCK = pathlib.Path(r"C:\Projects\YeYuGamer\.cache\heartbeat.lock")
STALE_SECONDS = 1500


def read_lock() -> dict:
    try:
        raw = LOCK.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        # The pre-2026-09-23 recipe wrote a bare pid.
        return {"pid": int(raw or 0)}


def describe() -> dict:
    info = read_lock()
    try:
        age = time.time() - LOCK.stat().st_mtime
    except OSError:
        return {"held": False}
    return {
        "held": True,
        "pid": int(info.get("pid") or 0),
        "takenAt": info.get("at"),
        "ageSeconds": round(age, 1),
        "stale": age > STALE_SECONDS,
    }


def take() -> int:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    for attempt in (1, 2):
        try:
            handle = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            state = describe()
            if attempt == 1 and state.get("stale"):
                print("taking over a stale lock: %s" % json.dumps(state))
                try:
                    LOCK.unlink()
                except OSError:
                    pass
                continue
            print("held by another heartbeat: %s" % json.dumps(state))
            return 3
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(payload)
        print("taken: %s" % payload)
        return 0
    return 3


def release() -> int:
    try:
        LOCK.unlink()
    except OSError:
        pass
    print("released")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "take":
        sys.exit(take())
    if mode == "release":
        sys.exit(release())
    if mode == "status":
        print(json.dumps(describe()))
        sys.exit(0)
    raise SystemExit("usage: hb-lock.py <take|release|status>")
