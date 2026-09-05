from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python 3.12 always provides zoneinfo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = KeyError  # type: ignore[assignment]


TODO_INSTANCE_NAMESPACE = uuid.UUID("2dcae8e6-55db-4e06-a0f6-3285e847c35a")
WEEKDAYS = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}


@lru_cache(maxsize=32)
def _timezone(name: str):
    # Memoized: without the tzdata wheel every ZoneInfo lookup walks sys.path
    # before failing (~10 ms), and one snapshot resolves hundreds of periods.
    if name == "Asia/Shanghai":
        # The packaged Windows Python may not include the IANA tzdata wheel.
        # China Standard Time has no DST, so this fallback is exact.
        try:
            if ZoneInfo is not None:
                return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
        return timezone(timedelta(hours=8), name="Asia/Shanghai")
    if ZoneInfo is None:
        raise ValueError(f"timezone data is unavailable for {name}")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown reset timezone: {name}") from error


def _reset_hour_minute(reset_rule: dict[str, Any]) -> tuple[int, int]:
    value = str(reset_rule.get("time") or "04:00")
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid reset time: {value}") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid reset time: {value}")
    return hour, minute


def todo_period(reset_rule: dict[str, Any], at: datetime | None = None) -> dict[str, str]:
    """Return the current immutable daily/weekly period for one definition."""

    zone_name = str(reset_rule.get("timezone") or "Asia/Shanghai")
    zone = _timezone(zone_name)
    instant = at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(zone)
    hour, minute = _reset_hour_minute(reset_rule)
    reset_today = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    effective = local if local >= reset_today else local - timedelta(days=1)
    cadence = str(reset_rule.get("cadence") or "daily")
    if cadence == "daily":
        start = effective.replace(hour=hour, minute=minute, second=0, microsecond=0)
        key = effective.date().isoformat()
        end = start + timedelta(days=1)
    elif cadence == "weekly":
        weekday_name = str(reset_rule.get("weekStartDay") or "Monday")
        if weekday_name not in WEEKDAYS:
            raise ValueError(f"invalid weekly reset day: {weekday_name}")
        start_date = effective.date() - timedelta(
            days=(effective.weekday() - WEEKDAYS[weekday_name]) % 7
        )
        start = effective.replace(
            year=start_date.year,
            month=start_date.month,
            day=start_date.day,
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if local < start:
            start -= timedelta(days=7)
        key = f"week:{start.date().isoformat()}"
        end = start + timedelta(days=7)
    else:
        raise ValueError(f"unsupported todo cadence: {cadence}")
    return {
        "periodKey": key,
        "startsAt": start.isoformat(),
        "endsAt": end.isoformat(),
        "timezone": zone_name,
    }


def todo_instance_id(todo_definition_id: str, period_key: str) -> str:
    value = uuid.uuid5(
        TODO_INSTANCE_NAMESPACE,
        f"yeyu-gamer/todo-instance/v1/{todo_definition_id}/{period_key}",
    )
    return f"todo-instance-{value}"
