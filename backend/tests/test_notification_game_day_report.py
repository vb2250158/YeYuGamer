"""One report per game day.

Before 2026-09-22 the only mail was a round report built from the completion
contracts of the Batch that had just sealed.  Two things were wrong with it:
every retry round produced another alarming mail, and because acceptance was
adjudicated per round, a game accepted earlier the same day fell back to
"本轮未执行" while the operator actually had several games accepted.

These tests pin the replacement: a day-scoped report that uses the same
selection-aware projection the WebGUI shows, is held while the day is still
running, and is released at most once per game day.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.services.notifications import (
    FakeNotificationSecretProvider,
    FakeNotificationTransport,
    SmtpProfile,
)
from yeyu_gamer_manager.services.notifications.template import (
    render_game_day_notification,
)
from yeyu_gamer_manager.settings import Settings

GAME_DAY = "2026-09-22"
NEXT_GAME_DAY = "2026-09-23"


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


def game_entry(
    game_id: str,
    *,
    accepted: bool = False,
    failed: bool = False,
    human: bool = False,
    completed: int = 0,
    total: int = 7,
    order: int = 0,
) -> dict[str, object]:
    if human:
        acceptance, runtime = "not_started", "human_required"
    elif failed:
        acceptance, runtime = "not_started", "failed"
    elif accepted:
        acceptance, runtime = "accepted_done", "completed"
    else:
        acceptance, runtime = "not_started", "done"
    return {
        "gameId": game_id,
        "displayName": game_id,
        "orderIndex": order,
        "acceptanceState": acceptance,
        "runtimeState": runtime,
        "requiredCompleted": completed,
        "requiredTotal": total,
        "allRequiredCompleted": bool(total) and completed == total,
        "doneTitles": ["领取每日奖励"] if accepted else [],
        "attention": "" if accepted else "需要重试",
    }


class GameDayTemplateTests(unittest.TestCase):
    def test_a_game_accepted_earlier_the_same_day_still_reads_completed(self) -> None:
        rendered = render_game_day_notification(
            game_day=GAME_DAY,
            entries=[
                game_entry("PGR", accepted=True, completed=7, total=7),
                game_entry("ZZZ", failed=True, completed=0, total=9, order=1),
            ],
        )
        # The round renderer answered "本轮未执行" here, which is exactly the
        # misleading line the operator reported.
        self.assertIn("已完成：领取每日奖励", rendered.html_body)
        self.assertNotIn("本轮未执行", rendered.html_body)
        self.assertNotIn("本轮未执行", rendered.text_body)
        self.assertEqual("blocked", rendered.outcome)
        self.assertIn("已完成 1/2", rendered.subject)
        self.assertIn(GAME_DAY, rendered.subject)

    def test_a_fully_completed_day_reports_one_clear_result(self) -> None:
        rendered = render_game_day_notification(
            game_day=GAME_DAY,
            entries=[
                game_entry("PGR", accepted=True, completed=7, total=7),
                game_entry("GF2", accepted=True, completed=7, total=7, order=1),
            ],
        )
        self.assertEqual("completed", rendered.outcome)
        self.assertIn("全部完成", rendered.subject)
        self.assertIn("2/2", rendered.subject)
        self.assertNotIn("待处理", rendered.subject)

    def test_the_day_report_declares_that_it_is_one_per_day(self) -> None:
        rendered = render_game_day_notification(
            game_day=GAME_DAY,
            entries=[game_entry("PGR", accepted=True, completed=7, total=7)],
        )
        self.assertIn("一天一封", rendered.text_body)
        self.assertIn("不再按重试轮次单独发送", rendered.text_body)

    def test_a_day_report_requires_at_least_one_game(self) -> None:
        with self.assertRaises(ValueError):
            render_game_day_notification(game_day=GAME_DAY, entries=[])


class GameDayReportManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-game-day-report-")
        self.root = Path(self.temporary.name)
        self.settings = Settings.for_test(self.root)
        self.settings.legacy_root.mkdir(parents=True)
        (self.settings.legacy_root / "daily-gui-config.json").write_text(
            json.dumps(
                {
                    "order": ["PGR"],
                    "enabled": {"PGR": True},
                    "dailyScheduleEnabled": False,
                }
            ),
            encoding="utf-8",
        )
        (self.settings.legacy_root / "game-automation-policy.json").write_text(
            json.dumps({"schemaVersion": 2, "games": {"PGR": {}}}),
            encoding="utf-8",
        )
        self.settings.web_dist.mkdir(parents=True)
        (self.settings.web_dist / "index.html").write_text(
            "<!doctype html><div id='app'></div>", encoding="utf-8"
        )
        self.provider = FakeNotificationSecretProvider({"self-email": profile()})
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
        self.store = self.manager.store

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------

    def create_batch(self, tag: str) -> str:
        batch = self.store.create_batch(
            {
                "cadence": "daily",
                "mode": "execute",
                "state": "running",
                "game_ids": ["PGR"],
                "requested_by": f"test-{tag}",
                "result": {"gameDay": "daily:manager:deadbeef"},
            }
        )
        return str(batch["batch_id"])

    def seal_day_report(
        self, *, tag: str, report_day: str = GAME_DAY, hold: bool = True
    ) -> str:
        """Seal a Batch whose mail is a held game-day report."""

        batch_id = self.create_batch(tag)

        def factory(version: int, frozen: dict, policy: dict) -> dict:
            del version, frozen, policy
            return {
                "outcome": "blocked",
                "subject": f"每日结果 {tag}",
                "text_body": f"body {tag}",
                "html_body": f"<p>body {tag}</p>",
                "attachment_refs": [],
                "secret_state": self.manager.notification_secret_provider.state(
                    "self-email"
                ),
                "dispatch_disposition": "",
                "report_day": report_day,
                "dispatch_hold": hold,
            }

        self.store.seal_batch(
            batch_id,
            state="review_required",
            result={
                "gameDay": "daily:manager:deadbeef",
                "notificationOutcome": "blocked",
                "finalGameRunIds": [],
                "todoSnapshot": {},
                "unresolvedRequiredTodoIds": [],
                "sealEvidenceArtifactIds": [],
                "acceptedDone": False,
            },
            notification_draft_factory=factory,
        )
        return batch_id

    def day_rows(self, report_day: str = GAME_DAY) -> dict[str, dict]:
        return {
            str(row["text_body"]): row
            for row in self.store.list_game_day_notifications(
                report_day, channel="email", recipient_binding_id="self-email"
            )
        }

    def dispatch(self, day: str, entries: list[dict], final: bool | None = None) -> int:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    self.manager, "_game_day_report_snapshot", return_value=(day, entries)
                )
            )
            if final is not None:
                stack.enter_context(
                    patch.object(
                        self.manager,
                        "_game_day_report_finality",
                        return_value=(final, "tested"),
                    )
                )
            return self.manager.dispatch_game_day_reports()

    # -- policy ----------------------------------------------------------

    def test_the_report_scope_defaults_to_the_game_day(self) -> None:
        policy = self.store.get_notification_policy()
        self.assertEqual("game_day", policy["report_scope"])

    def test_the_report_scope_only_accepts_known_values(self) -> None:
        updated = self.store.update_notification_policy(
            {"report_scope": "batch"}, requested_by="test"
        )
        self.assertEqual("batch", updated["report_scope"])
        with self.assertRaises(ValueError):
            self.store.update_notification_policy(
                {"report_scope": "daily"}, requested_by="test"
            )

    def test_the_game_day_scope_marks_the_day_and_holds_the_report(self) -> None:
        with patch.object(
            self.manager,
            "_game_day_report_snapshot",
            return_value=(GAME_DAY, [game_entry("PGR", completed=0, total=7)]),
        ):
            draft = self.manager._notification_draft_for_seal(
                batch_id="batch-1",
                seal_version=1,
                sealed={"gameDay": "daily:manager:deadbeef"},
                policy={**self.store.get_notification_policy()},
            )
        self.assertEqual(GAME_DAY, draft["report_day"])
        self.assertTrue(draft["dispatch_hold"])
        # A day report spans several seals: it must not inherit either the
        # batch's staleness or its screenshots.
        self.assertEqual("", draft["dispatch_disposition"])
        self.assertEqual([], draft["attachment_refs"])
        self.assertIn(GAME_DAY, draft["subject"])

    def test_the_batch_scope_still_builds_the_legacy_round_report(self) -> None:
        draft = self.manager._notification_draft_for_seal(
            batch_id="batch-1",
            seal_version=1,
            sealed={
                "gameDay": "daily:manager:deadbeef",
                "notificationOutcome": "blocked",
                "finalGameRunIds": [],
                "todoSnapshot": {},
                "unresolvedRequiredTodoIds": [],
                "sealEvidenceArtifactIds": [],
                "acceptedDone": False,
            },
            policy={**self.store.get_notification_policy(), "report_scope": "batch"},
        )
        self.assertEqual("", draft.get("report_day", ""))
        self.assertFalse(draft.get("dispatch_hold", False))

    # -- finality --------------------------------------------------------

    def test_a_human_gate_is_not_on_its_own_a_final_day(self) -> None:
        # Releasing on the first gate would mail a "需人工处理" report and then a
        # second, contradictory one once the gate is released and the day ends.
        final, _ = self.manager._game_day_report_finality(
            [
                game_entry("PGR", human=True, completed=0, total=7),
                game_entry("GF2", accepted=True, completed=7, total=7, order=1),
            ]
        )
        self.assertFalse(final)

    def test_a_day_with_an_unfinished_game_is_not_final(self) -> None:
        final, _ = self.manager._game_day_report_finality(
            [game_entry("PGR", accepted=True, completed=7, total=7),
             game_entry("ZZZ", completed=8, total=9, order=1)]
        )
        self.assertFalse(final)

    def test_a_day_whose_games_are_all_completed_is_final(self) -> None:
        with patch.object(self.manager.store, "active_execution_summary", return_value={}):
            final, reason = self.manager._game_day_report_finality(
                [game_entry("PGR", accepted=True, completed=7, total=7)]
            )
        self.assertTrue(final)
        self.assertEqual("all_required_completed", reason)

    # -- dispatch --------------------------------------------------------

    def test_a_running_day_holds_its_report_instead_of_mailing_every_round(self) -> None:
        self.seal_day_report(tag="first")
        self.seal_day_report(tag="second")
        released = self.dispatch(
            GAME_DAY, [game_entry("PGR", completed=3, total=7)], final=False
        )
        self.assertEqual(0, released)
        held = self.store.list_game_day_pending_notifications()
        self.assertEqual(1, len(held))
        # Newest first: the day keeps the report that saw the most of it.
        self.assertEqual("body second", held[0]["text_body"])
        self.assertEqual("superseded", self.day_rows()["body first"]["state"])

    def test_a_settled_day_releases_exactly_one_report(self) -> None:
        self.seal_day_report(tag="first")
        self.seal_day_report(tag="second")
        entries = [game_entry("PGR", accepted=True, completed=7, total=7)]
        self.assertEqual(1, self.dispatch(GAME_DAY, entries, final=True))
        # A second tick must not mail the day again.
        self.assertEqual(0, self.dispatch(GAME_DAY, entries, final=True))
        rows = self.day_rows()
        self.assertNotEqual("game_day_pending", rows["body second"]["dispatch_gate"])
        self.assertEqual("superseded", rows["body first"]["state"])

    def test_a_batch_scope_policy_never_releases_a_day_report(self) -> None:
        self.seal_day_report(tag="only")
        self.store.update_notification_policy(
            {"report_scope": "batch"}, requested_by="test"
        )
        released = self.dispatch(
            GAME_DAY, [game_entry("PGR", accepted=True, completed=7, total=7)], final=True
        )
        self.assertEqual(0, released)
        self.assertEqual("game_day_pending", self.day_rows()["body only"]["dispatch_gate"])

    def test_a_rolled_over_day_releases_its_last_held_report_once(self) -> None:
        self.seal_day_report(tag="last")
        self.assertEqual(
            1, self.dispatch(NEXT_GAME_DAY, [game_entry("PGR", completed=5, total=7)])
        )
        self.assertEqual(
            0, self.dispatch(NEXT_GAME_DAY, [game_entry("PGR", completed=5, total=7)])
        )
        self.assertNotEqual(
            "game_day_pending", self.day_rows()["body last"]["dispatch_gate"]
        )

    def test_a_day_that_already_reported_is_not_mailed_again(self) -> None:
        # The first seal already settled the day and went out; a later seal of
        # the same day must only be retired, never sent.
        self.seal_day_report(tag="sent", hold=False)
        self.seal_day_report(tag="late", hold=True)
        self.assertEqual(
            0, self.dispatch(NEXT_GAME_DAY, [game_entry("PGR", completed=5, total=7)])
        )
        rows = self.day_rows()
        self.assertEqual("superseded", rows["body late"]["state"])
        self.assertNotEqual("superseded", rows["body sent"]["state"])

    def test_a_late_seal_of_an_already_reported_day_is_retired_too(self) -> None:
        # Same situation, but the day has not rolled over yet: the late held
        # report must be retired instead of becoming a second mail for the day.
        self.seal_day_report(tag="sent", hold=False)
        self.seal_day_report(tag="late", hold=True)
        self.assertEqual(
            0,
            self.dispatch(
                GAME_DAY,
                [game_entry("PGR", accepted=True, completed=7, total=7)],
                final=True,
            ),
        )
        self.assertEqual("superseded", self.day_rows()["body late"]["state"])

    def test_a_report_for_an_unreached_day_is_never_mailed(self) -> None:
        self.seal_day_report(tag="future", report_day="2026-10-01")
        self.assertEqual(0, self.dispatch(GAME_DAY, [game_entry("PGR", completed=5, total=7)]))
        self.assertEqual(
            "game_day_pending", self.day_rows("2026-10-01")["body future"]["dispatch_gate"]
        )

    def test_a_released_day_report_reaches_the_transport_once(self) -> None:
        """End to end: a settled day is actually delivered, without screenshot bytes."""

        self.seal_day_report(tag="only")
        entries = [game_entry("PGR", accepted=True, completed=7, total=7)]
        self.assertEqual(1, self.dispatch(GAME_DAY, entries, final=True))
        self.manager.notification_dispatcher.run_once()
        sent = list(self.transport.sent)
        self.assertEqual(1, len(sent))
        self.assertEqual("body only", sent[0].text_body)
        # A day report spans several seals, so it can never carry the frozen
        # screenshot bytes of just one of them.
        self.assertEqual((), sent[0].attachments)


if __name__ == "__main__":
    unittest.main()
