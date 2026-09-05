from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeyu_gamer_manager.store.sqlite_store import (
    BATCH_EVENT_RESULT_VALUE_LIMIT,
    SqliteStore,
    _batch_event_payload,
)


class LedgerRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-ledger-")
        self.addCleanup(self.temporary.cleanup)
        self.store = SqliteStore(Path(self.temporary.name) / "manager.sqlite3")
        self.store.initialize()
        self.addCleanup(self.store.close)

    def _batch(self, result: dict | None = None) -> dict:
        return self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "queued",
                "game_ids": ["WW"],
                "requested_by": "test",
                "result": result or {},
            }
        )

    def test_batch_events_do_not_embed_heavy_result_documents(self) -> None:
        heavy = {"todoSnapshot": {"x": "y" * (BATCH_EVENT_RESULT_VALUE_LIMIT * 4)}}
        batch = self._batch()
        self.store.update_batch(
            batch["batch_id"],
            state="running",
            result={**heavy, "gameDay": "2026-09-03", "candidateGameIds": ["WW"]},
        )
        events = [
            event
            for event in self.store.list_events(0, 100)
            if event["event_type"] == "batch.updated"
        ]
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["result"]["gameDay"], "2026-09-03")
        self.assertNotIn("todoSnapshot", payload["result"])
        self.assertIn("todoSnapshot", payload["result"]["_omittedResultKeys"])
        # The authoritative record keeps the full document.
        self.assertIn("todoSnapshot", self.store.get_batch(batch["batch_id"])["result"])

    def test_compact_rewrites_legacy_full_payload_events(self) -> None:
        batch = self._batch()
        heavy_record = {
            **self.store.get_batch(batch["batch_id"]),
            "result": {"todoPlans": {"k": "v" * 20000}, "gameDay": "2026-09-01"},
        }
        self.store.append_event("batch.updated", "batch", batch["batch_id"], heavy_record)
        before = self.store.ledger_size_report()["rows"]["events"]
        rewritten = self.store.compact_batch_event_payloads(minimum_bytes=1024)
        self.assertEqual(rewritten, 1)
        self.assertEqual(self.store.ledger_size_report()["rows"]["events"], before)
        latest = self.store.list_events(0, 100)[-1]
        self.assertNotIn("todoPlans", latest["payload"]["result"])
        self.assertEqual(latest["payload"]["result"]["gameDay"], "2026-09-01")
        self.assertEqual(self.store.compact_batch_event_payloads(minimum_bytes=1024), 0)

    def test_prune_keeps_latest_entity_event_protected_types_and_state_version(self) -> None:
        batch = self._batch()
        for index in range(5):
            self.store.update_batch(
                batch["batch_id"], state="running", result={"tick": index}
            )
        self.store.append_event(
            "todo.current-period-explicit-reset",
            "todo-instance",
            "todo-instance-1",
            {"reason": "test"},
        )
        version_before = self.store.latest_event_sequence()
        old_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        # Age every row so the retention window applies to all of them.
        with self.store.atomic():
            self.store.connection.execute(
                "UPDATE events SET created_at = ?", (old_cutoff.isoformat(),)
            )
        pruned = self.store.prune_ledger(event_retention_days=14)
        self.assertGreater(pruned["events"], 0)
        remaining = self.store.list_events(0, 1000)
        types = [event["event_type"] for event in remaining]
        self.assertIn("todo.current-period-explicit-reset", types)
        # The newest batch event survives so entity_revision still answers.
        batch_events = [e for e in remaining if e["entity_id"] == batch["batch_id"]]
        self.assertEqual(len(batch_events), 1)
        self.assertEqual(batch_events[0]["payload"]["result"]["tick"], 4)
        self.assertEqual(self.store.latest_event_sequence(), version_before)
        self.assertEqual(
            self.store.entity_revision("batch", batch["batch_id"]),
            batch_events[0]["sequence"],
        )
        # A new event continues the sequence rather than reusing a pruned one.
        self.store.append_event("manager.started", "manager", "m", {})
        self.assertEqual(self.store.latest_event_sequence(), version_before + 1)

    def test_prune_removes_aged_idempotency_and_settled_receipts_together(self) -> None:
        self.store.run_idempotent(
            key="cli-old",
            method="POST",
            path="/api/v1/batches",
            request={"a": 1},
            status_code=202,
            expected_state_version=None,
            operation=lambda: {"commandId": "cmd-old"},
        )
        self.store.save_command_receipt(
            "cmd-old", {"commandId": "cmd-old", "state": "succeeded"}
        )
        self.store.save_command_receipt(
            "cmd-running", {"commandId": "cmd-running", "state": "running"}
        )
        aged = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with self.store.atomic():
            self.store.connection.execute("UPDATE idempotency SET created_at = ?", (aged,))
            self.store.connection.execute("UPDATE command_receipts SET updated_at = ?", (aged,))
        pruned = self.store.prune_ledger(receipt_retention_days=14)
        self.assertEqual(pruned["idempotency"], 1)
        self.assertEqual(pruned["commandReceipts"], 1)
        rows = self.store.connection.execute(
            "SELECT command_id FROM command_receipts"
        ).fetchall()
        self.assertEqual([row["command_id"] for row in rows], ["cmd-running"])

    def test_batch_event_payload_projection_is_pure(self) -> None:
        record = {"batch_id": "b", "result": {"small": 1, "big": "z" * 5000}}
        projected = _batch_event_payload(record)
        self.assertEqual(record["result"]["big"], "z" * 5000)
        self.assertEqual(projected["result"]["small"], 1)
        self.assertEqual(json.loads(json.dumps(projected))["result"]["_omittedResultKeys"]["big"], 5002)


if __name__ == "__main__":
    unittest.main()
