#!/usr/bin/env python3
"""Offline contract validation for the FGO formal-MWU runner.

The validation probes the fixed binding and rejection boundary only.  It never
starts MWU, an emulator, ADB, or the game; the real task is accepted only through
the installed YeYu Gamer daily-batch entry.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4


# The already-running emulator can independently recycle its ADB daemon, so an
# ADB PID is not a causal launch signal.  The candidate's actual launcher and
# emulator process are stable process-boundary checks.
WATCHED_PROCESSES = {"mwu.exe", "bbchannel.exe", "dnplayer.exe"}
EXPECTED_TERMINALS = {
    "attach-home": ("review_required", "home_scene_verifier_unavailable"),
    "three-10ap-quests": ("blocked", "selected_todo_tool_binding_unavailable"),
}
DEFINITIONS = {
    "attach-home": "todo.v1.fgo.daily.attach-home",
    "three-10ap-quests": "todo.v1.fgo.daily.three-10ap-quests",
}


def fail(message: str) -> None:
    raise AssertionError(message)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        fail(f"{path.name} must be a JSON object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(candidate: Path) -> tuple[Path, dict]:
    if not candidate.is_absolute() or not candidate.is_dir():
        fail("candidate root must be an existing absolute directory")
    manifest = load_json(candidate / "install-manifest.json")
    if manifest.get("schemaVersion") != 2 or manifest.get("supportedGameIds") != ["FGO"]:
        fail("candidate manifest scope is invalid")
    if manifest.get("executionReady") is not False or manifest.get("promotion", {}).get("status") != "candidate":
        fail("the FGO runner must remain an unpromoted candidate")
    security = manifest.get("security", {})
    if any(security.get(key) is not False for key in ("allowsArbitraryCommand", "allowsArbitraryPath", "allowsArbitraryInput")):
        fail("candidate security switches must all be false")
    declared = set()
    for item in manifest.get("files", []):
        relative = item.get("path")
        if not isinstance(relative, str) or "/" in relative or "\\" in relative or relative in declared:
            fail("candidate file declaration is unsafe")
        path = candidate / relative
        if not path.is_file() or path.stat().st_size != item.get("sizeBytes") or sha256(path) != item.get("sha256"):
            fail(f"candidate file mismatch: {relative}")
        declared.add(relative)
    if declared != {"runner.exe", "tool-binding.json"}:
        fail("candidate files differ from the fixed package")
    actual = {path.name for path in candidate.iterdir() if path.is_file()}
    if actual != declared | {"install-manifest.json"}:
        fail("candidate includes an undeclared file")
    binding = load_json(candidate / "tool-binding.json")
    if binding.get("gameId") != "FGO" or binding.get("bindingId") != "fgo-maafgo-formal-v1":
        fail("tool binding scope is invalid")
    if set(binding.get("operations", {})) != set(EXPECTED_TERMINALS):
        fail("tool binding operations differ from the fixed selected-Todo set")
    safety = binding.get("safety", {})
    if any(safety.get(key) is not False for key in ("allowAppleUse", "allowSaintQuartz", "allowSummon", "allowMailboxAssetActions")):
        fail("FGO asset-affecting safety switches must remain false")
    if safety.get("formalGuiRequired") is not True:
        fail("the fixed formal GUI requirement is missing")
    return candidate / "runner.exe", manifest


def process_snapshot() -> dict[str, set[int]]:
    result = subprocess.run(
        ["tasklist.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    snapshot = {name: set() for name in WATCHED_PROCESSES}
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2:
            continue
        name = row[0].lower()
        if name in snapshot:
            try:
                snapshot[name].add(int(row[1]))
            except ValueError:
                pass
    return snapshot


def assert_no_new_process(before: dict[str, set[int]], after: dict[str, set[int]]) -> None:
    started = {name: sorted(after[name] - before[name]) for name in WATCHED_PROCESSES if after[name] - before[name]}
    if started:
        fail(f"runner started a forbidden tool/emulator/ADB process: {started}")


def request_document(run_id: str, attempt_id: str, operations: list[str]) -> tuple[dict, list[str]]:
    issued = datetime.now(timezone.utc)
    todo_ids = [f"todo-instance-{uuid4()}" for _ in operations]
    todos = [
        {
            "todoInstanceId": todo_id,
            "todoDefinitionId": DEFINITIONS[operation],
            "definitionVersion": 1,
            "operation": operation,
            "risk": "routine_action",
            "adapterCapabilityRef": "game.daily.run@1.0",
            "priorAttempts": 0,
            "executionDisposition": "executable",
        }
        for todo_id, operation in zip(todo_ids, operations)
    ]
    return {
        "schemaVersion": 1,
        "protocolVersion": "1.1",
        "requestType": "execute",
        "runId": run_id,
        "runAttemptId": attempt_id,
        "fencingToken": "fgo-offline-validation-token-000001",
        "gameId": "FGO",
        "cadence": "daily",
        "managerStateVersion": 1,
        "catalogVersion": "todo-catalog-v1",
        "policyDigest": "sha256:" + ("0" * 64),
        "issuedAt": issued.isoformat().replace("+00:00", "Z"),
        "expiresAt": (issued + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "timeoutSeconds": 60,
        "preserveClientOnStop": True,
        "executableTodoInstanceIds": todo_ids,
        "todos": todos,
    }, todo_ids


def invoke(runner: Path, request: dict, staging: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["YEYU_GAMER_ADAPTER_STAGING_DIR"] = str(staging)
    return subprocess.run(
        [
            str(runner), "--protocol-version", "1.1", "--run-id", request["runId"],
            "--run-attempt-id", request["runAttemptId"], "--game-id", "FGO",
        ],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
        timeout=15,
        check=False,
    )


def validate_probe(runner: Path) -> dict:
    result = subprocess.run(
        [str(runner), "--probe-binding"], capture_output=True, text=True,
        encoding="utf-8", errors="strict", timeout=15, check=False,
    )
    if result.returncode != 0 or result.stderr:
        fail(f"binding probe failed: {result.stderr.strip()}")
    probe = json.loads(result.stdout)
    expected = {"ok": True, "gameId": "FGO", "executionMode": "maafgo-formal-gui", "processStarted": False}
    if any(probe.get(key) != value for key, value in expected.items()):
        fail("binding probe returned an unsafe or unexpected state")
    return probe


def validate_blocked_replay(runner: Path, root: Path) -> dict:
    run_id, attempt_id = str(uuid4()), str(uuid4())
    request, todo_ids = request_document(run_id, attempt_id, list(EXPECTED_TERMINALS))
    staging = root / attempt_id
    staging.mkdir()
    before = process_snapshot()
    result = invoke(runner, request, staging)
    after = process_snapshot()
    assert_no_new_process(before, after)
    if result.returncode != 0 or result.stderr:
        fail(f"blocked replay transport failed: {result.stderr.strip()}")
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if [event.get("sequence") for event in events] != list(range(len(events))):
        fail("event sequence is not contiguous")
    expected_types = [
        "hello", "todo_attempt_started", "artifact_staged", "todo_terminal",
        "todo_attempt_started", "artifact_staged", "todo_terminal", "run_terminal",
    ]
    if [event.get("eventType") for event in events] != expected_types:
        fail("event transcript differs from the fixed two-Todo contract")
    terminals = [event for event in events if event["eventType"] == "todo_terminal"]
    for index, (operation, (status, reason_code)) in enumerate(EXPECTED_TERMINALS.items()):
        terminal = terminals[index]
        if terminal.get("todoInstanceId") != todo_ids[index] or terminal.get("status") != status:
            fail(f"{operation} terminal status is invalid")
        if terminal.get("reasonCode") != reason_code or terminal.get("retryable") is not False:
            fail(f"{operation} terminal reason is invalid")
    final = events[-1]
    if final.get("status") != "blocked" or final.get("transportOutcome") != "clean" or final.get("exitCode") != 0:
        fail("run terminal does not distinguish a clean transport from blocked work")
    if final.get("completedTodoInstanceIds") != [] or final.get("unresolvedTodoInstanceIds") != todo_ids:
        fail("runner falsely completed or lost an unresolved Todo")
    artifacts = [event for event in events if event["eventType"] == "artifact_staged"]
    files = list(staging.iterdir())
    if len(files) != 2 or any(path.suffix.lower() != ".txt" for path in files):
        fail("runner staged an unexpected artifact")
    for event in artifacts:
        path = staging / event["fileName"]
        if not path.is_file() or sha256(path) != event["sha256"] or path.stat().st_size != event["sizeBytes"]:
            fail("diagnostic artifact evidence is invalid")
        if event.get("kind") != "adapter-binding-diagnostic" or event.get("mimeType") != "text/plain":
            fail("artifact type is outside the candidate allowlist")
    return {"eventCount": len(events), "terminalStatuses": [event["status"] for event in terminals], "completedTodoCount": 0}


def validate_rejects_unbound_todo(runner: Path, root: Path) -> dict:
    run_id, attempt_id = str(uuid4()), str(uuid4())
    request, _ = request_document(run_id, attempt_id, ["attach-home"])
    request["todos"][0]["operation"] = "claim-mailbox"
    request["todos"][0]["todoDefinitionId"] = "todo.v1.fgo.daily.claim-mailbox"
    staging = root / attempt_id
    staging.mkdir()
    before = process_snapshot()
    result = invoke(runner, request, staging)
    after = process_snapshot()
    assert_no_new_process(before, after)
    if result.returncode != 64 or result.stderr.strip() != "operation_not_bound" or result.stdout.strip():
        fail("unbound asset-affecting Todo was not rejected before execution")
    if any(staging.iterdir()):
        fail("rejected Todo produced an artifact")
    return {"exitCode": result.returncode, "reasonCode": result.stderr.strip()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    candidate = args.candidate_root.resolve(strict=True)
    runner, manifest = validate_package(candidate)
    temp_parent = Path(tempfile.mkdtemp(prefix="yeyu-fgo-offline-"))
    try:
        probe_before = process_snapshot()
        probe = validate_probe(runner)
        probe_after = process_snapshot()
        assert_no_new_process(probe_before, probe_after)
        rejection = validate_rejects_unbound_todo(runner, temp_parent)
        evidence = {
            "schemaVersion": 1,
            "suite": "fgo-formal-mwu-offline-boundary",
            "status": "passed",
            "packageVersion": manifest["packageVersion"],
            "executionReady": False,
            "processStarted": False,
            "gameStarted": False,
            "probe": probe,
            "unboundTodoRejection": rejection,
        }
        serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
