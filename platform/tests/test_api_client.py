from __future__ import annotations

import json
from copy import deepcopy
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from yeyu_gamer_platform.agent_client import (
    AgentManagerClient,
    RabiRouteManagerClient,
)
from yeyu_gamer_platform.api_client import ManagerApiClient
from yeyu_gamer_platform.config import PlatformConfig


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    health_missing = False

    def log_message(self, *_: Any) -> None:
        return

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers.items()),
            }
        )
        if self.path.endswith("/artifacts/artifact-1/content"):
            payload_bytes = b"opaque-artifact-content"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition", 'attachment; filename="artifact.bin"'
            )
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)
            return
        if self.path.endswith("/health") and type(self).health_missing:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"missing"}')
            return
        if self.path.endswith("/health") or self.path.endswith("/meta"):
            payload = {"ok": True, "status": "healthy", "version": "test"}
            status = 200
        elif self.path.endswith("/webgui/bootstrap-nonces"):
            payload = {"nonce": "n" * 43, "expiresInSeconds": 60}
            status = 201
        elif self.path.endswith("/snapshot"):
            payload = {"stateVersion": 41}
            status = 200
        elif self.command == "GET" and "/agent/work-items/" in self.path:
            payload = {
                "workItemId": "work-1",
                "kind": "diagnose_game",
                "state": "pending",
                "requestedBy": "rabiroute",
                "gameId": "StarRail",
                "artifactRefs": ["artifact-1"],
                "allowedCapabilityRefs": ["game.daily.plan"],
            }
            status = 200
        elif self.command == "GET" and "/artifacts/" in self.path:
            payload = {
                "artifactId": "artifact-1",
                "kind": "screenshot",
                "contentType": "image/png",
                "sizeBytes": 23,
                "hash": "sha256:test",
                "fileName": "artifact.png",
                "runAttemptId": "attempt-1",
                "todoInstanceId": "todo-1",
                "todoAttemptId": "todo-attempt-1",
                "gameDayKey": "week:2026-08-24",
            }
            status = 200
        else:
            payload = {
                "commandId": "cmd-test",
                "statusUrl": "/api/v1/commands/cmd-test",
                "acceptedStateVersion": 7,
            }
            if self.path.endswith("/claims"):
                payload["result"] = {
                    "claim": {
                        "workItemId": "work-1",
                        "claimId": "claim-1",
                        "fencingToken": "f" * 32,
                    }
                }
            elif self.path.endswith("/agent/work-items"):
                payload["result"] = {"workItem": {"workItemId": "work-1"}}
            status = 202
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = _respond
    do_POST = _respond


class ManagerApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.requests = []
        _Handler.health_missing = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        actor_token = root / "runtime" / "secrets" / "actors" / "test-client.token"
        actor_token.parent.mkdir(parents=True)
        actor_token.write_text("test-scoped-bearer-token-0123456789", encoding="ascii")
        (actor_token.parent / "agent.token").write_text(
            "agent-scoped-bearer-token-0123456789", encoding="ascii"
        )
        (actor_token.parent / "agent.yeyu.token").write_text(
            "agent-yeyu-worker-token-0123456789", encoding="ascii"
        )
        (actor_token.parent / "rabiroute.token").write_text(
            "rabiroute-scoped-bearer-token-0123456", encoding="ascii"
        )
        (actor_token.parent / "tray-lifecycle.token").write_text(
            "tray-lifecycle-bearer-token-01234567", encoding="ascii"
        )
        self.config = PlatformConfig.for_test(
            install_root=root / "install",
            runtime_root=root / "runtime",
            manager_base_url=f"http://127.0.0.1:{self.server.server_port}/api/v1",
            web_url=f"http://127.0.0.1:{self.server.server_port}/",
            legacy_root=None,
            web_dist=root / "web-dist",
            actor_token_file=(
                root / "runtime" / "secrets" / "actors"
            ),
        )
        self.client = ManagerApiClient(self.config, actor="test-client")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health_is_typed(self) -> None:
        health = self.client.health()
        self.assertTrue(health.ok)
        self.assertEqual(health.version, "test")

    def test_production_endpoint_override_is_rejected_before_token_or_socket(self) -> None:
        unsafe = PlatformConfig(
            install_root=self.config.install_root,
            runtime_root=self.config.runtime_root,
            manager_base_url="http://127.0.0.1:18877/api/v1",
            web_url="http://127.0.0.1:18877/",
            legacy_root=None,
            web_dist=self.config.web_dist,
            actor_token_file=self.config.actor_token_file,
        )
        with (
            patch("yeyu_gamer_platform.api_client._token_from_file") as token_read,
            patch("yeyu_gamer_platform.api_client.urllib.request.urlopen") as urlopen,
            self.assertRaisesRegex(ValueError, "fixed at"),
        ):
            ManagerApiClient(unsafe, actor="test-client")
        token_read.assert_not_called()
        urlopen.assert_not_called()

    def test_only_tray_client_can_issue_typed_webgui_bootstrap(self) -> None:
        ephemeral_secret = "ephemeral-tray-bootstrap-secret-0123456789abcdefghijkl"
        tray = ManagerApiClient(
            self.config,
            actor="tray",
            actor_token_override=ephemeral_secret,
        )
        bootstrap = tray.issue_webgui_bootstrap()
        self.assertEqual(bootstrap.nonce, "n" * 43)
        self.assertEqual(bootstrap.expires_in_seconds, 60)
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/webgui/bootstrap-nonces")
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["x-yeyu-gamer-actor"], "tray")
        self.assertEqual(
            headers["authorization"],
            f"Bearer {ephemeral_secret}",
        )
        self.assertNotIn(ephemeral_secret, repr(self.config))
        self.assertFalse(any(
            ephemeral_secret.encode("ascii") in path.read_bytes()
            for path in self.config.runtime_root.rglob("*")
            if path.is_file()
        ))
        retired_path = self.config.actor_token_directory / "tray.token"
        retired_path.write_text(
            "retired-tray-token-that-must-never-be-read-00000000",
            encoding="ascii",
        )
        before = len(_Handler.requests)
        with self.assertRaisesRegex(PermissionError, "in-memory credential"):
            ManagerApiClient(self.config, actor="tray").issue_webgui_bootstrap()
        self.assertEqual(len(_Handler.requests), before)
        before = len(_Handler.requests)
        for actor in ("agent", "rabiroute", "test-client"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                ManagerApiClient(self.config, actor=actor).issue_webgui_bootstrap()
        self.assertEqual(len(_Handler.requests), before)

    def test_health_falls_back_to_meta_only_on_404(self) -> None:
        _Handler.health_missing = True
        health = self.client.health()
        self.assertTrue(health.ok)
        self.assertEqual(
            [request["path"] for request in _Handler.requests],
            ["/api/v1/health", "/api/v1/meta"],
        )

    def test_game_run_uses_typed_route_and_idempotency(self) -> None:
        receipt = self.client.create_game_run(
            "StarRail", idempotency_key="test-key", expected_state_version=6
        )
        self.assertEqual(receipt.command_id, "cmd-test")
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/games/StarRail/run-requests")
        self.assertEqual(request["body"]["kind"], "daily")
        self.assertEqual(request["headers"]["Idempotency-Key"], "test-key")
        self.assertEqual(request["headers"]["If-Match"], "6")
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["x-yeyu-gamer-actor"], "test-client")
        self.assertEqual(
            headers["authorization"],
            "Bearer test-scoped-bearer-token-0123456789",
        )

    def test_daily_batch_explicitly_requests_execute_mode(self) -> None:
        self.client.create_daily_batch(idempotency_key="daily-key")
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/batches")
        self.assertEqual(
            request["body"],
            {"kind": "daily", "mode": "execute", "requestedBy": "test-client"},
        )
        self.assertEqual(request["headers"]["If-Match"], "41")

    def test_batch_resume_uses_typed_manager_route(self) -> None:
        self.client.resume_batch(
            "batch-1",
            reason="resume only never-started members",
            idempotency_key="resume-key",
            expected_state_version=43,
        )
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/batches/batch-1/resume-requests")
        self.assertEqual(
            request["body"],
            {
                "reason": "resume only never-started members",
                "requestedBy": "test-client",
            },
        )
        self.assertEqual(request["headers"]["If-Match"], "43")

    def test_capability_uses_backend_contract_field_names(self) -> None:
        self.client.invoke_capability(
            "diagnostics.capture@1",
            {"observationId": "obs-test"},
            idempotency_key="cap-key",
        )
        request = _Handler.requests[-1]
        self.assertEqual(request["body"]["capability"], "diagnostics.capture@1")
        self.assertEqual(request["body"]["arguments"], {"observationId": "obs-test"})
        self.assertNotIn("capabilityRef", request["body"])
        self.assertNotIn("input", request["body"])

    def test_lifecycle_body_contains_only_supported_fields(self) -> None:
        self.client.request_restart(idempotency_key="restart-key")
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/manager/restart-requests")
        self.assertEqual(
            request["body"],
            {"requestedBy": "test-client", "reason": "operator-request"},
        )

    def test_each_mutation_prefetches_snapshot_when_version_is_omitted(self) -> None:
        mutations = (
            lambda: self.client.create_daily_batch(idempotency_key="daily"),
            lambda: self.client.create_game_run("StarRail", idempotency_key="run"),
            lambda: self.client.cancel_batch("batch-1", idempotency_key="cancel"),
            lambda: self.client.resume_batch("batch-1", idempotency_key="resume"),
            lambda: self.client.invoke_capability(
                "diagnostics.capture@1", {}, idempotency_key="capability"
            ),
            lambda: self.client.request_safe_stop(idempotency_key="stop"),
            lambda: self.client.request_restart(idempotency_key="restart"),
        )
        expected_keys = ("daily", "run", "cancel", "resume", "capability", "stop", "restart")
        for mutation, expected_key in zip(mutations, expected_keys, strict=True):
            with self.subTest(idempotency_key=expected_key):
                _Handler.requests = []
                mutation()
                self.assertEqual(len(_Handler.requests), 2)
                self.assertEqual(_Handler.requests[0]["path"], "/api/v1/snapshot")
                request = _Handler.requests[1]
                self.assertEqual(request["headers"]["If-Match"], "41")
                self.assertEqual(request["headers"]["Idempotency-Key"], expected_key)

    def test_explicit_version_skips_snapshot_and_replay_key_is_unchanged(self) -> None:
        for _ in range(2):
            self.client.request_safe_stop(
                idempotency_key="lost-response-replay", expected_state_version=9
            )
        self.assertEqual(len(_Handler.requests), 2)
        for request in _Handler.requests:
            self.assertEqual(request["path"], "/api/v1/manager/stop-requests")
            self.assertEqual(request["headers"]["If-Match"], "9")
            self.assertEqual(
                request["headers"]["Idempotency-Key"], "lost-response-replay"
            )

    def test_invalid_idempotency_keys_never_reach_http(self) -> None:
        before = len(_Handler.requests)
        for invalid_key in ("", "contains space", "含中文", "x" * 129):
            with self.subTest(invalid_key=invalid_key), self.assertRaises(ValueError):
                self.client.request_restart(
                    idempotency_key=invalid_key,
                    expected_state_version=1,
                )
        self.assertEqual(len(_Handler.requests), before)

    def test_legacy_single_actor_token_file_remains_supported(self) -> None:
        legacy_token = self.config.runtime_root / "secrets" / "legacy.token"
        legacy_token.write_text("legacy-local-token-0123456789012345", encoding="ascii")
        legacy_config = PlatformConfig.for_test(
            install_root=self.config.install_root,
            runtime_root=self.config.runtime_root,
            manager_base_url=self.config.manager_base_url,
            web_url=self.config.web_url,
            legacy_root=self.config.legacy_root,
            web_dist=self.config.web_dist,
            actor_token_file=legacy_token,
        )
        client = ManagerApiClient(legacy_config, actor="test-client")
        client.request_restart(
            idempotency_key="legacy-token", expected_state_version=3
        )
        self.assertEqual(
            _Handler.requests[-1]["headers"]["Authorization"],
            "Bearer legacy-local-token-0123456789012345",
        )

    def test_agent_claim_decision_and_claimed_capability_are_typed(self) -> None:
        client = ManagerApiClient(self.config, actor="agent")
        claim = client.claim_work_item(
            "work-1", idempotency_key="claim-key"
        )
        self.assertEqual(claim.claim_id, "claim-1")
        self.assertEqual(claim.fencing_token, "f" * 32)
        self.assertEqual(_Handler.requests[-2]["path"], "/api/v1/snapshot")
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/agent/work-items/work-1/claims")
        self.assertEqual(request["body"], {"claimant": "agent", "leaseSeconds": 300})
        self.assertEqual(request["headers"]["If-Match"], "41")
        self.assertEqual(
            request["headers"]["Authorization"],
            "Bearer agent-scoped-bearer-token-0123456789",
        )

        client.submit_claim_decision(
            "claim-1",
            "f" * 32,
            "review_required",
            "fresh visual evidence is still required",
            idempotency_key="decision-key",
            evidence_ids=("artifact-1",),
            todo_diagnoses=(
                {
                    "todoInstanceId": "todo-1",
                    "difficulty": "hard",
                    "automatable": True,
                    "confidence": 0.9,
                    "basis": ["repeated visual navigation failure"],
                    "failureStage": "daily-training",
                    "issue": "navigation drift",
                    "recommendation": "use current screenshot review",
                    "evidenceIds": ["artifact-1"],
                },
                {
                    "todoInstanceId": "todo-2",
                    "difficulty": "unsupported",
                    "automatable": False,
                    "confidence": 1.0,
                    "basis": ["active human login blocker"],
                    "failureStage": "login",
                    "issue": "login gate",
                    "recommendation": "wait for explicit human release",
                },
            ),
            completion_review={
                "gameId": "StarRail",
                "runId": "run-1",
                "runAttemptId": "attempt-1",
                "gameDayKey": "2026-08-28",
                "predicates": [
                    {
                        "predicateId": "starrail.daily_training.points",
                        "metrics": {"current": 500, "target": 500},
                        "artifactRefs": ["artifact-1"],
                    }
                ],
            },
        )
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/claims/decisions")
        self.assertEqual(request["headers"]["If-Match"], "41")
        self.assertEqual(request["body"]["evidenceIds"], ["artifact-1"])
        self.assertNotIn("todoDiagnosis", request["body"])
        self.assertEqual(
            [item["todoInstanceId"] for item in request["body"]["todoDiagnoses"]],
            ["todo-1", "todo-2"],
        )
        self.assertTrue(request["body"]["todoDiagnoses"][0]["automatable"])
        self.assertEqual(request["body"]["todoDiagnoses"][0]["evidenceIds"], ["artifact-1"])
        self.assertEqual(request["body"]["todoDiagnoses"][1]["issue"], "login gate")
        self.assertEqual(
            request["body"]["completionReview"]["predicates"][0]["metrics"],
            {"current": 500, "target": 500},
        )

        client.submit_claim_decision(
            "claim-1",
            "f" * 32,
            "review_required",
            "legacy single Todo compatibility",
            idempotency_key="decision-legacy-key",
            todo_diagnosis={
                "todoInstanceId": "todo-legacy",
                "difficulty": "unknown",
                "confidence": 0.5,
                "basis": ["legacy caller"],
            },
        )
        legacy_request = _Handler.requests[-1]
        self.assertIn("todoDiagnosis", legacy_request["body"])
        self.assertNotIn("todoDiagnoses", legacy_request["body"])

        client.submit_claim_decision(
            "claim-1",
            "f" * 32,
            "review_required",
            "new contract without Todo diagnoses",
            idempotency_key="decision-empty-diagnoses-key",
        )
        self.assertEqual(_Handler.requests[-1]["body"]["todoDiagnoses"], [])
        self.assertNotIn("todoDiagnosis", _Handler.requests[-1]["body"])

        before = len(_Handler.requests)
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            client.submit_claim_decision(
                "claim-1",
                "f" * 32,
                "review_required",
                "invalid mixed shape",
                idempotency_key="decision-mixed-key",
                todo_diagnoses=(),
                todo_diagnosis={
                    "todoInstanceId": "todo-legacy",
                    "difficulty": "unknown",
                    "confidence": 0.5,
                    "basis": ["legacy caller"],
                },
            )
        self.assertEqual(len(_Handler.requests), before)

        with self.assertRaises(ValueError):
            client.submit_claim_decision(
                "claim-1",
                "f" * 32,
                "accepted",
                "unsafe completion reference",
                idempotency_key="decision-invalid-artifact",
                evidence_ids=("artifact-1",),
                completion_review={
                    "gameId": "StarRail",
                    "runId": "run-1",
                    "runAttemptId": "attempt-1",
                    "gameDayKey": "2026-08-28",
                    "predicates": [
                        {
                            "predicateId": "starrail.daily_training.points",
                            "metrics": {"current": 500, "target": 500},
                            "artifactRefs": ["artifact-not-in-decision"],
                        }
                    ],
                },
            )

        client.invoke_claimed_capability(
            "game.daily.plan",
            {"gameId": "StarRail"},
            work_item_id="work-1",
            claim_id="claim-1",
            fencing_token="f" * 32,
            idempotency_key="invoke-claimed-key",
        )
        self.assertEqual(_Handler.requests[-2]["path"], "/api/v1/snapshot")
        request = _Handler.requests[-1]
        self.assertEqual(request["headers"]["If-Match"], "41")
        self.assertEqual(request["body"]["workItemId"], "work-1")
        self.assertEqual(request["body"]["claimId"], "claim-1")
        self.assertEqual(request["body"]["fencingToken"], "f" * 32)

    def test_weekly_completion_review_allows_empty_predicates(self) -> None:
        client = ManagerApiClient(self.config, actor="agent")
        client.submit_claim_decision(
            "claim-1",
            "f" * 32,
            "review_required",
            "unsupported weekly policy requires typed review without predicates",
            idempotency_key="weekly-empty-review",
            evidence_ids=(),
            todo_diagnoses=(),
            completion_review={
                "gameId": "ZZZ",
                "runId": "run-weekly-1",
                "runAttemptId": "attempt-weekly-1",
                "gameDayKey": "week:2026-08-24",
                "predicates": [],
            },
        )
        request = _Handler.requests[-1]
        self.assertEqual(
            request["body"]["completionReview"]["gameDayKey"],
            "week:2026-08-24",
        )
        self.assertEqual(request["body"]["completionReview"]["predicates"], [])
        self.assertEqual(request["body"]["completionReview"]["todoReviews"], [])

    @staticmethod
    def _ww_completion_review() -> dict[str, Any]:
        return {
            "gameId": "WW",
            "runId": "run-ww-1",
            "runAttemptId": "attempt-ww-1",
            "gameDayKey": "2026-09-05",
            "predicates": [{
                "predicateId": "ww.daily_activity.points",
                "metrics": {"current": 100},
                "artifactRefs": ["artifact-1"],
            }],
            "todoReviews": [{
                "todoInstanceId": "todo-ww-daily",
                "verdict": "confirmed",
                "reasonCode": "daily_reward_visually_confirmed",
                "artifactRefs": ["artifact-1"],
            }, {
                "todoInstanceId": "todo-ww-mail",
                "verdict": "confirmed",
                "reasonCode": "mail_visually_confirmed",
                "artifactRefs": ["artifact-2"],
            }],
        }

    def test_agent_facade_preserves_each_todo_completion_verdict(self) -> None:
        client = AgentManagerClient(self.config, worker_id="yeyu")
        review = self._ww_completion_review()
        client.submit_claim_decision(
            "claim-1", "f" * 32, "accepted", "same-run screenshots inspected",
            idempotency_key="ww-review-v2", expected_state_version=43,
            evidence_ids=("artifact-1", "artifact-2"), completion_review=review,
        )
        self.assertEqual(len(_Handler.requests), 1)
        request = _Handler.requests[0]
        self.assertEqual(request["path"], "/api/v1/claims/decisions")
        self.assertEqual(request["headers"]["If-Match"], "43")
        self.assertEqual(request["headers"]["Idempotency-Key"], "ww-review-v2")
        self.assertEqual(request["headers"]["Authorization"],
                         "Bearer agent-yeyu-worker-token-0123456789")
        self.assertEqual(request["body"]["completionReview"], review)

    def test_invalid_todo_completion_reviews_never_reach_http(self) -> None:
        valid = self._ww_completion_review()
        invalid: dict[str, dict[str, Any]] = {}
        for name, replacement in {
            "duplicate_todos": [valid["todoReviews"][0]] * 2,
            "too_many_todos": [valid["todoReviews"][0]] * 101,
            "not_a_list": "todo-ww-daily",
            "not_an_object": ["todo-ww-daily"],
        }.items():
            review = deepcopy(valid)
            review["todoReviews"] = replacement
            invalid[name] = review
        for name, field, replacement in (
            ("unknown_verdict", "verdict", "completed"),
            ("nontext_verdict", "verdict", []),
            ("nontext_todo", "todoInstanceId", 123),
            ("todo_path", "todoInstanceId", "../todo"),
            ("bad_reason", "reasonCode", "Confirmed by text"),
            ("empty_reason", "reasonCode", ""),
            ("long_reason", "reasonCode", "x" * 161),
            ("nontext_reason", "reasonCode", True),
            ("no_evidence", "artifactRefs", []),
            ("duplicate_evidence", "artifactRefs", ["artifact-1"] * 2),
            ("too_many_evidence", "artifactRefs", ["artifact-1"] * 21),
            ("outside_decision", "artifactRefs", ["artifact-outside"]),
            ("evidence_path", "artifactRefs", ["../artifact-1"]),
            ("nontext_evidence", "artifactRefs", [1]),
            ("unknown_field", "script", "unsafe"),
        ):
            review = deepcopy(valid)
            review["todoReviews"][0][field] = replacement
            invalid[name] = review
        for field in ("artifactRefs", "verdict", "reasonCode", "todoInstanceId"):
            review = deepcopy(valid)
            del review["todoReviews"][0][field]
            invalid[f"missing_{field}"] = review
        # schemaVersion belongs to the stored Manager record, not submission.
        invalid["record_schema_in_submission"] = {**valid, "schemaVersion": 2}
        client = AgentManagerClient(self.config, worker_id="yeyu")
        for name, review in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                client.submit_claim_decision(
                    "claim-1", "f" * 32, "review_required", "invalid typed review",
                    idempotency_key="invalid-review", evidence_ids=("artifact-1", "artifact-2"),
                    completion_review=review,
                )
        self.assertEqual(_Handler.requests, [])

    def test_acceptance_never_falls_back_to_predicate_only_review(self) -> None:
        client = AgentManagerClient(self.config, worker_id="yeyu")
        for verdicts in (None, [], [{
            "todoInstanceId": "todo-ww-daily", "verdict": "review_required",
            "reasonCode": "image_unclear", "artifactRefs": ["artifact-1"],
        }]):
            review = self._ww_completion_review()
            if verdicts is None:
                del review["todoReviews"]
            else:
                review["todoReviews"] = verdicts
            with self.subTest(verdicts=verdicts), self.assertRaisesRegex(
                ValueError, "explicit confirmed Todo verdicts"
            ):
                client.submit_claim_decision(
                    "claim-1", "f" * 32, "accepted", "must not silently downgrade",
                    idempotency_key="reject-v1-acceptance", evidence_ids=("artifact-1",),
                    completion_review=review,
                )
        self.assertEqual(_Handler.requests, [])

    def test_rabiroute_dispatch_is_scoped_and_uses_cas(self) -> None:
        client = ManagerApiClient(self.config, actor="rabiroute")
        dispatched = client.dispatch_work_item(
            "diagnose_game",
            game_id="StarRail",
            artifact_refs=("artifact-1",),
            allowed_capability_refs=("game.daily.plan",),
            idempotency_key="dispatch-key",
        )
        self.assertEqual(dispatched.work_item_id, "work-1")
        self.assertEqual(_Handler.requests[-2]["path"], "/api/v1/snapshot")
        request = _Handler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/agent/work-items")
        self.assertEqual(request["headers"]["If-Match"], "41")
        self.assertEqual(request["headers"]["Idempotency-Key"], "dispatch-key")
        self.assertEqual(request["body"]["requestedBy"], "rabiroute")

    def test_agent_reads_typed_work_item_and_opaque_artifact(self) -> None:
        client = ManagerApiClient(self.config, actor="agent")
        work_item = client.get_work_item("work-1")
        self.assertEqual(work_item.game_id, "StarRail")
        self.assertEqual(work_item.artifact_refs, ("artifact-1",))
        claim_context = {
            "work_item_id": "work-1",
            "claim_id": "claim-1",
            "fencing_token": "f" * 32,
        }
        artifact = client.get_artifact("artifact-1", **claim_context)
        self.assertEqual(artifact.content_type, "image/png")
        self.assertEqual(artifact.run_attempt_id, "attempt-1")
        self.assertEqual(artifact.todo_instance_id, "todo-1")
        self.assertEqual(artifact.todo_attempt_id, "todo-attempt-1")
        self.assertEqual(artifact.game_day_key, "week:2026-08-24")
        content = client.get_artifact_content("artifact-1", **claim_context)
        self.assertEqual(content.data, b"opaque-artifact-content")
        self.assertEqual(content.content_type, "application/octet-stream")
        for request in _Handler.requests[-2:]:
            headers = {key.lower(): value for key, value in request["headers"].items()}
            self.assertEqual(headers["x-yeyu-gamer-work-item-id"], "work-1")
            self.assertEqual(headers["claim-id"], "claim-1")
            self.assertEqual(headers["fencing-token"], "f" * 32)

        before = len(_Handler.requests)
        with self.assertRaisesRegex(ValueError, "require work_item_id"):
            client.get_artifact("artifact-1")
        self.assertEqual(len(_Handler.requests), before)

        before = len(_Handler.requests)
        for unsafe in ("../secret", "C:secret", "folder/file", "folder\\file"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                client.get_artifact_content(unsafe, **claim_context)
        self.assertEqual(len(_Handler.requests), before)

        cli = ManagerApiClient(self.config, actor="cli")
        self.assertEqual(cli.get_artifact("artifact-1").artifact_id, "artifact-1")
        before = len(_Handler.requests)
        with self.assertRaises(PermissionError):
            ManagerApiClient(self.config, actor="rabiroute").get_artifact(
                "artifact-1"
            )
        self.assertEqual(len(_Handler.requests), before)

    def test_actor_bound_mutations_fail_before_http_for_wrong_actor(self) -> None:
        agent = ManagerApiClient(self.config, actor="agent")
        rabiroute = ManagerApiClient(self.config, actor="rabiroute")
        before = len(_Handler.requests)
        with self.assertRaises(PermissionError):
            agent.dispatch_work_item(
                "observation", idempotency_key="agent-cannot-dispatch"
            )
        with self.assertRaises(PermissionError):
            rabiroute.claim_work_item(
                "work-1", idempotency_key="rabiroute-cannot-claim"
            )
        self.assertEqual(len(_Handler.requests), before)

    def test_narrow_facades_do_not_cross_agent_and_dispatch_boundaries(self) -> None:
        agent = AgentManagerClient(self.config)
        rabiroute = RabiRouteManagerClient(self.config)
        self.assertFalse(hasattr(agent, "dispatch_work_item"))
        self.assertFalse(hasattr(rabiroute, "claim_work_item"))
        self.assertFalse(hasattr(rabiroute, "get_artifact"))
        claim = agent.claim_work_item(
            "work-1", idempotency_key="facade-claim", expected_state_version=2
        )
        dispatched = rabiroute.dispatch_work_item(
            "observation",
            idempotency_key="facade-dispatch",
            expected_state_version=3,
        )
        self.assertEqual(claim.claim_id, "claim-1")
        self.assertEqual(dispatched.work_item_id, "work-1")
        self.assertEqual(
            _Handler.requests[-2]["headers"]["Authorization"],
            "Bearer agent-yeyu-worker-token-0123456789",
        )
        agent.get_command_status(claim.receipt.command_id)
        self.assertEqual(_Handler.requests[-1]["path"], "/api/v1/commands/cmd-test")
        self.assertEqual(
            _Handler.requests[-1]["headers"]["Authorization"],
            "Bearer agent-yeyu-worker-token-0123456789",
        )
        rabiroute.get_command_status(dispatched.receipt.command_id)
        self.assertEqual(
            _Handler.requests[-1]["headers"]["Authorization"],
            "Bearer rabiroute-scoped-bearer-token-0123456",
        )
        artifact = agent.get_artifact(
            "artifact-1",
            work_item_id=claim.work_item_id,
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
        )
        self.assertEqual(artifact.artifact_id, "artifact-1")
        before = len(_Handler.requests)
        with self.assertRaises(TypeError):
            agent.get_artifact("artifact-1")
        self.assertEqual(len(_Handler.requests), before)

    def test_agent_facade_requires_a_named_worker_credential(self) -> None:
        with self.assertRaises(ValueError):
            AgentManagerClient(self.config, worker_id="../unsafe")
        with self.assertRaises(FileNotFoundError):
            AgentManagerClient(self.config, worker_id="missing")

    def test_invalid_game_id_never_reaches_http(self) -> None:
        with self.assertRaises(ValueError):
            self.client.create_game_run("../bad", idempotency_key="no")
        self.assertEqual(_Handler.requests, [])


if __name__ == "__main__":
    unittest.main()
