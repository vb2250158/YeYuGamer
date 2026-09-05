from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from yeyu_gamer_manager.services.adapter_host import (
    AdapterExecutionRejected,
    ManagerAdapterHost,
)
from yeyu_gamer_manager.services.adapter_protocol import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_TODO,
    MAX_EVENT_BYTES,
    REQUIRED_FORBIDDEN_OPERATION_CLASSES,
    AdapterEventStream,
    AdapterExecutionPlan,
    AdapterProtocolError,
    parse_execute_request,
    serialize_execute_request,
    serialize_durable_cancel_control,
    validate_plan_against_manifest,
    verify_execution_candidate,
    verify_execution_package,
)
from yeyu_gamer_manager.services.legacy_adapter import LegacyAdapter


RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
TODO_ID = "todo-instance-33333333-3333-4333-8333-333333333333"
TODO_ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"
SECOND_TODO_ID = "todo-instance-66666666-6666-4666-8666-666666666666"
SECOND_TODO_ATTEMPT_ID = "77777777-7777-4777-8777-777777777777"
ARTIFACT_ID = "55555555-5555-4555-8555-555555555555"
FENCING_TOKEN = "a" * 32
CANCEL_AUTHORITY = "z" * 32
POLICY_DIGEST = "sha256:" + "b" * 64
PACKAGE_DIGEST = "sha256:" + "c" * 64
TERMINAL_DIGEST = "sha256:" + "d" * 64
AT = "2099-08-28T10:00:01+08:00"


def plan_document(**updates: object) -> dict[str, object]:
    todo = {
        "todoInstanceId": TODO_ID,
        "todoDefinitionId": "starrail.observe-panel",
        "definitionVersion": 1,
        "operation": "observe-panel",
        "risk": "observe_only",
        "adapterCapabilityRef": "game.daily.run@1.0",
        "priorAttempts": 0,
        "executionDisposition": "executable",
    }
    result: dict[str, object] = {
        "schemaVersion": 1,
        "protocolVersion": "1.1",
        "requestType": "execute",
        "runId": RUN_ID,
        "runAttemptId": RUN_ATTEMPT_ID,
        "fencingToken": FENCING_TOKEN,
        "cancelAuthority": CANCEL_AUTHORITY,
        "gameId": "StarRail",
        "cadence": "daily",
        "managerStateVersion": 7,
        "catalogVersion": "catalog-1",
        "policyDigest": POLICY_DIGEST,
        "issuedAt": "2099-08-28T10:00:00+08:00",
        "expiresAt": "2099-08-28T10:05:00+08:00",
        "timeoutSeconds": 60,
        "preserveClientOnStop": True,
        "executableTodoInstanceIds": [TODO_ID],
        "todos": [todo],
    }
    result.update(updates)
    return result


def event_base(event_type: str, sequence: int, **fields: object) -> bytes:
    document: dict[str, object] = {
        "schemaVersion": 1,
        "protocolVersion": "1.1",
        "eventType": event_type,
        "sequence": sequence,
        "runId": RUN_ID,
        "runAttemptId": RUN_ATTEMPT_ID,
        "fencingToken": FENCING_TOKEN,
        "gameId": "StarRail",
        "at": AT,
        **fields,
    }
    return json.dumps(document, separators=(",", ":")).encode()


def two_todo_plan_document() -> dict[str, object]:
    document = plan_document()
    second_todo = dict(document["todos"][0])  # type: ignore[index]
    second_todo["todoInstanceId"] = SECOND_TODO_ID
    second_todo["todoDefinitionId"] = "starrail.observe-second-panel"
    document["todos"].append(second_todo)  # type: ignore[union-attr]
    document["executableTodoInstanceIds"].append(SECOND_TODO_ID)  # type: ignore[union-attr]
    return document


