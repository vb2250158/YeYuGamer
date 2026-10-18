"""Official DailyTask source must keep its own conditions; YeYu must not rewrite them."""
from __future__ import annotations

import json
from pathlib import Path
import sys


OFFICIAL_DAILY = Path(__file__).resolve().parents[2] / "vendor-baselines" / "ok-wuthering-waves" / "src" / "task" / "DailyTask.py"
OFFICIAL_MAIN = Path(__file__).resolve().parents[2] / "vendor-baselines" / "ok-wuthering-waves" / "main.py"
WORKING_DAILY = Path(r"C:\Game\ok-ww\data\apps\ok-ww\working\src\task\DailyTask.py")
WORKING_MAIN = Path(r"C:\Game\ok-ww\data\apps\ok-ww\working\main.py")


FORBIDDEN = (
    "yeyu_operation_enabled",
    "spend_target",
    "stamina_target",
    "yeyu_capture_stage",
    "YEYU_GAMER_SELECTED_OPERATIONS",
    "GameWindowLostException",
)

REQUIRED = (
    "need_stamina = not daily_reward_ready and used_stamina < 180",
    "need_nightmare",
    "Farm Nightmare Nest for Daily Echo",
    "Additional Tasks to Run After Daily Task",
    "claim_daily",
    "claim_mail",
    "claim_battle_pass",
)


def assert_official_source(path: Path, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    for token in FORBIDDEN:
        assert token not in text, "{} still contains YeYu rewrite token {!r}".format(label, token)
    for token in REQUIRED:
        assert token in text, "{} is missing official DailyTask token {!r}".format(label, token)


def assert_official_main(path: Path, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    for token in ("yeyu", "YeYu", "YEYU_GAMER", "--task", "--exit"):
        assert token not in text, "{} still contains YeYu launch patch token {!r}".format(label, token)
    assert "ok.start()" in text, "{} is missing the official OK.start() entry".format(label)


def main() -> None:
    assert_official_source(OFFICIAL_DAILY, "vendor DailyTask")
    assert_official_main(OFFICIAL_MAIN, "vendor main.py")
    if WORKING_DAILY.is_file():
        assert_official_source(WORKING_DAILY, "installed working DailyTask")
        vendor = OFFICIAL_DAILY.read_bytes().replace(b"\r\n", b"\n")
        working = WORKING_DAILY.read_bytes().replace(b"\r\n", b"\n")
        assert vendor == working, "installed working DailyTask is not the official vendor baseline"
    if WORKING_MAIN.is_file():
        assert_official_main(WORKING_MAIN, "installed working main.py")
        vendor_main = OFFICIAL_MAIN.read_bytes().replace(b"\r\n", b"\n")
        working_main = WORKING_MAIN.read_bytes().replace(b"\r\n", b"\n")
        assert vendor_main == working_main, "installed working main.py is not the official vendor baseline"
    print(json.dumps({"status": "passed", "officialDailyTask": str(OFFICIAL_DAILY), "gameStarted": False, "toolStarted": False}))


if __name__ == "__main__":
    sys.exit(main())
