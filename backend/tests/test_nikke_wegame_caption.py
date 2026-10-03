"""Replay the real caption fallback with inert native collaborators."""
import os
from pathlib import Path
import subprocess
import unittest

from yeyu_gamer_manager.services.game_launcher import GameLaunchService


@unittest.skipUnless(os.name == 'nt', 'PowerShell replay is Windows-only')
class WeGameCaptionTests(unittest.TestCase):
    def replay(self, *, caption=True, owned=True, moved=False, cursor=True,
               raise_ok=True, restore=True, activate=True, topmost=False, timeout=False):
        source = GameLaunchService._NIKKE_WEGAME_ACTION_SCRIPT
        helper = 'function Invoke-YeYuWeGameCaptionActivation {' + source.split(
            'function Invoke-YeYuWeGameCaptionActivation {', 1)[1].split(
                'function Set-YeYuWeGameForeground {', 1)[0]
        stub = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
public struct YeYuWeGameRect { public int Left; public int Top; public int Right; public int Bottom; }
public static class YeYuWeGameSurfaceInput {
    public static int Checks;
    public static bool Activated;
    public static bool IsWindow(IntPtr handle) { return true; }
    public static bool GetWindowRect(IntPtr handle, out YeYuWeGameRect rect) {
        rect = new YeYuWeGameRect { Left=100, Top=100, Right=1100, Bottom=700 }; return true;
    }
    public static IntPtr SendMessageTimeoutW(IntPtr handle, uint message, IntPtr wParam,
        IntPtr lParam, uint flags, uint timeout, out IntPtr result) {
        Checks++;
        result = new IntPtr(CAPTION && !(MOVED && Checks > 1) ? 2 : 1);
        Console.WriteLine("hit:" + result);
        return new IntPtr(TIMEOUT ? 0 : 1);
    }
    public static int GetWindowLongW(IntPtr handle, int index) { return TOPMOST ? 8 : 0; }
    public static bool SetWindowPos(IntPtr handle, IntPtr after, int x, int y, int w, int h, uint flags) {
        Console.WriteLine("z:" + after);
        return after.ToInt64() == -1 ? RAISE : RESTORE;
    }
    public static bool SetCursorPos(int x, int y) { Console.WriteLine("cursor:" + x + ":" + y); return CURSOR; }
    public static void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra) {
        Console.WriteLine("mouse:" + flags); if (flags == 4) Activated = ACTIVATE;
    }
    public static IntPtr GetForegroundWindow() { return new IntPtr(Activated ? 123 : 456); }
}
'@
function Get-YeYuWeGameOwnerPid { param([IntPtr]$Handle) return 789 }
function Test-YeYuWeGamePointOwnedBy { param($X, $Y, $ProcessId) return $OWNED }
'''
        values = {'CAPTION': caption, 'OWNED': owned, 'MOVED': moved, 'CURSOR': cursor,
                  'RAISE': raise_ok, 'RESTORE': restore, 'ACTIVATE': activate,
                  'TOPMOST': topmost, 'TIMEOUT': timeout}
        fields = []
        for key, value in values.items():
            if key == 'OWNED':
                stub = stub.replace(key, str(value).lower())
            else:
                field = 'Fixture' + key.title()
                stub = stub.replace(key, field)
                fields.append(f'public static bool {field} = {str(value).lower()};')
        stub = stub.replace('public static int Checks;', '\n'.join(fields) + '\npublic static int Checks;')
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        return subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command',
                               stub + helper + '\n[Console]::Out.WriteLine((Invoke-YeYuWeGameCaptionActivation -Handle ([IntPtr]123)))'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_owned_caption_activates_once_and_restores_topmost(self):
        result = self.replay()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith('True'), result.stdout)
        self.assertEqual(result.stdout.count('mouse:2'), 1)
        self.assertEqual(result.stdout.count('mouse:4'), 1)
        self.assertIn('z:-1', result.stdout)
        self.assertIn('z:-2', result.stdout)
        self.assertIn('cursor:600:114', result.stdout)

    def test_client_controls_timeout_or_occlusion_receive_no_click(self):
        for flags in ({'caption': False}, {'timeout': True}, {'owned': False},
                      {'moved': True}, {'cursor': False}, {'raise_ok': False}):
            with self.subTest(flags=flags):
                result = self.replay(**flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.strip().endswith('False'), result.stdout)
                self.assertNotIn('mouse:', result.stdout)

    def test_no_foreground_or_failed_restore_does_not_report_success(self):
        for flags in ({'activate': False}, {'restore': False}):
            with self.subTest(flags=flags):
                result = self.replay(**flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.strip().endswith('False'), result.stdout)
                self.assertIn('z:-2', result.stdout)

    def test_original_topmost_is_preserved(self):
        result = self.replay(topmost=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().endswith('True'), result.stdout)
        self.assertNotIn('z:', result.stdout)


if __name__ == '__main__':
    unittest.main()