def package_manifest(runner: Path) -> dict[str, object]:
    runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
    runner_size = runner.stat().st_size
    payload_line = f"runner.exe\0{runner_size}\0{runner_hash}\n".encode("utf-8")
    payload_digest = "sha256:" + hashlib.sha256(payload_line).hexdigest()
    adapters_root = next(
        (parent for parent in runner.parents if parent.name == "adapters"),
        runner.parent.parent,
    )
    host = adapters_root / "manager-adapter-host" / "host.exe"
    host_hash = (
        hashlib.sha256(host.read_bytes()).hexdigest()
        if host.is_file()
        else "4" * 64
    )
    receipt_id = "77777777-7777-4777-8777-777777777777"
    receipt = {
        "schemaVersion": 1,
        "resourceType": "adapter-promotion-receipt",
        "resourceId": receipt_id,
        "state": "passed",
        "packageId": "legacy-night-rain-gamer",
        "packageVersion": "0.1.0",
        "buildId": "test-build",
        "supportedGameIds": ["StarRail"],
        "payloadDigest": payload_digest,
        "replaySuiteDigest": "sha256:" + "1" * 64,
        "shadowSuiteDigest": "sha256:" + "2" * 64,
        "canarySuiteDigest": "sha256:" + "3" * 64,
        "candidateTestEvidenceSha256": "sha256:" + "5" * 64,
        "managerCanaryEvidenceSha256": "sha256:" + "6" * 64,
        "hostEntryPointSha256": "sha256:" + host_hash,
        "issuedAt": "2026-08-28T00:01:00Z",
    }
    receipt_raw = json.dumps(
        receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    receipt_path = runner.parent / "promotion-receipt.json"
    receipt_path.write_bytes(receipt_raw)
    receipt_hash = hashlib.sha256(receipt_raw).hexdigest()
    return {
        "schemaVersion": 2,
        "packageId": "legacy-night-rain-gamer",
        "packageVersion": "0.1.0",
        "buildId": "test-build",
        "builtAt": "2026-08-28T00:00:00Z",
        "installedAt": "2026-08-28T00:01:00Z",
        "protocolVersions": ["1.1"],
        "hostPackageId": "manager-adapter-host",
        "minHostVersion": "0.2.0",
        "entryPoint": "runner.exe",
        "files": [
            {
                "path": "runner.exe",
                "sha256": runner_hash,
                "sizeBytes": runner_size,
            },
            {
                "path": "promotion-receipt.json",
                "sha256": receipt_hash,
                "sizeBytes": len(receipt_raw),
            },
        ],
        "supportedGameIds": ["StarRail"],
        "operationBindings": {
            "StarRail": {
                "observe-panel": {
                    "handlerId": "starrail.observe-panel",
                    "mode": "granular",
                    "actionClass": "observation",
                    "risk": "observe_only",
                    "todoDefinitionIds": ["starrail.observe-panel"],
                    "adapterCapabilityRefs": ["game.daily.run@1.0"],
                    "supportsResume": True,
                    "timeoutSeconds": 300,
                    "requiredEvidenceKinds": ["game-ui-task-result"],
                }
            }
        },
        "forbiddenOperationClasses": sorted(REQUIRED_FORBIDDEN_OPERATION_CLASSES),
        "limits": {
            "maxRequestBytes": 262144,
            "maxEventBytes": 65536,
            "maxArtifactsPerTodo": 20,
        },
        "artifactPolicy": {
            "allowedMimeTypes": ["image/png", "image/jpeg", "text/plain"],
            "maxArtifactBytes": 20 * 1024 * 1024,
        },
        "security": {
            "allowsArbitraryCommand": False,
            "allowsArbitraryPath": False,
            "allowsArbitraryInput": False,
        },
        "promotion": {
            "status": "promoted",
            "replaySuiteDigest": "sha256:" + "1" * 64,
            "shadowSuiteDigest": "sha256:" + "2" * 64,
            "canarySuiteDigest": "sha256:" + "3" * 64,
            "payloadDigest": payload_digest,
            "receiptFile": "promotion-receipt.json",
            "receiptSha256": "sha256:" + receipt_hash,
            "receiptResourceId": receipt_id,
        },
        "executionReady": True,
    }


def write_package(root: Path) -> Path:
    root.mkdir(parents=True)
    runner = root / "runner.exe"
    runner.write_bytes(b"fixed-fake-runner")
    (root / "install-manifest.json").write_text(
        json.dumps(package_manifest(runner), separators=(",", ":")),
        encoding="utf-8",
    )
    return runner


class AdapterProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = AdapterExecutionPlan.from_document(plan_document())

    def test_durable_cancel_control_uses_separate_hmac_authority(self) -> None:
        at = "2099-08-28T10:00:02+08:00"
        reason = "manager_restart_cancel_recovery"
        nonce = "n" * 32
        payload = serialize_durable_cancel_control(
            run_id=RUN_ID,
            run_attempt_id=RUN_ATTEMPT_ID,
            cancel_authority=CANCEL_AUTHORITY,
            at=at,
            reason_code=reason,
            nonce=nonce,
        )
        document = json.loads(payload)
        self.assertNotIn("fencingToken", document)
        self.assertNotIn("cancelAuthority", document)
        canonical = "\n".join(
            ("1.1", "cancel", RUN_ID, RUN_ATTEMPT_ID, at, reason, nonce)
        )
        expected = hmac.new(
            CANCEL_AUTHORITY.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(document["authorityMac"], f"hmac-sha256:{expected}")

    def test_real_todo_id_capability_and_observe_risk_round_trip(self) -> None:
        raw = serialize_execute_request(self.plan)
        self.assertLessEqual(len(raw), 262144)
        self.assertEqual(parse_execute_request(raw).todos[0].todo_instance_id, TODO_ID)
        self.assertEqual(
            parse_execute_request(raw).todos[0].adapter_capability_ref,
            "game.daily.run@1.0",
        )
        self.assertEqual(parse_execute_request(raw).todos[0].risk, "observe_only")

    def test_non_executable_or_empty_scope_is_rejected_before_process(self) -> None:
        with self.assertRaises(AdapterProtocolError) as empty:
            AdapterExecutionPlan.from_document(
                plan_document(executableTodoInstanceIds=[], todos=[])
            )
        self.assertEqual(empty.exception.code, "no_executable_todos")

        document = plan_document()
        document["todos"][0]["executionDisposition"] = "deferred"  # type: ignore[index]
        with self.assertRaises(AdapterProtocolError) as deferred:
            AdapterExecutionPlan.from_document(document)
        self.assertEqual(deferred.exception.code, "todo_not_executable")

    def test_todo_id_and_capability_use_their_real_distinct_contracts(self) -> None:
        document = plan_document(executableTodoInstanceIds=[RUN_ID])
        document["todos"][0]["todoInstanceId"] = RUN_ID  # type: ignore[index]
        with self.assertRaises(AdapterProtocolError):
            AdapterExecutionPlan.from_document(document)

        document = plan_document()
        document["todos"][0]["adapterCapabilityRef"] = "game.daily.run"  # type: ignore[index]
        with self.assertRaises(AdapterProtocolError):
            AdapterExecutionPlan.from_document(document)

    def test_valid_event_stream_completes_only_with_fresh_evidence(self) -> None:
        stream = AdapterEventStream(self.plan)
        frames = [
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            ),
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            ),
            event_base(
                "artifact_staged",
                2,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                artifactId=ARTIFACT_ID,
                kind="game-ui-task-result",
                fileName="evidence.png",
                mimeType="image/png",
                sizeBytes=123,
                sha256="e" * 64,
                capturedAt=AT,
            ),
            event_base(
                "todo_terminal",
                3,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                status="completed",
                reasonCode="evidence_confirmed",
                reason="fresh evidence confirmed",
                retryable=False,
                evidenceArtifactIds=[ARTIFACT_ID],
            ),
            event_base(
                "run_terminal",
                4,
                status="completed",
                transportOutcome="clean",
                attemptedTodoInstanceIds=[TODO_ID],
                completedTodoInstanceIds=[TODO_ID],
                unresolvedTodoInstanceIds=[],
                terminalEventDigest=TERMINAL_DIGEST,
                exitCode=0,
            ),
        ]
        for frame in frames:
            stream.consume_line(frame)
        result = stream.finish(process_exit_code=0)
        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.completed_todo_instance_ids, (TODO_ID,))

    def test_human_required_is_typed_and_never_retryable(self) -> None:
        stream = AdapterEventStream(self.plan)
        frames = [
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            ),
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            ),
            event_base(
                "todo_terminal",
                2,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                status="human_required",
                reasonCode="login_required",
                reason="operator login is required; preserve the client",
                retryable=False,
                evidenceArtifactIds=[],
            ),
            event_base(
                "run_terminal",
                3,
                status="human_required",
                transportOutcome="clean",
                attemptedTodoInstanceIds=[TODO_ID],
                completedTodoInstanceIds=[],
                unresolvedTodoInstanceIds=[TODO_ID],
                terminalEventDigest=TERMINAL_DIGEST,
                exitCode=0,
            ),
        ]
        for frame in frames:
            stream.consume_line(frame)
        result = stream.finish(process_exit_code=0)
        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "human_required")
        self.assertEqual(result.unresolved_todo_instance_ids, (TODO_ID,))

        unsafe = AdapterEventStream(self.plan)
        unsafe.consume_line(frames[0])
        unsafe.consume_line(frames[1])
        with self.assertRaises(AdapterProtocolError) as raised:
            unsafe.consume_line(
                event_base(
                    "todo_terminal",
                    2,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    status="human_required",
                    reasonCode="login_required",
                    reason="operator login is required",
                    retryable=True,
                    evidenceArtifactIds=[],
                )
            )
        self.assertEqual(raised.exception.code, "unsafe_human_retry")

    def test_human_required_todo_cannot_be_downgraded_to_review_required_run(self) -> None:
        stream = AdapterEventStream(self.plan)
        for frame in (
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            ),
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            ),
            event_base(
                "todo_terminal",
                2,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                status="human_required",
                reasonCode="login_required",
                reason="operator login is required",
                retryable=False,
                evidenceArtifactIds=[],
            ),
        ):
            stream.consume_line(frame)
        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "run_terminal",
                    3,
                    status="review_required",
                    transportOutcome="clean",
                    attemptedTodoInstanceIds=[TODO_ID],
                    completedTodoInstanceIds=[],
                    unresolvedTodoInstanceIds=[TODO_ID],
                    terminalEventDigest=TERMINAL_DIGEST,
                    exitCode=0,
                )
            )
        self.assertEqual(raised.exception.code, "run_summary_mismatch")

    def test_artifact_count_is_atomic_per_todo_and_twenty_is_allowed(self) -> None:
        plan = AdapterExecutionPlan.from_document(two_todo_plan_document())
        stream = AdapterEventStream(plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID, SECOND_TODO_ID],
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                2,
                todoInstanceId=SECOND_TODO_ID,
                todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        for index in range(MAX_ARTIFACTS_PER_TODO):
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    3 + index,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=index + 1)),
                    kind="game-ui-task-result",
                    fileName=f"evidence-{index}.png",
                    mimeType="image/png",
                    sizeBytes=1,
                    sha256="e" * 64,
                    capturedAt=AT,
                )
            )

        stream.consume_line(
            event_base(
                "artifact_staged",
                23,
                todoInstanceId=SECOND_TODO_ID,
                todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                artifactId=str(uuid.UUID(int=100)),
                kind="game-ui-task-result",
                fileName="second-todo.png",
                mimeType="image/png",
                sizeBytes=1,
                sha256="f" * 64,
                capturedAt=AT,
            )
        )
        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    24,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=101)),
                    kind="game-ui-task-result",
                    fileName="twenty-first.png",
                    mimeType="image/png",
                    sizeBytes=1,
                    sha256="a" * 64,
                    capturedAt=AT,
                )
            )
        self.assertEqual(raised.exception.code, "artifact_count_exceeded")
        self.assertEqual(stream.next_sequence, 24)
        self.assertEqual(stream.artifact_counts_by_todo[TODO_ID], 20)
        self.assertEqual(stream.artifact_counts_by_todo[SECOND_TODO_ID], 1)
        self.assertEqual(stream.artifact_bytes, 21)
        stream.consume_line(
            event_base(
                "todo_progress",
                24,
                todoInstanceId=SECOND_TODO_ID,
                todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                code="still-active",
            )
        )

    def test_downstream_todo_cannot_start_after_blocking_predecessor(self) -> None:
        plan = AdapterExecutionPlan.from_document(two_todo_plan_document())
        stream = AdapterEventStream(plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID, SECOND_TODO_ID],
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        stream.consume_line(
            event_base(
                "todo_terminal",
                2,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                status="blocked",
                reasonCode="entry_not_ready",
                reason="The entry screen was not interactive.",
                retryable=True,
                evidenceArtifactIds=[],
            )
        )

        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "todo_attempt_started",
                    3,
                    todoInstanceId=SECOND_TODO_ID,
                    todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                    attemptNo=1,
                    operation="observe-panel",
                )
            )

        self.assertEqual(
            raised.exception.code, "todo_after_blocking_predecessor"
        )
        self.assertNotIn(SECOND_TODO_ID, stream.started)

    def test_already_started_downstream_todo_cannot_continue_after_blocker(self) -> None:
        plan = AdapterExecutionPlan.from_document(two_todo_plan_document())
        stream = AdapterEventStream(plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID, SECOND_TODO_ID],
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                2,
                todoInstanceId=SECOND_TODO_ID,
                todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        stream.consume_line(
            event_base(
                "todo_terminal",
                3,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                status="blocked",
                reasonCode="entry_not_ready",
                reason="The entry screen was not interactive.",
                retryable=True,
                evidenceArtifactIds=[],
            )
        )

        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "todo_progress",
                    4,
                    todoInstanceId=SECOND_TODO_ID,
                    todoAttemptId=SECOND_TODO_ATTEMPT_ID,
                    code="would-have-been-false-progress",
                )
            )

        self.assertEqual(
            raised.exception.code, "todo_after_blocking_predecessor"
        )
        self.assertNotIn(SECOND_TODO_ID, stream.todo_terminals)

    def test_artifact_run_byte_limit_rejects_before_state_advances(self) -> None:
        stream = AdapterEventStream(self.plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        for index in range(12):
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    2 + index,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=index + 200)),
                    kind="raw-frame",
                    fileName=f"large-{index}.png",
                    mimeType="image/png",
                    sizeBytes=MAX_ARTIFACT_BYTES,
                    sha256="b" * 64,
                    capturedAt=AT,
                )
            )
        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    14,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=999)),
                    kind="raw-frame",
                    fileName="over-run-limit.png",
                    mimeType="image/png",
                    sizeBytes=MAX_ARTIFACT_BYTES,
                    sha256="c" * 64,
                    capturedAt=AT,
                )
            )
        self.assertEqual(raised.exception.code, "artifact_run_bytes_exceeded")
        self.assertEqual(stream.next_sequence, 14)
        self.assertEqual(stream.artifact_bytes, 12 * MAX_ARTIFACT_BYTES)
        self.assertEqual(len(stream.artifacts), 12)
        stream.consume_line(
            event_base(
                "todo_progress",
                14,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                code="quota-rejection-was-atomic",
            )
        )

    def test_wrong_scope_out_of_order_and_completed_without_evidence_fail(self) -> None:
        stream = AdapterEventStream(self.plan)
        wrong = json.loads(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            )
        )
        wrong["runAttemptId"] = str(uuid.uuid4())
        with self.assertRaises(AdapterProtocolError) as scope:
            stream.consume_line(json.dumps(wrong).encode())
        self.assertEqual(scope.exception.code, "event_scope_mismatch")

        stream = AdapterEventStream(self.plan)
        with self.assertRaises(AdapterProtocolError) as sequence:
            stream.consume_line(
                event_base(
                    "hello",
                    1,
                    packageId="legacy-night-rain-gamer",
                    packageVersion="0.1.0",
                    packageDigest=PACKAGE_DIGEST,
                    runnerPid=1234,
                    acceptedTodoInstanceIds=[TODO_ID],
                )
            )
        self.assertEqual(sequence.exception.code, "event_sequence_mismatch")

        stream = AdapterEventStream(self.plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            )
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        with self.assertRaises(AdapterProtocolError) as evidence:
            stream.consume_line(
                event_base(
                    "todo_terminal",
                    2,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    status="completed",
                    reasonCode="claimed",
                    reason="no evidence",
                    retryable=False,
                    evidenceArtifactIds=[],
                )
            )
        self.assertEqual(evidence.exception.code, "completed_without_evidence")

    def test_exit_code_never_synthesizes_todo_completion(self) -> None:
        stream = AdapterEventStream(self.plan)
        stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            )
        )
        result = stream.finish(process_exit_code=0)
        self.assertFalse(result.protocol_valid)
        self.assertEqual(result.code, "missing_run_terminal")
        self.assertEqual(result.completed_todo_instance_ids, ())

    def test_manifest_v2_integrity_binding_and_forbidden_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy-night-rain-gamer"
            runner = write_package(root)
            manifest = verify_execution_package(root)
            self.assertEqual(manifest.max_artifacts_per_todo, 20)
            validate_plan_against_manifest(self.plan, manifest)
            document = package_manifest(runner)
            document["limits"]["maxArtifactsPerTodo"] = 7  # type: ignore[index]
            (root / "install-manifest.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            self.assertEqual(verify_execution_package(root).max_artifacts_per_todo, 7)
            runner.write_bytes(b"tampered-runner")
            with self.assertRaises(AdapterProtocolError) as tamper:
                verify_execution_package(root)
            self.assertIn(tamper.exception.code, {"execution_size_mismatch", "execution_hash_mismatch"})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy-night-rain-gamer"
            runner = write_package(root)
            document = package_manifest(runner)
            document["forbiddenOperationClasses"] = ["gacha"]
            (root / "install-manifest.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(AdapterProtocolError) as forbidden:
                verify_execution_package(root)
            self.assertEqual(forbidden.exception.code, "unsafe_execution_manifest")

    def test_candidate_cannot_execute_and_zero_promotion_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy-night-rain-gamer"
            runner = write_package(root)
            document = package_manifest(runner)
            (root / "promotion-receipt.json").unlink()
            document["files"] = [
                item
                for item in document["files"]  # type: ignore[index]
                if item["path"] != "promotion-receipt.json"
            ]
            document["promotion"] = {
                **document["promotion"],  # type: ignore[index]
                "status": "candidate",
                "canarySuiteDigest": "sha256:" + "0" * 64,
                "receiptFile": "",
                "receiptSha256": "",
                "receiptResourceId": "",
            }
            document["executionReady"] = False
            (root / "install-manifest.json").write_text(
                json.dumps(document, separators=(",", ":")), encoding="utf-8"
            )
            self.assertEqual(
                verify_execution_candidate(root).promotion_status, "candidate"
            )
            with self.assertRaises(AdapterProtocolError) as unpromoted:
                verify_execution_package(root)
            self.assertEqual(unpromoted.exception.code, "execution_package_unpromoted")

            document["promotion"]["replaySuiteDigest"] = "sha256:" + "0" * 64  # type: ignore[index]
            (root / "install-manifest.json").write_text(
                json.dumps(document, separators=(",", ":")), encoding="utf-8"
            )
            with self.assertRaises(AdapterProtocolError) as zero_digest:
                verify_execution_candidate(root)
            self.assertEqual(zero_digest.exception.code, "execution_package_unpromoted")

    def test_promoted_receipt_must_equal_manager_owned_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy-night-rain-gamer"
            write_package(root)
            receipt = json.loads(
                (root / "promotion-receipt.json").read_text(encoding="utf-8")
            )
            resource = {
                "resource_id": receipt["resourceId"],
                "resource_type": "adapter-promotion-receipt",
                "state": "passed",
                "document": receipt,
            }
            verified = verify_execution_package(
                root, receipt_resolver=lambda _resource_id: resource
            )
            self.assertEqual(verified.promotion_receipt_id, receipt["resourceId"])

            mismatched = {**resource, "document": {**receipt, "state": "failed"}}
            with self.assertRaises(AdapterProtocolError) as mismatch:
                verify_execution_package(
                    root, receipt_resolver=lambda _resource_id: mismatched
                )
            self.assertEqual(mismatch.exception.code, "promotion_receipt_mismatch")

    def test_lower_manifest_artifact_limit_flows_into_event_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "legacy-night-rain-gamer"
            runner = write_package(root)
            document = package_manifest(runner)
            document["limits"]["maxArtifactsPerTodo"] = 2  # type: ignore[index]
            (root / "install-manifest.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            manifest = verify_execution_package(root)

        stream = AdapterEventStream(
            self.plan,
            allowed_artifact_mime_types=manifest.allowed_mime_types,
            max_artifact_bytes=manifest.max_artifact_bytes,
            max_artifacts_per_todo=manifest.max_artifacts_per_todo,
        )
        hello = stream.consume_line(
            event_base(
                "hello",
                0,
                packageId="legacy-night-rain-gamer",
                packageVersion="0.1.0",
                packageDigest=PACKAGE_DIGEST,
                runnerPid=1234,
                acceptedTodoInstanceIds=[TODO_ID],
            )
        )
        self.assertEqual(hello.max_artifacts_per_todo, 2)
        self.assertEqual(
            hello.allowed_artifact_mime_types,
            tuple(sorted(manifest.allowed_mime_types)),
        )
        stream.consume_line(
            event_base(
                "todo_attempt_started",
                1,
                todoInstanceId=TODO_ID,
                todoAttemptId=TODO_ATTEMPT_ID,
                attemptNo=1,
                operation="observe-panel",
            )
        )
        for index in range(2):
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    2 + index,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=1200 + index)),
                    kind="raw-frame",
                    fileName=f"lower-{index}.png",
                    mimeType="image/png",
                    sizeBytes=1,
                    sha256="e" * 64,
                    capturedAt=AT,
                )
            )
        with self.assertRaises(AdapterProtocolError) as raised:
            stream.consume_line(
                event_base(
                    "artifact_staged",
                    4,
                    todoInstanceId=TODO_ID,
                    todoAttemptId=TODO_ATTEMPT_ID,
                    artifactId=str(uuid.UUID(int=1202)),
                    kind="raw-frame",
                    fileName="lower-third.png",
                    mimeType="image/png",
                    sizeBytes=1,
                    sha256="f" * 64,
                    capturedAt=AT,
                )
            )
        self.assertEqual(raised.exception.code, "artifact_count_exceeded")

    def test_event_line_has_a_hard_size_limit(self) -> None:
        stream = AdapterEventStream(self.plan)
        with self.assertRaises(AdapterProtocolError) as error:
            stream.consume_line(b"{" + b"x" * MAX_EVENT_BYTES + b"}")
        self.assertEqual(error.exception.code, "payload_too_large")


class AdapterHostSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.host_root = self.runtime / "adapters" / "manager-adapter-host"
        self.host_root.mkdir(parents=True)
        self.host = self.host_root / "host.exe"
        self.host.write_bytes(b"fixed-test-host")
        host_hash = hashlib.sha256(self.host.read_bytes()).hexdigest()
        (self.host_root / "install-manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "packageId": "manager-adapter-host",
                    "hostVersion": "0.2.0",
                    "protocolVersion": "1.1",
                    "entryPoint": "host.exe",
                    "sha256": host_hash,
                    "sizeBytes": self.host.stat().st_size,
                    "hostReady": True,
                    "executionReady": False,
                    "supportedGameIds": ["StarRail"],
                    "installedAt": "2026-08-28T00:00:00Z",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        write_package(self.runtime / "adapters" / "legacy-night-rain-gamer")
        legacy = LegacyAdapter(
            legacy_root=self.root / "unused-nas-source",
            runtime_dir=self.runtime,
            allowed_game_ids=["StarRail"],
            execution_enabled=True,
        )
        self.host_service = ManagerAdapterHost(legacy)
        self.plan = AdapterExecutionPlan.from_document(plan_document())
        self.installation_binding = {
            "gamePath": r"C:\\Games\\StarRail\\StarRail.exe",
            "toolPath": r"C:\\Games\\March7thAssistant",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_persisted_cancel_authority_replays_without_fencing_token(self) -> None:
        authority_path = self.host_service._write_control_authority(self.plan)
        authority_hash = "sha256:" + hashlib.sha256(
            CANCEL_AUTHORITY.encode("utf-8")
        ).hexdigest()
        self.assertFalse(
            self.host_service.cancel_persisted_attempt(
                run_id=RUN_ID,
                run_attempt_id=RUN_ATTEMPT_ID,
                cancel_authority_hash="sha256:" + "0" * 64,
            )
        )
        self.assertTrue(
            self.host_service.cancel_persisted_attempt(
                run_id=RUN_ID,
                run_attempt_id=RUN_ATTEMPT_ID,
                cancel_authority_hash=authority_hash,
                reason_code="manager_restart_cancel_recovery",
            )
        )
        pending = (
            self.host_service.runtime_dir
            / "adapter-controls"
            / f"{RUN_ATTEMPT_ID}.cancel.json"
        )
        self.assertTrue(pending.is_file())
        control_text = pending.read_text(encoding="utf-8")
        self.assertNotIn(FENCING_TOKEN, control_text)
        self.assertNotIn(CANCEL_AUTHORITY, control_text)
        self.host_service._cleanup_attempt_controls(self.plan)
        self.assertFalse(authority_path.exists())

    def test_per_game_module_root_overrides_only_its_exact_game(self) -> None:
        module_root = self.runtime / "adapters" / "game-modules" / "starrail"
        write_package(module_root)

        self.assertEqual(
            self.host_service.compatibility_adapter.execution_package_root("StarRail"),
            module_root,
        )
        binding = self.host_service.execution_bindings("StarRail")
        self.assertEqual(binding["status"], "promoted")
        self.assertTrue(binding["manifestVerified"])

    def _fake_host_script(self, body: str) -> Path:
        path = self.root / f"fake-host-{uuid.uuid4().hex}.py"
        path.write_text(
            "import json,os,sys,time\n"
            "request=json.loads(sys.stdin.readline())\n"
            "base=lambda t,s: {'schemaVersion':1,'protocolVersion':'1.1','eventType':t,'sequence':s,'runId':request['runId'],'runAttemptId':request['runAttemptId'],'fencingToken':request['fencingToken'],'gameId':request['gameId'],'at':'2099-08-28T10:00:01+08:00'}\n"
            + body,
            encoding="utf-8",
        )
        return path

    def _patched_popen(self, script: Path):
        real_popen = subprocess.Popen

        def spawn(command: list[str], **kwargs: object):
            self.assertEqual(Path(command[0]), self.host.resolve())
            self.assertEqual(command[1:5], ["--operation", "execute", "--protocol-version", "1.1"])
            self.assertNotIn(TODO_ID, command)
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["stdin"], subprocess.PIPE)
            child_environment = kwargs["env"]
            self.assertNotIn("YEYU_GAMER_TRAY_BOOTSTRAP_SECRET", child_environment)
            self.assertNotIn("YEYU_GAMER_ACTOR_TOKEN", child_environment)
            self.assertNotIn("SMTP_PASSWORD", child_environment)
            self.assertEqual(
                {
                    key
                    for key in child_environment
                    if key.startswith("YEYU_")
                },
                {
                    "YEYU_GAMER_ADAPTER_STAGING_DIR",
                    "YEYU_GAMER_INSTALLATION_BINDING_PATH",
                    "YEYU_GAMER_EXECUTION_PACKAGE_ROOT",
                },
            )
            self.assertEqual(
                Path(child_environment["YEYU_GAMER_ADAPTER_STAGING_DIR"]),
                (
                    self.runtime
                    / "artifact-inbox"
                    / RUN_ATTEMPT_ID
                ).resolve(),
            )
            self.assertEqual(
                Path(child_environment["YEYU_GAMER_INSTALLATION_BINDING_PATH"]),
                (
                    self.runtime
                    / "artifact-inbox"
                    / RUN_ATTEMPT_ID
                    / "installation-binding.json"
                ).resolve(),
            )
            self.assertEqual(
                Path(child_environment["YEYU_GAMER_EXECUTION_PACKAGE_ROOT"]),
                (
                    self.runtime
                    / "adapters"
                    / "legacy-night-rain-gamer"
                ),
            )
            return real_popen(
                [sys.executable, "-u", str(script), *command[1:]],
                **kwargs,
            )

        return mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.Popen",
            side_effect=spawn,
        )

    def test_structured_plan_jsonl_callbacks_and_runtime_binding_projection(self) -> None:
        binding = self.host_service.execution_bindings("StarRail")
        self.assertTrue(binding["manifestVerified"])
        self.assertEqual(binding["bindings"][0]["todoDefinitionIds"], ["starrail.observe-panel"])
        self.assertEqual(binding["bindings"][0]["adapterCapabilityRefs"], ["game.daily.run@1.0"])

        script = self._fake_host_script(
            "todo=request['executableTodoInstanceIds'][0]\n"
            f"hello=base('hello',0);hello.update({{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0','packageDigest':{binding['packageDigest']!r},'runnerPid':os.getpid(),'acceptedTodoInstanceIds':[todo]}});print(json.dumps(hello),flush=True)\n"
            "terminal=base('run_terminal',1);terminal.update({'status':'cancelled','transportOutcome':'cancelled','attemptedTodoInstanceIds':[],'completedTodoInstanceIds':[],'unresolvedTodoInstanceIds':[todo],'terminalEventDigest':'sha256:'+'d'*64,'exitCode':0});print(json.dumps(terminal),flush=True)\n"
        )
        completed: list[object] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        with mock.patch.dict(
            os.environ,
            {
                "YEYU_GAMER_TRAY_BOOTSTRAP_SECRET": "must-not-cross",
                "YEYU_GAMER_ACTOR_TOKEN": "must-not-cross",
                "SMTP_PASSWORD": "must-not-cross",
            },
        ), self._patched_popen(script):
            pid = self.host_service.execute(
                RUN_ID,
                "StarRail",
                complete,
                plan=self.plan,
                on_event=lambda _event, _manifest: None,
                installation_binding=self.installation_binding,
            )
            self.assertGreater(pid, 0)
            self.assertTrue(signalled.wait(5))
        result = completed[0]
        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.completed_todo_instance_ids, ())

    def _host_rejection_response(self, **updates: object) -> dict[str, object]:
        probe = self.host_service.probe()
        result: dict[str, object] = {
            "protocolVersion": "1.1",
            "hostVersion": probe["hostVersion"],
            "operation": "execute",
            "success": False,
            "code": "execution_package_unavailable",
            "message": "Promotion receipt is invalid or belongs to another package.",
            "runId": RUN_ID,
            "gameId": "StarRail",
            "hostReady": True,
            "executionReady": False,
            "adapterProcessStarted": False,
            "gameProcessStarted": False,
            "entryPointSha256": probe["hostEntryPointSha256"],
            "supportedGameIds": probe["hostSupportedGameIds"],
        }
        result.update(updates)
        return result

    def _run_host_rejection(
        self, document: dict[str, object], *, after_hello: bool = False
    ):
        body = ""
        if after_hello:
            digest = self.host_service.execution_bindings("StarRail")["packageDigest"]
            body += (
                "hello=base('hello',0);hello.update("
                f"{{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0',"
                f"'packageDigest':{digest!r},'runnerPid':os.getpid(),"
                "'acceptedTodoInstanceIds':request['executableTodoInstanceIds']});"
                "print(json.dumps(hello),flush=True)\n"
            )
        body += f"print({json.dumps(document)!r},flush=True)\nsys.exit(78)\n"
        script = self._fake_host_script(body)
        completed = []
        events = []
        signalled = threading.Event()

        def complete(result):
            completed.append(result)
            signalled.set()

        with self._patched_popen(script):
            self.host_service.execute(
                RUN_ID, "StarRail", complete, plan=self.plan,
                on_event=lambda event, _manifest: events.append(event.event_type),
                installation_binding=self.installation_binding,
            )
            self.assertTrue(signalled.wait(5))
        self.assertEqual(len(completed), 1)
        result = completed[0]
        self.assertFalse(result.protocol_valid)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempted_todo_instance_ids, ())
        self.assertEqual(result.completed_todo_instance_ids, ())
        self.assertEqual(result.unresolved_todo_instance_ids, (TODO_ID,))
        self.assertEqual(events, ["hello"] if after_hello else [])
        return result

    def test_execute_package_rejection_preserves_host_diagnostic(self) -> None:
        document = self._host_rejection_response()
        result = self._run_host_rejection(document)
        self.assertEqual(result.code, document["code"])
        self.assertEqual(result.message, document["message"])
        self.assertEqual(result.exit_code, 78)

    def test_execute_rejection_requires_exact_host_scope_and_no_processes(self) -> None:
        for changes in (
            {"operation": "probe"},
            {"runId": str(uuid.uuid4())},
            {"gameId": "WW"},
            {"hostVersion": "unexpected"},
            {"entryPointSha256": "0" * 64},
            {"supportedGameIds": ["StarRail", "WW"]},
            {"success": True},
            {"hostReady": False},
            {"executionReady": True},
            {"adapterProcessStarted": True},
            {"gameProcessStarted": True},
            {"adapterProcessStarted": 0},
            {"code": "made_up_failure"},
            {"message": ""},
            {"message": "x" * 2049},
            {"unexpectedField": True},
        ):
            with self.subTest(changes=changes):
                result = self._run_host_rejection(self._host_rejection_response(**changes))
                self.assertEqual(result.code, "invalid_host_response")

    def test_execute_rejection_is_not_accepted_after_adapter_hello(self) -> None:
        result = self._run_host_rejection(self._host_rejection_response(), after_hello=True)
        self.assertEqual(result.code, "invalid_schema")
        self.assertEqual(result.message, "eventType is invalid")

    def test_completion_callback_failure_retains_restart_cancel_material(self) -> None:
        package_digest = self.host_service.execution_bindings("StarRail")[
            "packageDigest"
        ]
        script = self._fake_host_script(
            "todo=request['executableTodoInstanceIds'][0]\n"
            f"hello=base('hello',0);hello.update({{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0','packageDigest':{package_digest!r},'runnerPid':os.getpid(),'acceptedTodoInstanceIds':[todo]}});print(json.dumps(hello),flush=True)\n"
            "time.sleep(.3)\n"
            "terminal=base('run_terminal',1);terminal.update({'status':'cancelled','transportOutcome':'cancelled','attemptedTodoInstanceIds':[],'completedTodoInstanceIds':[],'unresolvedTodoInstanceIds':[todo],'terminalEventDigest':'sha256:'+'d'*64,'exitCode':0});print(json.dumps(terminal),flush=True)\n"
        )
        callback_entered = threading.Event()

        def fail_completion(_result: object) -> None:
            callback_entered.set()
            raise RuntimeError("fixture terminal persistence failure")

        authority_hash = "sha256:" + hashlib.sha256(
            CANCEL_AUTHORITY.encode("utf-8")
        ).hexdigest()
        with self._patched_popen(script):
            self.host_service.execute(
                RUN_ID,
                "StarRail",
                fail_completion,
                plan=self.plan,
                installation_binding=self.installation_binding,
            )
            self.assertTrue(
                self.host_service.cancel_persisted_attempt(
                    run_id=RUN_ID,
                    run_attempt_id=RUN_ATTEMPT_ID,
                    cancel_authority_hash=authority_hash,
                    reason_code="fixture_restart_recovery",
                )
            )
            self.assertTrue(callback_entered.wait(5))

        authority = self.host_service._control_authority_path(RUN_ATTEMPT_ID)
        pending = self.host_service._durable_control_path(RUN_ATTEMPT_ID)
        self.assertTrue(authority.is_file())
        self.assertTrue(pending.is_file())
        self.host_service._cleanup_attempt_controls(self.plan)

    def test_host_wires_manifest_artifact_count_limit_into_event_stream(self) -> None:
        package_root = self.runtime / "adapters" / "legacy-night-rain-gamer"
        runner = package_root / "runner.exe"
        document = package_manifest(runner)
        document["limits"]["maxArtifactsPerTodo"] = 1  # type: ignore[index]
        (package_root / "install-manifest.json").write_text(
            json.dumps(document, separators=(",", ":")), encoding="utf-8"
        )
        package_digest = self.host_service.execution_bindings("StarRail")[
            "packageDigest"
        ]
        script = self._fake_host_script(
            "todo=request['executableTodoInstanceIds'][0]\n"
            f"hello=base('hello',0);hello.update({{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0','packageDigest':{package_digest!r},'runnerPid':os.getpid(),'acceptedTodoInstanceIds':[todo]}});print(json.dumps(hello),flush=True)\n"
            "started=base('todo_attempt_started',1);started.update({'todoInstanceId':todo,'todoAttemptId':'44444444-4444-4444-8444-444444444444','attemptNo':1,'operation':'observe-panel'});print(json.dumps(started),flush=True)\n"
            "first=base('artifact_staged',2);first.update({'todoInstanceId':todo,'todoAttemptId':'44444444-4444-4444-8444-444444444444','artifactId':'55555555-5555-4555-8555-555555555555','kind':'screenshot','fileName':'first.png','mimeType':'image/png','sizeBytes':1,'sha256':'e'*64,'capturedAt':'2099-08-28T10:00:01+08:00'});print(json.dumps(first),flush=True)\n"
            "second=base('artifact_staged',3);second.update({'todoInstanceId':todo,'todoAttemptId':'44444444-4444-4444-8444-444444444444','artifactId':'66666666-6666-4666-8666-666666666666','kind':'screenshot','fileName':'second.png','mimeType':'image/png','sizeBytes':1,'sha256':'f'*64,'capturedAt':'2099-08-28T10:00:01+08:00'});print(json.dumps(second),flush=True)\n"
        )
        completed: list[object] = []
        seen_event_types: list[str] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        with self._patched_popen(script):
            self.host_service.execute(
                RUN_ID,
                "StarRail",
                complete,
                plan=self.plan,
                on_event=lambda event, _manifest: seen_event_types.append(
                    event.event_type
                ),
                installation_binding=self.installation_binding,
            )
            self.assertTrue(signalled.wait(5))

        result = completed[0]
        self.assertFalse(result.protocol_valid)
        self.assertEqual(result.code, "artifact_count_exceeded")
        self.assertEqual(
            seen_event_types,
            ["hello", "todo_attempt_started", "artifact_staged"],
        )

    def test_empty_plan_seals_without_starting_host(self) -> None:
        with mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.Popen",
            side_effect=AssertionError("no executable Todo must not start Host"),
        ) as popen:
            with self.assertRaises(AdapterExecutionRejected) as rejected:
                self.host_service.execute(RUN_ID, "StarRail", lambda _result: None)
        popen.assert_not_called()
        self.assertEqual(rejected.exception.code, "no_executable_todos")

    def test_cancel_control_is_fenced_and_relayed_to_the_active_attempt(self) -> None:
        package_digest = self.host_service.execution_bindings("StarRail")[
            "packageDigest"
        ]
        script = self._fake_host_script(
            "todo=request['executableTodoInstanceIds'][0]\n"
            f"hello=base('hello',0);hello.update({{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0','packageDigest':{package_digest!r},'runnerPid':os.getpid(),'acceptedTodoInstanceIds':[todo]}});print(json.dumps(hello),flush=True)\n"
            "control=json.loads(sys.stdin.readline())\n"
            "assert control['controlType']=='cancel' and control['runAttemptId']==request['runAttemptId'] and control['fencingToken']==request['fencingToken']\n"
            "terminal=base('run_terminal',1);terminal.update({'status':'cancelled','transportOutcome':'cancelled','attemptedTodoInstanceIds':[],'completedTodoInstanceIds':[],'unresolvedTodoInstanceIds':[todo],'terminalEventDigest':'sha256:'+'d'*64,'exitCode':0});print(json.dumps(terminal),flush=True)\n"
        )
        completed: list[object] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        with self._patched_popen(script):
            self.host_service.execute(RUN_ID, "StarRail", complete, plan=self.plan, installation_binding=self.installation_binding)
            self.assertFalse(
                self.host_service.cancel(
                    run_attempt_id=RUN_ATTEMPT_ID,
                    fencing_token="wrong-token-that-is-long-enough-0000",
                )
            )
            self.assertTrue(
                self.host_service.cancel(
                    run_attempt_id=RUN_ATTEMPT_ID,
                    fencing_token=FENCING_TOKEN,
                )
            )
            self.assertFalse(
                self.host_service.cancel(
                    run_attempt_id=RUN_ATTEMPT_ID,
                    fencing_token=FENCING_TOKEN,
                )
            )
            self.assertTrue(signalled.wait(5))
        self.assertTrue(completed[0].protocol_valid)
        self.assertEqual(completed[0].transport_outcome, "cancelled")

    def test_timeout_requests_cancel_then_terminates_only_fixed_host(self) -> None:
        document = plan_document(timeoutSeconds=1)
        plan = AdapterExecutionPlan.from_document(document)
        script = self._fake_host_script("time.sleep(10)\n")
        self.host_service.CANCEL_GRACE_SECONDS = 0.1
        completed: list[object] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        started = time.monotonic()
        with self._patched_popen(script):
            self.host_service.execute(RUN_ID, "StarRail", complete, plan=plan, installation_binding=self.installation_binding)
            self.assertTrue(signalled.wait(5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(completed[0].code, "adapter_timeout")
        self.assertEqual(completed[0].transport_outcome, "timeout")
        self.assertEqual(completed[0].completed_todo_instance_ids, ())

    def test_cancel_terminates_non_cooperative_fixed_host_after_grace(self) -> None:
        package_digest = self.host_service.execution_bindings("StarRail")[
            "packageDigest"
        ]
        script = self._fake_host_script(
            "todo=request['executableTodoInstanceIds'][0]\n"
            f"hello=base('hello',0);hello.update({{'packageId':'legacy-night-rain-gamer','packageVersion':'0.1.0','packageDigest':{package_digest!r},'runnerPid':os.getpid(),'acceptedTodoInstanceIds':[todo]}});print(json.dumps(hello),flush=True)\n"
            "json.loads(sys.stdin.readline())\n"
            "time.sleep(10)\n"
        )
        self.host_service.CANCEL_GRACE_SECONDS = 0.1
        completed: list[object] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        started = time.monotonic()
        with self._patched_popen(script):
            self.host_service.execute(
                RUN_ID,
                "StarRail",
                complete,
                plan=self.plan,
                installation_binding=self.installation_binding,
            )
            self.assertTrue(
                self.host_service.cancel(
                    run_attempt_id=RUN_ATTEMPT_ID,
                    fencing_token=FENCING_TOKEN,
                )
            )
            self.assertTrue(signalled.wait(5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(completed[0].code, "adapter_cancelled_without_terminal")
        self.assertEqual(completed[0].transport_outcome, "cancelled")


if __name__ == "__main__":
    unittest.main()
