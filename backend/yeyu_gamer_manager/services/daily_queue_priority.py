"""Stable daily queue ordering without mutating items or caller configuration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache
import json
from pathlib import Path
from typing import TypeVar


QueueItem = TypeVar("QueueItem")


def _validate_priority(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("daily_queue_priority must be a list")
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        raise ValueError("daily_queue_priority entries must be non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError("daily_queue_priority entries must be unique")
    return tuple(value)


@lru_cache(maxsize=1)
def _load_default_priority() -> tuple[str, ...]:
    resource_path = Path(__file__).parent / "resources" / "daily_queue_priority.json"
    with resource_path.open(encoding="utf-8") as resource:
        return _validate_priority(json.load(resource))


def _identity_game_id(item: str) -> str:
    return item


def order_daily_queue(
    items: Iterable[QueueItem],
    config_values: Mapping[str, object],
    *,
    game_id: Callable[[QueueItem], str] = _identity_game_id,
) -> list[QueueItem]:
    """Return a stable ordered copy; invalid configuration or IDs raise ValueError.

    Missing or None priority uses resource defaults; an empty list preserves input
    order. The legacy order field is ignored. Unknown IDs follow ranked IDs while
    retaining input order, including account order within each game.
    """
    if not isinstance(config_values, Mapping):
        raise ValueError("config_values must be a mapping")
    configured = config_values.get("daily_queue_priority")
    priority = _load_default_priority() if configured is None else _validate_priority(configured)
    ranks = {identifier: index for index, identifier in enumerate(priority)}

    def rank(item: QueueItem) -> int:
        identifier = game_id(item)
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("queue game IDs must be non-empty strings")
        return ranks.get(identifier, len(ranks))

    return sorted(items, key=rank)
