from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import struct
import subprocess
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeyu_gamer_manager.services.adapter_protocol import (
    AdapterEventStream,
    parse_execute_request,
)


def validate_currency_wars_bounded_recovery_source() -> dict[str, object]:
    runner_root = Path(__file__).parents[1] / "starrail-runner"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (runner_root / "March7thTool.cs", runner_root / "Program.cs")
    )
    required = (
        'private const string CurrencyWarsEntryFailure = "无法切换到 货币战争-主界面";',
        'freshLogs.IndexOf(CurrencyWarsEntryFailure',
        'if (fixedTaskFailure) exitCode = 74;',
        'private const string GameSwitchTimeoutFailure = "尝试启动游戏时发生错误：切换到游戏超时";',
        'if (gameSwitchFailure) exitCode = 76;',
        'TryActivateVerifiedGameWindow();',
        'process.WaitForExit(5000);',
        'GetForegroundWindow() != window',
        'keybd_event(VirtualKeyF, 0, 0, UIntPtr.Zero);',
        'ToolRunResult currencyWars = RunFixedTask("currencywars", command.TimeoutSeconds',
        'ToolRunResult currencyWarsRetry = RunFixedTask("currencywars", command.TimeoutSeconds',
        'CombineRecovered(first, currencyWars, tableEntry, currencyWarsRetry',
        'tool.TryDetectHumanGate(out humanGateReason)',
        '"human_required", "starrail_login_required"',
        'HasCurrentGameDayLatestConfirmation()',
        'gameDay.AddHours(4)',
    )
    missing = [marker for marker in required if marker not in source]
    assert not missing, missing
    assert 'RunFixedTask("currencywars", 1200' not in source
    assert 'try { process.WaitForExit(); } catch { }' not in source
    assert 'starrail-daily-training-live-' in source
    assert 'starrail-daily-rewards-live-' in source
    assert 'DailyRewardsFramePath' in source
    assert 'game-ui-daily-training-panel' in source
    return {"boundedFailureMarker": True, "verifiedForegroundF": True, "singleRetry": True}


DEFINITIONS = {
    "attach-home": "todo.v1.starrail.daily.attach-home",
    "spend-trailblaze-power": "todo.v1.starrail.daily.spend-trailblaze-power",
    "daily-training-objectives": "todo.v1.starrail.daily.daily-training-objectives",
    "claim-daily-training-rewards": "todo.v1.starrail.daily.claim-daily-training-rewards",
    "verify-daily-task-list": "todo.v1.starrail.daily.verify-daily-task-list",
}

RISKS = {
    "attach-home": "routine_action",
    "spend-trailblaze-power": "routine_action",
    "daily-training-objectives": "routine_action",
    "claim-daily-training-rewards": "routine_action",
    "verify-daily-task-list": "observe_only",
}

TIMEOUTS = {
    "attach-home": 1800,
    "spend-trailblaze-power": 1200,
    "daily-training-objectives": 3600,
    "claim-daily-training-rewards": 3600,
    "verify-daily-task-list": 60,
}


