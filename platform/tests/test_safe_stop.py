from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from yeyu_gamer_platform import cli
from yeyu_gamer_platform.api_client import ManagerApiClient, ManagerApiError


class SafeStopConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        # Exercise the typed protocol above a mocked HTTP transport. No live
        # Manager, credential file or lifecycle controller is accessed.
        self.client = object.__new__(ManagerApiClient)
        self.client.actor = "cli"
        self.client.config = SimpleNamespace(request_timeout_seconds=1)
        self.receipt = {"commandId": "stop-fixture", "acceptedStateVersion": 44}

    def test_cas_conflicts_refresh_versions_and_preserve_key_body_and_route(self) -> None:
        conflict = ManagerApiError("state changed", status_code=412)
        with patch.object(self.client, "_request", side_effect=[
            {"stateVersion": 41}, conflict,
            {"stateVersion": 42}, conflict,
            {"stateVersion": 43}, self.receipt,
        ]) as request:
            result = self.client.request_safe_stop(idempotency_key="same-stop-key")
        self.assertEqual(result.command_id, "stop-fixture")
        self.assertEqual([call.args for call in request.call_args_list], [
            ("GET", "snapshot"), ("POST", "manager/stop-requests"),
        ] * 3)
        posts = request.call_args_list[1::2]
        self.assertEqual([call.kwargs["expected_state_version"] for call in posts], [41, 42, 43])
        for call in posts:
            self.assertEqual(call.kwargs["idempotency_key"], "same-stop-key")
            self.assertEqual(call.kwargs["body"], {"requestedBy": "cli", "reason": "operator-request"})

    def test_repeated_conflicts_stop_after_three_posts_with_original_error(self) -> None:
        conflict = ManagerApiError("state changed", status_code=412, response_body="fixture-conflict")
        with patch.object(self.client, "_request", side_effect=[{"stateVersion": 41}, conflict] * 3) as request:
            with self.assertRaises(ManagerApiError) as caught:
                self.client.request_safe_stop(idempotency_key="bounded-stop")
        self.assertIs(caught.exception, conflict)
        self.assertEqual(request.call_count, 6)

    def test_new_active_batch_is_left_to_manager_busy_gate_without_cancel(self) -> None:
        busy = ManagerApiError("new active work", status_code=409)
        with patch.object(self.client, "_request", side_effect=[
            {"stateVersion": 41, "activeBatch": None},
            ManagerApiError("state changed", status_code=412),
            {"stateVersion": 42, "activeBatch": {"batchId": "new-user-batch"}},
            busy,
        ]) as request:
            with self.assertRaises(ManagerApiError) as caught:
                self.client.request_safe_stop(idempotency_key="preserve-user-work")
        self.assertIs(caught.exception, busy)
        self.assertEqual([call.args for call in request.call_args_list], [
            ("GET", "snapshot"), ("POST", "manager/stop-requests"),
        ] * 2)

    def test_non_cas_errors_and_unknown_transport_outcomes_are_not_retried(self) -> None:
        for status in (None, 401, 403, 409, 422, 428, 500):
            with self.subTest(status=status):
                error = ManagerApiError("fixture failure", status_code=status)
                with patch.object(self.client, "_request", side_effect=[{"stateVersion": 41}, error]) as request:
                    with self.assertRaises(ManagerApiError) as caught:
                        self.client.request_safe_stop(idempotency_key="no-blind-retry")
                self.assertIs(caught.exception, error)
                self.assertEqual(request.call_count, 2)

    def test_explicit_version_is_not_silently_replaced_after_conflict(self) -> None:
        conflict = ManagerApiError("pinned version changed", status_code=412)
        with patch.object(self.client, "_request", side_effect=conflict) as request:
            with self.assertRaises(ManagerApiError):
                self.client.request_safe_stop(idempotency_key="explicit-stop", expected_state_version=9)
        request.assert_called_once_with(
            "POST", "manager/stop-requests",
            body={"requestedBy": "cli", "reason": "operator-request"},
            idempotency_key="explicit-stop", expected_state_version=9,
        )

    def test_lost_response_replay_keeps_the_callers_original_key(self) -> None:
        with patch.object(self.client, "_request", side_effect=[
            {"stateVersion": 41}, self.receipt,
            {"stateVersion": 45}, self.receipt,
        ]) as request:
            first = self.client.request_safe_stop(idempotency_key="replay-stop")
            replay = self.client.request_safe_stop(idempotency_key="replay-stop")
        self.assertEqual(first, replay)
        self.assertEqual([call.kwargs["idempotency_key"] for call in request.call_args_list[1::2]], ["replay-stop"] * 2)

    def test_invalid_refresh_snapshot_does_not_send_another_stop(self) -> None:
        with patch.object(self.client, "_request", side_effect=[
            {"stateVersion": 41}, ManagerApiError("changed", status_code=412),
            {"stateVersion": "invalid"},
        ]) as request:
            with self.assertRaisesRegex(ManagerApiError, "valid non-negative stateVersion"):
                self.client.request_safe_stop(idempotency_key="invalid-refresh")
        self.assertEqual(request.call_count, 3)

    def test_cli_stop_uses_only_the_typed_client_and_never_starts_a_host(self) -> None:
        with (
            patch.object(cli.PlatformConfig, "load", return_value=object()),
            patch.object(cli, "ManagerApiClient") as client,
            patch.object(cli, "LocalManagerController") as controller,
            patch.object(cli.subprocess, "Popen") as popen,
            patch.object(cli, "_write_result"),
        ):
            exit_code = cli.main(("manager-stop", "--idempotency-key", "cli-stop-fixture"))
        self.assertEqual(exit_code, 0)
        client.return_value.request_safe_stop.assert_called_once_with(
            idempotency_key="cli-stop-fixture", expected_state_version=None,
        )
        controller.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
