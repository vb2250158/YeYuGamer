from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.services.notifications import (
    DpapiNotificationSecretProvider,
    FakeNotificationSecretProvider,
    FakeNotificationTransport,
    NotificationDispatcher,
    NotificationSecretError,
    SmtpProfile,
)
from yeyu_gamer_manager.settings import Settings
from yeyu_gamer_manager.store.sqlite_store import SqliteStore
from yeyu_gamer_manager.services.notifications import artifacts as notification_artifacts


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def profile(binding_id: str = "self-email") -> SmtpProfile:
    return SmtpProfile(
        binding_id=binding_id,
        host="smtp.invalid",
        port=465,
        username="test-user",
        password="test-password",
        sender_address="sender@example.invalid",
        recipient_address="recipient@example.invalid",
        security="tls",
    )


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self.now

    def move_to(self, value: str | datetime) -> None:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        with self.lock:
            self.now = parsed.astimezone(timezone.utc)


class NotificationStoreAndWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-notification-")
        self.root = Path(self.temporary.name)
        self.store = SqliteStore(self.root / "manager.sqlite3")
        self.store.initialize()
        now = datetime.now(timezone.utc).isoformat()
        self.store.connection.execute(
            """
            INSERT INTO games(
                game_id, display_name, order_index, enabled, state,
                reward_claimed, message, policy_json, updated_at
            ) VALUES ('StarRail', 'StarRail', 0, 1, 'unknown', 0, '', '{}', ?)
            """,
            (now,),
        )
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def dispatcher(
        self,
        *,
        configured: bool = True,
        outcomes: list[str] | None = None,
        clock: MutableClock | None = None,
    ) -> tuple[NotificationDispatcher, FakeNotificationTransport]:
        provider = FakeNotificationSecretProvider(
            {"self-email": profile()} if configured else {}
        )
        transport = FakeNotificationTransport(outcomes)
        dispatcher = NotificationDispatcher(
            store=self.store,
            artifact_root=self.artifact_root,
            secret_provider=provider,
            transport=transport,
            clock=clock or MutableClock(datetime.now(timezone.utc) + timedelta(seconds=1)),
            poll_seconds=60,
        )
        return dispatcher, transport

    @staticmethod
    def sealed_result(
        *,
        outcome: str = "review_required",
        accepted_done: bool = False,
        evidence_ids: list[str] | None = None,
        run_id: str = "run-1",
    ) -> dict[str, object]:
        evidence = list(evidence_ids or [])
        return {
            "gameDay": "2026-08-28",
            "notificationOutcome": "completed" if accepted_done else "blocked",
            "finalGameRunIds": [run_id],
            "todoSnapshot": {
                "StarRail": {
                    "summary": {
                        "displayName": "崩坏：星穹铁道",
                        "requiredCompleted": 0,
                        "requiredTotal": 1,
                    },
                    "instances": [
                        {
                            "title": "领取每日奖励",
                            "status": "blocked" if not accepted_done else "completed",
                            "attempts": 1,
                            "evidenceRefs": evidence,
                        }
                    ],
                }
            },
            "unresolvedRequiredTodoIds": [] if accepted_done else ["todo-1"],
            "sealEvidenceArtifactIds": evidence,
            "acceptedDone": accepted_done,
            "outcome": outcome,
        }

    def seal_execute(
        self,
        dispatcher: NotificationDispatcher,
        *,
        result: dict[str, object] | None = None,
        state: str = "review_required",
    ) -> tuple[dict[str, object], dict[str, object]]:
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "test",
                "result": {"gameDay": "2026-08-28"},
            }
        )
        sealed = self.store.seal_batch(
            batch["batch_id"],
            state=state,
            result=result or self.sealed_result(),
            notification_draft_factory=lambda version, frozen, policy: dispatcher.build_draft(
                game_day="2026-08-28",
                batch_id=batch["batch_id"],
                seal_version=version,
                sealed_result=frozen,
                policy=policy,
            ),
        )
        return batch, sealed

    def test_stale_batch_seal_is_never_dispatched_automatically(self) -> None:
        # A recovered review phase can seal a batch created long ago (Manager
        # restart, barrier expiry).  That mail is history, not a round report:
        # it stays a draft behind the stale_batch gate even with a configured
        # secret, and the worker never picks it up.
        late = MutableClock(datetime.now(timezone.utc) + timedelta(hours=30))
        dispatcher, transport = self.dispatcher(configured=True, clock=late)
        batch, _ = self.seal_execute(dispatcher)
        deliveries = self.store.list_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["batch_id"], batch["batch_id"])
        self.assertEqual(deliveries[0]["dispatch_gate"], "stale_batch")
        self.assertEqual(deliveries[0]["state"], "draft")
        self.assertIsNone(deliveries[0]["next_attempt_at"])
        self.assertEqual(dispatcher.run_once(), 0)
        self.assertEqual(transport.sent, [])

    def test_fresh_batch_seal_dispatches_automatically(self) -> None:
        dispatcher, transport = self.dispatcher(configured=True)
        self.seal_execute(dispatcher)
        deliveries = self.store.list_notification_deliveries()
        self.assertEqual(deliveries[0]["dispatch_gate"], "automatic")
        dispatcher.run_once()
        self.assertEqual(len(transport.sent), 1)

    def test_report_is_frozen_through_seed_render_restart_and_retry(self) -> None:
        clock = MutableClock(datetime.now(timezone.utc) + timedelta(seconds=1))
        dispatcher, transport = self.dispatcher(outcomes=["transient", "sent"], clock=clock)
        rendered = SimpleNamespace(
            outcome="blocked", subject="frozen subject", text_body="compact text",
            html_body="<p>compact</p>", report_html="<details><summary>冻结详情</summary>same seal</details>",
        )
        with patch("yeyu_gamer_manager.services.notifications.dispatcher.render_batch_notification", return_value=rendered):
            batch, _ = self.seal_execute(dispatcher)
        original = self.store.list_notification_deliveries()[0]
        self.assertEqual(original["report_html"], rendered.report_html)
        self.store.close()
        self.store = SqliteStore(self.root / "manager.sqlite3")
        self.store.initialize()
        dispatcher, transport = self.dispatcher(outcomes=["transient", "sent"], clock=clock)
        with patch("yeyu_gamer_manager.services.notifications.dispatcher.render_batch_notification", side_effect=AssertionError("frozen notification must not render again")):
            replay = self.store.seal_batch(batch["batch_id"], state="failed", result={})
            self.assertEqual(replay["result"]["sealVersion"], original["seal_version"])
            self.store._complete_notification_render(
                original["notification_id"],
                draft={"outcome": "blocked", "subject": "replacement", "text_body": "replacement", "html_body": "replacement", "report_html": "replacement", "attachment_refs": []},
                policy=self.store.get_notification_policy(),
            )
            self.assertEqual(dispatcher.run_once(), 1)
            failed = self.store.get_notification_delivery(original["notification_id"])
            self.assertEqual(failed["report_html"], rendered.report_html)
            clock.move_to(str(failed["next_attempt_at"]))
            self.assertEqual(dispatcher.run_once(), 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.sent[0].report_html, rendered.report_html)
        self.assertEqual(transport.sent[0].html_body, "<p>compact</p>")
        self.assertEqual(transport.sent[0].message_id, original["message_id"])

    def test_report_column_migration_preserves_legacy_frozen_notification(self) -> None:
        dispatcher, _ = self.dispatcher()
        rendered = SimpleNamespace(outcome="blocked", subject="legacy", text_body="legacy text", html_body="<p>legacy</p>", report_html="")
        with patch("yeyu_gamer_manager.services.notifications.dispatcher.render_batch_notification", return_value=rendered):
            self.seal_execute(dispatcher)
        before = self.store.list_notification_deliveries()[0]
        # The current schema's generated public-text guards mention every text
        # column; remove only this fixture table's guards before emulating its
        # pre-report schema. initialize() recreates guards after migration.
        triggers = self.store.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE type = 'trigger' AND tbl_name = 'notification_deliveries'"
        ).fetchall()
        for trigger in triggers:
            self.store.connection.execute("DROP TRIGGER " + self.store._quote_identifier(str(trigger["name"])))
        self.store.connection.execute("ALTER TABLE notification_deliveries DROP COLUMN report_html")
        self.store.close()
        self.store = SqliteStore(self.root / "manager.sqlite3")
        with patch("yeyu_gamer_manager.services.notifications.dispatcher.render_batch_notification", side_effect=AssertionError("migration must not render")):
            self.store.initialize()
        self.assertEqual(self.store.get_notification_delivery(before["notification_id"]), before)
        column = next(row for row in self.store.connection.execute("PRAGMA table_info(notification_deliveries)") if row["name"] == "report_html")
        self.assertEqual(column["type"], "TEXT")
        self.assertEqual(column["notnull"], 1)
        self.assertEqual(column["dflt_value"], "''")
        dispatcher, transport = self.dispatcher()
        self.assertEqual(dispatcher.run_once(), 1)
        self.assertEqual(transport.sent[0].report_html, "")

    def test_direct_delivery_insert_keeps_optional_report_and_deduplicates_frozen_content(self) -> None:
        batch = self.store.create_batch({"cadence": "daily", "mode": "execute", "state": "running", "game_ids": ["StarRail"], "requested_by": "test"})
        policy = self.store.get_notification_policy()
        draft = {"outcome": "blocked", "subject": "subject", "text_body": "text", "html_body": "<p>body</p>", "attachment_refs": []}
        for version, report in ((1, ""), (2, "<p>frozen report</p>")):
            with self.subTest(version=version), self.store._write_scope():
                source = {**draft, **({"report_html": report} if report else {})}
                row = self.store._create_notification_delivery_locked(batch_id=batch["batch_id"], seal_version=version, draft=source, policy=policy, timestamp=datetime.now(timezone.utc).isoformat())
                replay = self.store._create_notification_delivery_locked(batch_id=batch["batch_id"], seal_version=version, draft={**source, "report_html": "changed"}, policy=policy, timestamp=datetime.now(timezone.utc).isoformat())
                self.assertEqual(row["report_html"], report)
                self.assertEqual(replay["report_html"], report)
                self.assertEqual(replay["message_id"], row["message_id"])
        with self.assertRaisesRegex(ValueError, "report HTML must be text"):
            self.store._validate_notification_draft({**draft, "report_html": None})

    def test_plan_seal_has_no_delivery_and_execute_seal_is_irreversible_and_deduped(self) -> None:
        dispatcher, transport = self.dispatcher(configured=False)
        plan = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "plan",
                "state": "planned",
                "game_ids": ["StarRail"],
                "requested_by": "test",
            }
        )
        self.store.seal_batch(
            plan["batch_id"], state="cancelled", result=self.sealed_result()
        )
        self.assertEqual(self.store.list_notification_deliveries(), [])

        batch, sealed = self.seal_execute(dispatcher)
        deliveries = self.store.list_notification_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["dispatch_gate"], "secret_missing")
        self.assertEqual(deliveries[0]["batch_id"], batch["batch_id"])
        message_id = deliveries[0]["message_id"]
        replay = self.store.seal_batch(
            batch["batch_id"], state="failed", result={}
        )
        self.assertEqual(replay["result"], sealed["result"])
        self.assertEqual(len(self.store.list_notification_deliveries()), 1)
        self.assertEqual(self.store.list_notification_deliveries()[0]["message_id"], message_id)

        broken = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "test",
            }
        )
        protected_tables = (
            "batches",
            "game_runs",
            "run_attempts",
            "todo_instances",
            "todo_attempts",
        )
        counts_before = {
            table: int(
                self.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in protected_tables
        }
        broken_seal = self.store.seal_batch(
            broken["batch_id"],
            state="failed",
            result=self.sealed_result(),
            notification_draft_factory=lambda *_: (_ for _ in ()).throw(
                RuntimeError("template failure")
            ),
        )
        counts_after = {
            table: int(
                self.store.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in protected_tables
        }
        self.assertEqual(counts_after, counts_before)
        stored = self.store.get_batch(broken["batch_id"])
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(stored["result"], broken_seal["result"])
        self.assertIsInstance(stored["result"].get("sealVersion"), int)

        broken_delivery = next(
            item
            for item in self.store.list_notification_deliveries()
            if item["batch_id"] == broken["batch_id"]
        )
        self.assertEqual(broken_delivery["seal_version"], stored["result"]["sealVersion"])
        self.assertEqual(broken_delivery["state"], "failed")
        self.assertEqual(broken_delivery["dispatch_gate"], "manual_review")
        self.assertEqual(broken_delivery["last_error_class"], "render_failed")
        self.assertEqual(broken_delivery["report_html"], "")
        self.assertEqual(broken_delivery["attempt_count"], 0)
        self.assertIsNone(broken_delivery["next_attempt_at"])
        self.assertEqual(
            self.store.list_notification_attempts(broken_delivery["notification_id"]), []
        )
        self.assertEqual(dispatcher.run_once(), 0)
        self.assertEqual(transport.sent, [])
        with self.assertRaisesRegex(ValueError, "rendering is incomplete"):
            self.store.arm_notification_delivery(
                broken_delivery["notification_id"],
                requested_by="test",
                reason="must not send an unrendered seed",
                secret_state="configured",
                confirm_ambiguous=False,
            )

        replay = self.store.seal_batch(
            broken["batch_id"], state="done", result={"acceptedDone": True}
        )
        self.assertEqual(replay["result"], broken_seal["result"])
        with self.assertRaisesRegex(ValueError, "sealed batches are immutable"):
            self.store.update_batch(
                broken["batch_id"],
                state="cancelled",
                result={"reason": "must not erase the seal"},
            )
        self.assertEqual(
            self.store.get_batch(broken["batch_id"])["result"],
            broken_seal["result"],
        )
        broken_deliveries = [
            item
            for item in self.store.list_notification_deliveries()
            if item["batch_id"] == broken["batch_id"]
        ]
        self.assertEqual(len(broken_deliveries), 1)
        self.assertEqual(
            int(
                self.store.connection.execute(
                    """
                    SELECT COUNT(*) FROM events
                    WHERE event_type = 'batch.sealed' AND entity_id = ?
                    """,
                    (broken["batch_id"],),
                ).fetchone()[0]
            ),
            1,
        )

    def test_fake_send_and_sent_state_are_isolated_and_immutable(self) -> None:
        dispatcher, transport = self.dispatcher()
        self.seal_execute(dispatcher)
        before = {
            "batch": self.store.list_batches(),
            "runs": self.store.list_game_runs(),
            "todos": self.store.list_todo_instances(limit=100),
        }
        self.assertEqual(dispatcher.run_once(), 1)
        delivery = self.store.list_notification_deliveries()[0]
        self.assertEqual(delivery["state"], "sent")
        self.assertEqual(delivery["attempt_count"], 1)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(len(self.store.list_notification_attempts(delivery["notification_id"])), 1)
        self.assertEqual(before["batch"], self.store.list_batches())
        self.assertEqual(before["runs"], self.store.list_game_runs())
        self.assertEqual(before["todos"], self.store.list_todo_instances(limit=100))
        database_bytes = b"".join(
            path.read_bytes()
            for path in self.root.glob("manager.sqlite3*")
            if path.is_file()
        )
        for secret in [
            b"smtp.invalid",
            b"test-user",
            b"test-password",
            b"sender@example.invalid",
            b"recipient@example.invalid",
        ]:
            self.assertNotIn(secret, database_bytes)
        with self.assertRaises(ValueError):
            self.store.arm_notification_delivery(
                delivery["notification_id"],
                requested_by="test",
                reason="must remain sent",
                secret_state="configured",
                confirm_ambiguous=False,
            )

    def test_concurrent_claim_has_one_winner(self) -> None:
        dispatcher, _ = self.dispatcher()
        self.seal_execute(dispatcher)
        delivery = self.store.list_notification_deliveries()[0]
        now = datetime.now(timezone.utc) + timedelta(seconds=2)

        def claim(owner: str):
            return self.store.claim_notification_delivery(
                delivery["notification_id"],
                owner=owner,
                now=now.isoformat(),
                lease_seconds=60,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["worker-a", "worker-b"]))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.store.list_notification_attempts(delivery["notification_id"])), 1)

    def test_each_dispatch_claim_uses_a_fresh_lease_timestamp(self) -> None:
        first = datetime.now(timezone.utc) + timedelta(seconds=2)
        second = first + timedelta(seconds=45)
        # build_draft reads the clock once (stale-batch disposition), run_once
        # reads it at loop start, and the claim must use a fresh third reading.
        values = iter((first, first, second))
        dispatcher, _ = self.dispatcher(clock=lambda: next(values))
        self.seal_execute(dispatcher)
        observed: list[str] = []

        def record_claim(
            notification_id: str,
            *,
            owner: str,
            now: str,
            lease_seconds: int,
        ) -> None:
            del notification_id, owner, lease_seconds
            observed.append(now)
            return None

        with patch.object(
            self.store, "claim_notification_delivery", side_effect=record_claim
        ):
            dispatcher.run_once()
        self.assertEqual(observed, [second.isoformat()])

    def test_retry_schedule_and_ambiguous_manual_gate(self) -> None:
        clock = MutableClock(datetime.now(timezone.utc) + timedelta(seconds=2))
        dispatcher, _ = self.dispatcher(
            outcomes=["transient", "transient", "transient"], clock=clock
        )
        self.seal_execute(dispatcher)
        dispatcher.run_once()
        first = self.store.list_notification_deliveries()[0]
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["dispatch_gate"], "automatic")
        self.assertAlmostEqual(
            (
                datetime.fromisoformat(first["next_attempt_at"]) - clock()
            ).total_seconds(),
            120,
            delta=1,
        )
        clock.move_to(first["next_attempt_at"])
        dispatcher.run_once()
        second = self.store.list_notification_deliveries()[0]
        self.assertAlmostEqual(
            (
                datetime.fromisoformat(second["next_attempt_at"]) - clock()
            ).total_seconds(),
            900,
            delta=1,
        )
        clock.move_to(second["next_attempt_at"])
        dispatcher.run_once()
        exhausted = self.store.list_notification_deliveries()[0]
        self.assertEqual(exhausted["attempt_count"], 3)
        self.assertEqual(exhausted["dispatch_gate"], "manual_review")
        self.assertIsNone(exhausted["next_attempt_at"])

    def test_ambiguous_and_expired_leases_require_explicit_manual_confirmation(self) -> None:
        clock = MutableClock(datetime.now(timezone.utc) + timedelta(seconds=2))
        dispatcher, transport = self.dispatcher(outcomes=["ambiguous", "sent"], clock=clock)
        self.seal_execute(dispatcher)
        dispatcher.run_once()
        ambiguous = self.store.list_notification_deliveries()[0]
        self.assertEqual(ambiguous["dispatch_gate"], "manual_review")
        self.assertEqual(ambiguous["last_error_class"], "transport_ambiguous")
        with self.assertRaises(ValueError):
            self.store.arm_notification_delivery(
                ambiguous["notification_id"],
                requested_by="test",
                reason="blind retry forbidden",
                secret_state="configured",
                confirm_ambiguous=False,
            )
        self.store.arm_notification_delivery(
            ambiguous["notification_id"],
            requested_by="test",
            reason="operator reviewed ambiguous attempt",
            secret_state="configured",
            confirm_ambiguous=True,
        )
        clock.move_to(datetime.now(timezone.utc) + timedelta(seconds=4))
        dispatcher.run_once()
        self.assertEqual(self.store.list_notification_deliveries()[0]["state"], "sent")
        self.assertEqual(len(transport.sent), 1)

        second_dispatcher, _ = self.dispatcher()
        self.seal_execute(second_dispatcher)
        second = self.store.list_notification_deliveries()[0]
        claimed = self.store.claim_notification_delivery(
            second["notification_id"],
            owner="crashed-worker",
            now=clock().isoformat(),
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        recovered = self.store.recover_expired_notification_leases(
            (clock() + timedelta(seconds=31)).isoformat()
        )
        self.assertEqual(recovered, 1)
        recovered_delivery = self.store.get_notification_delivery(second["notification_id"])
        self.assertEqual(recovered_delivery["dispatch_gate"], "manual_review")
        self.assertEqual(recovered_delivery["last_error_class"], "lease_expired_ambiguous")

    def test_blocked_notice_attaches_only_strict_seal_artifact(self) -> None:
        dispatcher, transport = self.dispatcher()
        run_id = "run-accepted"
        artifact_id = "artifact-valid"
        file_name = f"{artifact_id}.png"
        path = self.artifact_root / file_name
        path.write_bytes(PNG_1X1)
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "test",
                "result": {"gameDay": "2026-08-28"},
            }
        )
        captured = datetime.now(timezone.utc).isoformat()
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="captured",
            document={
                "kind": "screenshot",
                "capturedAt": captured,
                "source": "test",
                "raw": True,
                "contentType": "image/png",
                "gameId": "StarRail",
                "runId": run_id,
                "hash": hashlib.sha256(PNG_1X1).hexdigest(),
                "sizeBytes": len(PNG_1X1),
                "fileName": file_name,
                "relativePath": file_name,
            },
        )
        result = self.sealed_result(evidence_ids=[artifact_id], run_id=run_id)
        self.store.seal_batch(
            batch["batch_id"],
            state="blocked",
            result=result,
            notification_draft_factory=lambda version, frozen, policy: dispatcher.build_draft(
                game_day="2026-08-28",
                batch_id=batch["batch_id"],
                seal_version=version,
                sealed_result=frozen,
                policy=policy,
            ),
        )
        dispatcher.run_once()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(
            [item.artifact_id for item in transport.sent[0].attachments],
            [artifact_id],
        )

    def test_completed_notice_attaches_contract_screenshot(self) -> None:
        dispatcher, transport = self.dispatcher()
        run_id = "run-completed"
        artifact_id = "artifact-completed"
        file_name = f"{artifact_id}.png"
        path = self.artifact_root / file_name
        path.write_bytes(PNG_1X1)
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "test",
                "result": {"gameDay": "2026-08-28"},
            }
        )
        self.store.create_resource(
            "artifact",
            resource_id=artifact_id,
            state="captured",
            document={
                "kind": "game-ui-daily-training-panel",
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "source": "agent-visual-review",
                "raw": True,
                "contentType": "image/png",
                "gameId": "StarRail",
                "runId": run_id,
                "hash": hashlib.sha256(PNG_1X1).hexdigest(),
                "sizeBytes": len(PNG_1X1),
                "fileName": file_name,
                "relativePath": file_name,
            },
        )
        result = self.sealed_result(
            accepted_done=True, evidence_ids=[artifact_id], run_id=run_id
        )
        result["completionContracts"] = [
            {
                "gameId": "StarRail",
                "runId": run_id,
                "acceptedDone": True,
                "outcome": "accepted_done",
            }
        ]
        self.store.seal_batch(
            batch["batch_id"],
            state="done",
            result=result,
            notification_draft_factory=lambda version, frozen, policy: dispatcher.build_draft(
                game_day="2026-08-28",
                batch_id=batch["batch_id"],
                seal_version=version,
                sealed_result=frozen,
                policy=policy,
            ),
        )
        dispatcher.run_once()
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(
            [item.artifact_id for item in transport.sent[0].attachments],
            [artifact_id],
        )

    def test_contract_notice_without_per_game_screenshot_requires_manual_review(self) -> None:
        dispatcher, transport = self.dispatcher()
        result = self.sealed_result(accepted_done=False)
        result["completionContracts"] = [
            {
                "gameId": "StarRail",
                "runId": "run-missing-image",
                "acceptedDone": False,
                "outcome": "blocked",
            }
        ]
        self.seal_execute(dispatcher, result=result, state="blocked")
        dispatcher.run_once()
        delivery = self.store.list_notification_deliveries()[0]
        self.assertEqual(delivery["dispatch_gate"], "manual_review")
        self.assertEqual(
            delivery["last_error_class"], "attachment_contract_missing"
        )
        self.assertEqual(transport.sent, [])

    def test_attachment_coverage_requires_each_accounts_exact_frozen_run(self) -> None:
        dispatcher, transport = self.dispatcher()
        now = datetime.now(timezone.utc)
        contracts = [
            {"gameId": "StarRail", "accountId": account, "runId": run_id,
             "screenshotArtifactRefs": [artifact_id]}
            for account, run_id, artifact_id in (
                ("default", "run-a", "frame-a"), ("account-b", "run-b", "frame-b"),
            )
        ]
        for artifact_id, run_id, extra in (
            ("frame-a", "run-a", {}), ("frame-b", "run-b", {}),
            ("wrong-account", "run-b", {"accountId": "default"}),
            ("wrong-run", "older-run-b", {"accountId": "account-b"}),
            ("unlisted-frame", "run-b", {}),
        ):
            (self.artifact_root / f"{artifact_id}.png").write_bytes(PNG_1X1)
            self.store.create_resource("artifact", resource_id=artifact_id, state="captured", document={
                "kind": "game-ui-claimed-reward", "contentType": "image/png",
                "gameId": "StarRail", "runId": run_id, "capturedAt": now.isoformat(),
                "relativePath": f"{artifact_id}.png", "sizeBytes": len(PNG_1X1),
                "hash": hashlib.sha256(PNG_1X1).hexdigest(), **extra,
            })
        refs = ["frame-a", "frame-b", "wrong-account", "wrong-run", "unlisted-frame"]
        # Wrong-account is named by the frozen contract but still fails its
        # explicit identity; unlisted-frame fails the contract's own whitelist.
        contracts[1]["screenshotArtifactRefs"].append("wrong-account")
        frozen = {"completionContracts": contracts, "sealEvidenceArtifactIds": refs,
                  "finalGameRunIds": ["run-a", "run-b", "older-run-b"],
                  "sealedAt": (now + timedelta(seconds=1)).isoformat()}
        batch = {"game_ids": ["StarRail"], "created_at": (now - timedelta(seconds=1)).isoformat(),
                 "result": frozen}
        delivery = {"batch_id": "batch-fixture", "outcome": "completed", "attachment_refs": refs}
        with patch.object(self.store, "get_batch", return_value=batch):
            for artifact_id, accepted in (("frame-b", True), ("wrong-account", False)):
                preflight = dispatcher.artifacts.preflight_reference(
                    artifact_id, allowed_games={"StarRail"}, allowed_runs={"run-b"},
                    started_at=now - timedelta(seconds=1), sealed_at=now + timedelta(seconds=1),
                    expected_contract=contracts[1],
                )
                self.assertEqual(preflight.accepted, accepted)
            attachments, decisions = dispatcher.artifacts.resolve(delivery)
            self.assertEqual([item.artifact_id for item in attachments], ["frame-a", "frame-b"])
            self.assertEqual({item.reason for item in decisions if not item.accepted}, {"artifact_contract_scope_mismatch"})
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, decisions), ())
            only_a = tuple(item for item in decisions if item.artifact_id != "frame-b")
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, only_a), ("StarRail::account-b",))
            frozen["notificationBlockers"] = [{"gameId": "StarRail", "accountId": "default",
                "screenshotUnavailableReason": "no frame for account a"}]
            delivery["outcome"] = "blocked"
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, only_a), ("StarRail::account-b",))
            frozen["notificationBlockers"] = [{"gameId": "StarRail", "accountId": "account-b",
                "targetId": "StarRail::account-b", "runId": "older-run-b", "screenshotUnavailableReason": "old run"}]
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, only_a), ("StarRail::account-b",))
            frozen["notificationBlockers"][0]["runId"] = "run-b"
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, only_a), ())
            # Legacy dictionary contracts/default artifacts remain compatible.
            frozen["completionContracts"] = {"StarRail": {"runId": "run-a"}}
            self.assertEqual(dispatcher.artifacts.missing_contract_games(delivery, only_a), ())
        self.assertEqual(transport.sent, [])

    def test_rendition_reads_verified_bytes_after_original_path_is_replaced(self) -> None:
        verified = PNG_1X1 + b"x" * (notification_artifacts._RENDITION_THRESHOLD_BYTES + 1)
        candidate = self.artifact_root / "replaced-frame.png"
        replacement = b"different frame after validation"
        candidate.write_bytes(replacement)
        seen = []
        def render(path):
            seen.append(path)
            self.assertNotEqual(path, candidate)
            self.assertEqual(path.read_bytes(), verified)
            self.assertEqual(candidate.read_bytes(), replacement)
            return SimpleNamespace(content=b"bounded-jpeg", content_type="image/jpeg")
        with patch.object(notification_artifacts, "render_mail_image", side_effect=render):
            result = notification_artifacts.SealArtifactResolver._wire_rendition(candidate, verified, "image/png")
        self.assertEqual(result, (b"bounded-jpeg", "image/jpeg"))
        self.assertTrue(seen)
        self.assertTrue(all(not path.exists() for path in seen))
        self.assertEqual(candidate.read_bytes(), replacement)
        with patch.object(notification_artifacts, "render_mail_image", side_effect=OSError("fixture decode failure")):
            self.assertEqual(notification_artifacts.SealArtifactResolver._wire_rendition(candidate, verified, "image/png"),
                             (verified, "image/png"))

    def test_artifact_reads_are_stat_gated_bounded_and_race_checked(self) -> None:
        dispatcher, _ = self.dispatcher()
        run_id = "run-security"
        captured = datetime.now(timezone.utc)

        def create_artifact(artifact_id: str, *, declared_size: int) -> Path:
            path = self.artifact_root / f"{artifact_id}.png"
            path.write_bytes(PNG_1X1)
            self.store.create_resource(
                "artifact",
                resource_id=artifact_id,
                state="captured",
                document={
                    "kind": "screenshot",
                    "capturedAt": captured.isoformat(),
                    "contentType": "image/png",
                    "gameId": "StarRail",
                    "runId": run_id,
                    "hash": hashlib.sha256(PNG_1X1).hexdigest(),
                    "sizeBytes": declared_size,
                    "relativePath": path.name,
                },
            )
            return path

        def validate(artifact_id: str):
            return dispatcher.artifacts._validate_reference(
                artifact_id,
                whitelist={artifact_id},
                allowed_games={"StarRail"},
                allowed_runs={run_id},
                started_at=captured - timedelta(seconds=1),
                sealed_at=captured + timedelta(seconds=1),
            )

        mismatch_id = "artifact-size-race"
        create_artifact(mismatch_id, declared_size=len(PNG_1X1) + 1)
        with patch.object(
            Path,
            "open",
            side_effect=AssertionError("size mismatch must be rejected before open"),
        ):
            self.assertEqual(validate(mismatch_id), "artifact_size_mismatch")

        bounded_id = "artifact-bounded-read"
        create_artifact(bounded_id, declared_size=len(PNG_1X1))
        read_sizes: list[int] = []
        original_open = Path.open

        class TrackingStream:
            def __init__(self, stream) -> None:
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self) -> int:
                return self.stream.fileno()

            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return self.stream.read(size)

        def tracking_open(path: Path, *args, **kwargs):
            return TrackingStream(original_open(path, *args, **kwargs))

        with patch.object(Path, "open", new=tracking_open):
            accepted = validate(bounded_id)
        self.assertIsInstance(accepted, tuple)
        self.assertEqual(read_sizes, [len(PNG_1X1) + 1])

        fstat_calls = 0
        original_fstat = notification_artifacts.os.fstat

        def changing_fstat(file_descriptor: int):
            nonlocal fstat_calls
            snapshot = original_fstat(file_descriptor)
            fstat_calls += 1
            if fstat_calls == 2:
                return SimpleNamespace(
                    st_mode=snapshot.st_mode,
                    st_dev=snapshot.st_dev,
                    st_ino=snapshot.st_ino,
                    st_size=snapshot.st_size,
                    st_mtime_ns=snapshot.st_mtime_ns + 1,
                )
            return snapshot

        with patch.object(
            notification_artifacts.os, "fstat", side_effect=changing_fstat
        ):
            self.assertEqual(validate(bounded_id), "artifact_changed_during_read")


