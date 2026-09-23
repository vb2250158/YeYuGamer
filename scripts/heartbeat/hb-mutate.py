"""Heartbeat helper: typed Manager mutations with idempotency + state version."""
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(r"C:\ProgramData\YeYuGamer\runtime")
TOKEN = (ROOT / "secrets" / "actors" / "cli.token").read_text(encoding="utf-8").strip()
BASE = "http://127.0.0.1:8877/api/v1"


def call(method, path, body=None, idem=None, expect=None):
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
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def state_version():
    status, payload = call("GET", "/snapshot")
    if status != 200:
        raise SystemExit("snapshot failed: %s %s" % (status, payload))
    return payload["stateVersion"]


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "version":
        print(state_version())
    elif mode == "release-takeover":
        run_id, reason = sys.argv[2], sys.argv[3]
        version = state_version()
        status, payload = call(
            "POST",
            "/game-runs/%s/takeover-release-requests" % run_id,
            {"reason": reason, "requested_by": "cli"},
            idem="hb-release-%s-%s" % (run_id[:8], version),
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:700] if isinstance(payload, dict) else payload[:700])
    elif mode == "resume-run":
        run_id, reason = sys.argv[2], sys.argv[3]
        version = state_version()
        status, payload = call(
            "POST",
            "/game-runs/%s/resume-requests" % run_id,
            {"reason": reason, "requested_by": "cli"},
            idem="hb-runresume-%s-%s" % (run_id[:8], version),
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:700] if isinstance(payload, dict) else payload[:700])
    elif mode == "resume-batch":
        batch_id, reason = sys.argv[2], sys.argv[3]
        version = state_version()
        status, payload = call(
            "POST",
            "/batches/%s/resume-requests" % batch_id,
            {"reason": reason, "requested_by": "cli"},
            idem="hb-resume-%s-%s" % (batch_id[:8], version),
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:700] if isinstance(payload, dict) else payload[:700])
    elif mode == "cancel-batch":
        batch_id, reason = sys.argv[2], sys.argv[3]
        version = state_version()
        status, payload = call(
            "POST",
            "/batches/%s/cancel-requests" % batch_id,
            {"reason": reason, "requested_by": "cli"},
            idem="hb-batchcancel-%s-%s" % (batch_id[:8], version),
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:700] if isinstance(payload, dict) else payload[:700])
    elif mode == "start-daily-api":
        version = state_version()
        status, payload = call(
            "POST",
            "/batches",
            {"kind": "daily", "mode": "execute", "requestedBy": "cli"},
            idem="hb-batch-%d" % version,
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:900] if isinstance(payload, dict) else payload[:900])
    elif mode == "start-daily-games":
        # start-daily-games <GameId> [<GameId> ...] —— 单/多游戏诊断批次（不动 enabled 配置）
        game_ids = [item for item in sys.argv[2:] if item]
        if not game_ids:
            raise SystemExit("start-daily-games needs at least one game id")
        version = state_version()
        status, payload = call(
            "POST",
            "/batches",
            {
                "kind": "daily",
                "mode": "execute",
                "gameIds": game_ids,
                "requestedBy": "cli",
            },
            idem="hb-batch-%s-%d" % ("-".join(game_ids), version),
            expect=version,
        )
        print("status", status)
        print(json.dumps(payload, ensure_ascii=False)[:900] if isinstance(payload, dict) else payload[:900])
    elif mode == "patch-enabled":
        # patch-enabled <GameId> <true|false>
        game_id, raw = sys.argv[2], sys.argv[3]
        flag = raw.strip().lower() == "true"
        version = state_version()
        data = json.dumps({"enabled": {game_id: flag}}).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + TOKEN,
            "X-YeYu-Gamer-Actor": "cli",
            "Content-Type": "application/json",
            "If-Match": '"%d"' % version,
            "Idempotency-Key": "hb-enabled-%s-%s-%s" % (game_id, flag, version),
            "X-Expected-State-Version": str(version),
        }
        request = urllib.request.Request(BASE + "/config", data=data, method="PATCH", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                print("status", response.status)
                print(response.read().decode("utf-8")[:500])
        except urllib.error.HTTPError as error:
            print("status", error.code)
            print(error.read().decode("utf-8", "replace")[:500])
    else:
        raise SystemExit("unknown mode")
