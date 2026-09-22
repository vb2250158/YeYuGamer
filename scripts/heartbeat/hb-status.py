"""Heartbeat helper: compact Manager status from the typed snapshot."""
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(r"C:\ProgramData\YeYuGamer\runtime")
TOKEN = (ROOT / "secrets" / "actors" / "cli.token").read_text(encoding="utf-8").strip()
BASE = "http://127.0.0.1:8877/api/v1"


def snapshot() -> dict:
    request = urllib.request.Request(
        BASE + "/snapshot",
        headers={"Authorization": "Bearer " + TOKEN, "X-YeYu-Gamer-Actor": "cli"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    s = snapshot()
    batch = s.get("activeBatch") or {}
    print("manager   : %s startedAt=%s" % (s["manager"]["version"], s["manager"]["startedAt"][:19]))
    print("gameDay   : %s" % (s.get("gameDay")))
    print("activeBatch: %s state=%s" % (batch.get("batchId"), batch.get("state")))
    print("execution : %s" % json.dumps(s.get("executionControl"), ensure_ascii=False))
    print("counters  : %s" % json.dumps(s.get("counters"), ensure_ascii=False))
    todo = s.get("todo") or {}
    print("scope     : %s  req=%s/%s" % (
        todo.get("scopeGameIds"), todo.get("requiredCompleted"), todo.get("requiredTotal")))
    games = todo.get("games") or {}
    detail = {g.get("gameId"): g for g in (s.get("games") or [])}
    for game_id, summary in games.items():
        runtime = detail.get(game_id, {})
        print("  %-9s req=%-6s enabled=%-5s runtime=%-13s acc=%-12s next=%s" % (
            game_id,
            "%s/%s" % (summary.get("requiredCompleted"), summary.get("requiredTotal")),
            runtime.get("enabled"),
            runtime.get("runtimeState"),
            runtime.get("acceptanceState"),
            str(runtime.get("nextAction"))[:80],
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
