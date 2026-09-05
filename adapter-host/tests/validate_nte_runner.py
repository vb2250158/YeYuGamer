from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeyu_gamer_manager.services.adapter_protocol import AdapterEventStream, parse_execute_request


DEFINITIONS = {
    "attach-home": "todo.v1.nte.daily.attach-home",
    "mail": "todo.v1.nte.daily.mail",
    "daily-activity": "todo.v1.nte.daily.daily-activity",
    "spend-city-vitality": "todo.v1.nte.daily.spend-city-vitality",
    "claim-activity-reward": "todo.v1.nte.daily.claim-activity-reward",
    "claim-cycle-reward": "todo.v1.nte.daily.claim-cycle-reward",
}


def make_request(operations: list[str]) -> tuple[dict[str, object], bytes]:
    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    ids: list[str] = []
    todos: list[dict[str, object]] = []
    for operation in operations:
        todo_id = "todo-instance-" + str(uuid.uuid4())
        ids.append(todo_id)
        todos.append(
            {
                "todoInstanceId": todo_id,
                "todoDefinitionId": DEFINITIONS[operation],
                "definitionVersion": 1,
                "operation": operation,
                "risk": "routine_action",
                "adapterCapabilityRef": "game.daily.run@1.0",
                "priorAttempts": 2,
                "executionDisposition": "executable",
            }
        )
    document: dict[str, object] = {
        "schemaVersion": 1,
        "protocolVersion": "1.1",
        "requestType": "execute",
        "runId": run_id,
        "runAttemptId": attempt_id,
        "fencingToken": "a" * 64,
        "gameId": "NTE",
        "cadence": "daily",
        "managerStateVersion": 1,
        "catalogVersion": "nte-test.1",
        "policyDigest": "sha256:" + "b" * 64,
        "issuedAt": (now - timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "timeoutSeconds": 3600,
        "preserveClientOnStop": True,
        "executableTodoInstanceIds": ids,
        "todos": todos,
    }
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return document, raw


def invoke(
    runner: Path,
    fake_root: Path,
    staging_parent: Path,
    operations: list[str],
    mode: str,
    coffee_mode: str = "不执行",
) -> tuple[object, list[dict[str, object]], str]:
    document, raw = make_request(operations)
    staging = staging_parent / str(document["runAttemptId"])
    staging.mkdir(parents=True)
    binding = staging / "installation-binding.json"
    binding.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "gameId": "NTE",
                "gamePath": str(fake_root / "NTEGame.exe"),
                "toolPath": str(fake_root),
                "dailyTaskProfile": {
                    "anomalyTaskType": "异能升级材料",
                    "expRewardTarget": "甲硬币",
                    "materialIndex": 5,
                    "staminaTarget": 200,
                    "autoCycleSubTask": True,
                    "coffeeMode": coffee_mode,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (fake_root / "fake-mode.txt").write_text(mode, encoding="utf-8")
    env = os.environ.copy()
    env["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(staging)
    env["YEYU_GAMER_INSTALLATION_BINDING_PATH"] = str(binding)
    command = [
        str(runner),
        "--protocol-version",
        "1.1",
        "--run-id",
        str(document["runId"]),
        "--run-attempt-id",
        str(document["runAttemptId"]),
        "--game-id",
        "NTE",
    ]
    completed = subprocess.run(
        command,
        input=raw + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
        check=False,
    )
    raw_lines = [line for line in completed.stdout.splitlines() if line]
    plan = parse_execute_request(raw)
    stream = AdapterEventStream(plan)
    events: list[dict[str, object]] = []
    for line in raw_lines:
        events.append(json.loads(line))
        stream.consume_line(line)
    result = stream.finish(process_exit_code=completed.returncode)
    assert result.protocol_valid, result
    return result, events, completed.stderr.decode(errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--fake-root", type=Path, required=True)
    parser.add_argument("--staging-parent", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    runner_source = (Path(__file__).resolve().parents[1] / "nte-runner" / "Program.cs").read_text(encoding="utf-8")
    assert "FileShare.ReadWrite | FileShare.Delete" in runner_source

    all_operations = list(DEFINITIONS)
    success, events, stderr = invoke(
        args.runner, args.fake_root, args.staging_parent, all_operations, "complete"
    )
    assert success.status == "completed", success
    assert success.exit_code == 0
    assert len(success.completed_todo_instance_ids) == len(all_operations)
    assert not stderr
    assert all(event["eventType"] != "fake tool stdout is captured by runner" for event in events)

    selected = ["attach-home", "spend-city-vitality", "claim-activity-reward"]
    partial, partial_events, _ = invoke(
        args.runner, args.fake_root, args.staging_parent, selected, "complete"
    )
    assert partial.status == "completed", partial
    assert partial.exit_code == 0
    started_operations = {
        str(event["operation"])
        for event in partial_events
        if event["eventType"] == "todo_attempt_started"
    }
    assert started_operations == set(selected), started_operations

    missing, _, _ = invoke(
        args.runner, args.fake_root, args.staging_parent, ["attach-home", "mail"], "missing"
    )
    assert missing.status == "review_required", missing
    assert missing.exit_code == 0
    assert len(missing.completed_todo_instance_ids) == 1

    failed, _, _ = invoke(
        args.runner, args.fake_root, args.staging_parent, ["attach-home", "mail"], "failed"
    )
    assert failed.status == "failed", failed
    assert failed.exit_code == 0
    assert not failed.completed_todo_instance_ids

    before_review = (args.fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    coffee_review, _, _ = invoke(
        args.runner,
        args.fake_root,
        args.staging_parent,
        ["attach-home", "mail"],
        "complete",
        coffee_mode="领取/补货",
    )
    assert coffee_review.status == "review_required", coffee_review
    assert not coffee_review.attempted_todo_instance_ids
    after_review = (args.fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    assert after_review == before_review, "coffee review gate must stop before tool launch"

    invocations = (args.fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    assert all(line.endswith("main.py --task 2 --exit") for line in invocations), invocations
    profiles = [json.loads(line) for line in (args.fake_root / "profiles.log").read_text(encoding="utf-8").splitlines()]
    assert profiles and all(
        profile
        == {
            "anomalyTaskType": "异能升级材料",
            "expRewardTarget": "甲硬币",
            "materialIndex": 5,
            "staminaTarget": 200,
            "autoCycleSubTask": True,
            "coffeeMode": "不执行",
        }
        for profile in profiles
    ), profiles
    report = {
        "schemaVersion": 1,
        "suite": "nte-selected-daily-replay",
        "passed": True,
        "gameStarted": False,
        "cases": {
            "allSelected": success.status,
            "selectedSubset": partial.status,
            "missingStage": missing.status,
            "failedStage": failed.status,
            "coffeeModeReviewGate": coffee_review.status,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(report, separators=(",", ":")))


if __name__ == "__main__":
    main()
