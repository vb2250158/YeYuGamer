from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yeyu_gamer_platform.config import PlatformConfig, ensure_loopback_url


class PlatformConfigTests(unittest.TestCase):
    def _load_from_fixture(
        self,
        root: Path,
        document: dict[str, object],
        *,
        environ: dict[str, str] | None = None,
    ) -> PlatformConfig:
        install_root = root / "install"
        runtime_root = root / "runtime"
        config_path = root / "platform.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")
        with (
            patch(
                "yeyu_gamer_platform.config.default_install_root",
                return_value=install_root,
            ),
            patch(
                "yeyu_gamer_platform.config.default_runtime_root",
                return_value=runtime_root,
            ),
        ):
            return PlatformConfig.load(config_path, environ=environ or {})

    def test_manager_launch_is_derived_from_fixed_install_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._load_from_fixture(
                root,
                {},
                environ={
                    "YEYU_GAMER_INSTALL_ROOT": str(root / "attacker-install"),
                    "YEYU_GAMER_MANAGER_COMMAND_JSON": '["calc.exe"]',
                },
            )
            self.assertEqual(config.install_root, root / "install")
            self.assertEqual(
                config.manager_command[1:],
                ("-I", "-B", "-m", "yeyu_gamer_manager"),
            )
            self.assertEqual(
                config.manager_working_directory,
                root / "install" / "app",
            )
            self.assertNotIn("calc.exe", config.manager_command)

    def test_legacy_command_keys_are_ignored_and_login_startup_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_root = root / "install"
            runtime_root = root / "runtime"
            config = self._load_from_fixture(
                root,
                {
                    "installRoot": str(install_root),
                    "runtimeRoot": str(runtime_root),
                    "managerBaseUrl": "http://127.0.0.1:8877/api/v1",
                    "webUrl": "http://127.0.0.1:8877/",
                    "managerCommand": ["python", "-m", "custom_manager"],
                    "managerWorkingDirectory": str(root / "attacker-cwd"),
                    "legacyRoot": str(runtime_root / "import" / "legacy-config"),
                    "webDist": str(install_root / "app" / "webgui" / "dist"),
                    "actorTokenFile": str(runtime_root / "secrets" / "actors"),
                    "launchAtWindowsLogon": True,
                },
            )
            self.assertEqual(config.manager_command[-1], "yeyu_gamer_manager")
            self.assertEqual(config.manager_working_directory, install_root / "app")
            self.assertFalse(config.launch_at_windows_logon)
            self.assertEqual(config.log_directory, runtime_root / "logs")
            self.assertEqual(
                config.actor_token_file,
                runtime_root / "secrets" / "actors",
            )

    def test_rejects_configured_install_or_runtime_root_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for key in ("installRoot", "runtimeRoot"):
                with self.subTest(key=key), self.assertRaisesRegex(
                    ValueError, "fixed by the installed"
                ):
                    self._load_from_fixture(root, {key: str(root / "attacker")})

    def test_rejects_non_loopback_manager(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback-only"):
            ensure_loopback_url("http://0.0.0.0:8877/api/v1", name="manager")

    def test_rejects_config_and_environment_endpoint_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "fixed at"):
                self._load_from_fixture(
                    root,
                    {"managerBaseUrl": "http://127.0.0.1:18877/api/v1"},
                )
            with self.assertRaisesRegex(ValueError, "fixed at"):
                self._load_from_fixture(
                    root,
                    {},
                    environ={"YEYU_GAMER_WEB_URL": "http://localhost:8877/"},
                )

    def test_variable_endpoint_requires_explicit_test_seam(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = {
                "install_root": root / "install",
                "runtime_root": root / "runtime",
                "manager_base_url": "http://127.0.0.1:18877/api/v1",
                "web_url": "http://127.0.0.1:18877/",
                "legacy_root": None,
                "web_dist": root / "web-dist",
                "actor_token_file": root / "runtime" / "secrets" / "actors",
            }
            with self.assertRaisesRegex(ValueError, "fixed at"):
                PlatformConfig(**values).validate()
            PlatformConfig.for_test(**values).validate()

    def test_rejects_url_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            ensure_loopback_url(
                "http://operator:secret@127.0.0.1:8877/api/v1", name="manager"
            )


if __name__ == "__main__":
    unittest.main()
