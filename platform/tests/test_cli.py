from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yeyu_gamer_platform import cli


class _UnicodeManagerClient:
    def __init__(self, _config: object, *, actor: str) -> None:
        if actor != "cli":
            raise AssertionError(actor)

    def snapshot(self) -> dict[str, object]:
        return {"status": "可执行 ⭐", "games": ["星穹铁道"]}

    def games(self) -> list[dict[str, str]]:
        return [{"name": "星穹铁道 ⭐", "status": "就绪"}]


class CliUtf8Tests(unittest.TestCase):
    def test_open_webgui_uses_installed_start_menu_desktop_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            programs = root / "Programs"
            windows = root / "Windows"
            install = root / "Install"
            shortcut = programs / "YeYu Gamer" / "YeYu Gamer.lnk"
            explorer = windows / "explorer.exe"
            desktop_host = install / "app" / "desktop-host" / "YeYuGamer.exe"
            for path in (shortcut, explorer, desktop_host):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            config = SimpleNamespace(install_root=install)

            with (
                patch.object(
                    cli,
                    "_windows_known_folder",
                    side_effect=lambda csidl: programs if csidl == 0x0002 else windows,
                ),
                patch.object(cli.subprocess, "Popen") as popen,
            ):
                result = cli._request_desktop_host_open_webgui(
                    config, manager_healthy=False
                )

            self.assertEqual(
                result,
                {"requested": True, "via": "desktop-host-shortcut", "managerHealthy": False},
            )
            self.assertEqual(popen.call_args.args[0], [str(explorer), str(shortcut)])

    def test_open_webgui_only_opens_browser_when_manager_is_healthy(self) -> None:
        config = SimpleNamespace(
            install_root=Path("C:/nonexistent-install"),
            web_url="http://127.0.0.1:8877/",
        )
        with (
            patch.object(cli.webbrowser, "open") as browser_open,
            patch.object(cli.subprocess, "Popen") as popen,
        ):
            result = cli._request_desktop_host_open_webgui(config, manager_healthy=True)
        self.assertEqual(
            result, {"requested": True, "via": "browser", "managerHealthy": True}
        )
        browser_open.assert_called_once_with("http://127.0.0.1:8877/")
        popen.assert_not_called()

    def test_open_webgui_fails_closed_without_installed_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop_host = root / "Install" / "app" / "desktop-host" / "YeYuGamer.exe"
            desktop_host.parent.mkdir(parents=True)
            desktop_host.write_bytes(b"fixture")
            config = SimpleNamespace(install_root=root / "Install")
            with (
                patch.object(cli, "_windows_known_folder", return_value=root / "Programs"),
                self.assertRaisesRegex(RuntimeError, "Start Menu shortcut"),
            ):
                cli._request_desktop_host_open_webgui(config, manager_healthy=False)

    def test_legacy_idempotency_scope_comes_from_manager_todo_period(self) -> None:
        global_scope = "a" * 64
        game_scope = "b" * 64
        snapshot = {
            "todo": {
                "scopeFingerprint": global_scope,
                "games": {"FGO": {"scopeFingerprint": game_scope}},
            }
        }
        self.assertEqual(
            cli._manager_todo_scope_id(snapshot), global_scope[:32]
        )
        self.assertEqual(
            cli._manager_todo_scope_id(snapshot, game_id="FGO"), game_scope[:32]
        )

    def test_legacy_scope_fails_closed_without_manager_projection(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scope fingerprint"):
            cli._manager_todo_scope_id({"todo": {}})

    def test_cli_has_no_manager_endpoint_override(self) -> None:
        with self.assertRaises(SystemExit):
            cli._parser().parse_args(
                ("--base-url", "http://127.0.0.1:18877/api/v1", "health")
            )

    def test_agent_decision_reads_multi_todo_payload_from_stdin(self) -> None:
        captured: dict[str, object] = {}

        class _AgentClient:
            def __init__(self, _config: object, *, worker_id: str) -> None:
                captured["worker_id"] = worker_id

            def submit_claim_decision(
                self,
                claim_id: str,
                fencing_token: str,
                decision: str,
                reason: str,
                **kwargs: object,
            ) -> dict[str, object]:
                captured.update({
                    "claim_id": claim_id,
                    "fencing_token": fencing_token,
                    "decision": decision,
                    "reason": reason,
                    **kwargs,
                })
                return {"commandId": "cmd-agent", "state": "accepted"}

        payload = {
            "claimId": "claim-1",
            "fencingToken": "f" * 32,
            "decision": "review_required",
            "reason": "two Todo diagnoses",
            "evidenceIds": [],
            "todoDiagnoses": [
                {
                    "todoInstanceId": "todo-1",
                    "difficulty": "moderate",
                    "automatable": True,
                    "confidence": 0.8,
                    "basis": ["latest attempt blocked"],
                    "failureStage": "navigation",
                    "issue": "navigation drift",
                    "recommendation": "capture a new frame",
                },
                {
                    "todoInstanceId": "todo-2",
                    "difficulty": "unsupported",
                    "automatable": False,
                    "confidence": 1.0,
                    "basis": ["active blocker"],
                    "failureStage": "login",
                    "issue": "login gate",
                    "recommendation": "wait for explicit release",
                },
            ],
        }
        with (
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(cli.PlatformConfig, "load", return_value=object()),
            patch.object(cli, "AgentManagerClient", _AgentClient),
            patch.object(cli, "_write_result"),
        ):
            exit_code = cli.main((
                "agent-submit-decision",
                "--request-file",
                "-",
                "--worker-id",
                "yeyu",
                "--idempotency-key",
                "decision-multi",
                "--expected-state-version",
                "42",
            ))

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["worker_id"], "yeyu")
        self.assertEqual(captured["fencing_token"], "f" * 32)
        diagnoses = captured["todo_diagnoses"]
        self.assertIsInstance(diagnoses, list)
        self.assertEqual([item["todoInstanceId"] for item in diagnoses], ["todo-1", "todo-2"])

    def test_agent_decision_cli_does_not_accept_fencing_token_argument(self) -> None:
        with self.assertRaises(SystemExit):
            cli._parser().parse_args((
                "agent-submit-decision",
                "--request-file",
                "-",
                "--fencing-token",
                "secret",
            ))

    def test_resume_batch_cli_uses_typed_client_method(self) -> None:
        captured: dict[str, object] = {}

        class _ManagerClient:
            def __init__(self, _config: object, *, actor: str) -> None:
                captured["actor"] = actor

            def resume_batch(self, batch_id: str, **kwargs: object) -> dict[str, object]:
                captured["batch_id"] = batch_id
                captured.update(kwargs)
                return {"commandId": "cmd-resume", "state": "accepted"}

        with (
            patch.object(cli.PlatformConfig, "load", return_value=object()),
            patch.object(cli, "ManagerApiClient", _ManagerClient),
            patch.object(cli, "_write_result"),
        ):
            exit_code = cli.main((
                "resume-batch",
                "batch-1",
                "--reason",
                "resume never-started members",
                "--idempotency-key",
                "resume-cli",
                "--expected-state-version",
                "44",
            ))

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["actor"], "cli")
        self.assertEqual(captured["batch_id"], "batch-1")
        self.assertEqual(captured["reason"], "resume never-started members")
        self.assertEqual(captured["idempotency_key"], "resume-cli")
        self.assertEqual(captured["expected_state_version"], 44)

    def test_json_commands_reconfigure_gbk_stdout_to_utf8(self) -> None:
        for command in ("snapshot", "games"):
            with self.subTest(command=command):
                raw_output = io.BytesIO()
                gbk_stdout = io.TextIOWrapper(raw_output, encoding="gbk")
                try:
                    with (
                        patch.object(sys, "stdout", gbk_stdout),
                        patch.object(cli.PlatformConfig, "load", return_value=object()),
                        patch.object(cli, "ManagerApiClient", _UnicodeManagerClient),
                    ):
                        exit_code = cli.main(("--json", command))
                        gbk_stdout.flush()

                    self.assertEqual(exit_code, 0)
                    self.assertEqual(gbk_stdout.encoding.lower(), "utf-8")
                    decoded = raw_output.getvalue().decode("utf-8")
                    self.assertIn("⭐", decoded)
                    self.assertTrue(json.loads(decoded))
                finally:
                    gbk_stdout.detach()


if __name__ == "__main__":
    unittest.main()