def request_for(operations: list[str]) -> tuple[dict[str, object], bytes]:
    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    todos: list[dict[str, object]] = []
    ids: list[str] = []
    for operation in operations:
        todo_id = "todo-instance-" + str(uuid.uuid4())
        ids.append(todo_id)
        todos.append(
            {
                "todoInstanceId": todo_id,
                "todoDefinitionId": DEFINITIONS[operation],
                "definitionVersion": 1,
                "operation": operation,
                "risk": RISKS[operation],
                "adapterCapabilityRef": "game.daily.run@1.0",
                "priorAttempts": 0,
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
        "cancelAuthority": "c" * 64,
        "gameId": "StarRail",
        "cadence": "daily",
        "managerStateVersion": 1,
        "catalogVersion": "test.1",
        "policyDigest": "sha256:" + "b" * 64,
        "issuedAt": (now - timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "expiresAt": (
            now
            + timedelta(
                seconds=sum(TIMEOUTS[operation] for operation in operations) + 60
            )
        ).isoformat().replace("+00:00", "Z"),
        "timeoutSeconds": sum(TIMEOUTS[operation] for operation in operations),
        "preserveClientOnStop": True,
        "executableTodoInstanceIds": ids,
        "todos": todos,
    }
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return document, raw


def runner_request(raw: bytes) -> bytes:
    """Mirror the Adapter Host boundary without weakening Manager validation.

    Manager-to-Host requests carry the separate durable-cancel authority.  The
    Host validates and retains that secret, then removes it before forwarding
    the execute document to a package runner.  Direct runner tests must model
    both sides of that boundary instead of validating an obsolete request.
    """
    parse_execute_request(raw)
    document = json.loads(raw)
    authority = document.pop("cancelAuthority", None)
    assert isinstance(authority, str) and authority != document["fencingToken"]
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def command(runner: Path, document: dict[str, object]) -> list[str]:
    return [
        str(runner),
        "--protocol-version",
        "1.1",
        "--run-id",
        str(document["runId"]),
        "--run-attempt-id",
        str(document["runAttemptId"]),
        "--game-id",
        "StarRail",
    ]


def staging(parent: Path, document: dict[str, object]) -> Path:
    result = parent / str(document["runAttemptId"])
    result.mkdir(parents=True)
    return result


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def non_uniform_png() -> bytes:
    width = 640
    height = 360
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend((x * 255 // (width - 1), y * 255 // (height - 1), (x + y) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def write_visual_evidence(
    root: Path,
    document: dict[str, object],
    *,
    captured_at: datetime | None = None,
    thresholds: tuple[int, ...] = (100, 200, 300, 400, 500),
) -> tuple[Path, Path]:
    capture_id = str(uuid.uuid4())
    prefix = f"starrail-daily-task-list-{capture_id}"
    image_path = root / f"{prefix}.png"
    sidecar_path = root / f"{prefix}.ocr.json"
    image = non_uniform_png()
    image_path.write_bytes(image)
    observed = captured_at or datetime.now(timezone.utc)
    sidecar = {
        "schemaVersion": 1,
        "evidenceType": "starrail.daily-task-list.visual@1.0",
        "producer": "yeyu-gamer-starrail-observer",
        "detectorVersion": "starrail-daily-task-list-v1",
        "captureId": capture_id,
        "runId": document["runId"],
        "runAttemptId": document["runAttemptId"],
        "capturedAt": observed.isoformat().replace("+00:00", "Z"),
        "scene": "daily-training-task-list",
        "imageFileName": image_path.name,
        "imageSha256": hashlib.sha256(image).hexdigest(),
        "activity": {"current": 500, "target": 500, "ocrText": "500/500"},
        "rewardTiers": [
            {
                "threshold": threshold,
                "claimed": True,
                "visualState": "claimed",
                "ocrText": f"{threshold} claimed",
            }
            for threshold in thresholds
        ],
    }
    sidecar_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return image_path, sidecar_path


def invoke_runner(
    runner: Path,
    document: dict[str, object],
    raw: bytes,
    evidence_root: Path,
) -> tuple[object, list[bytes], str, int]:
    env = os.environ.copy()
    env["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(evidence_root)
    completed = subprocess.run(
        command(runner, document),
        input=runner_request(raw) + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    lines = [line for line in completed.stdout.splitlines() if line]
    result = validate_stream(raw, lines, completed.returncode)
    return result, lines, completed.stderr.decode(errors="replace"), completed.returncode


def validate_stream(raw_request: bytes, lines: list[bytes], exit_code: int):
    plan = parse_execute_request(raw_request)
    stream = AdapterEventStream(plan)
    for line in lines:
        stream.consume_line(line)
    result = stream.finish(process_exit_code=exit_code)
    assert result.protocol_valid, result
    return result


def run_success(runner: Path, fake_root: Path, staging_parent: Path) -> dict[str, object]:
    document, raw = request_for(list(DEFINITIONS))
    evidence_root = staging(staging_parent, document)
    write_visual_evidence(evidence_root, document)
    result, lines, stderr, _ = invoke_runner(runner, document, raw, evidence_root)
    assert result.status == "completed", (
        result,
        [line.decode("utf-8", errors="replace") for line in lines],
    )
    assert tuple(document["executableTodoInstanceIds"]) == result.completed_todo_instance_ids
    invocations = (fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    assert invocations == ["game -e", "power -e", "daily -e"], invocations
    event_types = [json.loads(line)["eventType"] for line in lines]
    assert event_types.count("todo_attempt_started") == 5
    assert event_types.count("todo_terminal") == 5
    home_terminal = next(json.loads(line) for line in lines if json.loads(line)["eventType"] == "todo_terminal")
    assert home_terminal["reasonCode"] == "home_frame_confirmed", home_terminal
    assert event_types.count("artifact_staged") >= 10
    return {
        "status": result.status,
        "completedTodoCount": len(result.completed_todo_instance_ids),
        "toolInvocations": invocations,
        "eventTypes": event_types,
        "stderr": stderr,
    }


def run_cancel(runner: Path, fake_root: Path, staging_parent: Path) -> dict[str, object]:
    (fake_root / "mode.txt").write_text("hang", encoding="utf-8")
    document, raw = request_for(["spend-trailblaze-power"])
    env = os.environ.copy()
    env["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(staging(staging_parent, document))
    process = subprocess.Popen(
        command(runner, document),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(runner_request(raw) + b"\n")
    process.stdin.flush()
    lines: list[bytes] = []
    for _ in range(2):
        line = process.stdout.readline().rstrip(b"\r\n")
        assert line, "runner ended before Todo start"
        lines.append(line)
    cancel = {
        "schemaVersion": 1,
        "protocolVersion": "1.1",
        "controlType": "cancel",
        "runId": document["runId"],
        "runAttemptId": document["runAttemptId"],
        "fencingToken": document["fencingToken"],
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reasonCode": "test_cancel",
    }
    process.stdin.write(json.dumps(cancel, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()
    process.stdin.close()
    remainder = process.stdout.read().splitlines()
    lines.extend(line for line in remainder if line)
    exit_code = process.wait(timeout=20)
    stderr = process.stderr.read().decode(errors="replace") if process.stderr is not None else ""
    result = validate_stream(raw, lines, exit_code)
    assert result.status == "cancelled", result
    time.sleep(0.5)
    (fake_root / "mode.txt").write_text("success", encoding="utf-8")
    return {"status": result.status, "transportOutcome": result.transport_outcome, "exitCode": exit_code, "stderr": stderr}


def run_human_required(
    runner: Path, fake_root: Path, staging_parent: Path
) -> dict[str, object]:
    (fake_root / "mode.txt").write_text("human", encoding="utf-8")
    document, raw = request_for(["spend-trailblaze-power"])
    env = os.environ.copy()
    env["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(staging(staging_parent, document))
    completed = subprocess.run(
        command(runner, document),
        input=runner_request(raw) + b"\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    lines = [line for line in completed.stdout.splitlines() if line]
    result = validate_stream(raw, lines, completed.returncode)
    assert result.status == "human_required", result
    terminal = next(
        json.loads(line) for line in lines if json.loads(line)["eventType"] == "todo_terminal"
    )
    assert terminal["status"] == "human_required", terminal
    assert terminal["retryable"] is False, terminal
    (fake_root / "mode.txt").write_text("success", encoding="utf-8")
    return {
        "status": result.status,
        "todoStatus": terminal["status"],
        "retryable": terminal["retryable"],
        "clientPreserved": True,
        "exitCode": completed.returncode,
        "stderr": completed.stderr.decode(errors="replace"),
    }


def _invocations(fake_root: Path) -> list[str]:
    path = fake_root / "invocations.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _todo_terminal(lines: list[bytes]) -> dict[str, object]:
    terminals = [
        json.loads(line)
        for line in lines
        if json.loads(line)["eventType"] == "todo_terminal"
    ]
    assert len(terminals) == 1, terminals
    return terminals[0]


def run_home_scene_fixtures(
    runner: Path, fake_root: Path, staging_parent: Path
) -> list[dict[str, object]]:
    reports = []
    for mode, operations, expected_task in (
        ("missing-home", ["attach-home", "spend-trailblaze-power"], "game -e"),
        ("home-lookalike", ["attach-home", "spend-trailblaze-power"], "game -e"),
        ("home-failure", ["spend-trailblaze-power", "daily-training-objectives", "claim-daily-training-rewards"], "power -e"),
        ("home-failure-clean", ["spend-trailblaze-power", "daily-training-objectives", "claim-daily-training-rewards"], "power -e"),
    ):
        before = _invocations(fake_root)
        original_config = (fake_root / "config.yaml").read_bytes()
        (fake_root / "mode.txt").write_text(mode, encoding="utf-8")
        document, raw = request_for(operations)
        result, lines, _, _ = invoke_runner(runner, document, raw, staging(staging_parent, document))
        terminal = _todo_terminal(lines)
        assert result.status == "human_required", (mode, result)
        assert terminal["status"] == "human_required", terminal
        assert terminal["reasonCode"] == "home_scene_unconfirmed", terminal
        assert terminal["retryable"] is False, terminal
        assert not result.completed_todo_instance_ids, result
        artifacts = [json.loads(line) for line in lines if json.loads(line)["eventType"] == "artifact_staged"]
        referenced = [artifact for artifact in artifacts if artifact["artifactId"] in terminal["evidenceArtifactIds"]]
        assert any(artifact["mimeType"] == "image/png" for artifact in referenced), terminal
        assert any(artifact["mimeType"] == "text/plain" for artifact in referenced), terminal
        assert _invocations(fake_root)[len(before):] == [expected_task], "unsafe scene continued to the next operation"
        assert (fake_root / "config.yaml").read_bytes() == original_config
        reports.append({"fixture": mode, "status": result.status, "reasonCode": terminal["reasonCode"], "completedTodoCount": 0, "nextOperationStarted": False})
    (fake_root / "mode.txt").write_text("success", encoding="utf-8")
    return reports


def run_visual_verifier_fixtures(
    runner: Path, fake_root: Path, staging_parent: Path
) -> dict[str, object]:
    before = _invocations(fake_root)

    passed_document, passed_raw = request_for(["verify-daily-task-list"])
    passed_root = staging(staging_parent, passed_document)
    write_visual_evidence(passed_root, passed_document)
    passed, passed_lines, passed_stderr, _ = invoke_runner(
        runner, passed_document, passed_raw, passed_root
    )
    passed_terminal = _todo_terminal(passed_lines)
    assert passed.status == "completed", passed
    assert passed_terminal["status"] == "completed", passed_terminal
    assert passed_terminal["reasonCode"] == "daily_task_list_visual_confirmed"
    assert _invocations(fake_root) == before, "read-only verifier started March7th"

    stale_document, stale_raw = request_for(["verify-daily-task-list"])
    stale_root = staging(staging_parent, stale_document)
    issued = datetime.fromisoformat(str(stale_document["issuedAt"]).replace("Z", "+00:00"))
    write_visual_evidence(
        stale_root, stale_document, captured_at=issued - timedelta(minutes=5)
    )
    stale, stale_lines, stale_stderr, _ = invoke_runner(
        runner, stale_document, stale_raw, stale_root
    )
    stale_terminal = _todo_terminal(stale_lines)
    assert stale.status == "review_required", stale
    assert stale_terminal["status"] == "review_required", stale_terminal
    assert stale_terminal["reasonCode"] == "daily_visual_evidence_stale"
    assert not stale.completed_todo_instance_ids
    assert _invocations(fake_root) == before, "stale evidence invoked March7th"

    incomplete_document, incomplete_raw = request_for(["verify-daily-task-list"])
    incomplete_root = staging(staging_parent, incomplete_document)
    write_visual_evidence(
        incomplete_root,
        incomplete_document,
        thresholds=(100, 200, 300, 400),
    )
    incomplete, incomplete_lines, incomplete_stderr, _ = invoke_runner(
        runner, incomplete_document, incomplete_raw, incomplete_root
    )
    incomplete_terminal = _todo_terminal(incomplete_lines)
    assert incomplete.status == "review_required", incomplete
    assert incomplete_terminal["status"] == "review_required", incomplete_terminal
    assert incomplete_terminal["reasonCode"] == "daily_reward_tiers_incomplete"
    assert not incomplete.completed_todo_instance_ids
    assert _invocations(fake_root) == before, "incomplete evidence invoked March7th"

    unpaired_document, unpaired_raw = request_for(["claim-daily-training-rewards"])
    unpaired_root = staging(staging_parent, unpaired_document)
    unpaired, unpaired_lines, unpaired_stderr, _ = invoke_runner(
        runner, unpaired_document, unpaired_raw, unpaired_root
    )
    unpaired_terminal = _todo_terminal(unpaired_lines)
    assert unpaired.status == "review_required", unpaired
    assert unpaired_terminal["status"] == "review_required", unpaired_terminal
    assert unpaired_terminal["reasonCode"] == "daily_training_pair_required"
    assert not unpaired.completed_todo_instance_ids
    after = _invocations(fake_root)
    assert after == before, "unpaired daily scope invoked March7th"

    return {
        "passed": {
            "status": passed.status,
            "reasonCode": passed_terminal["reasonCode"],
            "march7thInvocationDelta": 0,
            "stderr": passed_stderr,
        },
        "staleEvidence": {
            "status": stale.status,
            "reasonCode": stale_terminal["reasonCode"],
            "march7thInvocationDelta": 0,
            "stderr": stale_stderr,
        },
        "missingFifthTier": {
            "status": incomplete.status,
            "reasonCode": incomplete_terminal["reasonCode"],
            "march7thInvocationDelta": 0,
            "stderr": incomplete_stderr,
        },
        "unpairedDailyScope": {
            "status": unpaired.status,
            "reasonCode": unpaired_terminal["reasonCode"],
            "march7thInvocationDelta": 0,
            "stderr": unpaired_stderr,
        },
    }


def run_negative(runner: Path, fake_root: Path) -> dict[str, object]:
    before = (fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    malformed = subprocess.run(
        [str(runner), "--protocol-version", "1.1", "--run-id", str(uuid.uuid4()), "--run-attempt-id", str(uuid.uuid4()), "--game-id", "StarRail"],
        input=b'{"schemaVersion":1}\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False
    )
    assert malformed.returncode != 0 and not malformed.stdout
    config = fake_root / "config.yaml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original + "\n# formal GUI harmless rewrite\n", encoding="utf-8")
    harmless = subprocess.run([str(runner), "--probe-binding"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
    assert harmless.returncode == 0 and harmless.stdout and not harmless.stderr
    config.write_text(original.replace("use_reserved_trailblaze_power: false", "use_reserved_trailblaze_power: true"), encoding="utf-8")
    unsafe = subprocess.run([str(runner), "--probe-binding"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
    config.write_text(original, encoding="utf-8")
    assert unsafe.returncode != 0 and not unsafe.stdout and b"unsafe_tool_config" in unsafe.stderr
    after = (fake_root / "invocations.log").read_text(encoding="utf-8").splitlines()
    assert after == before
    return {
        "malformedExitCode": malformed.returncode,
        "harmlessConfigRewriteExitCode": harmless.returncode,
        "unsafeConfigExitCode": unsafe.returncode,
        "processInvocationDelta": len(after) - len(before),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--fake-root", type=Path, required=True)
    parser.add_argument("--staging-parent", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.staging_parent.mkdir(parents=True, exist_ok=True)
    fake_game = subprocess.Popen([str(args.fake_root / "StarRail.exe")])
    try:
        time.sleep(0.2)
        report = {
            "schemaVersion": 1,
            "suite": "starrail-runner-replay",
            "realGameProcessStarted": False,
            "managerOwnedFakeGameAvailable": True,
            "currencyWarsBoundedRecovery": validate_currency_wars_bounded_recovery_source(),
            "success": run_success(args.runner, args.fake_root, args.staging_parent),
            "cancel": run_cancel(args.runner, args.fake_root, args.staging_parent),
            "humanRequired": run_human_required(
                args.runner, args.fake_root, args.staging_parent
            ),
            "homeScene": run_home_scene_fixtures(
                args.runner, args.fake_root, args.staging_parent
            ),
            "visualVerifier": run_visual_verifier_fixtures(
                args.runner, args.fake_root, args.staging_parent
            ),
            "negative": run_negative(args.runner, args.fake_root),
            "passed": True,
        }
        assert fake_game.poll() is None, "Adapter stopped the Manager-owned fake client"
        report["fakeClientPreserved"] = True
    finally:
        fake_game.terminate()
        fake_game.wait(timeout=10)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
