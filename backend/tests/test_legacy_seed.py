from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yeyu_gamer_manager.services.legacy_seed import export_safe_legacy_seed


class LegacySeedTests(unittest.TestCase):
    def test_exports_only_safe_configuration_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yeyu-gamer-seed-") as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "runtime" / "import" / "legacy-config"
            source.mkdir(parents=True)
            (source / "daily-gui-config.json").write_text(
                json.dumps(
                    {
                        "order": ["StarRail"],
                        "enabled": {"StarRail": True},
                        "dailyScheduleEnabled": False,
                        "automationEntry": "must-not-copy",
                        "smtpPassword": "must-not-copy",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (source / "game-automation-policy.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "games": {
                            "StarRail": {
                                "tier": "stable",
                                "runnerPath": "must-not-copy",
                                "nested": {"token": "must-not-copy"},
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            first = export_safe_legacy_seed(source, destination)
            self.assertTrue(first["configWritten"])
            self.assertTrue(first["policyWritten"])
            exported_config = json.loads(
                (destination / "daily-gui-config.json").read_text(encoding="utf-8")
            )
            exported_policy = json.loads(
                (destination / "game-automation-policy.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(exported_config["order"], ["StarRail"])
            self.assertNotIn("automationEntry", exported_config)
            self.assertNotIn("smtpPassword", exported_config)
            self.assertNotIn("runnerPath", exported_policy["games"]["StarRail"])
            self.assertNotIn(
                "token", exported_policy["games"]["StarRail"]["nested"]
            )

            (destination / "daily-gui-config.json").write_text(
                '{"preserved":true}', encoding="utf-8"
            )
            second = export_safe_legacy_seed(source, destination)
            self.assertFalse(second["configWritten"])
            self.assertFalse(second["policyWritten"])
            self.assertEqual(
                json.loads(
                    (destination / "daily-gui-config.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {"preserved": True},
            )


if __name__ == "__main__":
    unittest.main()
