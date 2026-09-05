from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.current_completion import (
    CurrentCompletionStatus,
    project_current_game_completion,
)


class CurrentCompletionProjectionTests(unittest.TestCase):
    scope = {
        "scopeKey": "daily:2026-08-28:scope",
        "scopeFingerprint": "a" * 64,
        "periodKeys": ["daily:2026-08-28T04:00:00+08:00"],
    }

    def contract(
        self,
        *,
        outcome: str = "accepted_done",
        accepted: bool = True,
        game_id: str = "StarRail",
        game_day_key: str | None = None,
        artifact_id: str = "artifact-current",
    ) -> dict:
        return {
            "completionAdjudicationId": f"adjudication-{outcome}-{artifact_id}",
            "outcome": outcome,
            "acceptedDone": accepted,
            "policyId": "StarRail.daily",
            "policyVersion": "1+sha256.test",
            "gameId": game_id,
            "runId": "run-current",
            "runAttemptId": "attempt-current",
            "attemptLineageIds": ["attempt-current"],
            "gameDayKey": game_day_key or self.scope["periodKeys"][0],
            "evaluations": [],
            "missingPredicates": [],
            "blockingPredicates": [],
            "blockerIds": [],
            "artifactRefs": [artifact_id],
            "screenshotArtifactRefs": [artifact_id],
            "acceptedEvidenceRefs": [artifact_id],
            "currentAttemptEvidenceRefs": [artifact_id],
            "carriedEvidenceRefs": [],
            "supportingArtifactRefs": [artifact_id],
            "excludedEvidence": [],
            "reviewId": f"review-{artifact_id}",
            "message": "immutable contract decision",
        }

    def seal(
        self,
        *,
        seal_version: int,
        contracts: list[dict],
        state: str = "done",
        scope: dict | None = None,
        batch_id: str | None = None,
    ) -> dict:
        game_scope = dict(scope or self.scope)
        return {
            "batch_id": batch_id or f"batch-{seal_version}",
            "state": state,
            "rewardClaimed": True,
            "result": {
                "sealVersion": seal_version,
                "sealedAt": f"2026-08-28T12:00:{seal_version:02d}+00:00",
                "todoScope": {
                    "schemaVersion": 1,
                    "games": [
                        {
                            "gameId": "StarRail",
                            **game_scope,
                        }
                    ],
                },
                "completionContracts": contracts,
                "sealEvidenceArtifactIds": ["unrelated-batch-artifact"],
            },
        }

    def test_no_current_seal_returns_none(self) -> None:
        result = project_current_game_completion(
            game_id="StarRail", current_scope=self.scope, sealed_batches=[]
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)
        self.assertIsNone(result.batch_id)

    def test_matching_accepted_contract_is_current_done_authority(self) -> None:
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[self.seal(seal_version=10, contracts=[self.contract()])],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.ACCEPTED_DONE)
        self.assertEqual(result.batch_id, "batch-10")
        self.assertEqual(result.review_id, "review-artifact-current")
        self.assertEqual(
            result.decision_id,
            "adjudication-accepted_done-artifact-current",
        )
        self.assertEqual(result.evidence_ids, ("artifact-current",))

    def test_newest_current_contract_supersedes_an_older_acceptance(self) -> None:
        older = self.seal(
            seal_version=10,
            contracts=[self.contract(artifact_id="artifact-old")],
        )
        newer = self.seal(
            seal_version=11,
            state="blocked",
            contracts=[
                self.contract(
                    outcome="blocked",
                    accepted=False,
                    artifact_id="artifact-new",
                )
            ],
        )
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            # Seal order, rather than caller list order, is authoritative.
            sealed_batches=[older, newer],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.EVIDENCE_PENDING)
        self.assertEqual(result.batch_id, "batch-11")
        self.assertEqual(result.evidence_ids, ("artifact-new",))

    def test_explicit_reset_watermark_invalidates_older_same_scope_seal(self) -> None:
        older = self.seal(
            seal_version=10,
            contracts=[self.contract(artifact_id="artifact-before-reset")],
        )
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[older],
            invalidated_at="2026-08-28T12:01:00+00:00",
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)
        self.assertIsNone(result.batch_id)

    def test_post_reset_seal_can_establish_current_acceptance(self) -> None:
        newer = self.seal(
            seal_version=10,
            contracts=[self.contract(artifact_id="artifact-after-reset")],
        )
        newer["result"]["sealedAt"] = "2026-08-28T12:02:00+00:00"
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[newer],
            invalidated_at="2026-08-28T12:01:00+00:00",
        )
        self.assertEqual(result.status, CurrentCompletionStatus.ACCEPTED_DONE)
        self.assertEqual(result.batch_id, "batch-10")

    def test_stale_period_and_changed_todo_scope_are_ignored(self) -> None:
        stale_period = self.seal(
            seal_version=12,
            contracts=[
                self.contract(game_day_key="daily:2026-08-27T04:00:00+08:00")
            ],
        )
        changed_scope = {
            **self.scope,
            "scopeFingerprint": "b" * 64,
        }
        stale_definition = self.seal(
            seal_version=13,
            contracts=[self.contract()],
            scope=changed_scope,
        )
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[stale_definition, stale_period],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)

    def test_unsealed_or_legacy_scope_cannot_establish_acceptance(self) -> None:
        unsealed = self.seal(seal_version=14, contracts=[self.contract()])
        del unsealed["result"]["sealVersion"]
        legacy = self.seal(seal_version=15, contracts=[self.contract()])
        del legacy["result"]["todoScope"]
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[unsealed, legacy],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)

    def test_inconsistent_contract_cannot_revive_legacy_reward_flag(self) -> None:
        malformed = self.contract(outcome="blocked", accepted=True)
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[self.seal(seal_version=16, contracts=[malformed])],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)

    def test_contract_without_immutable_completion_review_is_ignored(self) -> None:
        no_review = self.contract()
        no_review["reviewId"] = None
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[self.seal(seal_version=17, contracts=[no_review])],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.NONE)

    def test_cancelled_batch_never_projects_accepted_done(self) -> None:
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[
                self.seal(
                    seal_version=18,
                    state="cancelled",
                    contracts=[self.contract()],
                )
            ],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.EVIDENCE_PENDING)
        self.assertEqual(
            result.reason_code,
            "accepted_contract_belongs_to_cancelled_batch",
        )

    def test_game_projection_uses_only_its_contract_evidence(self) -> None:
        other = self.contract(
            game_id="ZZZ",
            artifact_id="artifact-zzz",
        )
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[
                self.seal(
                    seal_version=19,
                    contracts=[other, self.contract()],
                )
            ],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.ACCEPTED_DONE)
        self.assertNotIn("artifact-zzz", result.evidence_ids)
        self.assertNotIn("unrelated-batch-artifact", result.evidence_ids)

    def test_duplicate_current_game_contracts_fail_closed(self) -> None:
        result = project_current_game_completion(
            game_id="StarRail",
            current_scope=self.scope,
            sealed_batches=[
                self.seal(
                    seal_version=20,
                    contracts=[
                        self.contract(artifact_id="artifact-one"),
                        self.contract(artifact_id="artifact-two"),
                    ],
                )
            ],
        )
        self.assertEqual(result.status, CurrentCompletionStatus.EVIDENCE_PENDING)
        self.assertEqual(
            result.reason_code,
            "ambiguous_current_completion_contracts",
        )
        self.assertEqual(
            set(result.evidence_ids),
            {"artifact-one", "artifact-two"},
        )


if __name__ == "__main__":
    unittest.main()
