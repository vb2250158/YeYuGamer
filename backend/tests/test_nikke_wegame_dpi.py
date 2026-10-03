"""Replay the production DPI scope with a 150-percent coordinate fixture."""
import os
from pathlib import Path
import subprocess
import unittest

from yeyu_gamer_manager.services.game_launcher import GameLaunchService


@unittest.skipUnless(os.name == 'nt', 'PowerShell replay is Windows-only')
class WeGameDpiTests(unittest.TestCase):
    def replay(self, probe, *, available=True, restore=True):
        source = GameLaunchService._NIKKE_WEGAME_ACTION_SCRIPT
        helper = 'function Invoke-YeYuWeGamePhysicalCoordinateScope {' + source.split(
            'function Invoke-YeYuWeGamePhysicalCoordinateScope {', 1)[1].split(
                'Invoke-YeYuWeGamePhysicalCoordinateScope {', 1)[0]
        stub = r'''
$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
public static class YeYuWeGameSurfaceInput {
    public static bool Available = AVAILABLE;
    public static bool Restore = RESTORE;
    public static long Context = -1;
    public static IntPtr SetThreadDpiAwarenessContext(IntPtr value) {
        Console.WriteLine("context:" + value);
        if (value.ToInt64() == -4 && !Available) return IntPtr.Zero;
        if (value.ToInt64() != -4 && !Restore) return IntPtr.Zero;
        var old = Context; Context = value.ToInt64(); return new IntPtr(old);
    }
    public static int PrimaryX() { return Context == -4 ? 2657 : 1771; }
}
'@
'''.replace('AVAILABLE',str(available).lower()).replace('RESTORE',str(restore).lower())
        shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        return subprocess.run([str(shell),'-NoProfile','-NonInteractive','-Command',
                               stub+helper+'\nInvoke-YeYuWeGamePhysicalCoordinateScope { '+probe+' }'],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',
                              timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)

    def test_geometry_and_cursor_coordinate_are_physical_inside_scope(self):
        result=self.replay('[Console]::Out.WriteLine("point:" + [YeYuWeGameSurfaceInput]::PrimaryX())')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.splitlines(),['context:-4','point:2657','context:-1'])

    def test_return_exception_and_exit_restore_the_original_context(self):
        for probe,code in [('return',0),("throw 'fixture'",1),('exit 3',3)]:
            with self.subTest(probe=probe):
                result=self.replay(probe)
                self.assertEqual(result.returncode,code,result.stderr)
                self.assertEqual(result.stdout.splitlines(),['context:-4','context:-1'])

    def test_missing_context_or_failed_restore_is_rejected(self):
        result=self.replay("Write-Output 'action'",available=False)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('action',result.stdout)
        self.assertIn('error:wegame-physical-coordinate-context-unavailable',result.stdout)
        result=self.replay("Write-Output 'action'",restore=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('context:-1',result.stdout)


if __name__=='__main__':
    unittest.main()
