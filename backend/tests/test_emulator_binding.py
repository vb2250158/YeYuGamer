from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from yeyu_gamer_manager.services.emulator_binding import (
    EMULATOR_GAME_PROFILES,
    EmulatorBindingError,
    EmulatorInspection,
    EmulatorLaunchReceipt,
    LDPlayerBinding,
    LDPlayerBindingService,
    LDPlayerInstanceState,
)


class LDPlayerBindingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="yeyu-emulator-binding-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.console = root / "ldconsole.exe"
        self.adb = root / "adb.exe"
        self.console.write_bytes(b"test")
        self.adb.write_bytes(b"test")
        self.binding = LDPlayerBinding(
            game_id="FGO",
            console_path=str(self.console),
            adb_path=str(self.adb),
            instance_index=0,
            instance_name="雷电模拟器",
            adb_serial="emulator-5554",
        )
        self.service = LDPlayerBindingService()

    def test_profiles_fix_each_game_to_the_verified_package(self) -> None:
        self.assertEqual(
            EMULATOR_GAME_PROFILES["FGO"].package_name, "com.bilibili.fatego"
        )
        self.assertEqual(
            EMULATOR_GAME_PROFILES["BD2"].package_name,
            "com.neowizgames.game.browndust2",
        )
        self.assertEqual(
            EMULATOR_GAME_PROFILES["CZN"].package_name, "com.tencent.czn"
        )
        self.assertIn(
            "com.smilegate.chaoszero.stove.google",
            EMULATOR_GAME_PROFILES["CZN"].excluded_package_names,
        )

    def test_rejects_an_unknown_game_and_nonlocal_serial(self) -> None:
        unknown = LDPlayerBinding(
            "UNKNOWN", str(self.console), str(self.adb), 0, "emulator-5554"
        )
        remote = LDPlayerBinding(
            "FGO", str(self.console), str(self.adb), 0, "192.168.1.20:5555"
        )
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), self.assertRaises(EmulatorBindingError):
            self.service._validate(unknown)
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), self.assertRaises(EmulatorBindingError):
            self.service._validate(remote)

    def test_inspection_parses_instance_adb_and_exact_packages(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                [], 0, "0,雷电模拟器,1,2,1,3344,5256,1280,720,280\n", ""
            ),
            subprocess.CompletedProcess(
                [], 0, "List of devices attached\nemulator-5554 device product:test\n", ""
            ),
            subprocess.CompletedProcess([], 0, "package:/data/app/fgo/base.apk\n", ""),
            subprocess.CompletedProcess([], 0, "17890\n", ""),
        ]
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(self.service, "_run", side_effect=responses) as run:
            result = self.service.inspect(self.binding)

        self.assertTrue(result.instance.running)
        self.assertEqual(result.instance.player_process_id, 3344)
        self.assertTrue(result.adb_online)
        self.assertTrue(result.package_installed)
        self.assertTrue(result.package_running)
        self.assertEqual(run.call_args_list[2].args[0][-3:], ["pm", "path", "com.bilibili.fatego"])

    def test_ensure_started_uses_only_fixed_launch_and_runapp_arguments(self) -> None:
        stopped = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", False, None, None),
            False,
            False,
            False,
            (),
        )
        ready = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", True, 3344, 5256),
            True,
            True,
            False,
            (),
        )
        running = EmulatorInspection(
            ready.instance, True, True, True, ()
        )
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(
            self.service, "inspect", side_effect=[stopped, ready, running]
        ), mock.patch.object(self.service, "_run_console") as run_console:
            receipt = self.service.ensure_started(self.binding)

        self.assertTrue(receipt.instance_started)
        self.assertTrue(receipt.package_started)
        self.assertEqual(
            run_console.call_args_list[0].args[1:], ("launch", "--index", "0")
        )
        self.assertEqual(
            run_console.call_args_list[1].args[1:],
            ("runapp", "--index", "0", "--packagename", "com.bilibili.fatego"),
        )

    def test_close_preserves_a_preexisting_instance_and_package(self) -> None:
        receipt = EmulatorLaunchReceipt(
            "FGO", 0, "emulator-5554", "com.bilibili.fatego", False, False
        )
        final = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", True, 3344, 5256),
            True,
            True,
            True,
            (),
        )
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(self.service, "inspect", return_value=final), mock.patch.object(
            self.service, "_run_console"
        ) as run_console:
            result = self.service.close_started(self.binding, receipt)

        self.assertEqual(result.state, "preserved-preexisting")
        run_console.assert_not_called()

    def test_close_stops_owned_package_before_owned_instance(self) -> None:
        receipt = EmulatorLaunchReceipt(
            "FGO", 0, "emulator-5554", "com.bilibili.fatego", True, True
        )
        closed = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", False, None, None),
            False,
            False,
            False,
            (),
        )
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(self.service, "_wait_for_package"), mock.patch.object(
            self.service, "_wait_for_instance"
        ), mock.patch.object(self.service, "inspect", return_value=closed), mock.patch.object(
            self.service, "_run_console"
        ) as run_console:
            result = self.service.close_started(self.binding, receipt)

        self.assertEqual(result.state, "closed")
        self.assertEqual(run_console.call_args_list[0].args[1], "killapp")
        self.assertEqual(run_console.call_args_list[1].args[1], "quit")

    def test_failed_start_rolls_back_the_owned_instance(self) -> None:
        stopped = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", False, None, None),
            False,
            False,
            False,
            (),
        )
        ready_without_package = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", True, 3344, 5256),
            True,
            False,
            False,
            (),
        )
        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(
            self.service, "inspect", side_effect=[stopped, ready_without_package]
        ), mock.patch.object(self.service, "_run_console") as run_console, self.assertRaises(
            EmulatorBindingError
        ):
            self.service.ensure_started(self.binding)

        commands = [call.args[1] for call in run_console.call_args_list]
        self.assertEqual(commands, ["launch", "killapp", "quit"])

    def test_close_still_quits_owned_instance_when_killapp_fails(self) -> None:
        receipt = EmulatorLaunchReceipt(
            "FGO", 0, "emulator-5554", "com.bilibili.fatego", True, True
        )
        closed = EmulatorInspection(
            LDPlayerInstanceState(0, "雷电模拟器", False, None, None),
            False,
            False,
            False,
            (),
        )

        def command(_path: Path, operation: str, *_arguments: str) -> None:
            if operation == "killapp":
                raise EmulatorBindingError("test killapp failure")

        with mock.patch(
            "yeyu_gamer_manager.services.emulator_binding.os.name", "nt"
        ), mock.patch.object(self.service, "_run_console", side_effect=command) as run_console, mock.patch.object(
            self.service, "_wait_for_instance"
        ), mock.patch.object(self.service, "inspect", return_value=closed):
            result = self.service.close_started(self.binding, receipt)

        self.assertEqual(result.state, "close-failed")
        self.assertEqual([call.args[1] for call in run_console.call_args_list], ["killapp", "quit"])


if __name__ == "__main__":
    unittest.main()
