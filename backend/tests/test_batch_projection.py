from __future__ import annotations

import unittest
from datetime import datetime, timezone

from yeyu_gamer_manager.services.batch_projection import project_batch_record


class BatchProjectionTests(unittest.TestCase):
    def test_resume_projection_uses_only_supplied_readiness_facts(self) -> None:
        timestamp = datetime(2026, 9, 5, tzinfo=timezone.utc)
        item = {
            "batch_id": "batch-1",
            "cadence": "daily",
            "mode": "execute",
            "state": "review_required",
            "game_ids": ["StarRail"],
            "requested_by": "test",
            "created_at": timestamp,
            "updated_at": timestamp,
            "result": {
                "recoveryPhase": {
                    "schemaVersion": 1,
                    "status": "ready_for_resume",
                    "affectedRunIds": ["run-1"],
                }
            },
            "run_memberships": [
                {
                    "batch_id": "batch-1",
                    "run_id": "run-1",
                    "ordinal": 0,
                    "role": "initial",
                    "state": "queued",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            ],
        }

        ready = project_batch_record(
            item,
            execution_readiness=(True, "execution_ready", "ready"),
            terminal_retry_run_ids=[],
            pending_with_attempt_ids=[],
            run_resume_targets=[],
            run_reconcile_targets=[],
        )
        unavailable = project_batch_record(
            item,
            execution_readiness=None,
            terminal_retry_run_ids=[],
            pending_with_attempt_ids=[],
            run_resume_targets=[],
            run_reconcile_targets=[],
        )

        self.assertTrue(ready.result["batchActionAvailability"]["resume"])
        self.assertEqual(
            ready.result["batchActionAvailability"]["nextAction"], "resume_batch"
        )
        self.assertFalse(unavailable.result["batchActionAvailability"]["resume"])
        self.assertEqual(
            unavailable.result["batchActionAvailability"]["reasonCode"],
            "refresh_batch_projection",
        )
        self.assertNotIn("batchActionAvailability", item["result"])


if __name__ == "__main__":
    unittest.main()