@unittest.skipUnless(os.name == "nt", "DPAPI CurrentUser is Windows-only")
class NotificationDpapiProviderTests(unittest.TestCase):
    def test_current_user_blob_round_trip_and_repr_redacts_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yeyu-dpapi-") as folder:
            root = Path(folder)
            target = root / "profile.dpapi"
            payload = {
                "schemaVersion": 1,
                "bindingId": "self-email",
                "smtpHost": "smtp.test.invalid",
                "smtpPort": 465,
                "smtpUsername": "private-user",
                "smtpPassword": "private-password",
                "senderAddress": "sender@test.invalid",
                "recipientAddress": "recipient@test.invalid",
                "security": "tls",
                "timeoutSeconds": 20,
            }
            environment = dict(os.environ)
            environment["YEYU_TEST_PROFILE_JSON"] = json.dumps(payload)
            environment["YEYU_TEST_PROFILE_OUT"] = str(target)
            script = (
                "$bytes=[Text.Encoding]::UTF8.GetBytes($env:YEYU_TEST_PROFILE_JSON);"
                "$entropy=[Text.Encoding]::UTF8.GetBytes('YeYuGamer.NotificationSecrets.v1');"
                "$cipher=[Security.Cryptography.ProtectedData]::Protect("
                "$bytes,$entropy,[Security.Cryptography.DataProtectionScope]::CurrentUser);"
                "[IO.File]::WriteAllBytes($env:YEYU_TEST_PROFILE_OUT,$cipher);"
                "[Array]::Clear($bytes,0,$bytes.Length);[Array]::Clear($cipher,0,$cipher.Length)"
            )
            subprocess.run(
                ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                env=environment,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            provider = DpapiNotificationSecretProvider(root)
            self.assertEqual(provider.state("self-email"), "configured")
            loaded = provider.load("self-email")
            self.assertEqual(loaded.host, "smtp.test.invalid")
            rendered = repr(loaded)
            for secret in [
                "smtp.test.invalid",
                "private-user",
                "private-password",
                "sender@test.invalid",
                "recipient@test.invalid",
            ]:
                self.assertNotIn(secret, rendered)

    def test_secret_file_read_errors_are_redacted_and_classified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yeyu-dpapi-read-") as folder:
            root = Path(folder)
            (root / "profile.dpapi").write_bytes(b"ciphertext")
            provider = DpapiNotificationSecretProvider(root)
            with patch.object(
                Path,
                "read_bytes",
                side_effect=PermissionError("private filesystem detail"),
            ):
                self.assertEqual(provider.state("self-email"), "invalid")
                with self.assertRaisesRegex(
                    NotificationSecretError, "blob cannot be read"
                ) as caught:
                    provider.load("self-email")
            self.assertNotIn("private filesystem detail", str(caught.exception))


class NotificationApiSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-notification-api-")
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(
            json.dumps(
                {
                    "order": ["StarRail"],
                    "enabled": {"StarRail": True},
                    "dailyScheduleEnabled": False,
                }
            ),
            encoding="utf-8",
        )
        (self.settings.legacy_root / "game-automation-policy.json").write_text(
            json.dumps({"schemaVersion": 2, "games": {"StarRail": {}}}),
            encoding="utf-8",
        )
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text(
            "<!doctype html><div id='app'></div>", encoding="utf-8"
        )
        self.provider = FakeNotificationSecretProvider()
        self.transport = FakeNotificationTransport()
        self.context = TestClient(
            create_app(
                self.settings,
                notification_secret_provider=self.provider,
                notification_transport=self.transport,
            )
        )
        self.client = self.context.__enter__()
        self.manager = self.client.app.state.manager
        batch = self.manager.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["StarRail"],
                "requested_by": "test",
                "result": {"gameDay": "2026-08-28"},
            }
        )
        result = NotificationStoreAndWorkerTests.sealed_result()
        self.manager.store.seal_batch(
            batch["batch_id"],
            state="blocked",
            result=result,
            notification_draft_factory=lambda version, frozen, policy: self.manager.notification_dispatcher.build_draft(
                game_day="2026-08-28",
                batch_id=batch["batch_id"],
                seal_version=version,
                sealed_result=frozen,
                policy=policy,
            ),
        )
        self.notification_id = self.manager.store.list_notification_deliveries()[0][
            "notification_id"
        ]

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()

    def actor_headers(self, actor: str) -> dict[str, str]:
        token_name = "agent.yeyu.token" if actor == "agent" else f"{actor}.token"
        token = (self.settings.actor_tokens_dir / token_name).read_text(
            encoding="ascii"
        ).strip()
        return {
            "Authorization": f"Bearer {token}",
            "X-YeYu-Gamer-Actor": actor,
        }

    def mutation_headers(self, actor: str, key: str) -> dict[str, str]:
        state = self.manager.store.latest_event_sequence()
        return {
            **self.actor_headers(actor),
            "Idempotency-Key": key,
            "If-Match": str(state),
        }

    def test_typed_reads_preview_and_openapi_have_no_transport_secrets(self) -> None:
        headers = self.actor_headers("cli")
        delivery = self.client.get(
            f"/api/v1/notifications/{self.notification_id}", headers=headers
        )
        self.assertEqual(delivery.status_code, 200, delivery.text)
        serialized = json.dumps(delivery.json()).lower()
        for forbidden in ["smtp", "password", "recipientaddress", "htmlbody"]:
            self.assertNotIn(forbidden, serialized)
        preview = self.client.get(
            f"/api/v1/notifications/{self.notification_id}/preview", headers=headers
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("htmlBody", preview.json())
        attempts = self.client.get(
            f"/api/v1/notifications/{self.notification_id}/attempts", headers=headers
        )
        self.assertEqual(attempts.status_code, 200, attempts.text)
        policy = self.client.get("/api/v1/notification-policy", headers=headers)
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["secretState"], "missing")
        openapi = self.client.get("/api/v1/openapi.json").json()
        for path in [
            "/api/v1/notifications/{notification_id}/attempts",
            "/api/v1/notifications/{notification_id}/preview",
            "/api/v1/notifications/{notification_id}/send-requests",
            "/api/v1/notifications/{notification_id}/retry-requests",
            "/api/v1/notification-policy",
        ]:
            self.assertIn(path, openapi["paths"])

    def test_raw_transport_fields_rejected_and_agent_rabiroute_cannot_send_or_configure(self) -> None:
        rejected = self.client.patch(
            "/api/v1/notification-policy",
            json={"smtpHost": "attacker.invalid", "recipientAddress": "x@y.invalid"},
            headers=self.mutation_headers("cli", "raw-transport-fields"),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        rejected_html = self.client.post(
            f"/api/v1/notifications/{self.notification_id}/send-requests",
            json={"reason": "test", "htmlBody": "<img src=x>"},
            headers=self.mutation_headers("cli", "raw-html-field"),
        )
        self.assertEqual(rejected_html.status_code, 422, rejected_html.text)
        for actor in ["agent", "rabiroute"]:
            denied_send = self.client.post(
                f"/api/v1/notifications/{self.notification_id}/send-requests",
                json={"reason": "forbidden"},
                headers=self.mutation_headers(actor, f"{actor}-send"),
            )
            self.assertEqual(denied_send.status_code, 403, denied_send.text)
            denied_policy = self.client.patch(
                "/api/v1/notification-policy",
                json={"enabled": False},
                headers=self.mutation_headers(actor, f"{actor}-policy"),
            )
            self.assertEqual(denied_policy.status_code, 403, denied_policy.text)

    def test_send_request_without_secret_stays_draft_and_is_idempotent(self) -> None:
        path = f"/api/v1/notifications/{self.notification_id}/send-requests"
        first = self.client.post(
            path,
            json={"reason": "try configured self binding"},
            headers=self.mutation_headers("cli", "send-missing-secret"),
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(
            first.json()["result"]["notification"]["dispatchGate"],
            "secret_missing",
        )
        replay_headers = self.actor_headers("cli") | {
            "Idempotency-Key": "send-missing-secret",
            "If-Match": "0",
        }
        replay = self.client.post(
            path,
            json={"reason": "try configured self binding"},
            headers=replay_headers,
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.transport.sent, [])


if __name__ == "__main__":
    unittest.main()
