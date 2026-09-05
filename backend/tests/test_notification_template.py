from __future__ import annotations

import unittest

from yeyu_gamer_manager.services.notifications.screenshots import is_reward_capture

from yeyu_gamer_manager.services.notifications import render_batch_notification


class BatchNotificationTemplateTests(unittest.TestCase):
    def test_reward_step_capture_requires_matching_completed_todo_and_evidence(self) -> None:
        frame = {"artifactId": "a", "kind": "game-ui-step-after-watermarked", "operation": "claim-daily-reward", "todoInstanceId": "t"}
        item = {"todoInstanceId": "t", "operation": "claim-daily-reward", "status": "completed", "evidenceRefs": ["a"]}
        self.assertTrue(is_reward_capture(frame, "WW", [item]))
        for changed in ({**item, "status": "pending"}, {**item, "todoInstanceId": "other"}, {**item, "evidenceRefs": []}):
            self.assertFalse(is_reward_capture(frame, "WW", [changed]))
        self.assertFalse(is_reward_capture({**frame, "operation": "claim-mail"}, "WW", [{**item, "operation": "claim-mail"}]))

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
        self.assertIn("StarRail: review_required", rendered.report_html)
        self.assertIn("agent_review_accepted", rendered.report_html)
        self.assertNotIn("agent_review_accepted", rendered.html_body)

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
        self.assertIn("必做事项 1/1", rendered.text_body)
        self.assertIn("全部完成", rendered.subject)
        self.assertIn("WW: accepted_done", rendered.report_html)
        self.assertIn("completed_skip", rendered.report_html)
        self.assertIn("奖励页识别偶发模糊", rendered.report_html)
        self.assertIn("保持现有验证器", rendered.report_html)
        self.assertNotIn("completed_skip", rendered.html_body)
        self.assertIn("已完成：领取活跃奖励", rendered.html_body)

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
        self.assertIn("领取结果仍待验收", rendered.html_body)
        self.assertIn("不替代每日完成证据合同", rendered.report_html)
        self.assertIn("agent_visual_review", rendered.report_html)
        self.assertNotIn("agent_visual_review", rendered.html_body)

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
        self.assertNotIn("<script>", rendered.report_html)
        self.assertNotIn("<img src=x", rendered.report_html)
        self.assertIn("&lt;script&gt;", rendered.report_html)
        self.assertIn("&lt;img src=x", rendered.report_html)

    def test_accounts_keep_their_result_and_only_one_reward_image_each(self) -> None:
        seal = {
            "notificationOutcome": "blocked",
            "accountTargets": [
                {"gameId": "WW", "accountId": "a", "accountLabel": "主号"},
                {"gameId": "WW", "accountId": "b", "accountLabel": "小号", "state": "human_required"},
            ],
            "todoSnapshot": {
                "WW::a": {"gameId": "WW", "accountId": "a",
                          "summary": {"displayName": "鸣潮", "requiredCompleted": 1, "requiredTotal": 1},
                          "instances": [{"title": "领取活跃奖励", "status": "completed"}]},
                "WW::b": {"gameId": "WW", "accountId": "b",
                          "summary": {"displayName": "鸣潮", "requiredCompleted": 0, "requiredTotal": 1},
                          "instances": [{"title": "领取活跃奖励", "status": "pending"}]},
            },
            "completionContracts": [
                {"gameId": "WW", "accountId": "a", "runId": "run-a", "acceptedDone": True,
                 "screenshotArtifactRefs": ["reward-a-old", "reward-a", "scene-a", "wrong-run"],
                 "acceptedEvidenceRefs": ["reward-a-old", "reward-a"]},
                {"gameId": "WW", "accountId": "b", "runId": "run-b", "outcome": "blocked",
                 "screenshotArtifactRefs": ["scene-b"], "acceptedEvidenceRefs": []},
            ],
            "notificationBlockers": [{"gameId": "WW", "accountId": "b", "reason": "请完成登录验证。"}],
            "sealEvidenceArtifactIds": ["reward-a-old", "reward-a", "scene-a", "scene-b", "wrong-run"],
            "mailScreenshotsByGame": {"WW": [
                {"artifactId": "reward-a-old", "kind": "game-ui-claimed-reward", "capturedAt": "2026-09-05T00:00:00Z"},
                {"artifactId": "reward-a", "kind": "game-ui-claimed-reward", "capturedAt": "2026-09-05T00:01:00Z"},
                {"artifactId": "scene-a", "kind": "game-ui-step-before-raw"},
                {"artifactId": "scene-b", "kind": "game-ui-main-window"},
                {"artifactId": "wrong-run", "kind": "game-ui-claimed-reward", "runId": "run-b"},
                {"artifactId": "not-whitelisted", "kind": "game-ui-claimed-reward"},
            ]},
        }
        rendered = render_batch_notification(game_day="2026-09-05", batch_id="batch-accounts", seal_version=2, sealed_result=seal)
        self.assertIn("1 款游戏 · 2 个账号任务 · 已验收 1/2", rendered.html_body)
        first, second = rendered.html_body.split("鸣潮 · 小号", 1)
        self.assertIn("鸣潮 · 主号", first)
        self.assertIn("已完成：领取活跃奖励", first)
        self.assertIn('src="cid:evidence-reward-a"', first)
        self.assertIn("需人工处理", second)
        self.assertIn("请完成登录验证", second)
        self.assertNotIn('src="cid:', second)
        self.assertEqual(rendered.html_body.count("<img "), 1)
        self.assertNotIn("<details", rendered.html_body)
        self.assertNotIn("<details open", rendered.report_html)
        self.assertIn('src="cid:evidence-scene-b"', rendered.report_html)
        self.assertIn('src="cid:evidence-reward-a-old"', rendered.report_html)
        self.assertNotIn("cid:evidence-wrong-run", rendered.report_html)
        self.assertNotIn("cid:evidence-not-whitelisted", rendered.report_html)
        self.assertEqual(rendered, render_batch_notification(game_day="2026-09-05", batch_id="batch-accounts", seal_version=2, sealed_result=seal))

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
