"""Offline AdapterEventStream replay for immutable upstream-log artifacts."""
from __future__ import annotations

import hashlib
import argparse
import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeyu_gamer_manager.services.adapter_protocol import (
    AdapterEventStream,
    AdapterProtocolError,
    parse_execute_request,
)

ROOT = Path(__file__).resolve().parents[2]


def verify_runner_snapshot_contract(runner: str = "all") -> None:
    # Both runners must snapshot the live upstream log before later writes can
    # change it, and neither may re-archive a log it already staged.
    runners = ("adapter-host/openkuro-runner/Program.cs", "adapter-host/nte-runner/Program.cs")
    for relative in runners[:1] if runner == "OpenKuro" else runners:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "File.Copy(upstreamLog, snapshotPath, true);" in source
        assert "!Path.GetFileName(value).Contains(\"-snapshot-\")" in source
        assert "if (!toolFinished && state != \"human_required\")" in source
        assert "toolFinished = true;" in source
    # One OpenKuro run archives the same upstream log for several Todos, so its
    # bookkeeping is a set; the NTE runner keeps the single-shot flag its own
    # lifecycle needs.
    openkuro = (ROOT / "adapter-host/openkuro-runner/Program.cs").read_text(encoding="utf-8")
    assert "stagedUpstreamLogs" in openkuro
    if runner == "all":
        nte = (ROOT / "adapter-host/nte-runner/Program.cs").read_text(encoding="utf-8")
        assert "bool upstreamLogStaged = false;" in nte
    # A run where no configured Todo ever started must still archive the raw
    # upstream log as a run diagnostic.  Without it a failed multi-account run
    # keeps no upstream evidence at all: every stage stayed unattributed, so no
    # Todo terminal ever staged the outcome log or the log snapshot.
    assert '"-upstream-*.jsonl"' in openkuro
    archive_start = openkuro.index("// A run which never started one of the configured Todos")
    archive_end = openkuro.index("// A normal process exit does not erase an official task failure.")
    archive_block = openkuro[archive_start:archive_end]
    assert '"run_artifact_staged"' in archive_block
    assert '"tool-log-upstream"' in archive_block
    assert archive_start < openkuro.index('"run_terminal", sequence++')


def event(request: dict[str, object], event_type: str, sequence: int, **fields: object) -> bytes:
    return json.dumps({
        "schemaVersion": 1, "protocolVersion": "1.1", "eventType": event_type,
        "sequence": sequence, "runId": request["runId"], "runAttemptId": request["runAttemptId"],
        "fencingToken": request["fencingToken"], "gameId": request["gameId"],
        "at": datetime.now(timezone.utc).isoformat(), **fields,
    }, separators=(",", ":")).encode()


