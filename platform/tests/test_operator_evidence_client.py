import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yeyu_gamer_platform.api_client import ManagerApiClient
from yeyu_gamer_platform.config import PlatformConfig


class OperatorEvidenceClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = PlatformConfig.for_test(
            install_root=self.root / "install", runtime_root=self.root / "runtime",
            manager_base_url="http://127.0.0.1:8877/api/v1", web_url="http://127.0.0.1:8877/",
            legacy_root=None, web_dist=self.root / "web", actor_token_file=self.root / "actors",
        )
        self.client = ManagerApiClient(config, actor="agent-launch")

    def test_upload_reads_bytes_and_preserves_command_identity(self):
        image = self.root / "shot.png"
        image.write_bytes(b"image-fixture")
        with patch.object(self.client, "_request", return_value={"commandId": "receipt"}) as request:
            result = self.client.upload_todo_screenshot("todo-id", image, note="manual review", idempotency_key="same-key", expected_state_version=7)
        self.assertEqual(result.command_id, "receipt")
        request.assert_called_once_with(
            "POST", "todo-instances/todo-id/evidence-uploads",
            body={"fileName": "shot.png", "contentBase64": base64.b64encode(b"image-fixture").decode(), "note": "manual review"},
            idempotency_key="same-key", expected_state_version=7,
        )

    def test_empty_or_oversize_file_never_submits(self):
        image = self.root / "shot.png"
        with patch.object(self.client, "_request") as request:
            for content in (b"", b"x" * (8 * 1024 * 1024 + 1)):
                image.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.client.upload_todo_screenshot("todo-id", image, note="review", idempotency_key="same-key")
            request.assert_not_called()

    def test_queries_are_typed_and_current_period(self):
        with patch.object(self.client, "_request", return_value={"items": []}) as request:
            self.client.todo_instances(game_id="WW", account_id="account-1")
            request.assert_called_once_with("GET", "todo-instances?limit=500&current=true&gameId=WW&accountId=account-1")
            request.reset_mock()
            self.client.game_runs(limit=4)
            request.assert_called_once_with("GET", "game-runs?limit=4")


if __name__ == "__main__":
    unittest.main()
