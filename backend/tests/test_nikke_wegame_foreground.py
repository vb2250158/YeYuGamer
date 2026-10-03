"""Replay the production foreground helper without native input or windows."""
import os
from pathlib import Path
import subprocess
import unittest

from yeyu_gamer_manager.services.game_launcher import GameLaunchService


@unittest.skipUnless(os.name == 'nt', 'PowerShell replay is Windows-only')
class WeGameForegroundTests(unittest.TestCase):
    def replay(self, *, attach=True, activate=True, same_thread=False, already=False, throw=False, detach=True):
        source = GameLaunchService._NIKKE_WEGAME_ACTION_SCRIPT
        helper = 'function Set-YeYuWeGameForeground {' + source.split(
            'function Set-YeYuWeGameForeground {', 1)[1].split('function Test-YeYuNikkeStarted', 1)[0]
        stub = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -TypeDefinition @'
using System;
public static class YeYuWeGameSurfaceInput {
    public static bool Attached;
    public static bool CanActivate = ACTIVATE;
    public static bool ThrowActivation = THROW;
    public static bool Activated = ALREADY;
    public static bool IsWindow(IntPtr handle) { return true; }
    public static uint GetCurrentThreadId() { return 11; }
    public static uint GetWindowThreadProcessId(IntPtr handle, IntPtr ignored) { return TARGET_THREAD; }
    public static bool AttachThreadInput(uint first, uint second, bool attach) {
        Console.WriteLine("attach:" + first + ":" + second + ":" + attach);
        Attached = attach && ATTACH;
        return attach ? ATTACH : DETACH;
    }
    public static void keybd_event(byte key, byte code, uint flags, UIntPtr extra) { }
    public static bool BringWindowToTop(IntPtr handle) {
        if (ThrowActivation) throw new Exception("fixture activation error");
        return true;
    }
    public static bool SetForegroundWindow(IntPtr handle) {
        Activated = CanActivate && (Attached || TARGET_THREAD == 11);
        return Activated;
    }
    public static IntPtr GetForegroundWindow() { return new IntPtr(Activated ? 123 : 456); }
}
'@
function Get-YeYuWeGameOwnerPid { param([IntPtr]$Handle) return 789 }
function Invoke-YeYuWeGameCaptionActivation { param([IntPtr]$Handle) [Console]::Out.WriteLine('caption-fallback'); return $false }
'''
        for key, value in {'ALREADY': already, 'ATTACH': attach, 'DETACH': detach,
                           'ACTIVATE': activate, 'THROW': throw}.items():
            stub = stub.replace(key, str(value).lower())
        stub = stub.replace('TARGET_THREAD', '11' if same_thread else '22')
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        return subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command', stub + helper
                               + "[Console]::Out.WriteLine((Set-YeYuWeGameForeground -Handle ([IntPtr]123)))"],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_only_the_target_thread_is_attached_and_released(self):
        result = self.replay()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('attach:11:22:True', result.stdout)
        self.assertIn('attach:11:22:False', result.stdout)
        self.assertTrue(result.stdout.strip().endswith('True'), result.stdout)

    def test_attachment_failure_never_claims_foreground(self):
        result = self.replay(attach=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith('False'), result.stdout)
        self.assertNotIn('attach:11:22:False', result.stdout)

    def test_still_foreign_foreground_is_not_success(self):
        result = self.replay(activate=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('attach:11:22:False', result.stdout)
        self.assertTrue(result.stdout.strip().endswith('False'), result.stdout)

    def test_exception_releases_its_attachment(self):
        result = self.replay(throw=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('attach:11:22:False', result.stdout)

    def test_current_or_same_thread_target_does_not_attach_to_itself(self):
        for flags in ({'already': True}, {'same_thread': True}):
            with self.subTest(flags=flags):
                result = self.replay(**flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('attach:', result.stdout)
                self.assertTrue(result.stdout.strip().endswith('True'), result.stdout)

    def test_detachment_failure_is_not_success(self):
        result = self.replay(detach=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith('False'), result.stdout)
        self.assertNotIn('caption-fallback', result.stdout)


if __name__ == '__main__':
    unittest.main()
