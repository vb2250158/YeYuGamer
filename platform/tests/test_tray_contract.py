from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from yeyu_gamer_platform.config import PlatformConfig
from yeyu_gamer_platform.lifecycle_lock import InstallLifecycleLock
from yeyu_gamer_platform.tray import _acquire_tray_guard, _bootstrap_web_url


class TrayContractTests(unittest.TestCase):
    def test_install_lock_blocks_new_tray_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = PlatformConfig(
                install_root=root / "install",
                runtime_root=root / "runtime",
                manager_base_url="http://127.0.0.1:8877/api/v1",
                web_url="http://127.0.0.1:8877/",
                legacy_root=None,
                web_dist=root / "web-dist",
                actor_token_file=None,
            )
            install_lock = InstallLifecycleLock(
                config.state_directory / "install-lifecycle.lock"
            )
            with install_lock, concurrent.futures.ThreadPoolExecutor(
                max_workers=1
            ) as executor:
                future = executor.submit(_acquire_tray_guard, config)
                with self.assertRaisesRegex(RuntimeError, "installation is in progress"):
                    future.result(timeout=5)

    def test_tray_has_only_lifecycle_menu_contract(self) -> None:
        source = (
            Path(__file__).parents[1] / "yeyu_gamer_platform" / "tray.py"
        ).read_text(encoding="utf-8")
        required = (
            "打开 YeYu Gamer WebGUI",
            "Manager 状态：检查中",
            "启动 Manager",
            "安全停止 Manager",
            "重启 Manager",
            "打开本机日志目录",
            "退出托盘",
        )
        for label in required:
            self.assertIn(label, source)
        self.assertNotIn("QMain" + "Window", source)
        self.assertNotIn("QWeb" + "Engine", source)
        self.assertNotIn("NightRain" + "Gamer.bat", source)
        self.assertIn("secrets.token_urlsafe(48)", source)
        self.assertIn("ensure_bootstrap_ready()", source)
        self.assertIn("actor_token_override=tray_bootstrap_secret", source)
        self.assertNotIn("tray" + ".token", source)
        cli_source = (
            Path(__file__).parents[1] / "yeyu_gamer_platform" / "cli.py"
        ).read_text(encoding="utf-8")
        # open-webgui must never bootstrap a second Manager: a healthy Manager
        # only gets its page opened, an absent one goes through the installed
        # desktop-host shortcut (never the retired tray chain).
        self.assertIn('LocalManagerController(config, actor="cli").is_healthy()', cli_source)
        self.assertNotIn('"yeyu_gamer_platform.tray"', cli_source)
        self.assertIn("desktop-host-shortcut", cli_source)
        self.assertIn("YeYu Gamer.lnk", cli_source)

    def test_tray_puts_bootstrap_nonce_only_in_fragment(self) -> None:
        nonce = "n" * 43
        url = _bootstrap_web_url("http://127.0.0.1:8877/?old=value#old", nonce)
        self.assertEqual(url, f"http://127.0.0.1:8877/#bootstrap={nonce}")
        self.assertNotIn("?", url)


if __name__ == "__main__":
    unittest.main()
