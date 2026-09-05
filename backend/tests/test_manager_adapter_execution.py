from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yeyu_gamer_manager.services.adapter_protocol import (
    ALLOWED_ARTIFACT_MIME_TYPES,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_TODO,
    MAX_EVENT_BYTES,
    MAX_REQUEST_BYTES,
    REQUIRED_FORBIDDEN_OPERATION_CLASSES,
    AdapterEvent,
    AdapterRunResult,
)
from yeyu_gamer_manager.domain.models import (
    AdapterGovernanceRequest,
    BatchCreateRequest,
    BatchResumeRequest,
    RunControlRequest,
)
from yeyu_gamer_manager.services.legacy_adapter import LegacyAdapter
from yeyu_gamer_manager.services.legacy_import import LegacyImportReport
from yeyu_gamer_manager.services.game_launcher import (
    GameCloseReceipt,
    GameLaunchCancelled,
    GameLaunchError,
    GameLaunchHumanRequired,
    GameLaunchReceipt,
    LaunchObservation,
)
from yeyu_gamer_manager.services.manager import ManagerConflict, ManagerService
from yeyu_gamer_manager.services.window_capture import CapturedWindow, _encode_bgra_png
from yeyu_gamer_manager.store.sqlite_store import PublicFencingMaterialRejected
from yeyu_gamer_manager.services.todo_catalog import catalog as todo_catalog
from yeyu_gamer_manager.settings import Settings
from yeyu_gamer_manager.store.sqlite_store import SqliteStore


GAME_ID = "StarRail"
CAPABILITY_REF = "game.daily.run@1.0"
CATALOG_VERSION = "manager-adapter-e2e-1"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

EXECUTE_ALPHA = "todo.v1.starrail.daily.e2e-observe-alpha"
EXECUTE_BETA = "todo.v1.starrail.daily.e2e-run-beta"
DEFERRED = "todo.v1.starrail.daily.e2e-unbound-gamma"
APPROVAL = "todo.v1.starrail.daily.e2e-approval"
FORBIDDEN = "todo.v1.starrail.daily.e2e-forbidden"

ALPHA_ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
BETA_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
ALPHA_ARTIFACT_ID = "33333333-3333-4333-8333-333333333333"


def _definition(
    definition_id: str,
    operation: str,
    *,
    risk: str,
    capability: str | None,
    initial_status: str = "pending",
) -> dict[str, object]:
    return {
        "todo_definition_id": definition_id,
        "definition_version": 1,
        "catalog_version": CATALOG_VERSION,
        "source_hash": hashlib.sha256(definition_id.encode("utf-8")).hexdigest(),
        "game_id": GAME_ID,
        "cadence": "daily",
        "operation": operation,
        "title": operation,
        "category": "test",
        "order_index": {
            EXECUTE_ALPHA: 1001,
            EXECUTE_BETA: 1002,
            DEFERRED: 1003,
            APPROVAL: 1004,
            FORBIDDEN: 1005,
        }[definition_id],
        "required": True,
        "risk": risk,
        "automation_difficulty": "low",
        "adapter_capability_ref": capability,
        "automation_state": (
            "forbidden-by-policy"
            if risk == "forbidden"
            else "disabled-unimplemented"
            if risk == "approval_required"
            else "source-tool-declared-unbound"
        ),
        "initial_status": initial_status,
        "initial_reason": (
            "requires explicit review" if initial_status == "review_required" else ""
        ),
        "reset_rule": {
            "timezone": "Asia/Shanghai",
            "time": "04:00",
            "cadence": "daily",
        },
        "source_refs": ["backend/tests/test_manager_adapter_execution.py"],
    }


