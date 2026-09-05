from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..store.sqlite_store import SqliteStore


DISPLAY_NAMES = {
    "StarRail": "崩坏：星穹铁道",
    "Endfield": "明日方舟：终末地",
    "GF2": "少女前线2：追放",
    "ZZZ": "绝区零",
    "WW": "鸣潮",
    "NIKKE": "胜利女神：新的希望",
    "NTE": "异环",
    "PGR": "战双帕弥什",
    "FGO": "命运-冠位指定",
    "BD2": "棕色尘埃2",
    "CZN": "卡厄思梦境",
    "BA": "蔚蓝档案",
}

SAFE_CONFIG_KEYS = {
    "order",
    "enabled",
    "dailyScheduleEnabled",
    "dailyScheduleTime",
    "weeklyEnabled",
    "weeklyDay",
    "skipBlockedOnRunAll",
    "executionStrategy",
    "dailyResetHour",
    "stepTimeoutSeconds",
    "stopOnHumanTakeover",
    "continueOnIncompleteGames",
    "strictStopOnIncomplete",
}

BLOCKED_KEY_FRAGMENTS = {
    "path",
    "command",
    "secret",
    "token",
    "password",
    "credential",
    "cookie",
    "webhook",
    "smtp",
}


def _snake_case(value: str) -> str:
    chars: list[str] = []
    for character in value:
        if character.isupper() and chars:
            chars.append("_")
        chars.append(character.lower())
    return "".join(chars)


def _safe_projection(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in BLOCKED_KEY_FRAGMENTS):
                continue
            result[str(key)] = _safe_projection(child)
        return result
    if isinstance(value, list):
        return [_safe_projection(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _read_json(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = path.read_bytes()
    decoded = raw.decode("utf-8-sig")
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    stat = path.stat()
    return value, {
        "fileName": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "lastModifiedAt": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(),
    }


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    status: str
    detail: str
    imported: bool
    source_info: dict[str, Any]
    allowed_game_ids: tuple[str, ...]


class LegacyImporter:
    """Reads legacy JSON only; the Manager database is the sole write target."""

    def __init__(self, root: Path, store: SqliteStore):
        self.root = root
        self.store = store

    def run(self) -> LegacyImportReport:
        config_path = self.root / "daily-gui-config.json"
        policy_path = self.root / "game-automation-policy.json"
        missing = [path.name for path in (config_path, policy_path) if not path.is_file()]
        if missing:
            return LegacyImportReport(
                status="degraded",
                detail=f"legacy source missing: {', '.join(missing)}",
                imported=False,
                source_info={"missing": missing},
                allowed_game_ids=(),
            )

        try:
            config, config_source = _read_json(config_path)
            policy, policy_source = _read_json(policy_path)
            order = config.get("order", [])
            enabled = config.get("enabled", {})
            policy_games = policy.get("games", {})
            if not isinstance(order, list) or not isinstance(enabled, dict):
                raise ValueError("legacy order/enabled fields have invalid types")
            if not all(
                isinstance(game_id, str) and game_id in enabled for game_id in order
            ):
                raise ValueError("every ordered GameId must have an enabled flag")

            config_values = {
                _snake_case(key): _safe_projection(value)
                for key, value in config.items()
                if key in SAFE_CONFIG_KEYS
            }
            raw_reset_hour = config.get("dailyResetHour", 4)
            reset_hour = (
                int(raw_reset_hour)
                if isinstance(raw_reset_hour, int) and 0 <= raw_reset_hour <= 23
                else 4
            )
            config_values["todo_reset_policy"] = {
                "timezone": "Asia/Shanghai",
                "time": f"{reset_hour:02d}:00",
                "week_start_day": "Monday",
                "per_game": (
                    {"FGO": {"time": "00:00"}} if "FGO" in order else {}
                ),
                "per_definition": {},
            }
            games = [
                {
                    "game_id": game_id,
                    "display_name": DISPLAY_NAMES.get(game_id, game_id),
                    "order_index": index,
                    "enabled": bool(enabled[game_id]),
                    "policy": _safe_projection(policy_games.get(game_id, {})),
                }
                for index, game_id in enumerate(order)
            ]
            source_info = {
                "dailyGuiConfig": config_source,
                "automationPolicy": policy_source,
                "policySchemaVersion": policy.get("schemaVersion"),
            }
            imported = self.store.import_legacy(
                config_values=config_values,
                games=games,
                source_info=source_info,
                policy_projection=_safe_projection(policy),
            )
            return LegacyImportReport(
                status="ok",
                detail="legacy configuration imported read-only"
                if imported
                else "legacy configuration unchanged",
                imported=imported,
                source_info=source_info,
                allowed_game_ids=tuple(order),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return LegacyImportReport(
                status="error",
                detail=f"legacy import failed: {error}",
                imported=False,
                source_info={},
                allowed_game_ids=(),
            )
