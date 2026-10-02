"""Replay the production click guard without sending native input."""
import os
from pathlib import Path
import subprocess
import unittest

from yeyu_gamer_manager.services.game_launcher import GameLaunchService


@unittest.skipUnless(os.name == "nt", "PowerShell replay is Windows-only")
class EndfieldLauncherOcclusionTests(unittest.TestCase):
    script_attribute = "_ENDFIELD_UIA_SCRIPT"
    function_name = "Invoke-YeYuPhysicalClick"
    function_end = "$expected ="

    def replay(self, *, covered=True, reveal=True, topmost=False, foreground=True, click_error=False, lose_after_cursor=False, raise_success=True, restore_success=True):
        source = getattr(GameLaunchService, self.script_attribute)
        declaration = "function " + self.function_name + " {"
        function = declaration + source.split(declaration, 1)[1].split(self.function_end, 1)[0]
        stub = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
public static class YeYuEndfieldLauncherInput {
    public static bool Promoted;
    public static bool CursorMoved;
    public static bool ThrowInput = CLICK_ERROR;
    public static bool RaiseSuccess = RAISE_SUCCESS;
    public static bool RestoreSuccess = RESTORE_SUCCESS;
    public static int GetWindowLongW(IntPtr h, int index) { return INITIAL_STYLE; }
    public static bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int z, uint flags) {
        Console.WriteLine("position:" + h.ToInt64() + ":" + after.ToInt64() + ":" + flags);
        Promoted = after.ToInt64() == -1;
        return Promoted ? RaiseSuccess : RestoreSuccess;
    }
    public static IntPtr GetForegroundWindow() { return new IntPtr(123); }
    public static bool SetCursorPos(int x, int y) { CursorMoved = true; Console.WriteLine("cursor"); return true; }
    public static void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra) {
        if (ThrowInput) throw new Exception("simulated input error");
        Console.WriteLine("mouse:" + flags);
    }
}
'@
function Set-YeYuLauncherForeground { param([IntPtr]$Handle) return FOREGROUND }
function Test-YeYuPointOwnedBy {
    param([int]$X, [int]$Y, [int]$ProcessId)
    return ((-not COVERED -or ([YeYuEndfieldLauncherInput]::Promoted -and REVEAL)) -and
        -not (LOSE_AFTER_CURSOR -and [YeYuEndfieldLauncherInput]::CursorMoved))
}
function Test-YeYuEndfieldStarted { return $true }
'''
        stub = (stub.replace("INITIAL_STYLE", "8" if topmost else "0")
                .replace("CLICK_ERROR", str(click_error).lower())
                .replace("RAISE_SUCCESS", str(raise_success).lower())
                .replace("RESTORE_SUCCESS", str(restore_success).lower())
                .replace("LOSE_AFTER_CURSOR", "$true" if lose_after_cursor else "$false")
                .replace("FOREGROUND", "$true" if foreground else "$false")
                .replace("COVERED", "$true" if covered else "$false")
                .replace("REVEAL", "$true" if reveal else "$false"))
        if self.script_attribute == "_NIKKE_WEGAME_ACTION_SCRIPT":
            stub = (stub.replace("YeYuEndfieldLauncherInput", "YeYuWeGameSurfaceInput")
                    .replace("Set-YeYuLauncherForeground", "Set-YeYuWeGameForeground")
                    .replace("Test-YeYuPointOwnedBy", "Test-YeYuWeGamePointOwnedBy")
                    .replace("Test-YeYuEndfieldStarted", "Test-YeYuNikkeStarted"))
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-Command", stub + function
             + self.function_name + " -Handle ([IntPtr]123) -ProcessId 456 -X 100 -Y 200 -Label 'start'"],
            capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_covered_verified_launcher_is_raised_rechecked_and_restored(self):
        result = self.replay()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("position:123:-1:19", result.stdout)
        self.assertIn("mouse:2", result.stdout)
        self.assertIn("position:123:-2:19", result.stdout)
        self.assertLess(result.stdout.index("position:123:-1"), result.stdout.index("mouse:2"))
        self.assertLess(result.stdout.index("mouse:4"), result.stdout.index("position:123:-2"))

    def test_still_covered_launcher_never_receives_a_click(self):
        result = self.replay(reveal=False)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertNotIn("mouse:", result.stdout)
        self.assertNotIn("cursor", result.stdout)
        self.assertIn("position:123:-2:19", result.stdout)

    def test_existing_topmost_property_is_preserved(self):
        result = self.replay(topmost=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("position:123:-2", result.stdout)

    def test_visible_target_does_not_change_its_z_order(self):
        result = self.replay(covered=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mouse:2", result.stdout)
        self.assertNotIn("position:", result.stdout)

    def test_failure_to_acquire_foreground_never_promotes_or_clicks(self):
        result = self.replay(foreground=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("position:", result.stdout)
        self.assertNotIn("mouse:", result.stdout)

    def test_input_exception_still_restores_original_topmost_property(self):
        result = self.replay(click_error=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("position:123:-2:19", result.stdout)

    def test_pixel_ownership_is_checked_again_immediately_before_input(self):
        result = self.replay(lose_after_cursor=True)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertNotIn("mouse:", result.stdout)
        self.assertIn("position:123:-2:19", result.stdout)

    def test_failed_promotion_never_clicks_even_if_the_pixel_now_matches(self):
        result = self.replay(raise_success=False)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertNotIn("mouse:", result.stdout)
        self.assertIn("position:123:-2:19", result.stdout)

    def test_failed_restore_stops_the_launcher_action(self):
        result = self.replay(restore_success=False)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("z-order-restore-failed", result.stdout)


class WeGameLauncherOcclusionTests(EndfieldLauncherOcclusionTests):
    """Run the same native-input-free failure cases against WeGame production code."""

    script_attribute = "_NIKKE_WEGAME_ACTION_SCRIPT"
    function_name = "Invoke-YeYuWeGamePhysicalClick"
    function_end = "$roots ="


if __name__ == "__main__":
    unittest.main()
