from __future__ import annotations

import unittest

from pydantic import ValidationError

from yeyu_gamer_manager.domain.models import CZNProfileConfig, GamePathConfig


class GamePathConfigTests(unittest.TestCase):
    def test_czn_profile_rejects_target_from_another_category(self) -> None:
        with self.assertRaises(ValueError):
            CZNProfileConfig.model_validate(
                {
                    "staminaCategory": "成长",
                    "staminaTarget": "正义",
                    "battleEfficiency": 4,
                    "untilExhausted": True,
                }
            )

    def test_accepts_typed_ldplayer_binding_without_game_exe(self) -> None:
        model = GamePathConfig.model_validate(
            {
                "toolPath": r"C:\Tools\DailyRunner",
                "emulator": {
                    "provider": "ldplayer",
                    "consolePath": r"C:\Game\LDPlayer9\ldconsole.exe",
                    "adbPath": r"C:\Game\LDPlayer9\adb.exe",
                    "instanceIndex": 0,
                    "instanceName": "雷电模拟器",
                    "adbSerial": "emulator-5554",
                },
            }
        )

        self.assertIsNone(model.game_path)
        self.assertEqual(model.emulator.instance_index, 0)
        self.assertEqual(model.emulator.adb_serial, "emulator-5554")

    def test_rejects_arbitrary_emulator_executable_or_remote_adb(self) -> None:
        base = {
            "provider": "ldplayer",
            "consolePath": r"C:\Game\LDPlayer9\ldconsole.exe",
            "adbPath": r"C:\Game\LDPlayer9\adb.exe",
            "instanceIndex": 0,
            "adbSerial": "emulator-5554",
        }
        for patch in (
            {"consolePath": r"C:\Windows\System32\cmd.exe"},
            {"adbSerial": "192.168.1.5:5555"},
            {"adbPath": r"D:\Other\adb.exe"},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                GamePathConfig.model_validate({"emulator": {**base, **patch}})


if __name__ == "__main__":
    unittest.main()
