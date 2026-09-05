from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yeyu_gamer_manager.services.adapter_artifacts import (
    AdapterArtifactImporter,
    AdapterArtifactImportError,
    _same_directory_identity,
)
from yeyu_gamer_manager.services.adapter_protocol import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_TODO,
    AdapterExecutionPlan,
    run_artifact_byte_limit,
)
from yeyu_gamer_manager.store.sqlite_store import SqliteStore


RUN_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
TODO_ID = "todo-instance-33333333-3333-4333-8333-333333333333"
OTHER_TODO_ID = "todo-instance-66666666-6666-4666-8666-666666666666"
TODO_ATTEMPT_ID = "44444444-4444-4444-8444-444444444444"
FENCING_TOKEN = "a" * 32
CANCEL_AUTHORITY = "z" * 32
TODO_DEFINITION_ID = "todo.v1.starrail.daily.observe-panel"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ALLOWED_MIME_TYPES = ("image/png", "image/jpeg", "text/plain")


class AdapterArtifactImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-adapter-artifacts-")
        self.root = Path(self.temporary.name)
        self.staging_root = self.root / "adapter-staging"
        self.artifact_root = self.root / "artifacts"
        self.store = SqliteStore(self.root / "manager.sqlite3")
        self.store.initialize()
        self._seed_game_and_todo()
        run = self.store.create_game_run(
            {
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "running",
                "mode": "execute",
                "requested_by": "test",
                "todo_instance_ids": [TODO_ID],
            }
        )
        self.run_id = str(run["run_id"])
        self.plan = AdapterExecutionPlan.from_document(
            {
                "schemaVersion": 1,
                "protocolVersion": "1.1",
                "requestType": "execute",
                "runId": self.run_id,
                "runAttemptId": RUN_ATTEMPT_ID,
                "fencingToken": FENCING_TOKEN,
                "cancelAuthority": CANCEL_AUTHORITY,
                "gameId": "StarRail",
                "cadence": "daily",
                "managerStateVersion": self.store.latest_event_sequence(),
                "catalogVersion": "catalog-test",
                "policyDigest": "sha256:" + "b" * 64,
                "issuedAt": "2026-08-28T10:00:00+08:00",
                "expiresAt": "2026-08-28T10:05:00+08:00",
                "timeoutSeconds": 60,
                "preserveClientOnStop": True,
                "executableTodoInstanceIds": [TODO_ID],
                "todos": [
                    {
                        "todoInstanceId": TODO_ID,
                        "todoDefinitionId": TODO_DEFINITION_ID,
                        "definitionVersion": 1,
                        "operation": "observe-panel",
                        "risk": "observe_only",
                        "adapterCapabilityRef": "game.daily.run@1.0",
                        "priorAttempts": 0,
                        "executionDisposition": "executable",
                    }
                ],
            }
        )
        stored_plan = self.plan.to_document()
        stored_plan.pop("fencingToken")
        stored_plan.pop("cancelAuthority")
        self.store.create_run_attempt(
            {
                "run_attempt_id": RUN_ATTEMPT_ID,
                "run_id": self.run_id,
                "game_id": "StarRail",
                "cadence": "daily",
                "state": "starting",
                "fencing_token_hash": hashlib.sha256(
                    FENCING_TOKEN.encode("utf-8")
                ).hexdigest(),
                "cancel_authority_hash": "sha256:"
                + hashlib.sha256(CANCEL_AUTHORITY.encode("utf-8")).hexdigest(),
                "plan": stored_plan,
            }
        )
        self.store.update_run_attempt(
            RUN_ATTEMPT_ID,
            state="running",
            process_id=1234,
            completed=False,
        )
        self.store.start_todo_attempt(
            {
                "todo_attempt_id": TODO_ATTEMPT_ID,
                "run_attempt_id": RUN_ATTEMPT_ID,
                "run_id": self.run_id,
                "todo_instance_id": TODO_ID,
                "attempt_number": 1,
                "operation": "observe-panel",
            }
        )
        self.importer = AdapterArtifactImporter(
            self.store, self.staging_root, self.artifact_root
        )
        self.run_staging = self.staging_root / RUN_ATTEMPT_ID
        self.run_staging.mkdir()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _seed_game_and_todo(self) -> None:
        self.store.import_legacy(
            config_values={},
            games=[
                {
                    "game_id": "StarRail",
                    "display_name": "StarRail",
                    "order_index": 1,
                    "enabled": True,
                    "policy": {},
                }
            ],
            source_info={"test": "adapter-artifacts"},
            policy_projection={},
        )
        definition = {
            "todo_definition_id": TODO_DEFINITION_ID,
            "definition_version": 1,
            "catalog_version": "catalog-test",
            "source_hash": "c" * 64,
            "game_id": "StarRail",
            "cadence": "daily",
            "operation": "observe-panel",
            "title": "Observe panel",
            "category": "observation",
            "order_index": 1,
            "required": True,
            "risk": "observe_only",
            "automation_difficulty": "low",
            "adapter_capability_ref": "game.daily.run@1.0",
            "automation_state": "implemented",
            "initial_status": "pending",
            "initial_reason": "",
            "reset_rule": {
                "timezone": "Asia/Shanghai",
                "time": "04:00",
                "cadence": "daily",
            },
            "source_refs": ["test"],
        }
        self.store.sync_todo_definitions([definition])
        self.store.reconcile_todo_instances(
            [
                {
                    "todo_instance_id": TODO_ID,
                    "todo_definition_id": TODO_DEFINITION_ID,
                    "game_id": "StarRail",
                    "cadence": "daily",
                    "period_key": "2026-08-28",
                    "period_starts_at": "2026-08-27T20:00:00+00:00",
                    "period_ends_at": "2026-08-28T20:00:00+00:00",
                    "definition_snapshot": definition,
                    "status": "pending",
                    "reason": "",
                }
            ],
            intent="reconcile",
            requested_by="test",
            reason="test fixture",
        )

    def event(self, data: bytes = PNG_1X1, **updates: object) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": 1,
            "protocolVersion": "1.1",
            "eventType": "artifact_staged",
            "sequence": 2,
            "runId": self.run_id,
            "runAttemptId": RUN_ATTEMPT_ID,
            "fencingToken": FENCING_TOKEN,
            "gameId": "StarRail",
            "at": "2026-08-28T10:00:02+08:00",
            "todoInstanceId": TODO_ID,
            "todoAttemptId": TODO_ATTEMPT_ID,
            "artifactId": str(uuid.uuid4()),
            "kind": "raw-frame",
            "fileName": "frame.png",
            "mimeType": "image/png",
            "sizeBytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "capturedAt": "2026-08-28T10:00:02+08:00",
        }
        result.update(updates)
        return result

    def stage(self, event: dict[str, object], data: bytes = PNG_1X1) -> Path:
        path = self.run_staging / str(event["fileName"])
        path.write_bytes(data)
        return path

    def seed_ledger_artifact(self, *, todo_instance_id: str, size_bytes: int) -> None:
        self.store.create_resource(
            "artifact",
            resource_id=str(uuid.uuid4()),
            state="captured",
            document={
                "runId": self.run_id,
                "runAttemptId": RUN_ATTEMPT_ID,
                "todoInstanceId": todo_instance_id,
                "sizeBytes": size_bytes,
            },
        )

    def import_staged(
        self,
        event: dict[str, object],
        *,
        importer: AdapterArtifactImporter | None = None,
        allowed_mime_types: tuple[str, ...] = ALLOWED_MIME_TYPES,
        max_artifacts_per_todo: int = MAX_ARTIFACTS_PER_TODO,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> str:
        return (importer or self.importer).import_staged(
            self.plan,
            event,
            allowed_mime_types=allowed_mime_types,
            max_artifacts_per_todo=max_artifacts_per_todo,
            max_artifact_bytes=max_artifact_bytes,
        )

    def assert_import_error(self, code: str, event: dict[str, object]) -> None:
        with self.assertRaises(AdapterArtifactImportError) as raised:
            self.import_staged(event)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.store.list_resources("artifact"), [])
        self.assertEqual(list(self.artifact_root.iterdir()), [])

    def test_success_registers_opaque_owned_artifact(self) -> None:
        event = self.event()
        source = self.stage(event)
        with mock.patch.object(
            self.store,
            "list_resources",
            side_effect=AssertionError("artifact import must use targeted SQL"),
        ):
            artifact_id = self.import_staged(event)

        self.assertEqual(artifact_id, event["artifactId"])
        resource = self.store.get_resource("artifact", artifact_id)
        document = resource["document"]
        self.assertEqual(resource["state"], "captured")
        self.assertEqual(document["gameId"], "StarRail")
        self.assertEqual(document["runId"], self.run_id)
        self.assertEqual(document["runAttemptId"], RUN_ATTEMPT_ID)
        self.assertEqual(document["todoInstanceId"], TODO_ID)
        self.assertEqual(document["todoAttemptId"], TODO_ATTEMPT_ID)
        self.assertEqual(document["gameDayKey"], "2026-08-28")
        self.assertEqual(document["source"], "manager-adapter-v1.1")
        self.assertEqual(document["verdict"], "unreviewed")
        self.assertIs(document["raw"], True)
        self.assertRegex(document["fileName"], r"^artifact-[0-9a-f]{32}\.png$")
        self.assertEqual(document["relativePath"], document["fileName"])
        self.assertNotEqual(document["fileName"], source.name)
        target = self.artifact_root / document["relativePath"]
        self.assertEqual(target.read_bytes(), PNG_1X1)
        self.assertTrue(source.exists(), "the importer must not erase runner staging")

    def test_watermarked_kind_is_registered_as_derivative(self) -> None:
        event = self.event(kind="game-ui-daily-reward-watermarked")
        self.stage(event)

        artifact_id = self.import_staged(event)

        document = self.store.get_resource("artifact", artifact_id)["document"]
        self.assertIs(document["raw"], False)
        self.assertEqual(document["kind"], "game-ui-daily-reward-watermarked")

    def test_twenty_first_artifact_is_rejected_from_rebuilt_ledger_quota(self) -> None:
        for _ in range(MAX_ARTIFACTS_PER_TODO):
            self.seed_ledger_artifact(todo_instance_id=TODO_ID, size_bytes=1)
        event = self.event(fileName="twenty-first.png")
        self.stage(event)
        before_files = set(self.artifact_root.iterdir())
        before_resources = self.store.list_resources("artifact", limit=1000)
        before_event_sequence = self.store.latest_event_sequence()
        restarted_importer = AdapterArtifactImporter(
            self.store, self.staging_root, self.artifact_root
        )

        with self.assertRaises(AdapterArtifactImportError) as raised:
            self.import_staged(event, importer=restarted_importer)

        self.assertEqual(raised.exception.code, "artifact_count_exceeded")
        self.assertEqual(
            self.store.list_resources("artifact", limit=1000), before_resources
        )
        self.assertEqual(self.store.latest_event_sequence(), before_event_sequence)
        self.assertEqual(set(self.artifact_root.iterdir()), before_files)
        self.assertEqual(self.store.list_adapter_events(RUN_ATTEMPT_ID), [])

    def test_lower_manifest_artifact_count_is_enforced(self) -> None:
        for _ in range(2):
            self.seed_ledger_artifact(todo_instance_id=TODO_ID, size_bytes=1)
        event = self.event(fileName="third-under-lower-manifest.png")
        self.stage(event)

        with self.assertRaises(AdapterArtifactImportError) as raised:
            self.import_staged(event, max_artifacts_per_todo=2)

        self.assertEqual(raised.exception.code, "artifact_count_exceeded")
        self.assertEqual(len(self.store.list_resources("artifact", limit=1000)), 2)
        self.assertEqual(list(self.artifact_root.iterdir()), [])

    def test_two_connections_racing_for_twentieth_artifact_have_one_winner(self) -> None:
        for _ in range(MAX_ARTIFACTS_PER_TODO - 1):
            self.seed_ledger_artifact(todo_instance_id=TODO_ID, size_bytes=1)
        first_event = self.event(fileName="race-first.png")
        second_event = self.event(fileName="race-second.png")
        self.stage(first_event)
        self.stage(second_event)
        second_store = SqliteStore(self.root / "manager.sqlite3")
        second_store.initialize()
        second_importer = AdapterArtifactImporter(
            second_store, self.staging_root, self.artifact_root
        )
        barrier = threading.Barrier(3)
        result_lock = threading.Lock()
        results: list[str] = []

        def import_after_barrier(
            importer: AdapterArtifactImporter,
            event: dict[str, object],
        ) -> None:
            barrier.wait()
            try:
                self.import_staged(event, importer=importer)
            except AdapterArtifactImportError as error:
                result = error.code
            except Exception as error:  # pragma: no cover - diagnostic assertion
                result = f"unexpected:{type(error).__name__}:{error}"
            else:
                result = "success"
            with result_lock:
                results.append(result)

        threads = [
            threading.Thread(
                target=import_after_barrier,
                args=(self.importer, first_event),
            ),
            threading.Thread(
                target=import_after_barrier,
                args=(second_importer, second_event),
            ),
        ]
        try:
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=15)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertCountEqual(results, ["success", "artifact_count_exceeded"])
            usage = self.store.get_adapter_artifact_usage(
                run_attempt_id=RUN_ATTEMPT_ID,
                todo_instance_id=TODO_ID,
            )
            self.assertEqual(usage["todo_artifact_count"], 20)
            self.assertEqual(len(self.store.list_resources("artifact", limit=1000)), 20)
            self.assertEqual(len(list(self.artifact_root.iterdir())), 1)
        finally:
            second_store.close()

    def test_artifact_count_is_independent_across_todos(self) -> None:
        for _ in range(MAX_ARTIFACTS_PER_TODO):
            self.seed_ledger_artifact(todo_instance_id=OTHER_TODO_ID, size_bytes=1)
        event = self.event(fileName="first-current-todo.png")
        self.stage(event)

        artifact_id = self.import_staged(event)

        self.assertEqual(artifact_id, event["artifactId"])
        current_todo_artifacts = [
            resource
            for resource in self.store.list_resources("artifact", limit=1000)
            if resource["document"].get("todoInstanceId") == TODO_ID
        ]
        self.assertEqual(len(current_todo_artifacts), 1)

    def test_run_byte_quota_rejection_has_no_file_ledger_or_event_side_effect(self) -> None:
        run_limit = run_artifact_byte_limit(executable_todo_count=1)
        for _ in range(12):
            self.seed_ledger_artifact(
                todo_instance_id=TODO_ID,
                size_bytes=MAX_ARTIFACT_BYTES,
            )
        remaining_before_new = run_limit - (12 * MAX_ARTIFACT_BYTES)
        self.seed_ledger_artifact(
            todo_instance_id=TODO_ID,
            size_bytes=remaining_before_new - len(PNG_1X1) + 1,
        )
        event = self.event(fileName="over-run-bytes.png")
        self.stage(event)
        before_files = set(self.artifact_root.iterdir())
        before_resources = self.store.list_resources("artifact", limit=1000)
        before_event_sequence = self.store.latest_event_sequence()

        with self.assertRaises(AdapterArtifactImportError) as raised:
            self.import_staged(event)

        self.assertEqual(raised.exception.code, "artifact_run_bytes_exceeded")
        self.assertEqual(
            self.store.list_resources("artifact", limit=1000), before_resources
        )
        self.assertEqual(self.store.latest_event_sequence(), before_event_sequence)
        self.assertEqual(set(self.artifact_root.iterdir()), before_files)
        self.assertEqual(self.store.list_adapter_events(RUN_ATTEMPT_ID), [])

    def test_path_escape_and_reserved_leaf_are_rejected(self) -> None:
        self.assert_import_error(
            "unsafe_artifact_path", self.event(fileName="../outside.png")
        )
        self.assert_import_error("unsafe_artifact_path", self.event(fileName="CON.png"))

    def test_symlink_or_reparse_source_is_rejected(self) -> None:
        event = self.event()
        outside = self.root / "outside.png"
        outside.write_bytes(PNG_1X1)
        source = self.run_staging / str(event["fileName"])
        try:
            source.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        self.assert_import_error("artifact_path_reparse", event)

    def test_symlink_or_reparse_run_directory_is_rejected(self) -> None:
        outside = self.root / "outside-run"
        outside.mkdir()
        event = self.event()
        (outside / str(event["fileName"])).write_bytes(PNG_1X1)
        self.run_staging.rmdir()
        try:
            self.run_staging.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.run_staging.mkdir()
            self.skipTest(f"directory symlink creation is unavailable: {error}")
        self.assert_import_error("artifact_path_reparse", event)

    def test_size_limit_hash_and_magic_fail_closed(self) -> None:
        self.assert_import_error(
            "artifact_size_rejected",
            self.event(sizeBytes=MAX_ARTIFACT_BYTES + 1),
        )

        bad_hash = self.event(sha256="0" * 64)
        self.stage(bad_hash)
        self.assert_import_error("artifact_hash_mismatch", bad_hash)

        not_png = b"plain UTF-8 text"
        bad_magic = self.event(
            not_png,
            fileName="wrong.png",
            sizeBytes=len(not_png),
            sha256=hashlib.sha256(not_png).hexdigest(),
        )
        self.stage(bad_magic, not_png)
        self.assert_import_error("artifact_magic_mismatch", bad_magic)

    def test_invalid_utf8_and_direct_json_mime_bypass_are_rejected(self) -> None:
        invalid_utf8 = b"\xff\xfe"
        text_event = self.event(
            invalid_utf8,
            fileName="output.txt",
            mimeType="text/plain",
            sizeBytes=len(invalid_utf8),
            sha256=hashlib.sha256(invalid_utf8).hexdigest(),
        )
        self.stage(text_event, invalid_utf8)
        self.assert_import_error("artifact_encoding_mismatch", text_event)

        json_data = b'{"valid":true}'
        json_event = self.event(
            json_data,
            fileName="output.json",
            mimeType="application/json",
            sizeBytes=len(json_data),
            sha256=hashlib.sha256(json_data).hexdigest(),
        )
        self.stage(json_event, json_data)
        with self.assertRaises(AdapterArtifactImportError) as raised:
            self.import_staged(
                json_event,
                allowed_mime_types=("application/json",),
            )
        self.assertEqual(raised.exception.code, "artifact_mime_denied")
        self.assertEqual(self.store.list_resources("artifact"), [])
        self.assertEqual(list(self.artifact_root.iterdir()), [])

    def test_import_requires_explicit_manifest_policy(self) -> None:
        event = self.event()
        self.stage(event)
        with self.assertRaises(TypeError):
            self.importer.import_staged(self.plan, event)
        self.assertEqual(self.store.list_resources("artifact"), [])
        self.assertEqual(list(self.artifact_root.iterdir()), [])

    def test_event_scope_todo_attempt_and_lease_are_enforced(self) -> None:
        wrong_game = self.event(gameId="ZZZ")
        self.assert_import_error("event_scope_mismatch", wrong_game)

        wrong_attempt = self.event(
            todoAttemptId="55555555-5555-4555-8555-555555555555"
        )
        self.assert_import_error("event_scope_mismatch", wrong_attempt)

        expired = self.event(capturedAt="2026-08-28T10:06:00+08:00")
        self.assert_import_error("artifact_time_out_of_scope", expired)

    def test_read_is_bounded_to_declared_size_plus_one(self) -> None:
        event = self.event()
        self.stage(event)
        requested: list[int] = []
        received: list[int] = []
        real_read = os.read

        def recording_read(descriptor: int, count: int) -> bytes:
            requested.append(count)
            chunk = real_read(descriptor, count)
            received.append(len(chunk))
            return chunk

        with mock.patch(
            "yeyu_gamer_manager.services.adapter_artifacts.os.read",
            side_effect=recording_read,
        ):
            self.import_staged(event)
        self.assertEqual(requested[0], len(PNG_1X1) + 1)
        self.assertLessEqual(sum(received), len(PNG_1X1) + 1)
        self.assertTrue(
            all(
                current
                == len(PNG_1X1) + 1 - sum(received[:index])
                for index, current in enumerate(requested[1:], start=1)
            )
        )

    def test_short_reads_are_joined_before_import_validation(self) -> None:
        event = self.event()
        self.stage(event)
        real_read = os.read
        calls = 0

        def short_read(descriptor: int, count: int) -> bytes:
            nonlocal calls
            calls += 1
            return real_read(descriptor, min(count, 5))

        with mock.patch(
            "yeyu_gamer_manager.services.artifact_integrity.os.read",
            side_effect=short_read,
        ):
            imported_id = self.import_staged(event)

        self.assertGreater(calls, 1)
        imported = self.store.get_resource("artifact", imported_id)
        self.assertEqual(imported["document"]["hash"], event["sha256"])

    def test_handle_race_is_rejected(self) -> None:
        event = self.event()
        self.stage(event)
        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            snapshot = real_fstat(descriptor)
            if calls != 2:
                return snapshot
            return SimpleNamespace(
                st_dev=snapshot.st_dev,
                st_ino=snapshot.st_ino,
                st_mode=snapshot.st_mode,
                st_size=snapshot.st_size,
                st_mtime_ns=snapshot.st_mtime_ns + 1,
            )

        with mock.patch(
            "yeyu_gamer_manager.services.adapter_artifacts.os.fstat",
            side_effect=changing_fstat,
        ):
            self.assert_import_error("artifact_changed_during_read", event)

    def test_directory_identity_allows_sibling_metadata_change(self) -> None:
        snapshot = os.lstat(self.staging_root / RUN_ATTEMPT_ID)
        changed = SimpleNamespace(
            st_dev=snapshot.st_dev,
            st_ino=snapshot.st_ino,
            st_mode=snapshot.st_mode,
            st_size=snapshot.st_size + 1,
            st_mtime_ns=snapshot.st_mtime_ns + 1,
        )
        self.assertTrue(_same_directory_identity(snapshot, changed))

    def test_directory_identity_rejects_replacement(self) -> None:
        snapshot = os.lstat(self.staging_root / RUN_ATTEMPT_ID)
        replaced = SimpleNamespace(
            st_dev=snapshot.st_dev,
            st_ino=snapshot.st_ino + 1,
            st_mode=snapshot.st_mode,
            st_size=snapshot.st_size,
            st_mtime_ns=snapshot.st_mtime_ns,
        )
        self.assertFalse(_same_directory_identity(snapshot, replaced))

    def test_ledger_failure_removes_new_manager_file(self) -> None:
        event = self.event()
        self.stage(event)
        with mock.patch.object(
            self.store, "create_resource", side_effect=RuntimeError("ledger failed")
        ):
            with self.assertRaises(AdapterArtifactImportError) as raised:
                self.import_staged(event)
        self.assertEqual(raised.exception.code, "artifact_ledger_write_failed")
        self.assertNotIn("ledger failed", str(raised.exception))
        self.assertEqual(list(self.artifact_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
