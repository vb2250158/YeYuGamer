"""Verify desktop startup budgets and silent second-launch behavior."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from yeyu_gamer_platform.config import PlatformConfig
from yeyu_gamer_platform.desktop_host import DesktopHost, main


class DesktopStartupContractTests(unittest.TestCase):
    def config(self):
        root=Path(__file__).resolve().parents[2]/'.cache/platform-test/desktop-contract'
        return PlatformConfig(root/'install',root/'runtime','http://127.0.0.1:8877/api/v1',
                              'http://127.0.0.1:8877/',None,root/'web',None)

    def test_direct_startup_budget_reaches_host_without_persisting_config(self):
        original=self.config()
        with patch('yeyu_gamer_platform.desktop_host.PlatformConfig.load',return_value=original), \
             patch('yeyu_gamer_platform.desktop_host.DesktopHost') as host:
            host.return_value.run.return_value=0
            self.assertEqual(0,main(['--no-browser','--startup-timeout-seconds','240']))
        self.assertEqual(240,host.call_args.args[0].startup_timeout_seconds)
        self.assertEqual(150,original.startup_timeout_seconds)
        self.assertFalse(host.call_args.kwargs['open_browser'])

    def test_configured_budget_is_preserved_without_override(self):
        original=replace(self.config(),startup_timeout_seconds=275)
        with patch('yeyu_gamer_platform.desktop_host.PlatformConfig.load',return_value=original), \
             patch('yeyu_gamer_platform.desktop_host.DesktopHost') as host:
            host.return_value.run.return_value=0
            self.assertEqual(0,main(['--no-browser']))
        self.assertIs(original,host.call_args.args[0])

    def test_invalid_budget_never_constructs_host(self):
        with patch('yeyu_gamer_platform.desktop_host.PlatformConfig.load',return_value=self.config()), \
             patch('yeyu_gamer_platform.desktop_host.DesktopHost') as host:
            self.assertEqual(2,main(['--no-browser','--startup-timeout-seconds','0']))
        host.assert_not_called()

    def test_silent_second_launch_never_opens_browser(self):
        with patch('yeyu_gamer_platform.desktop_host.SingleInstance') as guard, \
             patch('yeyu_gamer_platform.desktop_host.webbrowser.open') as browser:
            guard.return_value.acquire.return_value=False
            self.assertEqual(0,DesktopHost(self.config(),open_browser=False).run())
        browser.assert_not_called()

    def test_interactive_second_launch_still_opens_existing_page(self):
        with patch('yeyu_gamer_platform.desktop_host.SingleInstance') as guard, \
             patch('yeyu_gamer_platform.desktop_host.webbrowser.open') as browser:
            guard.return_value.acquire.return_value=False
            self.assertEqual(0,DesktopHost(self.config(),open_browser=True).run())
        browser.assert_called_once_with(self.config().web_url)


if __name__=='__main__':
    unittest.main()