class ManagerAdapterExecutionTests(unittest.TestCase):
    """Exercise Manager orchestration without starting a runner or a game."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="yeyu-manager-adapter-e2e-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = replace(
            Settings.for_test(self.root), legacy_execution_enabled=True
        )
        self.settings.data_dir.mkdir(parents=True)
        self.settings.legacy_root.mkdir(parents=True)
        self.store = SqliteStore(self.settings.database_path)
        self.store.initialize()
        self.addCleanup(self.store.close)
        self.store.import_legacy(
            config_values={"step_timeout_seconds": 120},
            games=[
                {
                    "game_id": GAME_ID,
                    "display_name": "StarRail",
                    "order_index": 1,
                    "enabled": True,
                    "policy": {},
                }
            ],
            source_info={"test": "manager-adapter-e2e"},
            policy_projection={},
        )
        self._write_fixed_host_package()
        self._write_execution_package()
        adapter = LegacyAdapter(
            legacy_root=self.settings.legacy_root,
            runtime_dir=self.settings.data_dir,
            allowed_game_ids=[GAME_ID],
            execution_enabled=True,
        )
        self.manager = ManagerService(
            settings=self.settings,
            store=self.store,
            legacy_report=LegacyImportReport(
                status="ok",
                detail="test fixture",
                imported=True,
                source_info={"test": "manager-adapter-e2e"},
                allowed_game_ids=(GAME_ID,),
            ),
            adapter=adapter,
        )
        self.store.update_config(
            {
                "game_paths": {
                    GAME_ID: {
                        "game_path": r"C:\\Games\\StarRail.exe",
                        "tool_path": r"C:\\Tools\\StarRailTool.exe",
                    }
                }
            }
        )
        self.manager.game_launcher = mock.Mock()
        self.manager.game_launcher.ensure_started.return_value = GameLaunchReceipt(
            "started", 4321, "starrail.exe"
        )
        self.manager.game_launcher.close_started.return_value = GameCloseReceipt(
            "closed", (4321,), ()
        )
        self.manager.game_launcher.close_for_queue.return_value = GameCloseReceipt(
            "closed", (4321,), ()
        )
        self.manager.game_launcher.list_zombies.return_value = {}
        self.manager.game_launcher.reap_zombies.return_value = {
            "attempted": {},
            "released": [],
            "stuck": {},
        }
        # Step frames must not depend on a real StarRail window existing on the
        # build machine; the fixture returns one deterministic PNG per capture.
        fake_frame = _encode_bgra_png(2, 2, bytes(16))
        self.manager.window_capture = mock.Mock()
        self.manager.window_capture.capture.return_value = CapturedWindow(
            fake_frame, 1, 4321, "starrail.exe", "崩坏：星穹铁道", 2, 2, "PrintWindow"
        )
        self.addCleanup(self.manager.stop_notification_worker)
        custom_definitions = [
            _definition(
                EXECUTE_ALPHA,
                "e2e-observe-alpha",
                risk="observe_only",
                capability=CAPABILITY_REF,
            ),
            _definition(
                EXECUTE_BETA,
                "e2e-run-beta",
                risk="routine_action",
                capability=CAPABILITY_REF,
            ),
            _definition(
                DEFERRED,
                "e2e-unbound-gamma",
                risk="routine_action",
                capability=CAPABILITY_REF,
            ),
            _definition(
                APPROVAL,
                "e2e-approval",
                risk="approval_required",
                capability=None,
                initial_status="review_required",
            ),
            _definition(
                FORBIDDEN,
                "e2e-forbidden",
                risk="forbidden",
                capability=None,
                initial_status="review_required",
            ),
        ]
        self.store.sync_todo_definitions(
            [
                *[
                    item
                    for item in todo_catalog()
                    if item["game_id"] == GAME_ID
                ],
                *custom_definitions,
            ]
        )
        self.store.reconcile_todo_instances(
            self.manager._todo_instance_candidates([GAME_ID], "daily"),
            intent="reconcile",
            requested_by="test",
            reason="manager-adapter-e2e-custom-definitions",
        )
        self.instances = {
            item.todo_definition_id: item
            for item in self.manager.list_todo_instances(
                game_id=GAME_ID,
                cadence="daily",
                current=True,
                limit=1000,
            )
        }
        for definition_id in (
            EXECUTE_ALPHA,
            EXECUTE_BETA,
            DEFERRED,
            APPROVAL,
            FORBIDDEN,
        ):
            self.assertIn(definition_id, self.instances)

    def tearDown(self) -> None:
        deadline = time.monotonic() + 5
        while self.manager.adapter_host.active_execution() is not None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self.manager.close_attempt_logs()

    def test_promoted_package_survives_compatible_host_build_rotation(self) -> None:
        package_root = (
            self.settings.data_dir / "adapters" / "legacy-night-rain-gamer"
        )
        receipt = json.loads(
            (package_root / "promotion-receipt.json").read_text(encoding="utf-8")
        )
        promotion_host_digest = receipt["hostEntryPointSha256"]

        self.host_entrypoint.write_bytes(b"compatible-test-host-next-build")
        current_host_hash = hashlib.sha256(
            self.host_entrypoint.read_bytes()
        ).hexdigest()
        host_manifest_path = self.host_root / "install-manifest.json"
        host_manifest = json.loads(host_manifest_path.read_text(encoding="utf-8"))
        host_manifest.update(
            {
                "hostVersion": "0.2.1",
                "sha256": current_host_hash,
                "sizeBytes": self.host_entrypoint.stat().st_size,
            }
        )
        host_manifest_path.write_text(
            json.dumps(host_manifest, separators=(",", ":")), encoding="utf-8"
        )

        self.assertNotEqual(promotion_host_digest, "sha256:" + current_host_hash)
        probe = self.manager.adapter_host.probe_game(GAME_ID)
        self.assertTrue(probe["hostManifestVerified"])
        self.assertEqual(probe["hostEntryPointSha256"], current_host_hash)
        self.assertEqual(probe["executionPackage"]["status"], "promoted")
        binding = self.manager.adapter_host.execution_bindings(GAME_ID)
        self.assertTrue(binding["manifestVerified"])
        self.assertEqual(binding["status"], "promoted")

    def test_execution_catalog_version_accepts_mixed_release_metadata(self) -> None:
        first = SimpleNamespace(
            todo_definition_id="todo.v1.starrail.daily.first",
            definition_version=1,
            source_hash="1" * 64,
            catalog_version="2026.08.28.3",
        )
        second = SimpleNamespace(
            todo_definition_id="todo.v1.starrail.daily.second",
            definition_version=2,
            source_hash="2" * 64,
            catalog_version="2026.08.30.3",
        )

        forward = self.manager._execution_catalog_version([first, second])
        reverse = self.manager._execution_catalog_version([second, first])

        self.assertEqual(forward, reverse)
        self.assertRegex(forward, r"^snapshot-sha256\.[0-9a-f]{64}$")

    def test_manager_owned_promotion_binds_fixed_candidate_tests_canary_and_receipt(
        self,
    ) -> None:
        module_root = (
            self.settings.data_dir / "adapters" / "game-modules" / "starrail"
        )
        module_root.mkdir(parents=True)
        runner = module_root / "runner.exe"
        runner.write_bytes(b"fixed-candidate-runner")
        runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
        runner_size = runner.stat().st_size
        payload_digest = "sha256:" + hashlib.sha256(
            f"runner.exe\0{runner_size}\0{runner_hash}\n".encode("utf-8")
        ).hexdigest()
        promoted_root_manifest = json.loads(
            (
                self.settings.data_dir
                / "adapters"
                / "legacy-night-rain-gamer"
                / "install-manifest.json"
            ).read_text(encoding="utf-8")
        )
        candidate = {
            **promoted_root_manifest,
            "buildId": "fixed-candidate-build",
            "files": [
                {
                    "path": "runner.exe",
                    "sha256": runner_hash,
                    "sizeBytes": runner_size,
                }
            ],
            "promotion": {
                "status": "candidate",
                "replaySuiteDigest": "sha256:" + "a" * 64,
                "shadowSuiteDigest": "sha256:" + "b" * 64,
                "canarySuiteDigest": "sha256:" + "0" * 64,
                "payloadDigest": payload_digest,
                "receiptFile": "",
                "receiptSha256": "",
                "receiptResourceId": "",
            },
            "executionReady": False,
        }
        (module_root / "install-manifest.json").write_text(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        evidence_dir = (
            self.settings.data_dir
            / "adapters"
            / "promotion-evidence"
            / "starrail"
        )
        evidence_dir.mkdir(parents=True)
        evidence = {
            "schemaVersion": 1,
            "resourceType": "adapter-candidate-test-evidence",
            "status": "passed",
            "passed": True,
            "packageId": "legacy-night-rain-gamer",
            "packageVersion": "0.1.0",
            "buildId": "fixed-candidate-build",
            "supportedGameIds": [GAME_ID],
            "payloadDigest": payload_digest,
            "replaySuiteDigest": "sha256:" + "a" * 64,
            "shadowSuiteDigest": "sha256:" + "b" * 64,
            "generatedAt": "2026-08-28T00:02:00Z",
            "gameStarted": False,
        }
        (evidence_dir / "candidate-test-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        host_hash = hashlib.sha256(self.host_entrypoint.read_bytes()).hexdigest()
        diagnostic = {
            "schemaVersion": 1,
            "adapterId": "legacy-starrail",
            "gameId": GAME_ID,
            "canaryStatus": "passed",
            "diagnosticOnly": True,
            "hostHealthy": True,
            "hostManifestVerified": True,
            "hostEntryPointSha256": host_hash,
            "adapterProcessStarted": False,
            "gameProcessStarted": False,
        }
        candidate_projection = self.manager.list_adapters()[0]
        self.assertIsNone(candidate_projection.active_version)
        self.assertEqual(candidate_projection.candidate_version, "0.1.0")
        self.assertEqual(candidate_projection.candidate_version_id, "0.1.0")
        self.assertEqual(candidate_projection.implementation_hash, payload_digest)
        governance_request = AdapterGovernanceRequest(
            adapter_id="legacy-starrail",
            target_stage="promoted",
            reason="focused promotion contract test",
            requested_by="contract-test",
        )
        idempotency_key = str(uuid.uuid4())
        expected_state_version = self.store.latest_event_sequence()
        with mock.patch.object(
            self.manager.adapter_host, "canary", return_value=diagnostic
        ) as canary:
            receipt = self.manager.request_adapter_governance(
                "0.1.0",
                "promotion",
                governance_request,
                idempotency_key=idempotency_key,
                request_id=None,
                path="/api/v1/adapter-versions/0.1.0/promotion-requests",
                expected_state_version=expected_state_version,
            )
            replay = self.manager.request_adapter_governance(
                "0.1.0",
                "promotion",
                governance_request,
                idempotency_key=idempotency_key,
                request_id=None,
                path="/api/v1/adapter-versions/0.1.0/promotion-requests",
                expected_state_version=expected_state_version,
            )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.command_id, receipt.command_id)
        canary.assert_called_once()

        result = receipt.result
        self.assertTrue(result["executionReady"])
        self.assertFalse(result["gameStarted"])
        receipt_id = result["promotionReceipt"]["resourceId"]
        ledger = self.store.get_resource("adapter-promotion-receipt", receipt_id)
        on_disk = json.loads(
            (module_root / "promotion-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["document"], on_disk)
        binding = self.manager.adapter_host.execution_bindings(GAME_ID)
        self.assertTrue(binding["manifestVerified"])
        self.assertEqual(binding["status"], "promoted")
        promoted_projection = self.manager.list_adapters()[0]
        self.assertEqual(promoted_projection.active_version, "0.1.0")
        self.assertEqual(promoted_projection.active_version_id, "0.1.0")
        self.assertIsNone(promoted_projection.candidate_version)
        self.assertEqual(promoted_projection.implementation_hash, payload_digest)
        with self.assertRaises(ValueError):
            self.store.update_resource(
                "adapter-promotion-receipt",
                receipt_id,
                state="failed",
                document={**on_disk, "state": "failed"},
            )

    def test_execution_catalog_version_preserves_homogeneous_release(self) -> None:
        item = SimpleNamespace(
            todo_definition_id="todo.v1.starrail.daily.first",
            definition_version=1,
            source_hash="1" * 64,
            catalog_version="2026.08.30.3",
        )

        self.assertEqual(
            self.manager._execution_catalog_version([item]),
            "2026.08.30.3",
        )

    def test_completed_session_reenters_fresh_run_without_rewriting_completion(self) -> None:
        alpha_id = self._todo_id(EXECUTE_ALPHA)
        beta_id = self._todo_id(EXECUTE_BETA)
        self.store.update_config(
            {"daily_todo_selection": {GAME_ID: [EXECUTE_ALPHA, EXECUTE_BETA]}}
        )
        self.store.transition_todo_instance(
            alpha_id,
            status="in_progress",
            reason="fixture session started",
            evidence_refs=[],
            run_id=None,
            increment_attempt=False,
            requested_by="session-reentry-test",
        )
        self.store.transition_todo_instance(
            alpha_id,
            status="completed",
            reason="fixture session completed",
            evidence_refs=["fixture-original-session-evidence"],
            run_id=None,
            increment_attempt=False,
            requested_by="session-reentry-test",
        )
        original = self.store.get_todo_instance(alpha_id)
        runtime = json.loads(
            json.dumps(self.manager.adapter_host.execution_bindings(GAME_ID))
        )
        next(
            binding
            for binding in runtime["bindings"]
            if binding["operation"] == "e2e-observe-alpha"
        )["actionClass"] = "session"

        with mock.patch.object(
            self.manager.adapter_host,
            "execution_bindings",
            return_value=runtime,
        ):
            todo_plan = self.manager._todo_plans_for_games(
                [GAME_ID], "daily"
            )[GAME_ID]
            self.assertEqual(todo_plan["unresolvedRequiredTodoIds"], [beta_id])
            self.assertEqual(todo_plan["sessionReentryTodoInstanceIds"], [alpha_id])
            self.assertEqual(
                todo_plan["executableTodoInstanceIds"], [alpha_id, beta_id]
            )
            self.assertEqual(todo_plan["skippedCompletedCount"], 0)

            run = self.store.create_game_run(
                {
                    "game_id": GAME_ID,
                    "cadence": "daily",
                    "state": "queued",
                    "mode": "execute",
                    "requested_by": "session-reentry-test",
                    "message": "fresh process must re-enter its session",
                    "todo_instance_ids": todo_plan["executableTodoInstanceIds"],
                    "completed_todo_instance_ids": todo_plan[
                        "completedTodoInstanceIds"
                    ],
                    "completion_todo_instance_ids": todo_plan[
                        "completionTodoInstanceIds"
                    ],
                }
            )
            execution_plan = self.manager._prepare_run_attempt(run)

        persisted = self.store.get_run_attempt(execution_plan.run_attempt_id)
        self.assertEqual(
            persisted["plan"]["sessionReentryTodoInstanceIds"], [alpha_id]
        )
        attempt = self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": execution_plan.run_attempt_id,
                "run_id": run["run_id"],
                "todo_instance_id": alpha_id,
                "attempt_number": int(original["attempts"]) + 1,
                "operation": original["operation"],
            }
        )
        after_start = self.store.get_todo_instance(alpha_id)
        for field in ("status", "reason", "evidence_refs", "run_id", "completed_at"):
            self.assertEqual(after_start[field], original[field], field)
        self.assertEqual(after_start["attempts"], int(original["attempts"]) + 1)

        artifact_id = self._ledger_artifact(
            run=run,
            run_attempt_id=execution_plan.run_attempt_id,
            todo_attempt_id=attempt["todo_attempt_id"],
            todo_instance_id=alpha_id,
        )
        terminal = self.store.finish_todo_attempt(
            attempt["todo_attempt_id"],
            status="completed",
            reason_code="fresh_session_ready",
            reason="fresh process reached its usable session",
            retryable=False,
            evidence_refs=[artifact_id],
        )
        self.assertEqual(terminal["state"], "completed")
        after_finish = self.store.get_todo_instance(alpha_id)
        for field in ("status", "reason", "evidence_refs", "run_id", "completed_at"):
            self.assertEqual(after_finish[field], original[field], field)

    @property
    def host_root(self) -> Path:
        return self.settings.data_dir / "adapters" / "manager-adapter-host"

    @property
    def host_entrypoint(self) -> Path:
        return self.host_root / "host.exe"

    def _write_fixed_host_package(self) -> None:
        self.host_root.mkdir(parents=True)
        self.host_entrypoint.write_bytes(b"fixed-test-host-never-directly-executed")
        host_hash = hashlib.sha256(self.host_entrypoint.read_bytes()).hexdigest()
        (self.host_root / "install-manifest.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "packageId": "manager-adapter-host",
                    "hostVersion": "0.2.0",
                    "protocolVersion": "1.1",
                    "entryPoint": "host.exe",
                    "sha256": host_hash,
                    "sizeBytes": self.host_entrypoint.stat().st_size,
                    "hostReady": True,
                    "executionReady": False,
                    "supportedGameIds": [GAME_ID],
                    "installedAt": "2026-08-28T00:00:00Z",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _write_execution_package(self) -> None:
        package_root = (
            self.settings.data_dir / "adapters" / "legacy-night-rain-gamer"
        )
        package_root.mkdir(parents=True)
        runner = package_root / "runner.exe"
        runner.write_bytes(b"fixed-fake-runner-never-started")
        runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
        runner_size = runner.stat().st_size
        payload_digest = "sha256:" + hashlib.sha256(
            f"runner.exe\0{runner_size}\0{runner_hash}\n".encode("utf-8")
        ).hexdigest()
        receipt_id = "77777777-7777-4777-8777-777777777777"
        host_hash = hashlib.sha256(self.host_entrypoint.read_bytes()).hexdigest()
        canary_resource = self.store.create_resource(
            "adapter-diagnostic-canary",
            state="passed",
            document={
                "schemaVersion": 1,
                "adapterId": "legacy-starrail",
                "gameId": GAME_ID,
                "canaryStatus": "passed",
                "diagnosticOnly": True,
                "hostHealthy": True,
                "hostManifestVerified": True,
                "hostEntryPointSha256": host_hash,
                "adapterProcessStarted": False,
                "gameProcessStarted": False,
            },
        )
        public_canary = {
            "resourceId": canary_resource["resource_id"],
            "resourceType": canary_resource["resource_type"],
            "state": canary_resource["state"],
            "document": canary_resource["document"],
            "createdAt": canary_resource["created_at"],
            "updatedAt": canary_resource["updated_at"],
        }
        canary_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                public_canary,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        receipt = {
            "schemaVersion": 1,
            "resourceType": "adapter-promotion-receipt",
            "resourceId": receipt_id,
            "state": "passed",
            "packageId": "legacy-night-rain-gamer",
            "packageVersion": "0.1.0",
            "buildId": "manager-e2e-test",
            "supportedGameIds": [GAME_ID],
            "payloadDigest": payload_digest,
            "replaySuiteDigest": "sha256:" + "1" * 64,
            "shadowSuiteDigest": "sha256:" + "2" * 64,
            "canarySuiteDigest": canary_digest,
            "candidateTestEvidenceSha256": "sha256:" + "4" * 64,
            "managerCanaryEvidenceSha256": canary_digest,
            "hostEntryPointSha256": "sha256:" + host_hash,
            "issuedAt": "2026-08-28T00:01:00Z",
        }
        receipt_raw = json.dumps(
            receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        (package_root / "promotion-receipt.json").write_bytes(receipt_raw)
        receipt_hash = hashlib.sha256(receipt_raw).hexdigest()

        def binding(
            definition_id: str, operation: str, risk: str
        ) -> dict[str, object]:
            return {
                "handlerId": f"test.{operation}",
                "mode": "granular",
                "actionClass": "observation" if risk == "observe_only" else "routine",
                "risk": risk,
                "todoDefinitionIds": [definition_id],
                "adapterCapabilityRefs": [CAPABILITY_REF],
                "supportsResume": True,
                "timeoutSeconds": 60,
                "requiredEvidenceKinds": ["game-ui-task-result"],
            }

        manifest = {
            "schemaVersion": 2,
            "packageId": "legacy-night-rain-gamer",
            "packageVersion": "0.1.0",
            "buildId": "manager-e2e-test",
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
            "supportedGameIds": [GAME_ID],
            "operationBindings": {
                GAME_ID: {
                    "e2e-observe-alpha": binding(
                        EXECUTE_ALPHA, "e2e-observe-alpha", "observe_only"
                    ),
                    "e2e-run-beta": binding(
                        EXECUTE_BETA, "e2e-run-beta", "routine_action"
                    ),
                }
            },
            "forbiddenOperationClasses": sorted(
                REQUIRED_FORBIDDEN_OPERATION_CLASSES
            ),
            "limits": {
                "maxRequestBytes": MAX_REQUEST_BYTES,
                "maxEventBytes": MAX_EVENT_BYTES,
                "maxArtifactsPerTodo": 20,
            },
            "artifactPolicy": {
                "allowedMimeTypes": ["image/png", "text/plain"],
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
                "canarySuiteDigest": canary_digest,
                "payloadDigest": payload_digest,
                "receiptFile": "promotion-receipt.json",
                "receiptSha256": "sha256:" + receipt_hash,
                "receiptResourceId": receipt_id,
            },
            "executionReady": True,
        }
        (package_root / "install-manifest.json").write_text(
            json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
        )
        self.store.create_resource(
            "adapter-promotion-receipt",
            resource_id=receipt_id,
            state="passed",
            document=receipt,
        )

    def _todo_id(self, definition_id: str) -> str:
        return self.instances[definition_id].todo_instance_id

    def _create_run(self, todo_ids: list[str]) -> dict[str, object]:
        return self.store.create_game_run(
            {
                "game_id": GAME_ID,
                "cadence": "daily",
                "state": "queued",
                "mode": "execute",
                "requested_by": "test",
                "message": "Manager Adapter end-to-end test",
                "todo_instance_ids": todo_ids,
            }
        )

    def _create_batch_with_runs(
        self, runs: list[dict[str, object]], *, state: str = "queued"
    ) -> dict[str, object]:
        period_key = self.store.get_todo_instance(
            str(runs[0]["todo_instance_ids"][0])
        )["period_key"]
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": state,
                "game_ids": [GAME_ID],
                "requested_by": "batch-recovery-test",
                "result": {
                    "gameDay": period_key,
                    "candidateGameIds": [GAME_ID],
                    "executableGameIds": [GAME_ID],
                },
            }
        )
        for ordinal, run in enumerate(runs):
            self.store.add_batch_run_membership(
                str(batch["batch_id"]),
                str(run["run_id"]),
                ordinal=ordinal,
                role="initial",
                state="queued",
            )
        return self.store.get_batch(str(batch["batch_id"]))

    def _ledger_artifact(
        self,
        *,
        run: dict[str, object],
        run_attempt_id: str,
        todo_attempt_id: str,
        todo_instance_id: str,
        kind: str = "game-ui-task-result",
    ) -> str:
        artifact_id = str(uuid.uuid4())
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="accepted",
            document={
                "kind": kind,
                "contentType": "image/png",
                "source": "resume-ledger-test",
                "raw": True,
                "gameId": GAME_ID,
                "runId": run["run_id"],
                "runAttemptId": run_attempt_id,
                "todoAttemptId": todo_attempt_id,
                "todoInstanceId": todo_instance_id,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "hash": hashlib.sha256(artifact_id.encode()).hexdigest(),
                "sizeBytes": 1,
                "fileName": f"{artifact_id}.png",
            },
        )
        return artifact_id

    def _fake_host_script(self, body: str) -> Path:
        path = self.root / f"fixed-fake-host-{uuid.uuid4().hex}.py"
        path.write_text(
            "import base64,hashlib,json,os,sys\n"
            "from datetime import datetime,timezone\n"
            "from pathlib import Path\n"
            "request=json.loads(sys.stdin.readline())\n"
            "now=lambda: datetime.now(timezone.utc).isoformat()\n"
            "def emit(event_type,sequence,**fields):\n"
            "    event={'schemaVersion':1,'protocolVersion':'1.1','eventType':event_type,'sequence':sequence,'runId':request['runId'],'runAttemptId':request['runAttemptId'],'fencingToken':request['fencingToken'],'gameId':request['gameId'],'at':now()}\n"
            "    event.update(fields)\n"
            "    print(json.dumps(event,separators=(',',':')),flush=True)\n"
            f"png=base64.b64decode({base64.b64encode(PNG_1X1).decode('ascii')!r})\n"
            + body,
            encoding="utf-8",
        )
        return path

    def _success_body(self, package_digest: str) -> str:
        return (
            "todo=request['todos'][0]\n"
            f"attempt_id={ALPHA_ATTEMPT_ID!r}\n"
            f"artifact_id={ALPHA_ARTIFACT_ID!r}\n"
            "staging=Path(os.environ['YEYU_GAMER_ADAPTER_STAGING_DIR'])\n"
            "evidence=staging/'evidence.png'\n"
            "evidence.write_bytes(png)\n"
            f"emit('hello',0,packageId='legacy-night-rain-gamer',packageVersion='0.1.0',packageDigest={package_digest!r},runnerPid=os.getpid(),acceptedTodoInstanceIds=[todo['todoInstanceId']])\n"
            "emit('todo_attempt_started',1,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,attemptNo=todo['priorAttempts']+1,operation=todo['operation'])\n"
            "emit('artifact_staged',2,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,artifactId=artifact_id,kind='game-ui-task-result',fileName='evidence.png',mimeType='image/png',sizeBytes=len(png),sha256=hashlib.sha256(png).hexdigest(),capturedAt=now())\n"
            "emit('todo_terminal',3,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,status='completed',reasonCode='evidence_confirmed',reason='fresh scoped evidence confirmed',retryable=False,evidenceArtifactIds=[artifact_id])\n"
            "emit('run_terminal',4,status='completed',transportOutcome='clean',attemptedTodoInstanceIds=[todo['todoInstanceId']],completedTodoInstanceIds=[todo['todoInstanceId']],unresolvedTodoInstanceIds=[],terminalEventDigest='sha256:'+'d'*64,exitCode=0)\n"
        )

    def _text_success_body(self, package_digest: str) -> str:
        return (
            "todo=request['todos'][0]\n"
            f"attempt_id={ALPHA_ATTEMPT_ID!r}\n"
            f"artifact_id={ALPHA_ARTIFACT_ID!r}\n"
            "staging=Path(os.environ['YEYU_GAMER_ADAPTER_STAGING_DIR'])\n"
            "evidence=staging/'tool-outcome.txt'\n"
            "payload=b'operation=e2e-observe-alpha; stageState=completed; fixed GUI tool daily completed'\n"
            "evidence.write_bytes(payload)\n"
            f"emit('hello',0,packageId='legacy-night-rain-gamer',packageVersion='0.1.0',packageDigest={package_digest!r},runnerPid=os.getpid(),acceptedTodoInstanceIds=[todo['todoInstanceId']])\n"
            "emit('todo_attempt_started',1,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,attemptNo=todo['priorAttempts']+1,operation=todo['operation'])\n"
            "emit('artifact_staged',2,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,artifactId=artifact_id,kind='tool-log-outcome',fileName='tool-outcome.txt',mimeType='text/plain',sizeBytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),capturedAt=now())\n"
            "emit('todo_terminal',3,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,status='completed',reasonCode='upstream_stage_completed',reason='fixed GUI tool daily completed',retryable=False,evidenceArtifactIds=[artifact_id])\n"
            "emit('run_terminal',4,status='completed',transportOutcome='clean',attemptedTodoInstanceIds=[todo['todoInstanceId']],completedTodoInstanceIds=[todo['todoInstanceId']],unresolvedTodoInstanceIds=[],terminalEventDigest='sha256:'+'d'*64,exitCode=0)\n"
        )

    def _text_failure_body(self, package_digest: str) -> str:
        return (
            "todo=request['todos'][0]\n"
            f"attempt_id={ALPHA_ATTEMPT_ID!r}\n"
            f"artifact_id={ALPHA_ARTIFACT_ID!r}\n"
            "staging=Path(os.environ['YEYU_GAMER_ADAPTER_STAGING_DIR'])\n"
            "evidence=staging/'tool-outcome.txt'\n"
            "payload=b'operation=e2e-observe-alpha; stageState=failed; fixed GUI tool daily failed'\n"
            "evidence.write_bytes(payload)\n"
            f"emit('hello',0,packageId='legacy-night-rain-gamer',packageVersion='0.1.0',packageDigest={package_digest!r},runnerPid=os.getpid(),acceptedTodoInstanceIds=[todo['todoInstanceId']])\n"
            "emit('todo_attempt_started',1,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,attemptNo=todo['priorAttempts']+1,operation=todo['operation'])\n"
            "emit('artifact_staged',2,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,artifactId=artifact_id,kind='tool-log-outcome',fileName='tool-outcome.txt',mimeType='text/plain',sizeBytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),capturedAt=now())\n"
            "emit('todo_terminal',3,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,status='failed',reasonCode='upstream_stage_failed',reason='fixed GUI tool daily failed',retryable=True,evidenceArtifactIds=[artifact_id])\n"
            "emit('run_terminal',4,status='blocked',transportOutcome='clean',attemptedTodoInstanceIds=[todo['todoInstanceId']],completedTodoInstanceIds=[],unresolvedTodoInstanceIds=[todo['todoInstanceId']],terminalEventDigest='sha256:'+'d'*64,exitCode=0)\n"
        )

    def _human_required_body(self, package_digest: str) -> str:
        return (
            "todo=request['todos'][0]\n"
            f"attempt_id={ALPHA_ATTEMPT_ID!r}\n"
            f"emit('hello',0,packageId='legacy-night-rain-gamer',packageVersion='0.1.0',packageDigest={package_digest!r},runnerPid=os.getpid(),acceptedTodoInstanceIds=[todo['todoInstanceId']])\n"
            "emit('todo_attempt_started',1,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,attemptNo=todo['priorAttempts']+1,operation=todo['operation'])\n"
            "emit('todo_terminal',2,todoInstanceId=todo['todoInstanceId'],todoAttemptId=attempt_id,status='human_required',reasonCode='login_required',reason='operator login is required; client preserved',retryable=False,evidenceArtifactIds=[])\n"
            "emit('run_terminal',3,status='human_required',transportOutcome='clean',attemptedTodoInstanceIds=[todo['todoInstanceId']],completedTodoInstanceIds=[],unresolvedTodoInstanceIds=[todo['todoInstanceId']],terminalEventDigest='sha256:'+'d'*64,exitCode=0)\n"
        )

    def _partial_then_missing_terminal_body(self, package_digest: str) -> str:
        return (
            "alpha,beta=request['todos']\n"
            f"alpha_attempt={ALPHA_ATTEMPT_ID!r}\n"
            f"beta_attempt={BETA_ATTEMPT_ID!r}\n"
            f"artifact_id={ALPHA_ARTIFACT_ID!r}\n"
            "staging=Path(os.environ['YEYU_GAMER_ADAPTER_STAGING_DIR'])\n"
            "evidence=staging/'alpha-evidence.png'\n"
            "evidence.write_bytes(png)\n"
            f"emit('hello',0,packageId='legacy-night-rain-gamer',packageVersion='0.1.0',packageDigest={package_digest!r},runnerPid=os.getpid(),acceptedTodoInstanceIds=[alpha['todoInstanceId'],beta['todoInstanceId']])\n"
            "emit('todo_attempt_started',1,todoInstanceId=alpha['todoInstanceId'],todoAttemptId=alpha_attempt,attemptNo=alpha['priorAttempts']+1,operation=alpha['operation'])\n"
            "emit('artifact_staged',2,todoInstanceId=alpha['todoInstanceId'],todoAttemptId=alpha_attempt,artifactId=artifact_id,kind='game-ui-task-result',fileName='alpha-evidence.png',mimeType='image/png',sizeBytes=len(png),sha256=hashlib.sha256(png).hexdigest(),capturedAt=now())\n"
            "emit('todo_terminal',3,todoInstanceId=alpha['todoInstanceId'],todoAttemptId=alpha_attempt,status='completed',reasonCode='evidence_confirmed',reason='fresh scoped evidence confirmed',retryable=False,evidenceArtifactIds=[artifact_id])\n"
            "emit('todo_attempt_started',4,todoInstanceId=beta['todoInstanceId'],todoAttemptId=beta_attempt,attemptNo=beta['priorAttempts']+1,operation=beta['operation'])\n"
            "# Deliberately exit 0 without todo_terminal/run_terminal for beta.\n"
        )

    def _patched_popen(self, script: Path):
        real_popen = subprocess.Popen

        def spawn(command: list[str], **kwargs: object):
            self.assertEqual(Path(command[0]), self.host_entrypoint.resolve())
            self.assertEqual(
                command[1:5],
                ["--operation", "execute", "--protocol-version", "1.1"],
            )
            self.assertIs(kwargs["shell"], False)
            child_environment = kwargs["env"]
            self.assertNotIn(
                "YEYU_GAMER_TRAY_BOOTSTRAP_SECRET", child_environment
            )
            self.assertEqual(
                {
                    key
                    for key in child_environment
                    if str(key).startswith("YEYU_")
                },
                {
                    "YEYU_GAMER_ADAPTER_STAGING_DIR",
                    "YEYU_GAMER_EXECUTION_PACKAGE_ROOT",
                    "YEYU_GAMER_INSTALLATION_BINDING_PATH",
                },
            )
            return real_popen(
                [sys.executable, "-u", str(script), *command[1:]],
                **kwargs,
            )

        return mock.patch(
            "yeyu_gamer_manager.services.adapter_host.subprocess.Popen",
            side_effect=spawn,
        )

    def _start_and_wait(
        self, todo_ids: list[str], script: Path
    ) -> tuple[object, object]:
        run = self._create_run(todo_ids)
        completed: list[object] = []
        signalled = threading.Event()

        def complete(result: object) -> None:
            completed.append(result)
            signalled.set()

        with mock.patch.dict(
            os.environ,
            {"YEYU_GAMER_TRAY_BOOTSTRAP_SECRET": "must-not-cross-host-boundary"},
        ), self._patched_popen(script):
            plan, pid = self.manager._start_game_run(
                str(run["run_id"]), completed_callback=complete
            )
            self.assertGreater(pid, 0)
            self.assertTrue(signalled.wait(10), "fake Host did not finish")
        self.assertEqual(len(completed), 1)
        return plan, completed[0]

    def test_promoted_binding_selects_only_exact_executable_todos(self) -> None:
        plan = self.manager._todo_plans_for_games([GAME_ID], "daily")[GAME_ID]
        alpha = self._todo_id(EXECUTE_ALPHA)
        beta = self._todo_id(EXECUTE_BETA)
        excluded = {
            self._todo_id(DEFERRED),
            self._todo_id(APPROVAL),
            self._todo_id(FORBIDDEN),
        }

        self.assertEqual(plan["executableTodoInstanceIds"], [alpha, beta])
        self.assertTrue(excluded.isdisjoint(plan["executableTodoInstanceIds"]))
        self.assertTrue(excluded.issubset(plan["deferredTodoInstanceIds"]))
        self.assertEqual(
            plan["deferredReasons"][self._todo_id(DEFERRED)]["code"],
            "operation_not_promoted",
        )
        self.assertEqual(
            plan["deferredReasons"][self._todo_id(APPROVAL)]["code"],
            "review_resolution_required",
        )
        self.assertEqual(
            plan["deferredReasons"][self._todo_id(FORBIDDEN)]["code"],
            "forbidden_by_policy",
        )
        self.assertTrue(plan["runtimeBinding"]["manifestVerified"])

    def test_starts_configured_game_before_adapter_host(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        calls: list[str] = []

        def start_game(
            game_id: str,
            game_path: str,
            observer=None,
            *,
            cancel_requested=None,
        ) -> GameLaunchReceipt:
            self.assertEqual(game_id, GAME_ID)
            self.assertEqual(game_path, r"C:\\Games\\StarRail.exe")
            self.assertTrue(callable(observer))
            self.assertTrue(callable(cancel_requested))
            calls.append("game")
            return GameLaunchReceipt("started", 4321, "starrail.exe")

        def start_adapter(*args: object, **kwargs: object) -> int:
            calls.append("adapter")
            return 8765

        self.manager.game_launcher.ensure_started.side_effect = start_game
        self.manager.adapter_host.execute = mock.Mock(side_effect=start_adapter)
        plan, pid = self.manager._start_game_run(str(run["run_id"]))

        self.assertEqual(pid, 8765)
        self.assertEqual(calls, ["game", "adapter"])
        self.manager.game_launcher.close_for_queue.assert_not_called()
        attempt = self.manager.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt.result["gameLaunch"]["state"], "started")

    def _queue_cleanup_fixture(self, candidates: list[str] | None = None):
        self.store.import_legacy(
            config_values={},
            games=[{"game_id": game_id, "display_name": game_id, "order_index": index + 2, "enabled": True, "policy": {}} for index, game_id in enumerate(["WW", "PGR"])],
            source_info={"test": "queue-cleanup-games"}, policy_projection={},
        )
        paths = dict(self.store.get_config()["values"]["game_paths"])
        paths.update({"WW": {"game_path": r"C:\Games\WW\launcher.exe"}, "PGR": {"game_path": r"C:\Games\PGR\PGR.exe"}})
        self.store.update_config({"game_paths": paths})
        run = self._create_run([self._todo_id(EXECUTE_ALPHA)])
        batch = self._create_batch_with_runs([run])
        self.store.update_batch(str(batch["batch_id"]), state="running", result={
            **batch["result"], "candidateGameIds": candidates if candidates is not None else [GAME_ID, "WW"],
        })
        self.manager.game_launcher.close_for_queue.return_value = GameCloseReceipt("closed", (321,), ())
        self.manager.adapter_host.execute = mock.Mock(return_value=8765)
        return run, batch

    def _terminal_cleanup_fixture(self):
        run, batch = self._queue_cleanup_fixture([GAME_ID])
        self.manager.game_launcher.ensure_started.return_value = GameLaunchReceipt(
            "already-running", 4321, "starrail.exe",
        )
        results = []
        plan, _ = self.manager._start_game_run(str(run["run_id"]), completed_callback=results.append)
        callback = self.manager.adapter_host.execute.call_args.args[2]
        result = AdapterRunResult(
            run_id=plan.run_id, run_attempt_id=plan.run_attempt_id, game_id=plan.game_id,
            status="completed", transport_outcome="completed", attempted_todo_instance_ids=(),
            completed_todo_instance_ids=(), unresolved_todo_instance_ids=(),
            exit_code=0, protocol_valid=True, code="fixture_complete", message="tool ended",
        )
        return run, batch, plan, callback, result, results

    def test_terminal_cleanup_closes_preexisting_last_game_using_start_binding(self) -> None:
        run, _, plan, callback, result, results = self._terminal_cleanup_fixture()
        self.store.update_config({"game_paths": {GAME_ID: {"game_path": r"C:\Other\StarRail.exe"}}})
        callback(result)
        self.manager.game_launcher.close_for_queue.assert_called_once()
        call = self.manager.game_launcher.close_for_queue.call_args
        self.assertEqual(call.args, (GAME_ID, r"C:\\Games\\StarRail.exe"))
        self.assertTrue(callable(call.kwargs["cancel_requested"]))
        self.manager.game_launcher.close_started.assert_not_called()
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["result"]["gameCleanup"]["state"], "closed")
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
        self.assertEqual(results[0].status, "completed")

    def test_terminal_cleanup_preserves_durable_human_gate_despite_late_completed_callback(self) -> None:
        run, _, plan, callback, result, results = self._terminal_cleanup_fixture()
        self.store.update_game_run(run["run_id"], state="human_required", message="operator takeover")
        callback(replace(result, protocol_valid=False))
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()
        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt["state"], "human_required")
        self.assertFalse(attempt["result"]["protocolValid"])
        self.assertEqual(attempt["result"]["managerControlOutcome"], "human_required")
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "human_required")
        self.assertEqual(results[0].status, "human_required")

    def test_terminal_cleanup_control_interrupts_are_preserved_not_persistence_failures(self) -> None:
        for control in ["cancelled", "human_required", "sealed"]:
            with self.subTest(control=control):
                run, batch, plan, callback, result, results = self._terminal_cleanup_fixture()
                def close(game_id, game_path, *, cancel_requested):
                    if control == "human_required":
                        self.store.update_game_run(run["run_id"], state="human_required", message="operator takeover")
                    elif control == "sealed":
                        self.store.update_batch(batch["batch_id"], state="cancelled", result={"sealVersion": 1})
                    else:
                        self.store.update_run_attempt(plan.run_attempt_id, state="cancelling", result={}, completed=False)
                    cancel_requested()
                    self.fail("control must interrupt closure")
                self.manager.game_launcher.close_for_queue.side_effect = close
                callback(result)
                expected = "human_required" if control == "human_required" else "cancelled"
                attempt = self.store.get_run_attempt(plan.run_attempt_id)
                self.assertEqual(attempt["state"], expected)
                self.assertEqual(results[0].status, expected)
                self.assertNotEqual(attempt["result"]["code"], "manager_persistence_failed")
                self.manager.game_launcher.close_started.assert_not_called()
                self.manager.game_launcher.close_for_queue.reset_mock(side_effect=True)

    def test_terminal_cleanup_rechecks_takeover_after_close_before_result_commit(self) -> None:
        run, _, plan, callback, result, results = self._terminal_cleanup_fixture()
        finish = self.manager._finish_adapter_result
        def takeover_then_finish(*args, **kwargs):
            self.store.update_game_run(run["run_id"], state="human_required", message="takeover at result handoff")
            return finish(*args, **kwargs)
        self.manager._finish_adapter_result = mock.Mock(side_effect=takeover_then_finish)
        callback(result)
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id)["state"], "human_required")
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "human_required")
        self.assertEqual(results[0].status, "human_required")

    def test_terminal_cleanup_duplicate_completion_cannot_reclose_or_cancel_finished_run(self) -> None:
        run, _, plan, callback, result, results = self._terminal_cleanup_fixture()
        callback(result)
        before = self.store.get_run_attempt(plan.run_attempt_id)
        callback(result)
        self.assertEqual(self.store.get_run_attempt(plan.run_attempt_id), before)
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "review_required")
        self.manager.game_launcher.close_for_queue.assert_called_once()
        self.assertEqual(len(results), 1)

    def test_terminal_cleanup_invalid_adapter_human_claim_does_not_create_manager_authority(self) -> None:
        run, _, plan, callback, result, _ = self._terminal_cleanup_fixture()
        callback(replace(result, status="human_required", protocol_valid=False))
        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt["state"], "failed")
        self.assertNotIn("managerControlOutcome", attempt["result"])
        self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "failed")
        self.manager.game_launcher.close_for_queue.assert_not_called()

    def test_terminal_cleanup_refuses_foreign_frozen_scope_or_inactive_membership(self) -> None:
        for boundary in ["foreign", "inactive", "sealed"]:
            with self.subTest(boundary=boundary):
                run, batch, plan, callback, result, _ = self._terminal_cleanup_fixture()
                if boundary == "foreign":
                    self.store.update_batch(batch["batch_id"], state="running", result={"candidateGameIds": ["WW"]})
                elif boundary == "sealed":
                    self.store.update_batch(batch["batch_id"], state="cancelled", result={"sealVersion": 1})
                else:
                    self.store.update_batch_run_membership(batch["batch_id"], run["run_id"], state="reconciliation_required")
                callback(result)
                self.manager.game_launcher.close_for_queue.assert_not_called()
                self.manager.game_launcher.close_started.assert_not_called()
                self.assertNotEqual(self.store.get_run_attempt(plan.run_attempt_id)["result"]["gameCleanup"]["state"], "closed")

    def test_terminal_cleanup_residuals_or_errors_never_report_success(self) -> None:
        for receipt in [GameCloseReceipt("close-failed", (42,), (42,)), GameCloseReceipt("closed", (), (), (42,)), GameCloseReceipt("closed", (), (), (), (42,)), RuntimeError("fixture close failed")]:
            with self.subTest(receipt=receipt):
                _, _, plan, callback, result, results = self._terminal_cleanup_fixture()
                if isinstance(receipt, Exception):
                    self.manager.game_launcher.close_for_queue.side_effect = receipt
                else:
                    self.manager.game_launcher.close_for_queue.return_value = receipt
                callback(result)
                attempt = self.store.get_run_attempt(plan.run_attempt_id)
                self.assertEqual(attempt["state"], "failed")
                self.assertEqual(attempt["result"]["code"], "game_cleanup_failed")
                self.assertEqual(results[0].status, "failed")
                self.manager.game_launcher.close_started.assert_not_called()
                self.manager.game_launcher.close_for_queue.reset_mock(side_effect=True)

    def test_terminal_cleanup_closes_preexisting_game_when_host_start_fails(self) -> None:
        run, _ = self._queue_cleanup_fixture([GAME_ID])
        self.manager.game_launcher.ensure_started.return_value = GameLaunchReceipt("already-running", 4321, "starrail.exe")
        self.manager.adapter_host.execute.side_effect = RuntimeError("fixture host start failed")
        with self.assertRaisesRegex(RuntimeError, "host start failed"):
            self.manager._start_game_run(run["run_id"])
        self.manager.game_launcher.close_for_queue.assert_called_once()
        self.manager.game_launcher.close_started.assert_not_called()
        attempt = self.store.list_run_attempts(run_id=run["run_id"], limit=1)[0]
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(attempt["result"]["gameCleanup"]["state"], "closed")

    def test_queue_cleanup_uses_frozen_scope_before_game_and_adapter(self) -> None:
        run, batch = self._queue_cleanup_fixture()
        calls = []
        self.manager.game_launcher.close_for_queue.side_effect = lambda *args, **kwargs: (calls.append("close"), GameCloseReceipt("closed", (321,), ()))[1]
        self.manager.game_launcher.ensure_started.side_effect = lambda *args, **kwargs: (calls.append("game"), GameLaunchReceipt("started", 4321, "starrail.exe"))[1]
        self.manager.adapter_host.execute.side_effect = lambda *args, **kwargs: (calls.append("adapter"), 8765)[1]
        plan, _ = self.manager._start_game_run(str(run["run_id"]))
        self.assertEqual(calls, ["close", "game", "adapter"])
        close = self.manager.game_launcher.close_for_queue.call_args
        self.assertEqual(close.args, ("WW", r"C:\Games\WW\launcher.exe"))
        self.assertTrue(callable(close.kwargs["cancel_requested"]))
        report = self.store.get_run_attempt(plan.run_attempt_id)["result"]["queueGameCleanup"]
        self.assertEqual(report["batchId"], batch["batch_id"])
        self.assertEqual(report["candidateGameIds"], [GAME_ID, "WW"])
        self.assertEqual(report["targetGameIds"], ["WW"])
        self.assertEqual(report["games"][0]["requestedProcessIds"], [321])
        self.assertNotIn("PGR", report["targetGameIds"])
        self.assertEqual(report["state"], "closed")

    def test_queue_cleanup_residuals_stop_before_target_without_cleanup_of_human_scene(self) -> None:
        for receipt in [GameCloseReceipt("close-failed", (321,), (321,)), GameCloseReceipt("closed", (), (), (321,))]:
            with self.subTest(receipt=receipt):
                run, _ = self._queue_cleanup_fixture()
                self.manager.game_launcher.close_for_queue.return_value = receipt
                with self.assertRaisesRegex(GameLaunchHumanRequired, "did not close"):
                    self.manager._start_game_run(str(run["run_id"]))
                self.assertEqual(self.store.get_game_run(str(run["run_id"]))["state"], "human_required")
                attempt = self.store.list_run_attempts(run_id=str(run["run_id"]), limit=1)[0]
                self.assertEqual(attempt["result"]["code"], "queue_game_cleanup_incomplete")
                self.assertEqual(attempt["result"]["queueGameCleanup"]["state"], "human_required")
                self.assertEqual(attempt["result"]["queueGameCleanup"]["games"][0], {"gameId": "WW", **receipt.as_result()})
                self.manager.game_launcher.ensure_started.assert_not_called()
                self.manager.game_launcher.close_started.assert_not_called()
                self.manager.adapter_host.execute.assert_not_called()

    def test_queue_cleanup_honors_persisted_batch_cancellation(self) -> None:
        run, batch = self._queue_cleanup_fixture()
        def close(game_id, game_path, *, cancel_requested):
            with mock.patch("yeyu_gamer_manager.services.manager.threading.Thread.start"):
                self.manager.cancel_batch(str(batch["batch_id"]), {"reason": "cancel while closing", "requestedBy": "test"},
                    idempotency_key="cancel-queue-cleanup", request_id=None,
                    path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests", expected_state_version=self.store.latest_event_sequence())
            cancel_requested()
            self.fail("cancellation did not interrupt cleanup")
        self.manager.game_launcher.close_for_queue.side_effect = close
        with self.assertRaises(GameLaunchCancelled):
            self.manager._start_game_run(str(run["run_id"]))
        self.assertEqual(self.store.get_game_run(str(run["run_id"]))["state"], "cancelled")
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_queue_cleanup_preserves_unreleased_human_game_but_not_cancelled_history(self) -> None:
        run, batch = self._queue_cleanup_fixture()
        protected = self.store.create_game_run({"game_id": "WW", "cadence": "daily", "state": "human_required", "mode": "execute", "requested_by": "test", "todo_instance_ids": []})
        owner = self.store.create_batch({"cadence": "daily", "mode": "execute", "state": "human_required", "game_ids": ["WW"], "requested_by": "test", "result": {"candidateGameIds": ["WW"]}})
        self.store.add_batch_run_membership(owner["batch_id"], protected["run_id"], ordinal=0, role="initial", state="terminal")
        with self.assertRaisesRegex(GameLaunchHumanRequired, "unreleased human gate"):
            self.manager._start_game_run(str(run["run_id"]))
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.assertEqual(self.store.get_game_run(protected["run_id"])["state"], "human_required")
        self.store.update_batch(owner["batch_id"], state="cancelled", result={"sealVersion": 1})
        next_run, _ = self._queue_cleanup_fixture()
        self.manager._start_game_run(str(next_run["run_id"]))
        self.manager.game_launcher.close_for_queue.assert_called_once()
        self.manager.game_launcher.ensure_started.assert_called_once()

    def test_queue_cleanup_preserves_an_independent_unreleased_human_game(self) -> None:
        run, _ = self._queue_cleanup_fixture()
        self.store.create_game_run({"game_id": "WW", "cadence": "daily", "state": "human_required", "mode": "execute", "requested_by": "test", "todo_instance_ids": []})
        with self.assertRaisesRegex(GameLaunchHumanRequired, "independent unreleased human gate"):
            self.manager._start_game_run(str(run["run_id"]))
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_queue_cleanup_current_human_takeover_interrupts_before_target(self) -> None:
        run, _ = self._queue_cleanup_fixture()
        def close(game_id, game_path, *, cancel_requested):
            self.manager.create_run_control_request(str(run["run_id"]), "takeover", RunControlRequest(reason="operator taking over", requested_by="test"),
                idempotency_key="takeover-queue-cleanup", request_id=None,
                path=f"/api/v1/game-runs/{run['run_id']}/takeover-requests", expected_state_version=self.store.latest_event_sequence())
            cancel_requested()
            self.fail("human takeover did not interrupt cleanup")
        self.manager.game_launcher.close_for_queue.side_effect = close
        with self.assertRaises(GameLaunchHumanRequired):
            self.manager._start_game_run(str(run["run_id"]))
        self.assertEqual(self.store.get_game_run(str(run["run_id"]))["state"], "human_required")
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_queue_cleanup_unavailable_binding_does_not_skip_other_authorized_games(self) -> None:
        run, _ = self._queue_cleanup_fixture([GAME_ID, "WW", "PGR"])
        paths = dict(self.store.get_config()["values"]["game_paths"])
        paths["PGR"] = {"emulator": {"provider": "ldplayer"}}
        self.store.update_config({"game_paths": paths})
        with self.assertRaisesRegex(GameLaunchHumanRequired, "did not close"):
            self.manager._start_game_run(str(run["run_id"]))
        self.manager.game_launcher.close_for_queue.assert_called_once()
        self.assertEqual(self.manager.game_launcher.close_for_queue.call_args.args[0], "WW")
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_queue_cleanup_finishes_scope_after_residuals_then_blocks_target(self) -> None:
        run, _ = self._queue_cleanup_fixture([GAME_ID, "PGR", "WW"])
        self.manager.game_launcher.close_for_queue.side_effect = [GameCloseReceipt("close-failed", (), (321,)), GameCloseReceipt("closed", (654,), ())]
        with self.assertRaises(GameLaunchHumanRequired):
            self.manager._start_game_run(str(run["run_id"]))
        self.assertEqual([call.args[0] for call in self.manager.game_launcher.close_for_queue.call_args_list], ["PGR", "WW"])
        attempt = self.store.list_run_attempts(run_id=str(run["run_id"]), limit=1)[0]
        self.assertEqual([item["state"] for item in attempt["result"]["queueGameCleanup"]["games"]], ["close-failed", "closed"])
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_zombie_diagnostics_never_reap_or_assert_a_driver_root_cause(self) -> None:
        self.manager.game_launcher.list_zombies.return_value = {321: "starrail.exe"}
        plan = SimpleNamespace(game_id=GAME_ID, run_id="run-diagnostic", run_attempt_id="attempt-diagnostic")
        with self.assertLogs("yeyu_gamer.manager", level="WARNING") as captured:
            self.manager._record_zombie_game_processes(plan)
        self.assertIn("cause=unverified", " ".join(captured.output))
        self.assertNotIn("driver", " ".join(captured.output))
        self.assertNotIn("reboot", " ".join(captured.output))
        self.assertTrue(self.manager.reap_game_zombies([GAME_ID])["readOnly"])
        self.manager.game_launcher.reap_zombies.assert_not_called()

    def test_queue_cleanup_refuses_an_invalid_frozen_scope(self) -> None:
        run, _ = self._queue_cleanup_fixture(["WW"])
        with self.assertRaisesRegex(GameLaunchHumanRequired, "valid frozen game scope"):
            self.manager._start_game_run(str(run["run_id"]))
        self.manager.game_launcher.close_for_queue.assert_not_called()
        self.manager.game_launcher.ensure_started.assert_not_called()
        self.manager.adapter_host.execute.assert_not_called()

    def test_batch_cancel_during_game_launch_stops_before_adapter_and_preserves_client(
        self,
    ) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        batch = self._create_batch_with_runs([run], state="running")
        launch_entered = threading.Event()
        errors: list[BaseException] = []

        def wait_for_cancel(
            game_id: str,
            game_path: str,
            observer=None,
            *,
            cancel_requested=None,
        ) -> GameLaunchReceipt:
            self.assertTrue(callable(cancel_requested))
            launch_entered.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if cancel_requested():
                    raise GameLaunchCancelled("fixture launch cancellation")
                time.sleep(0.01)
            self.fail("launch cancellation did not become visible")

        self.manager.game_launcher.ensure_started.side_effect = wait_for_cancel
        self.manager.adapter_host.execute = mock.Mock()

        def start() -> None:
            try:
                self.manager._start_game_run(str(run["run_id"]))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=start)
        worker.start()
        self.assertTrue(launch_entered.wait(2))
        with mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread.start"
        ):
            receipt = self.manager.cancel_batch(
                str(batch["batch_id"]),
                {"reason": "fixture cancel", "requestedBy": "cancel-test"},
                idempotency_key="cancel-during-launch",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        worker.join(3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], GameLaunchCancelled)
        attempt = self.store.list_run_attempts(
            run_id=str(run["run_id"]), limit=1
        )[0]
        self.assertEqual(attempt["state"], "cancelled")
        self.assertEqual(
            attempt["result"]["cancelDeliveryTarget"], "manager-game-launch"
        )
        self.assertEqual(
            self.store.get_game_run(str(run["run_id"]))["state"], "cancelled"
        )
        self.assertNotIn("Host", receipt.message)
        self.manager.adapter_host.execute.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()

    def _launch_human_required(
        self, *, strategy: str = "continue", with_pending_member: bool = True
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        first = self._create_run([self._todo_id(EXECUTE_ALPHA)])
        runs = [first]
        if with_pending_member:
            runs.append(self._create_run([self._todo_id(EXECUTE_BETA)]))
        batch = self._create_batch_with_runs(runs)
        self.store.update_config({"execution_strategy": strategy})
        self.manager.game_launcher.ensure_started.side_effect = GameLaunchHumanRequired(
            "launcher_login_required", "Official launcher requires human login",
            detail={"surface": "official-launcher"},
            process_ids=frozenset({4321}),
        )
        self.manager.adapter_host.execute = mock.Mock()
        self.manager._run_batch(
            str(batch["batch_id"]),
            [{"runId": run["run_id"], "gameId": GAME_ID} for run in runs],
            str(batch["batch_id"]),
        )
        current = self.store.get_batch(str(batch["batch_id"]))
        self.assertEqual(current["state"], "human_required")
        self.assertNotIn("sealVersion", current["result"])
        self.assertNotIn("completionReviewPhase", current["result"])
        self.assertEqual(
            current["result"]["recoveryPhase"]["humanRequiredRunId"], first["run_id"]
        )
        self.manager.game_launcher.ensure_started.assert_called_once()
        self.manager.adapter_host.execute.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()
        attempt = self.store.list_run_attempts(run_id=str(first["run_id"]), limit=1)[0]
        self.assertEqual(attempt["state"], "human_required")
        self.assertEqual(attempt["result"]["code"], "launcher_login_required")
        self.assertEqual(attempt["result"]["details"], {"surface": "official-launcher"})
        self.assertEqual(attempt["result"]["gameCleanup"], {
            "state": "preserved-human-required", "requestedProcessIds": [],
            "remainingProcessIds": [4321], "zombieProcessIds": [],
            "unverifiedProcessIds": [], "memoryBefore": None, "memoryAfter": None,
        })
        self.assertEqual(
            self.store.get_controller_lease_for_attempt(attempt["run_attempt_id"])["state"],
            "revoked",
        )
        for ordinal, run in enumerate(runs):
            membership = self.store.get_batch_run_membership(
                str(batch["batch_id"]), str(run["run_id"])
            )
            self.assertEqual(membership["state"], "terminal" if ordinal == 0 else "queued")
            self.assertEqual(
                membership["terminal_outcome"], "human_required" if ordinal == 0 else None
            )
            current_run = self.store.get_game_run(str(run["run_id"]))
            self.assertEqual(current_run["state"], "human_required" if ordinal == 0 else "queued")
            self.assertEqual(current_run["completed_todo_instance_ids"], [])
            for todo_id in run["todo_instance_ids"]:
                self.assertEqual(self.store.get_todo_instance(todo_id)["status"], "pending")
                self.assertEqual(self.store.list_todo_attempts(todo_instance_id=todo_id), [])
            if ordinal:
                self.assertEqual(self.store.list_run_attempts(run_id=str(run["run_id"])), [])
        return current, runs

    def test_launch_human_required_pauses_continue_queue_before_adapter(self) -> None:
        self._launch_human_required()

    def test_launch_human_required_pauses_stop_queue_before_adapter(self) -> None:
        self._launch_human_required(strategy="stop")

    def test_launch_human_required_requires_release_then_resumes_same_run(self) -> None:
        batch, runs = self._launch_human_required()
        run_id = str(runs[0]["run_id"])
        predecessor = self.store.list_run_attempts(run_id=run_id, limit=1)[0]
        request = RunControlRequest(reason="operator handled launcher", requested_by="test")
        with self.assertRaisesRegex(ManagerConflict, "explicit_human_release"):
            self.manager.create_run_control_request(
                run_id, "resume", request,
                idempotency_key="launch-gate-no-release", request_id=None,
                path=f"/api/v1/game-runs/{run_id}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        with self.assertRaisesRegex(ManagerConflict, "same-GameRun resume first"):
            self.manager.resume_batch(
                str(batch["batch_id"]),
                BatchResumeRequest(reason="cannot skip launcher", requested_by="test"),
                idempotency_key="launch-gate-no-bypass", request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.manager.create_run_control_request(
            run_id, "release-takeover", request,
            idempotency_key="launch-gate-release", request_id=None,
            path=f"/api/v1/game-runs/{run_id}/takeover-release-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        self.manager.game_launcher.ensure_started.assert_called_once()
        self.manager.adapter_host.execute.assert_not_called()
        released_actions = self.manager.get_batch(str(batch["batch_id"])).result["batchActionAvailability"]
        self.assertFalse(released_actions["humanTakeover"])
        self.assertTrue(released_actions["runResume"])
        released_snapshot = self.manager.snapshot()
        self.assertEqual(released_snapshot.active_batch["batchId"], batch["batch_id"])
        self.assertEqual(released_snapshot.active_batch["state"], "human_required")
        self.assertTrue(released_snapshot.active_batch["result"]["batchActionAvailability"]["runResume"])
        # Reopening the Manager uses durable batch/member facts, not an in-memory active flag.
        self.assertTrue(self.manager._current_human_batch(self.store.get_batch(str(batch["batch_id"]))))
        released_run = self.store.get_game_run(run_id)
        self.assertEqual(released_run["state"], "review_required")
        game_record = next(item for item in self.store.list_games() if item["game_id"] == GAME_ID)
        released_game = self.manager._game_projection(game_record, recent_runs=[released_run])
        self.assertEqual(released_game.acceptance_state, "not_started")
        self.manager.game_launcher.ensure_started.side_effect = None
        self.manager.adapter_host.execute.return_value = 8765
        receipt = self.manager.create_run_control_request(
            run_id, "resume", request,
            idempotency_key="launch-gate-resume", request_id=None,
            path=f"/api/v1/game-runs/{run_id}/resume-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        self.assertTrue(receipt.result["executionRequested"])
        self.assertEqual(receipt.result["targetBatchId"], batch["batch_id"])
        self.assertEqual(receipt.result["sourceCurrentAttemptId"], predecessor["run_attempt_id"])
        attempts = self.store.list_run_attempts(run_id=run_id, limit=10)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["attempt_ordinal"], 2)
        self.assertEqual(attempts[0]["run_id"], run_id)
        self.assertEqual(self.manager.game_launcher.ensure_started.call_count, 2)
        self.manager.adapter_host.execute.assert_called_once()
        self.manager.game_launcher.close_started.assert_not_called()
        self.assertEqual(self.store.list_run_attempts(run_id=str(runs[1]["run_id"])), [])

    def test_launch_human_gate_is_current_snapshot_owner_and_blocks_new_batch(self) -> None:
        batch, runs = self._launch_human_required(with_pending_member=False)
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot.active_batch["batchId"], batch["batch_id"])
        actions = snapshot.active_batch["result"]["batchActionAvailability"]
        self.assertTrue(actions["humanTakeover"])
        self.assertEqual(actions["humanTakeoverTargets"], [{"runId": runs[0]["run_id"], "gameId": GAME_ID}])
        self.assertFalse(actions["runResume"])
        self.assertEqual(actions["runResumeTargets"], [])
        self.assertFalse(actions["resume"])
        self.assertFalse(actions["review"])
        self.assertEqual(actions["nextAction"], "release_human_takeover")
        game = next(game for game in snapshot.games if game.game_id == GAME_ID)
        self.assertEqual(game.runtime_state, "human_required")
        self.assertEqual(game.review_state, "human_required")
        self.assertEqual(game.acceptance_state, "not_started")
        before = self.store.list_batches(100)
        with mock.patch("yeyu_gamer_manager.services.manager.threading.Thread") as worker:
            with self.assertRaisesRegex(ManagerConflict, "explicit_human_release_required"):
                self.manager.create_batch(
                    BatchCreateRequest(cadence="daily", mode="execute", game_ids=[GAME_ID], requested_by="test"),
                    idempotency_key="human-gate-new-batch-denied", request_id=None,
                    path="/api/v1/batches", expected_state_version=self.store.latest_event_sequence(),
                )
            worker.assert_not_called()
        self.assertEqual(len(self.store.list_batches(100)), len(before))
        self.manager.game_launcher.ensure_started.assert_called_once()

    def test_human_batch_projection_excludes_expired_scope_and_sealed_history(self) -> None:
        batch, runs = self._launch_human_required()
        current = self.store.get_batch(str(batch["batch_id"]))
        self.assertTrue(self.manager._current_human_batch(current))
        self.assertFalse(self.manager._current_human_batch({
            **current, "result": {**current["result"], "sealVersion": 1},
        }))
        todo = self.store.get_todo_instance(runs[0]["todo_instance_ids"][0])
        after_end = datetime.fromisoformat(todo["period_ends_at"]) + timedelta(seconds=1)
        with mock.patch("yeyu_gamer_manager.services.manager.utc_now", return_value=after_end):
            self.assertFalse(self.manager._current_human_batch(current))
            self.assertIsNone(self.manager.snapshot().active_batch)

    def test_same_run_launch_human_gate_again_does_not_seal_batch(self) -> None:
        batch, runs = self._launch_human_required(with_pending_member=False)
        run_id = str(runs[0]["run_id"])
        request = RunControlRequest(reason="operator handled launcher", requested_by="test")
        self.manager.create_run_control_request(
            run_id, "release-takeover", request,
            idempotency_key="launch-gate-again-release", request_id=None,
            path=f"/api/v1/game-runs/{run_id}/takeover-release-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        self.manager.create_run_control_request(
            run_id, "resume", request,
            idempotency_key="launch-gate-again-resume", request_id=None,
            path=f"/api/v1/game-runs/{run_id}/resume-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        current = self.store.get_batch(str(batch["batch_id"]))
        self.assertEqual(current["state"], "human_required")
        self.assertNotIn("sealVersion", current["result"])
        self.assertNotIn("completionReviewPhase", current["result"])
        self.assertEqual(current["result"]["recoveryPhase"]["humanRequiredRunId"], run_id)
        self.assertEqual(len(self.store.list_run_attempts(run_id=run_id)), 2)
        self.manager.adapter_host.execute.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()

    def test_launch_human_observation_requires_scoped_capture(self) -> None:
        run = self._create_run([self._todo_id(EXECUTE_ALPHA)])
        plan = self.manager._prepare_run_attempt(run)
        self.manager.window_capture.capture_processes.return_value = (
            self.manager.window_capture.capture.return_value
        )
        for process_ids in (frozenset(), frozenset({4321})):
            with self.subTest(process_ids=process_ids):
                self.manager.window_capture.capture_processes.reset_mock()
                self.manager._record_launch_observation(plan, LaunchObservation(
                    game_id=GAME_ID, phase="launch-human-required", elapsed_seconds=4,
                    process_names=frozenset({"launcher.exe"}), process_ids=process_ids,
                    detail={"reasonCode": "launcher_login_required"},
                ))
                payload = [
                    event["payload"] for event in self.store.list_events(0, 5000)
                    if event["event_type"] == "game-launch.phase"
                ][-1]
                if process_ids:
                    self.assertEqual(
                        self.manager.window_capture.capture_processes.call_args.kwargs["allowed_pids"],
                        process_ids,
                    )
                    self.assertIsNone(payload["captureError"])
                    artifact = self.store.get_resource("artifact", payload["artifactId"])
                    self.assertEqual(artifact["document"]["kind"], "game-ui-launch-human-required")
                else:
                    self.manager.window_capture.capture_processes.assert_not_called()
                    self.assertIsNone(payload["artifactId"])
                    self.assertIn("no verified process IDs", payload["captureError"])

    def test_ordinary_launch_error_remains_failed(self) -> None:
        run = self._create_run([self._todo_id(EXECUTE_ALPHA)])
        self.manager.game_launcher.ensure_started.side_effect = GameLaunchError("fixture failed")
        self.manager.adapter_host.execute = mock.Mock()
        with self.assertRaises(GameLaunchError):
            self.manager._start_game_run(str(run["run_id"]))
        attempt = self.store.list_run_attempts(run_id=str(run["run_id"]), limit=1)[0]
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(attempt["result"]["code"], "game_start_failed")
        self.assertEqual(self.store.get_game_run(str(run["run_id"]))["state"], "failed")
        self.manager.adapter_host.execute.assert_not_called()

    def test_ww_launcher_waiting_without_verified_pids_cannot_widen_capture(self) -> None:
        run = self._create_run([self._todo_id(EXECUTE_ALPHA)])
        plan = self.manager._prepare_run_attempt(run)
        self.manager._record_launch_observation(plan, LaunchObservation(
            game_id="WW", phase="launcher-waiting", elapsed_seconds=4,
            process_names=frozenset({"launcher.exe", "launcher_main.exe"}),
            process_ids=frozenset(), detail={"waiting": "official-launcher"},
        ))
        self.manager.window_capture.capture_processes.assert_not_called()
        payload = [
            event["payload"] for event in self.store.list_events(0, 5000)
            if event["event_type"] == "game-launch.phase"
        ][-1]
        self.assertIsNone(payload["artifactId"])
        self.assertIn("no verified process IDs", payload["captureError"])

    def test_cancel_handoff_waits_until_started_adapter_can_receive_signal(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        batch = self._create_batch_with_runs([run], state="running")
        adapter_dispatch_entered = threading.Event()
        release_adapter_dispatch = threading.Event()
        start_errors: list[BaseException] = []

        def start_adapter(*args: object, **kwargs: object) -> int:
            adapter_dispatch_entered.set()
            self.assertTrue(release_adapter_dispatch.wait(3))
            return 8765

        self.manager.adapter_host.execute = mock.Mock(side_effect=start_adapter)

        def start() -> None:
            try:
                self.manager._start_game_run(str(run["run_id"]))
            except BaseException as error:
                start_errors.append(error)

        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(adapter_dispatch_entered.wait(2))
        attempt = self.store.list_run_attempts(
            run_id=str(run["run_id"]), limit=1
        )[0]
        with mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread.start"
        ):
            receipt = self.manager.cancel_batch(
                str(batch["batch_id"]),
                {"reason": "handoff race", "requestedBy": "cancel-test"},
                idempotency_key="cancel-at-adapter-handoff",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.manager.adapter_host.cancel_run = mock.Mock(
            return_value=(str(attempt["run_attempt_id"]), True)
        )
        delivery = threading.Thread(
            target=self.manager._deliver_batch_cancellation,
            args=(str(batch["batch_id"]), receipt),
        )
        delivery.start()
        time.sleep(0.05)
        self.manager.adapter_host.cancel_run.assert_not_called()
        release_adapter_dispatch.set()
        starter.join(3)
        delivery.join(3)

        self.assertFalse(starter.is_alive())
        self.assertFalse(delivery.is_alive())
        self.assertEqual(start_errors, [])
        self.manager.adapter_host.cancel_run.assert_called_once_with(
            run_id=str(run["run_id"]), reason_code="batch_cancel_requested"
        )
        latest = self.store.get_run_attempt(str(attempt["run_attempt_id"]))
        self.assertEqual(latest["state"], "cancelling")
        self.assertEqual(latest["result"]["launchState"], "fixed-host-started")
        self.assertEqual(latest["result"]["cancelDeliveryTarget"], "adapter-host")

    def test_game_run_cancel_targets_manager_launch_gate_before_adapter(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"launchState": "starting-game-client"},
            completed=False,
        )
        self.store.update_game_run(
            str(run["run_id"]), state="running", message="launcher wait"
        )
        self.manager.adapter_host.cancel_run = mock.Mock()

        receipt = self.manager.cancel_game_run(
            str(run["run_id"]),
            {"reason": "operator cancel", "requestedBy": "cancel-test"},
            idempotency_key="cancel-game-run-during-launch",
            request_id=None,
            path=f"/api/v1/game-runs/{run['run_id']}/cancel-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )

        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt["state"], "cancelling")
        self.assertEqual(
            attempt["result"]["cancelDeliveryTarget"], "manager-game-launch"
        )
        self.assertEqual(receipt.result["gameRun"]["state"], "cancelling")
        command = self.store.get_command_receipt(str(receipt.command_id))
        self.assertIn("Manager game-launch gate", command["message"])
        self.manager.adapter_host.cancel_run.assert_not_called()

    def test_cooperative_cancel_preserves_manager_started_game(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        callbacks: list[object] = []

        self.manager.game_launcher.ensure_started.return_value = GameLaunchReceipt(
            "started", 4321, "starrail.exe"
        )

        def start_adapter(
            run_id: str, game_id: str, completed, *, plan, on_event,
            installation_binding=None, transcript=None,
        ) -> int:
            callbacks.append(completed)
            return 8765

        self.manager.adapter_host.execute = mock.Mock(side_effect=start_adapter)
        plan, _ = self.manager._start_game_run(str(run["run_id"]))
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="cancelling",
            result={"cancelReason": "fixture_cancel"},
            completed=False,
        )
        callback = callbacks[0]
        callback(
            AdapterRunResult(
                run_id=plan.run_id,
                run_attempt_id=plan.run_attempt_id,
                game_id=plan.game_id,
                status="cancelled",
                transport_outcome="cancelled",
                attempted_todo_instance_ids=(alpha,),
                completed_todo_instance_ids=(),
                unresolved_todo_instance_ids=(alpha,),
                exit_code=0,
                protocol_valid=True,
                code="cancelled_by_manager",
                message="fixed Host acknowledged cooperative cancellation",
            )
        )

        attempt = self.manager.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(
            attempt.result["gameCleanup"]["state"],
            "preserved-cooperative-cancel",
        )
        self.manager.game_launcher.close_started.assert_not_called()

    def test_jsonl_completion_persists_attempt_events_artifact_and_todo(self) -> None:
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        alpha = self._todo_id(EXECUTE_ALPHA)
        script = self._fake_host_script(self._success_body(str(package_digest)))
        plan, result = self._start_and_wait([alpha], script)

        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(plan.executable_todo_instance_ids, (alpha,))

        run_attempt = self.manager.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(run_attempt.state, "completed")
        self.assertEqual(run_attempt.executable_todo_instance_ids, [alpha])
        self.assertEqual(run_attempt.exit_code, 0)
        self.assertEqual(run_attempt.result["gameCleanup"]["state"], "closed")
        self.manager.game_launcher.close_started.assert_called_once()
        self.assertNotIn("fencingToken", run_attempt.model_dump(by_alias=True))
        self.assertNotIn("fencingTokenHash", run_attempt.model_dump(by_alias=True))
        self.assertNotIn("cancelAuthority", run_attempt.model_dump(by_alias=True))
        self.assertNotIn("cancelAuthorityHash", run_attempt.model_dump(by_alias=True))
        database_dump = "\n".join(self.store.connection.iterdump())
        self.assertNotIn(plan.fencing_token, database_dump)
        self.assertNotIn(plan.cancel_authority, database_dump)

        todo_attempts = self.manager.list_todo_attempts(todo_instance_id=alpha)
        self.assertEqual(len(todo_attempts), 1)
        self.assertEqual(todo_attempts[0].todo_attempt_id, ALPHA_ATTEMPT_ID)
        self.assertEqual(todo_attempts[0].state, "completed")
        self.assertIn(ALPHA_ARTIFACT_ID, todo_attempts[0].evidence_refs)
        evidence_documents = [
            self.store.get_resource("artifact", artifact_id)["document"]
            for artifact_id in todo_attempts[0].evidence_refs
        ]
        self.assertEqual(
            {document["kind"] for document in evidence_documents},
            {
                "game-ui-task-result",
                "game-ui-step-before-raw",
                "game-ui-step-after-watermarked",
            },
        )
        self.assertTrue(
            all(
                document["todoAttemptId"] == ALPHA_ATTEMPT_ID
                and document["todoInstanceId"] == alpha
                for document in evidence_documents
            )
        )

        events = self.manager.list_adapter_events(plan.run_attempt_id)
        self.assertEqual(
            [event.event_type for event in events],
            [
                "hello",
                "todo_attempt_started",
                "artifact_staged",
                "todo_terminal",
                "run_terminal",
            ],
        )
        self.assertTrue(
            all(
                "fencingToken" not in event.payload
                and "cancelAuthority" not in event.payload
                for event in events
            )
        )

        artifact = self.store.get_resource("artifact", ALPHA_ARTIFACT_ID)
        self.assertEqual(artifact["document"]["runAttemptId"], plan.run_attempt_id)
        self.assertEqual(artifact["document"]["todoAttemptId"], ALPHA_ATTEMPT_ID)
        self.assertEqual(artifact["document"]["todoInstanceId"], alpha)
        self.assertNotIn("evidence.png", artifact["document"]["relativePath"])
        self.assertEqual(
            (
                self.settings.data_dir
                / "artifacts"
                / artifact["document"]["relativePath"]
            ).read_bytes(),
            PNG_1X1,
        )
        self.assertEqual(self.manager.get_todo_instance(alpha).status, "completed")
        self.assertEqual(
            self.store.get_game_run(plan.run_id)["state"],
            "review_required",
            "Todo evidence does not by itself grant accepted_done",
        )

    def test_text_tool_outcome_artifact_completes_without_callback_error(self) -> None:
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        alpha = self._todo_id(EXECUTE_ALPHA)
        script = self._fake_host_script(self._text_success_body(str(package_digest)))

        plan, result = self._start_and_wait([alpha], script)

        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "completed")
        artifact = self.store.get_resource("artifact", ALPHA_ARTIFACT_ID)
        self.assertEqual(artifact["document"]["kind"], "tool-log-outcome")
        self.assertEqual(artifact["document"]["contentType"], "text/plain")
        self.assertEqual(
            [event.event_type for event in self.manager.list_adapter_events(plan.run_attempt_id)],
            [
                "hello",
                "todo_attempt_started",
                "artifact_staged",
                "todo_terminal",
                "run_terminal",
            ],
        )

    def test_adapter_failed_terminal_maps_to_blocked_without_callback_error(
        self,
    ) -> None:
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        alpha = self._todo_id(EXECUTE_ALPHA)
        script = self._fake_host_script(self._text_failure_body(str(package_digest)))

        plan, result = self._start_and_wait([alpha], script)

        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.exit_code, 0)
        todo_attempt = self.manager.list_todo_attempts(todo_instance_id=alpha)[0]
        self.assertEqual(todo_attempt.state, "blocked")
        self.assertEqual(todo_attempt.reason_code, "upstream_stage_failed")
        self.assertTrue(todo_attempt.retryable)
        self.assertIn(ALPHA_ARTIFACT_ID, todo_attempt.evidence_refs)
        evidence_documents = [
            self.store.get_resource("artifact", artifact_id)["document"]
            for artifact_id in todo_attempt.evidence_refs
        ]
        self.assertEqual(
            {document["kind"] for document in evidence_documents},
            {
                "tool-log-outcome",
                "game-ui-step-before-raw",
                "game-ui-step-after-watermarked",
            },
        )
        self.assertTrue(
            all(
                document["todoAttemptId"] == ALPHA_ATTEMPT_ID
                and document["todoInstanceId"] == alpha
                for document in evidence_documents
            )
        )
        terminal_event = self.manager.list_adapter_events(plan.run_attempt_id)[3]
        self.assertEqual(terminal_event.event_type, "todo_terminal")
        self.assertEqual(terminal_event.payload["status"], "failed")
        self.assertNotIn("event_callback_failed", result.message)

    def test_human_required_preserves_gate_and_cannot_auto_retry(self) -> None:
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        alpha = self._todo_id(EXECUTE_ALPHA)
        script = self._fake_host_script(
            self._human_required_body(str(package_digest))
        )
        plan, result = self._start_and_wait([alpha], script)

        self.assertTrue(result.protocol_valid)
        self.assertEqual(result.status, "human_required")
        self.assertEqual(result.unresolved_todo_instance_ids, (alpha,))
        run_attempt = self.manager.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(run_attempt.state, "human_required")
        todo_attempt = self.manager.list_todo_attempts(todo_instance_id=alpha)[0]
        self.assertEqual(todo_attempt.state, "human_required")
        self.assertFalse(todo_attempt.retryable)
        self.assertEqual(self.manager.get_todo_instance(alpha).status, "human_required")

        game_run = self.store.get_game_run(plan.run_id)
        self.assertEqual(game_run["state"], "human_required")
        self.assertIn("game client is preserved", game_run["message"])
        self.assertIn("automatic retry is forbidden", game_run["message"])

        resume = self.manager._todo_plans_for_games([GAME_ID], "daily")[GAME_ID]
        self.assertNotIn(alpha, resume["executableTodoInstanceIds"])
        self.assertIn(alpha, resume["humanRequiredTodoInstanceIds"])
        self.assertEqual(
            resume["deferredReasons"][alpha]["code"],
            "human_resolution_required",
        )
        summary = self.manager.todo_summary(GAME_ID, "daily")
        self.assertGreaterEqual(summary["counts"]["human_required"], 1)

    def test_resume_creates_one_successor_for_only_eligible_subset(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([alpha, beta])
        predecessor = self.manager._prepare_run_attempt(run)
        alpha_attempt = self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": predecessor.run_attempt_id,
                "run_id": predecessor.run_id,
                "todo_instance_id": alpha,
                "attempt_number": 1,
                "operation": "e2e-observe-alpha",
            }
        )
        evidence = self._ledger_artifact(
            run=run,
            run_attempt_id=predecessor.run_attempt_id,
            todo_attempt_id=alpha_attempt["todo_attempt_id"],
            todo_instance_id=alpha,
        )
        self.store.finish_todo_attempt(
            alpha_attempt["todo_attempt_id"],
            status="completed",
            reason_code="fixture_completed",
            reason="predecessor completed alpha",
            retryable=False,
            evidence_refs=[evidence],
        )
        self.store.update_run_attempt(
            predecessor.run_attempt_id, state="partial", completed=True
        )
        self.store.transition_controller_lease_for_attempt(
            predecessor.run_attempt_id,
            state="released",
            reason_code="partial_terminal",
            reason="fixture predecessor ended",
        )
        self.store.update_game_run(
            predecessor.run_id,
            state="review_required",
            completed_todo_instance_ids=[alpha],
            message="fixture ready for same-run resume",
        )
        expected_version = self.store.latest_event_sequence()
        request = RunControlRequest(
            reason="resume only beta", requested_by="resume-test"
        )
        with mock.patch.object(
            self.manager.adapter_host, "execute", return_value=43210
        ) as execute:
            receipt = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                request,
                idempotency_key="eligible-subset-successor",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=expected_version,
            )
            replay = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                request,
                idempotency_key="eligible-subset-successor",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=expected_version,
            )

        self.assertTrue(receipt.result["executionRequested"])
        self.assertEqual(receipt.result["eligibleTodoInstanceIds"], [beta])
        self.assertEqual(receipt.result["skippedTodoInstanceIds"], [alpha])
        self.assertTrue(replay.replayed)
        self.assertEqual(execute.call_count, 1)
        attempts = self.store.list_run_attempts(
            run_id=predecessor.run_id, limit=10
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            attempts[0]["plan"]["executableTodoInstanceIds"], [beta]
        )
        self.assertEqual(attempts[0]["attempt_ordinal"], 2)

    def test_sealed_blocked_resume_creates_one_continuation_and_review_barrier(
        self,
    ) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([alpha, beta])
        predecessor = self.manager._prepare_run_attempt(run)
        alpha_attempt = self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": predecessor.run_attempt_id,
                "run_id": predecessor.run_id,
                "todo_instance_id": alpha,
                "attempt_number": 1,
                "operation": "e2e-observe-alpha",
            }
        )
        evidence = self._ledger_artifact(
            run=run,
            run_attempt_id=predecessor.run_attempt_id,
            todo_attempt_id=alpha_attempt["todo_attempt_id"],
            todo_instance_id=alpha,
        )
        self.store.finish_todo_attempt(
            alpha_attempt["todo_attempt_id"],
            status="completed",
            reason_code="fixture_completed",
            reason="predecessor completed alpha",
            retryable=False,
            evidence_refs=[evidence],
        )
        self.store.update_run_attempt(
            predecessor.run_attempt_id, state="partial", completed=True
        )
        self.store.transition_controller_lease_for_attempt(
            predecessor.run_attempt_id,
            state="released",
            reason_code="partial_terminal",
            reason="fixture predecessor ended",
        )
        self.store.update_game_run(
            predecessor.run_id,
            state="review_required",
            completed_todo_instance_ids=[alpha],
            message="fixture ready for continuation",
        )
        root = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "blocked",
                "game_ids": [GAME_ID],
                "requested_by": "continuation-test",
                "result": {
                    "gameDay": self.store.get_todo_instance(beta)["period_key"]
                },
            }
        )
        self.store.add_batch_run_membership(
            root["batch_id"],
            predecessor.run_id,
            ordinal=0,
            role="initial",
            state="terminal",
        )
        sealed_root = self.store.seal_batch(
            root["batch_id"],
            state="blocked",
            result={
                "gameDay": self.store.get_todo_instance(beta)["period_key"],
                "notificationOutcome": "blocked",
                "notificationBlockers": [
                    {
                        "gameId": GAME_ID,
                        "kind": "fixture",
                        "reason": "beta remains",
                        "nextAction": "resume beta",
                        "screenshotUnavailableReason": "fixture has no screenshot",
                    }
                ],
                "todoSnapshot": {},
                "sealEvidenceArtifactIds": [],
                "finalGameRunIds": [predecessor.run_id],
            },
        )
        frozen_root = json.loads(json.dumps(sealed_root, sort_keys=True))

        def execute_side_effect(
            run_id: str,
            game_id: str,
            completed,
            *,
            plan,
            on_event,
            installation_binding,
            transcript,
        ) -> int:
            completed(
                AdapterRunResult(
                    run_id=run_id,
                    run_attempt_id=plan.run_attempt_id,
                    game_id=game_id,
                    status="completed",
                    transport_outcome="clean",
                    attempted_todo_instance_ids=(beta,),
                    completed_todo_instance_ids=(beta,),
                    unresolved_todo_instance_ids=(),
                    exit_code=0,
                    protocol_valid=True,
                    code="fixture_completed",
                    message="successor terminal",
                )
            )
            return 43210

        expected_version = self.store.latest_event_sequence()
        request = RunControlRequest(
            reason="resume sealed root", requested_by="resume-test"
        )
        with mock.patch.object(
            self.manager.adapter_host,
            "execute",
            side_effect=execute_side_effect,
        ) as execute:
            receipt = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                request,
                idempotency_key="sealed-continuation-successor",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=expected_version,
            )
            replay = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                request,
                idempotency_key="sealed-continuation-successor",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=expected_version,
            )

        continuation = self.store.get_batch(
            receipt.result["continuationBatchId"]
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(continuation["root_batch_id"], root["batch_id"])
        self.assertEqual(
            continuation["predecessor_batch_id"], root["batch_id"]
        )
        self.assertEqual(continuation["continuation_ordinal"], 1)
        self.assertEqual(
            continuation["result"]["batchLineage"]["runIds"],
            [predecessor.run_id],
        )
        self.assertIn("completionReviewPhase", continuation["result"])
        # The fixture's current attempt is still the failed predecessor
        # attempt, so no Agent review could turn it into accepted_done.  Such
        # a run must not hold the review barrier (and the round mail); the
        # continuation seals immediately from the machine adjudication.
        self.assertEqual(
            continuation["result"]["completionReviewPhase"]["status"],
            "completed",
        )
        self.assertIsNotNone(continuation["result"].get("sealVersion"))
        self.assertFalse(continuation["result"]["acceptedDone"])
        self.assertEqual(
            continuation["result"]["notificationOutcome"], "blocked"
        )
        self.assertEqual(
            json.loads(json.dumps(self.store.get_batch(root["batch_id"]), sort_keys=True)),
            frozen_root,
        )

    def test_queued_batch_cancel_request_seals_without_starting_host(self) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "queued",
                "game_ids": [GAME_ID],
                "requested_by": "cancel-test",
                "result": {
                    "gameDay": self.store.get_todo_instance(beta)["period_key"],
                    "candidateGameIds": [GAME_ID],
                },
            }
        )
        self.store.add_batch_run_membership(
            batch["batch_id"],
            run["run_id"],
            ordinal=0,
            role="initial",
            state="queued",
        )
        receipt = self.manager.cancel_batch(
            batch["batch_id"],
            {"reason": "fixture cancel", "requestedBy": "cancel-test"},
            idempotency_key="queued-batch-cancel",
            request_id=None,
            path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        sealed = self.store.get_batch(batch["batch_id"])
        cancel_request = self.store.get_batch_cancel_request(
            receipt.result["cancelRequestId"]
        )
        self.assertEqual(sealed["state"], "cancelled")
        self.assertEqual(receipt.state, "succeeded")
        self.assertEqual(sealed["result"]["notificationOutcome"], "blocked")
        self.assertEqual(cancel_request["state"], "sealed")
        self.assertEqual(
            sealed["run_memberships"][0]["state"], "cancelled"
        )
        self.assertEqual(sealed["result"]["finalGameRunIds"], [])
        self.assertEqual(
            sealed["result"]["allCandidateRunIds"], [run["run_id"]]
        )
        self.assertEqual(
            sealed["result"]["notStartedRunIds"], [run["run_id"]]
        )

    def test_running_batch_cancellation_retries_and_persists_delivery_attempts(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="running")
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"code": "fixture_running"},
            completed=False,
        )
        self.store.update_game_run(
            str(run["run_id"]), state="running", message="fixture running"
        )
        self.store.update_batch_run_membership(
            str(batch["batch_id"]),
            str(run["run_id"]),
            state="active",
            latest_run_attempt_id=plan.run_attempt_id,
        )
        with mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread.start"
        ):
            receipt = self.manager.cancel_batch(
                str(batch["batch_id"]),
                {"reason": "fixture cancel", "requestedBy": "cancel-test"},
                idempotency_key="running-batch-cancel-retry",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.manager.adapter_host.cancel_run = mock.Mock(
            side_effect=[
                (plan.run_attempt_id, False),
                (plan.run_attempt_id, False),
                (plan.run_attempt_id, True),
            ]
        )
        with mock.patch.object(
            self.manager, "BATCH_CANCEL_RETRY_DELAYS_SECONDS", (0.0, 0.0)
        ):
            self.manager._deliver_batch_cancellation(
                str(batch["batch_id"]), receipt
            )

        cancel_request = self.store.get_batch_cancel_request(
            str(receipt.result["cancelRequestId"])
        )
        self.assertEqual(cancel_request["state"], "signal_delivered")
        self.assertEqual(cancel_request["delivery_attempt_count"], 3)
        self.assertIsNotNone(cancel_request["last_delivery_at"])
        self.assertIsNone(cancel_request["next_retry_at"])
        self.assertEqual(cancel_request["last_error_class"], "")
        self.assertEqual(self.manager.adapter_host.cancel_run.call_count, 3)

    def test_startup_recovery_replays_a_persisted_cancel_authority(self) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="running")
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"code": "fixture_running"},
            completed=False,
        )
        self.store.update_game_run(
            str(run["run_id"]), state="running", message="fixture running"
        )
        self.store.update_batch_run_membership(
            str(batch["batch_id"]),
            str(run["run_id"]),
            state="active",
            latest_run_attempt_id=plan.run_attempt_id,
        )
        authority_path = self.manager.adapter_host._write_control_authority(plan)
        with mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread.start"
        ):
            self.manager.cancel_batch(
                str(batch["batch_id"]),
                {"reason": "fixture cancel", "requestedBy": "cancel-test"},
                idempotency_key="persisted-restart-cancel",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

        self.assertEqual(self.manager._recover_active_batches(), 1)
        pending = (
            self.settings.data_dir
            / "adapter-controls"
            / f"{plan.run_attempt_id}.cancel.json"
        )
        self.assertTrue(pending.is_file())
        persisted = pending.read_text(encoding="utf-8")
        self.assertNotIn(plan.cancel_authority, persisted)
        self.assertNotIn(plan.fencing_token, persisted)
        self.manager.adapter_host._cleanup_attempt_controls(plan)
        self.assertFalse(authority_path.exists())

    def test_startup_recovery_marks_interrupted_game_launch_review_required(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="running")
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"launchState": "starting-game-client"},
            completed=False,
        )
        self.store.update_game_run(
            str(run["run_id"]), state="running", message="launcher wait"
        )
        self.manager.adapter_host.cancel_persisted_attempt = mock.Mock()

        self.assertEqual(self.manager._recover_active_batches(), 1)

        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt["state"], "review_required")
        self.assertEqual(
            attempt["result"]["code"], "manager_restart_during_game_launch"
        )
        self.assertEqual(
            attempt["result"]["gameCleanup"]["state"],
            "preserved-restart-reconciliation",
        )
        self.assertEqual(
            self.store.get_game_run(str(run["run_id"]))["state"],
            "review_required",
        )
        recovered_batch = self.store.get_batch(str(batch["batch_id"]))
        self.assertEqual(recovered_batch["state"], "review_required")
        self.manager.adapter_host.cancel_persisted_attempt.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()

    def test_startup_recovery_seals_durable_cancel_before_adapter_dispatch(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="running")
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="running",
            result={"launchState": "starting-game-client"},
            completed=False,
        )
        self.store.update_game_run(
            str(run["run_id"]), state="running", message="launcher wait"
        )
        with mock.patch(
            "yeyu_gamer_manager.services.manager.threading.Thread.start"
        ):
            receipt = self.manager.cancel_batch(
                str(batch["batch_id"]),
                {"reason": "restart cancel", "requestedBy": "cancel-test"},
                idempotency_key="restart-cancel-during-launch",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/cancel-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        self.manager.adapter_host.cancel_persisted_attempt = mock.Mock()

        self.assertEqual(self.manager._recover_active_batches(), 1)

        attempt = self.store.get_run_attempt(plan.run_attempt_id)
        self.assertEqual(attempt["state"], "cancelled")
        self.assertEqual(
            attempt["result"]["code"],
            "manager_restart_cancelled_before_adapter",
        )
        self.assertEqual(
            attempt["result"]["cancelDeliveryTarget"], "manager-game-launch"
        )
        recovered_batch = self.store.get_batch(str(batch["batch_id"]))
        self.assertEqual(recovered_batch["state"], "cancelled")
        cancel_request = self.store.get_batch_cancel_request(
            str(receipt.result["cancelRequestId"])
        )
        self.assertEqual(cancel_request["state"], "sealed")
        cancel_command = self.store.get_command_receipt(
            str(receipt.result["cancelRequestId"])
        )
        self.assertEqual(cancel_command["state"], "succeeded")
        self.manager.adapter_host.cancel_persisted_attempt.assert_not_called()
        self.manager.game_launcher.close_started.assert_not_called()

    def test_unknown_without_receipt_or_checkpoint_is_reconcile_only(self) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        predecessor = self.manager._prepare_run_attempt(run)
        self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": predecessor.run_attempt_id,
                "run_id": predecessor.run_id,
                "todo_instance_id": beta,
                "attempt_number": 1,
                "operation": "e2e-run-beta",
            }
        )
        self.store.update_run_attempt(
            predecessor.run_attempt_id,
            state="failed",
            result={"code": "outcome_unknown_transport"},
            completed=True,
        )
        self.store.transition_controller_lease_for_attempt(
            predecessor.run_attempt_id,
            state="revoked",
            reason_code="outcome_unknown_transport",
            reason="fixture transport outcome is unknown",
        )
        self.store.update_game_run(
            predecessor.run_id,
            state="failed",
            message="fixture outcome unknown",
        )
        with self.assertRaisesRegex(ManagerConflict, "run_revision_mismatch"):
            self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                RunControlRequest(
                    reason="stale entity revision",
                    requested_by="resume-test",
                    expected_run_revision=0,
                ),
                idempotency_key="stale-resume-revision",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        with mock.patch.object(self.manager.adapter_host, "execute") as execute:
            receipt = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                RunControlRequest(
                    reason="must reconcile unknown", requested_by="resume-test"
                ),
                idempotency_key="unknown-reconcile-only",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

        self.assertFalse(receipt.result["executionRequested"])
        self.assertEqual(receipt.result["eligibleTodoInstanceIds"], [])
        self.assertEqual(receipt.result["deferredTodoInstanceIds"], [beta])
        self.assertIn(beta, receipt.result["decision"]["reconcileUnknown"])
        self.assertIsNotNone(receipt.result["reconcileWorkItemId"])
        execute.assert_not_called()

        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "review_required",
                "game_ids": [GAME_ID],
                "requested_by": "resume-test",
                "result": {
                    "gameDay": self.store.get_todo_instance(beta)["period_key"],
                    "recoveryPhase": {
                        "schemaVersion": 1,
                        "status": "ready_for_resume",
                        "affectedRunIds": [run["run_id"]],
                    },
                },
            }
        )
        self.store.add_batch_run_membership(
            batch["batch_id"],
            run["run_id"],
            ordinal=0,
            role="initial",
            state="terminal",
        )
        self.store.update_batch_run_membership(
            batch["batch_id"],
            run["run_id"],
            state="terminal",
            latest_run_attempt_id=predecessor.run_attempt_id,
            terminal_outcome="failed",
        )
        actions = self.manager.get_batch(batch["batch_id"]).result[
            "batchActionAvailability"
        ]
        self.assertFalse(actions["runResume"])
        self.assertFalse(actions["runReconcile"])
        self.assertTrue(actions["cancel"])
        self.assertEqual(
            actions["nextAction"], "cancel_old_batch_then_start_fresh"
        )
        self.assertEqual(
            actions["reasonCode"], "same_run_resume_unavailable_start_fresh"
        )

    def test_human_release_resolves_blocker_but_still_requires_reobserve(self) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        predecessor = self.manager._prepare_run_attempt(run)
        todo_attempt = self.store.start_todo_attempt(
            {
                "todo_attempt_id": str(uuid.uuid4()),
                "run_attempt_id": predecessor.run_attempt_id,
                "run_id": predecessor.run_id,
                "todo_instance_id": alpha,
                "attempt_number": 1,
                "operation": "e2e-observe-alpha",
            }
        )
        evidence = self._ledger_artifact(
            run=run,
            run_attempt_id=predecessor.run_attempt_id,
            todo_attempt_id=todo_attempt["todo_attempt_id"],
            todo_instance_id=alpha,
        )
        todo_attempt = self.store.finish_todo_attempt(
            todo_attempt["todo_attempt_id"],
            status="human_required",
            reason_code="login_required",
            reason="operator login is required",
            retryable=False,
            evidence_refs=[evidence],
        )
        persisted = self.manager._persist_terminal_todo_blocker(todo_attempt)
        self.assertIn(persisted["status"], {"created", "reused_active"})
        self.store.update_run_attempt(
            predecessor.run_attempt_id, state="human_required", completed=True
        )
        self.store.transition_controller_lease_for_attempt(
            predecessor.run_attempt_id,
            state="revoked",
            reason_code="adapter_human_required",
            reason="fixture human gate",
        )
        self.store.update_game_run(
            predecessor.run_id,
            state="human_required",
            message="fixture human gate",
        )

        self.manager.create_run_control_request(
            predecessor.run_id,
            "release-takeover",
            RunControlRequest(
                reason="operator explicitly released takeover",
                requested_by="operator",
            ),
            idempotency_key="human-release-explicit",
            request_id=None,
            path=f"/api/v1/game-runs/{predecessor.run_id}/takeover-release-requests",
            expected_state_version=self.store.latest_event_sequence(),
        )
        with mock.patch.object(self.manager.adapter_host, "execute") as execute:
            resume = self.manager.create_run_control_request(
                predecessor.run_id,
                "resume",
                RunControlRequest(
                    reason="reobserve after release", requested_by="operator"
                ),
                idempotency_key="human-release-reobserve",
                request_id=None,
                path=f"/api/v1/game-runs/{predecessor.run_id}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

        blocker = self.store.list_todo_blockers(
            run_id=predecessor.run_id, limit=10
        )[0]
        self.assertEqual(blocker["state"], "resolved")
        self.assertTrue(blocker["release_explicit"])
        self.assertFalse(resume.result["executionRequested"])
        self.assertIn("fresh_observation", resume.result["requirements"])
        self.assertIn(alpha, resume.result["decision"]["reconcileUnknown"])
        execute.assert_not_called()

    def test_adapter_event_public_values_cannot_copy_the_plan_fencing_token(
        self,
    ) -> None:
        alpha = self._todo_id(EXECUTE_ALPHA)
        run = self._create_run([alpha])
        plan = self.manager._prepare_run_attempt(run)
        todo_attempt_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        artifact_policy = {
            "allowed_artifact_mime_types": tuple(
                sorted(ALLOWED_ARTIFACT_MIME_TYPES)
            ),
            "max_artifact_bytes": MAX_ARTIFACT_BYTES,
            "max_artifacts_per_todo": MAX_ARTIFACTS_PER_TODO,
        }
        self.manager._handle_adapter_event(
            plan,
            AdapterEvent(
                event_type="todo_attempt_started",
                sequence=0,
                document={
                    "fencingToken": plan.fencing_token,
                    "todoInstanceId": alpha,
                    "todoAttemptId": todo_attempt_id,
                    "attemptNo": 1,
                    "operation": "e2e-observe-alpha",
                },
                **artifact_policy,
            ),
        )
        events_before = self.manager.list_adapter_events(plan.run_attempt_id)
        todo_before = self.manager.get_todo_instance(alpha).model_dump(mode="json")
        attempt_before = self.manager.list_todo_attempts(
            todo_instance_id=alpha
        )[0].model_dump(mode="json")
        run_before = self.store.get_game_run(run["run_id"])
        self.assertEqual(run_before["state"], "running")
        self.assertEqual(run_before["message"], "正在执行：e2e-observe-alpha")

        malicious_events = (
            AdapterEvent(
                event_type="todo_progress",
                sequence=1,
                document={
                    "fencingToken": plan.fencing_token,
                    "todoInstanceId": alpha,
                    "todoAttemptId": todo_attempt_id,
                    "code": "progress",
                    "message": f"copied::{plan.fencing_token}",
                },
                **artifact_policy,
            ),
            AdapterEvent(
                event_type="todo_terminal",
                sequence=1,
                document={
                    "fencingToken": plan.fencing_token,
                    "todoInstanceId": alpha,
                    "todoAttemptId": todo_attempt_id,
                    "status": "blocked",
                    "reasonCode": "blocked_by_test",
                    "reason": f"copied::{plan.fencing_token}",
                    "retryable": True,
                    "evidenceArtifactIds": [],
                },
                **artifact_policy,
            ),
        )
        for event in malicious_events:
            with self.subTest(event_type=event.event_type):
                with self.assertRaises(PublicFencingMaterialRejected) as captured:
                    self.manager._handle_adapter_event(plan, event)
                self.assertNotIn(plan.fencing_token, str(captured.exception))
                self.assertEqual(
                    self.manager.list_adapter_events(plan.run_attempt_id),
                    events_before,
                )
                self.assertEqual(
                    self.manager.get_todo_instance(alpha).model_dump(mode="json"),
                    todo_before,
                )
                self.assertEqual(
                    self.manager.list_todo_attempts(
                        todo_instance_id=alpha
                    )[0].model_dump(mode="json"),
                    attempt_before,
                )

    def test_missing_terminal_defers_unknown_todo_and_resume_skips_done(self) -> None:
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        alpha = self._todo_id(EXECUTE_ALPHA)
        beta = self._todo_id(EXECUTE_BETA)
        script = self._fake_host_script(
            self._partial_then_missing_terminal_body(str(package_digest))
        )
        plan, result = self._start_and_wait([alpha, beta], script)

        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.protocol_valid)
        self.assertEqual(result.code, "missing_run_terminal")
        self.assertEqual(result.completed_todo_instance_ids, (alpha,))
        self.assertEqual(result.unresolved_todo_instance_ids, (beta,))
        self.assertEqual(self.manager.get_run_attempt(plan.run_attempt_id).state, "failed")

        alpha_attempt = self.manager.list_todo_attempts(todo_instance_id=alpha)[0]
        beta_attempt = self.manager.list_todo_attempts(todo_instance_id=beta)[0]
        self.assertEqual(alpha_attempt.state, "completed")
        self.assertFalse(alpha_attempt.retryable)
        self.assertEqual(beta_attempt.state, "blocked")
        self.assertFalse(beta_attempt.retryable)
        self.assertEqual(self.manager.get_todo_instance(alpha).status, "completed")
        self.assertEqual(self.manager.get_todo_instance(beta).status, "blocked")

        resume = self.manager._todo_plans_for_games([GAME_ID], "daily")[GAME_ID]
        self.assertEqual(resume["executableTodoInstanceIds"], [beta])
        self.assertEqual(resume["completedTodoInstanceIds"], [alpha])
        self.assertEqual(resume["blockedTodoInstanceIds"], [beta])
        self.assertNotIn(beta, resume["deferredReasons"])
        self.assertNotIn(alpha, resume["todoInstanceIds"])

    def test_new_batch_can_fresh_start_prior_blocked_routine_todo(self) -> None:
        """A selected retry is a new run, never an unsafe same-run resume."""

        beta = self._todo_id(EXECUTE_BETA)
        old_run = self._create_run([beta])
        self.store.transition_todo_instance(
            beta,
            status="blocked",
            reason="previous Manager transport ended before the tool could report",
            evidence_refs=[],
            run_id=str(old_run["run_id"]),
            increment_attempt=False,
            requested_by="test",
        )
        fresh_run = self._create_run([beta])
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        script = self._fake_host_script(self._success_body(str(package_digest)))
        completed: list[object] = []
        signalled = threading.Event()

        with self._patched_popen(script):
            plan, pid = self.manager._start_game_run(
                str(fresh_run["run_id"]),
                completed_callback=lambda result: (completed.append(result), signalled.set()),
            )
            self.assertGreater(pid, 0)
            self.assertTrue(signalled.wait(10), "fake Host did not finish")

        self.assertEqual([target.todo_instance_id for target in plan.todos], [beta])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "completed")
        self.assertEqual(self.manager.get_todo_instance(beta).status, "completed")

    def test_missing_batch_callback_fences_active_attempt_before_completion_review(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run])

        with mock.patch.object(
            self.manager.adapter_host, "execute", return_value=43210
        ), mock.patch.object(
            self.manager.adapter_host, "cancel", return_value=False
        ), mock.patch.object(threading.Event, "wait", return_value=False):
            self.manager._run_batch(
                str(batch["batch_id"]),
                [{"runId": run["run_id"], "gameId": GAME_ID}],
                str(batch["batch_id"]),
            )

        recovered = self.store.get_batch(str(batch["batch_id"]))
        membership = recovered["run_memberships"][0]
        self.assertEqual(membership["state"], "reconciliation_required")
        self.assertEqual(
            recovered["result"]["recoveryPhase"]["status"],
            "awaiting_reconciliation",
        )
        self.assertNotIn("completionReviewPhase", recovered["result"])
        work_item_id = recovered["result"]["recoveryPhase"]["workItemId"]
        work_item = self.manager.get_work_item(work_item_id)
        self.assertEqual(work_item.state, "review_required")
        self.assertEqual(
            work_item.result["batchRecoveryScope"]["affectedRunIds"],
            [run["run_id"]],
        )

    def test_stop_strategy_freezes_unstarted_members_outside_completion_review(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        first = self._create_run([beta])
        second = self._create_run([beta])
        batch = self._create_batch_with_runs([first, second])
        self.store.update_config({"execution_strategy": "stop"})

        def blocked_result(
            run_id,
            game_id,
            completed,
            *,
            plan,
            on_event,
            installation_binding,
            transcript,
        ):
            completed(
                AdapterRunResult(
                    run_id=run_id,
                    run_attempt_id=plan.run_attempt_id,
                    game_id=game_id,
                    status="blocked",
                    transport_outcome="clean",
                    attempted_todo_instance_ids=(beta,),
                    completed_todo_instance_ids=(),
                    unresolved_todo_instance_ids=(beta,),
                    exit_code=20,
                    protocol_valid=True,
                    code="fixture_blocked",
                    message="stop after first member",
                )
            )
            return 43210

        with mock.patch.object(
            self.manager.adapter_host, "execute", side_effect=blocked_result
        ) as execute:
            self.manager._run_batch(
                str(batch["batch_id"]),
                [
                    {"runId": first["run_id"], "gameId": GAME_ID},
                    {"runId": second["run_id"], "gameId": GAME_ID},
                ],
                str(batch["batch_id"]),
            )

        current = self.store.get_batch(str(batch["batch_id"]))
        memberships = {
            item["run_id"]: item for item in current["run_memberships"]
        }
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(
            memberships[second["run_id"]]["terminal_outcome"],
            "not_started_stop_strategy",
        )
        self.assertEqual(
            current["result"]["finalGameRunIds"], [first["run_id"]]
        )
        self.assertEqual(
            current["result"]["allCandidateRunIds"],
            [first["run_id"], second["run_id"]],
        )
        self.assertEqual(
            current["result"]["notStartedRunIds"], [second["run_id"]]
        )

    def test_default_daily_strategy_continues_after_a_failed_game(self) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        first = self._create_run([beta])
        second = self._create_run([beta])
        batch = self._create_batch_with_runs([first, second])

        def failed_result(
            run_id,
            game_id,
            completed,
            *,
            plan,
            on_event,
            installation_binding,
            transcript,
        ):
            completed(
                AdapterRunResult(
                    run_id=run_id,
                    run_attempt_id=plan.run_attempt_id,
                    game_id=game_id,
                    status="failed",
                    transport_outcome="failed",
                    attempted_todo_instance_ids=(beta,),
                    completed_todo_instance_ids=(),
                    unresolved_todo_instance_ids=(beta,),
                    exit_code=70,
                    protocol_valid=False,
                    code="fixture_failed",
                    message="continue to the next selected game",
                )
            )
            return 43210

        with mock.patch.object(
            self.manager.adapter_host, "execute", side_effect=failed_result
        ) as execute:
            self.manager._run_batch(
                str(batch["batch_id"]),
                [
                    {"runId": first["run_id"], "gameId": GAME_ID},
                    {"runId": second["run_id"], "gameId": GAME_ID},
                ],
                str(batch["batch_id"]),
            )

        current = self.store.get_batch(str(batch["batch_id"]))
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(current["result"].get("notStartedRunIds", []), [])
        self.assertEqual(
            set(current["result"]["failedRunIds"]),
            {first["run_id"], second["run_id"]},
        )

    def _assert_batch_pauses_at_human_gate(
        self, *, strategy: str, with_pending_member: bool = True
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        alpha = self._todo_id(EXECUTE_ALPHA)
        first = self._create_run([alpha])
        runs = [first]
        if with_pending_member:
            runs.append(self._create_run([self._todo_id(EXECUTE_BETA)]))
        batch = self._create_batch_with_runs(runs)
        self.store.update_config({"execution_strategy": strategy})
        package_digest = self.manager.adapter_host.execution_bindings(GAME_ID)[
            "packageDigest"
        ]
        script = self._fake_host_script(
            self._human_required_body(str(package_digest))
        )

        with self._patched_popen(script):
            self.manager._run_batch(
                str(batch["batch_id"]),
                [
                    {"runId": run["run_id"], "gameId": GAME_ID}
                    for run in runs
                ],
                str(batch["batch_id"]),
            )

        current = self.store.get_batch(str(batch["batch_id"]))
        memberships = {
            item["run_id"]: item for item in current["run_memberships"]
        }
        self.assertEqual(current["state"], "human_required")
        self.assertEqual(current["result"]["currentGameId"], GAME_ID)
        self.assertNotIn("sealVersion", current["result"])
        self.assertNotIn("completionReviewPhase", current["result"])
        self.assertEqual(
            current["result"]["recoveryPhase"]["status"], "ready_for_resume"
        )
        self.assertEqual(
            current["result"]["recoveryPhase"]["humanRequiredRunId"],
            first["run_id"],
        )
        self.assertEqual(
            current["result"]["recoveryPhase"]["affectedRunIds"],
            [run["run_id"] for run in runs],
        )
        self.assertEqual(memberships[first["run_id"]]["state"], "terminal")
        self.assertEqual(
            memberships[first["run_id"]]["terminal_outcome"], "human_required"
        )
        self.assertEqual(self.manager.get_todo_instance(alpha).status, "human_required")
        self.manager.game_launcher.ensure_started.assert_called_once()
        self.manager.game_launcher.close_started.assert_not_called()
        for run in runs[1:]:
            self.assertEqual(memberships[run["run_id"]]["state"], "queued")
            self.assertIsNone(memberships[run["run_id"]]["terminal_outcome"])
            self.assertEqual(self.store.get_game_run(run["run_id"])["state"], "queued")
            self.assertEqual(self.store.list_run_attempts(run_id=run["run_id"]), [])
        return current, runs

    def test_human_gate_pauses_default_queue_without_bypassing_resume_lineage(self) -> None:
        batch, runs = self._assert_batch_pauses_at_human_gate(strategy="continue")
        first, second = runs
        self.assertFalse(
            self.manager.get_batch(batch["batch_id"]).result[
                "batchActionAvailability"
            ]["resume"]
        )
        with self.assertRaisesRegex(ManagerConflict, "same-GameRun resume first"):
            self.manager.resume_batch(
                batch["batch_id"],
                BatchResumeRequest(reason="cannot bypass login", requested_by="test"),
                idempotency_key="human-gate-bypass-denied",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )

        # The same-run successor contract is tested separately. Simulate its
        # successful terminal membership; only the never-started member may now
        # be queued, retaining the original Batch and GameRun identities.
        self.store.update_batch_run_membership(
            batch["batch_id"], first["run_id"],
            state="terminal", terminal_outcome="completed",
        )
        with mock.patch("yeyu_gamer_manager.services.manager.threading.Thread") as thread:
            self.manager.resume_batch(
                batch["batch_id"],
                BatchResumeRequest(reason="resume pending queue", requested_by="test"),
                idempotency_key="human-gate-pending-resume",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )
        dispatched_runs = thread.call_args.kwargs["args"][1]
        self.assertEqual([run["runId"] for run in dispatched_runs], [second["run_id"]])
        self.assertEqual(
            self.store.get_batch(batch["batch_id"])["member_run_ids"],
            [first["run_id"], second["run_id"]],
        )

    def test_human_gate_preserves_pending_members_under_stop_strategy(self) -> None:
        self._assert_batch_pauses_at_human_gate(strategy="stop")

    def test_human_gate_on_last_member_does_not_seal_batch(self) -> None:
        self._assert_batch_pauses_at_human_gate(
            strategy="continue", with_pending_member=False
        )

    def test_typed_batch_resume_is_idempotent_and_preserves_game_run(self) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="review_required")
        self.store.update_batch(
            str(batch["batch_id"]),
            state="review_required",
            result={
                **dict(batch["result"]),
                "recoveryPhase": {
                    "schemaVersion": 1,
                    "status": "ready_for_resume",
                    "affectedRunIds": [run["run_id"]],
                },
            },
        )
        request = BatchResumeRequest(
            reason="resume never-started member", requested_by="resume-test"
        )
        projected = self.manager.get_batch(str(batch["batch_id"]))
        self.assertTrue(
            projected.result["batchActionAvailability"]["resume"]
        )
        self.assertEqual(
            projected.result["batchActionAvailability"]["nextAction"],
            "resume_batch",
        )
        state_version = self.store.latest_event_sequence()
        with mock.patch("yeyu_gamer_manager.services.manager.threading.Thread") as thread:
            receipt = self.manager.resume_batch(
                str(batch["batch_id"]),
                request,
                idempotency_key="typed-batch-resume",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=state_version,
            )
            replay = self.manager.resume_batch(
                str(batch["batch_id"]),
                request,
                idempotency_key="typed-batch-resume",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=state_version,
            )

        current = self.store.get_batch(str(batch["batch_id"]))
        self.assertTrue(replay.replayed)
        self.assertEqual(thread.call_count, 1)
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(
            receipt.result,
            {
                "batchId": str(batch["batch_id"]),
                "resumeRequestId": str(receipt.command_id),
            },
        )
        self.assertEqual(replay.result, receipt.result)
        dispatched_runs = thread.call_args.kwargs["args"][1]
        self.assertEqual(
            [item["runId"] for item in dispatched_runs], [run["run_id"]]
        )
        self.assertEqual(current["member_run_ids"], [run["run_id"]])
        self.assertEqual(
            current["result"]["recoveryPhase"]["status"],
            "resume_dispatch_pending",
        )
        self.manager._complete_command(
            str(receipt.command_id), "failed", "resume contract terminal"
        )
        stored_receipt = self.store.get_command_receipt(str(receipt.command_id))
        self.assertEqual(stored_receipt["result"], receipt.result)
        receipt_events = [
            event
            for event in self.store.list_events(0, 5000)
            if event["event_type"] == "command.receipt"
            and event["entity_id"] == str(receipt.command_id)
        ]
        self.assertEqual(
            [event["payload"]["state"] for event in receipt_events],
            ["accepted", "failed"],
        )
        for event in receipt_events:
            self.assertEqual(event["payload"]["result"], receipt.result)

    def test_batch_resume_requires_same_run_successor_for_terminal_failure(
        self,
    ) -> None:
        beta = self._todo_id(EXECUTE_BETA)
        run = self._create_run([beta])
        batch = self._create_batch_with_runs([run], state="review_required")
        plan = self.manager._prepare_run_attempt(run)
        self.store.update_run_attempt(
            plan.run_attempt_id,
            state="failed",
            result={"code": "fixture_terminal_failure"},
            completed=True,
        )
        self.store.transition_controller_lease_for_attempt(
            plan.run_attempt_id,
            state="revoked",
            reason_code="fixture_terminal_failure",
            reason="fixture terminal failure",
        )
        self.store.update_game_run(
            str(run["run_id"]),
            state="failed",
            message="fixture terminal failure",
        )
        self.store.update_batch_run_membership(
            str(batch["batch_id"]),
            str(run["run_id"]),
            state="terminal",
            latest_run_attempt_id=plan.run_attempt_id,
            terminal_outcome="failed",
        )
        current = self.store.get_batch(str(batch["batch_id"]))
        self.store.update_batch(
            str(batch["batch_id"]),
            state="review_required",
            result={
                **dict(current["result"]),
                "recoveryPhase": {
                    "schemaVersion": 1,
                    "status": "ready_for_resume",
                    "affectedRunIds": [run["run_id"]],
                },
            },
        )

        projected = self.manager.get_batch(str(batch["batch_id"]))
        self.assertFalse(
            projected.result["batchActionAvailability"]["resume"]
        )
        self.assertEqual(
            projected.result["batchActionAvailability"]["nextAction"],
            "resume_terminal_game_runs",
        )
        self.assertTrue(
            projected.result["batchActionAvailability"]["runResume"]
        )
        self.assertEqual(
            projected.result["batchActionAvailability"]["runResumeTargets"],
            [
                {
                    "runId": run["run_id"],
                    "gameId": run["game_id"],
                    "requestPath": (
                        f"/game-runs/{run['run_id']}/resume-requests"
                    ),
                    "expectedRunRevision": self.store.entity_revision(
                        "game-run", str(run["run_id"])
                    ),
                    "expectedCurrentAttemptId": plan.run_attempt_id,
                    "expectedCurrentAttemptRevision": self.store.entity_revision(
                        "run-attempt", plan.run_attempt_id
                    ),
                }
            ],
        )

        with self.assertRaisesRegex(ManagerConflict, "same-GameRun resume first"):
            self.manager.resume_batch(
                str(batch["batch_id"]),
                BatchResumeRequest(
                    reason="must not replace run", requested_by="resume-test"
                ),
                idempotency_key="terminal-batch-resume-rejected",
                request_id=None,
                path=f"/api/v1/batches/{batch['batch_id']}/resume-requests",
                expected_state_version=self.store.latest_event_sequence(),
            )


if __name__ == "__main__":
    unittest.main()
