from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.notifications import render_batch_notification


class BatchNotificationTemplateTests(unittest.TestCase):
    def test_renders_manager_completion_contract_list_shape(self) -> None:
        rendered = render_batch_notification(
            game_day="2026-08-28",
            batch_id="batch-contract-list",
            seal_version=13,
            sealed_result={
                "notificationOutcome": "blocked",
                "outcome": "review_required",
                "acceptedDone": False,
                "unresolvedRequiredTodoIds": [],
                "completionContracts": [
                    {
                        "gameId": "StarRail",
                        "outcome": "review_required",
                        "acceptedDone": False,
                        "missingPredicates": ["agent_review_accepted"],
                    }
                ],
                "todoSnapshot": {
                    "StarRail": {
                        "summary": {"requiredCompleted": 1, "requiredTotal": 1},
                        "instances": [],
                    }
                },
            },
        )
        self.assertIn("StarRail: review_required", rendered.text_body)
        self.assertIn("agent_review_accepted", rendered.text_body)

    def test_completed_uses_frozen_manager_notification_outcome(self) -> None:
        rendered = render_batch_notification(
            game_day="2026-08-28",
            batch_id="batch-1",
            seal_version=17,
            sealed_result={
                "notificationOutcome": "completed",
                "outcome": "done",
                "acceptedDone": True,
                "unresolvedRequiredTodoIds": [],
                "completionContracts": {
                    "WW": {
                        "status": "accepted_done",
                        "contractVersion": "completion-contract.v1",
                        "missingPredicates": [],
                    }
                },
                "todoSnapshot": {
                    "WW": {
                        "summary": {"requiredCompleted": 1, "requiredTotal": 1},
                        "instances": [
                            {
                                "title": "领取活跃奖励",
                                "status": "completed",
                                "attempts": 1,
                                "automationDifficulty": "medium",
                                "dispatchDisposition": "completed_skip",
                                "dispatchReason": "Manager 已确认该 Todo 完成",
                                "latestTodoAttempt": {
                                    "state": "completed",
                                    "attemptNumber": 1,
                                },
                                "latestAutomationAssessment": {
                                    "difficulty": "moderate",
                                    "automatable": True,
                                    "issue": "奖励页识别偶发模糊",
                                    "recommendation": "保持现有验证器",
                                },
                                "orderIndex": 10,
                            }
                        ],
                    }
                },
            },
        )
        self.assertEqual(rendered.outcome, "completed")
        self.assertIn("Required Todo：1/1", rendered.text_body)
        self.assertIn("全部完成", rendered.subject)
        self.assertIn("WW: accepted_done", rendered.text_body)
        self.assertIn("completed_skip", rendered.text_body)
        self.assertIn("奖励页识别偶发模糊", rendered.text_body)
        self.assertIn("保持现有验证器", rendered.text_body)

    def test_all_todo_checked_does_not_override_incomplete_contract(self) -> None:
        rendered = render_batch_notification(
            game_day="2026-08-28",
            batch_id="batch-2",
            seal_version=18,
            sealed_result={
                "notificationOutcome": "blocked",
                "outcome": "review_required",
                "acceptedDone": False,
                "unresolvedRequiredTodoIds": [],
                "completionContracts": {
                    "StarRail": {
                        "status": "evidence_pending",
                        "contractVersion": "completion-contract.v1",
                        "missingPredicates": ["agent_visual_review", "reward_frame"],
                    }
                },
                "todoSnapshot": {
                    "StarRail": {
                        "summary": {"requiredCompleted": 4, "requiredTotal": 4},
                        "instances": [],
                    }
                },
            },
        )
        self.assertEqual(rendered.outcome, "blocked")
        self.assertIn("完成受阻", rendered.subject)
        self.assertIn("不替代每日完成证据合同", rendered.html_body)
        self.assertIn("agent_visual_review", rendered.html_body)

    def test_untrusted_todo_text_is_html_escaped(self) -> None:
        rendered = render_batch_notification(
            game_day="2026-08-28",
            batch_id="batch-3",
            seal_version=19,
            sealed_result={
                "notificationOutcome": "blocked",
                "outcome": "blocked",
                "acceptedDone": False,
                "unresolvedRequiredTodoIds": ["todo-1"],
                "todoSnapshot": {
                    "NTE": {
                        "summary": {"requiredCompleted": 0, "requiredTotal": 1},
                        "instances": [
                            {
                                "title": "<script>alert(1)</script>",
                                "status": "blocked",
                                "attempts": 2,
                                "reason": "<img src=x onerror=alert(2)>",
                            }
                        ],
                    }
                },
            },
        )
        self.assertNotIn("<script>", rendered.html_body)
        self.assertNotIn("<img src=x", rendered.html_body)
        self.assertIn("&lt;script&gt;", rendered.html_body)
        self.assertIn("&lt;img src=x", rendered.html_body)

    def test_renderer_refuses_to_infer_notification_outcome(self) -> None:
        with self.assertRaisesRegex(ValueError, "notificationOutcome"):
            render_batch_notification(
                game_day="2026-08-28",
                batch_id="batch-untyped",
                seal_version=20,
                sealed_result={
                    "outcome": "done",
                    "acceptedDone": True,
                    "unresolvedRequiredTodoIds": [],
                    "todoSnapshot": {},
                },
            )


if __name__ == "__main__":
    unittest.main()