def request() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    todos = []
    for operation in ("first", "second"):
        todos.append({
            "todoInstanceId": "todo-instance-" + str(uuid.uuid4()),
            "todoDefinitionId": "fixture." + operation, "definitionVersion": 1,
            "operation": operation, "risk": "routine_action",
            "adapterCapabilityRef": "game.daily.run@1.0", "priorAttempts": 0,
            "executionDisposition": "executable",
        })
    return {
        "schemaVersion": 1, "protocolVersion": "1.1", "requestType": "execute",
        "runId": str(uuid.uuid4()), "runAttemptId": str(uuid.uuid4()),
        "fencingToken": "a" * 32, "cancelAuthority": "z" * 32, "gameId": "NTE",
        "cadence": "daily", "managerStateVersion": 1, "catalogVersion": "fixture-1",
        "policyDigest": "sha256:" + "b" * 64,
        "issuedAt": (now - timedelta(minutes=1)).isoformat(),
        "expiresAt": (now + timedelta(minutes=1)).isoformat(), "timeoutSeconds": 60,
        "preserveClientOnStop": True,
        "executableTodoInstanceIds": [todo["todoInstanceId"] for todo in todos], "todos": todos,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", choices=("all", "OpenKuro"), default="all")
    verify_runner_snapshot_contract(parser.parse_args().runner)
    document = request()
    stream = AdapterEventStream(parse_execute_request(json.dumps(document).encode()))
    todos = document["todos"]
    assert isinstance(todos, list)
    stream.consume_line(event(document, "hello", 0, packageId="legacy-night-rain-gamer", packageVersion="0.1.0", packageDigest="sha256:" + "c" * 64, runnerPid=1, acceptedTodoInstanceIds=document["executableTodoInstanceIds"]))
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        attempts = [str(uuid.uuid4()), str(uuid.uuid4())]
        sequence = 1
        for todo, attempt_id in zip(todos, attempts):
            assert isinstance(todo, dict)
            stream.consume_line(event(document, "todo_attempt_started", sequence, todoInstanceId=todo["todoInstanceId"], todoAttemptId=attempt_id, attemptNo=1, operation=todo["operation"])); sequence += 1
        live_log = root / "nte-upstream-live.jsonl"
        live_log.write_text("official first stage\n", encoding="utf-8")
        # The runner defers ordinary terminals until RunTool returns, so this
        # tail must exist before the single immutable snapshot is staged.
        live_log.write_text("official first stage\nofficial final log tail\n", encoding="utf-8")
        snapshot = root / "nte-upstream-snapshot.jsonl"
        snapshot.write_bytes(live_log.read_bytes())
        snapshot_payload = snapshot.read_bytes()
        snapshot_hash = hashlib.sha256(snapshot_payload).hexdigest()
        live_log.write_text(live_log.read_text(encoding="utf-8") + "later live write\n", encoding="utf-8")
        assert snapshot.read_bytes() == snapshot_payload
        assert "official final log tail" in snapshot.read_text(encoding="utf-8")
        snapshot_id = str(uuid.uuid4())
        stream.consume_line(event(document, "artifact_staged", sequence, todoInstanceId=todos[0]["todoInstanceId"], todoAttemptId=attempts[0], artifactId=snapshot_id, kind="tool-log-upstream", fileName=snapshot.name, mimeType="text/plain", sizeBytes=len(snapshot_payload), sha256=snapshot_hash, capturedAt=datetime.now(timezone.utc).isoformat())); sequence += 1
        stream.consume_line(event(document, "todo_terminal", sequence, todoInstanceId=todos[0]["todoInstanceId"], todoAttemptId=attempts[0], status="failed", reasonCode="upstream_task_failed", reason="fake upstream terminal", retryable=True, evidenceArtifactIds=[snapshot_id])); sequence += 1
        outcome = root / "second-outcome.txt"
        outcome.write_text("official second stage completed\n", encoding="utf-8")
        outcome_payload = outcome.read_bytes(); outcome_id = str(uuid.uuid4())
        stream.consume_line(event(document, "artifact_staged", sequence, todoInstanceId=todos[1]["todoInstanceId"], todoAttemptId=attempts[1], artifactId=outcome_id, kind="tool-log-outcome", fileName=outcome.name, mimeType="text/plain", sizeBytes=len(outcome_payload), sha256=hashlib.sha256(outcome_payload).hexdigest(), capturedAt=datetime.now(timezone.utc).isoformat())); sequence += 1
        stream.consume_line(event(document, "todo_terminal", sequence, todoInstanceId=todos[1]["todoInstanceId"], todoAttemptId=attempts[1], status="completed", reasonCode="upstream_task_succeeded", reason="fake upstream terminal", retryable=False, evidenceArtifactIds=[outcome_id])); sequence += 1
        stream.consume_line(event(document, "run_terminal", sequence, status="partial", transportOutcome="clean", attemptedTodoInstanceIds=document["executableTodoInstanceIds"], completedTodoInstanceIds=[todos[1]["todoInstanceId"]], unresolvedTodoInstanceIds=[todos[0]["todoInstanceId"]], terminalEventDigest="sha256:" + "d" * 64, exitCode=0))
        result = stream.finish(process_exit_code=0)
    assert result.protocol_valid and result.status == "partial"
    assert len(result.completed_todo_instance_ids) == 1 and len(result.unresolved_todo_instance_ids) == 1
    # A run which never started a configured Todo (a multi-account task failing
    # before official account identity) still archives its raw upstream log as a
    # run-scoped diagnostic.  The Manager must accept that event, otherwise the
    # failed round keeps no upstream evidence beyond its own records.
    detached = request()
    detached_todos = detached["todos"]
    assert isinstance(detached_todos, list)
    detached_stream = AdapterEventStream(parse_execute_request(json.dumps(detached).encode()))
    detached_stream.consume_line(event(detached, "hello", 0, packageId="legacy-night-rain-gamer", packageVersion="0.1.0", packageDigest="sha256:" + "c" * 64, runnerPid=1, acceptedTodoInstanceIds=detached["executableTodoInstanceIds"]))
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        archived = root / "ww-upstream-snapshot.jsonl"
        archived.write_text("official stage emitted before account identity\n", encoding="utf-8")
        payload = archived.read_bytes()
        detached_stream.consume_line(event(
            detached, "run_artifact_staged", 1,
            todoInstanceId=detached_todos[0]["todoInstanceId"], artifactId=str(uuid.uuid4()),
            kind="tool-log-upstream", fileName=archived.name, mimeType="text/plain",
            sizeBytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            capturedAt=datetime.now(timezone.utc).isoformat()))
        detached_stream.consume_line(event(
            detached, "run_terminal", 2, status="failed", transportOutcome="clean",
            attemptedTodoInstanceIds=[], completedTodoInstanceIds=[],
            unresolvedTodoInstanceIds=list(detached["executableTodoInstanceIds"]),
            terminalEventDigest="sha256:" + "e" * 64, exitCode=0))
        detached_result = detached_stream.finish(process_exit_code=0)
    assert detached_result.protocol_valid and detached_result.status == "failed"
    # A run diagnostic is never Todo evidence.  Citing the archived log in a
    # Todo terminal must be rejected so it can never become a completion claim.
    diagnostic_id = str(uuid.uuid4())
    evidence_stream = AdapterEventStream(parse_execute_request(json.dumps(document).encode()))
    evidence_stream.consume_line(event(document, "hello", 0, packageId="legacy-night-rain-gamer", packageVersion="0.1.0", packageDigest="sha256:" + "c" * 64, runnerPid=1, acceptedTodoInstanceIds=document["executableTodoInstanceIds"]))
    evidence_attempt = str(uuid.uuid4())
    evidence_stream.consume_line(event(document, "todo_attempt_started", 1, todoInstanceId=todos[0]["todoInstanceId"], todoAttemptId=evidence_attempt, attemptNo=1, operation=todos[0]["operation"]))
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        diagnostic = root / "ww-upstream-snapshot.jsonl"
        diagnostic.write_text("official stage emitted before account identity\n", encoding="utf-8")
        payload = diagnostic.read_bytes()
        evidence_stream.consume_line(event(document, "run_artifact_staged", 2, todoInstanceId=todos[0]["todoInstanceId"], artifactId=diagnostic_id, kind="tool-log-upstream", fileName=diagnostic.name, mimeType="text/plain", sizeBytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), capturedAt=datetime.now(timezone.utc).isoformat()))
    try:
        evidence_stream.consume_line(event(document, "todo_terminal", 3, todoInstanceId=todos[0]["todoInstanceId"], todoAttemptId=evidence_attempt, status="completed", reasonCode="upstream_task_succeeded", reason="fake upstream terminal", retryable=False, evidenceArtifactIds=[diagnostic_id]))
    except AdapterProtocolError as error:
        assert error.code == "evidence_scope_mismatch"
    else:
        raise AssertionError("a run diagnostic was accepted as Todo evidence")
    print(json.dumps({"status": "passed", "cases": 5, "gameStarted": False, "toolStarted": False}))


if __name__ == "__main__":
    main()
