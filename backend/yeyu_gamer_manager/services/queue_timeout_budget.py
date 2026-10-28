"""Load immutable queue timeout budgets; invalid configuration raises ValueError."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from functools import lru_cache
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class QueueTimeoutBudget:
    human_takeover_seconds: float
    completion_review_seconds: float
    callback_overhead_seconds: float
    callback_cancel_grace_seconds: float
    poll_seconds: float
    orphan_pending_seconds: float


@lru_cache(maxsize=1)
def _load_budget_limits():
    path = Path(__file__).parent / "resources" / "queue_timeout_limits.json"
    with path.open(encoding="utf-8") as resource:
        return json.load(resource)


def _validate_budget(values: Mapping[str, object]) -> QueueTimeoutBudget:
    field_names = {field.name for field in fields(QueueTimeoutBudget)}
    if set(values) != field_names:
        raise ValueError("queue_timeout_budget contains unknown or missing fields")
    normalized = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"queue_timeout_budget.{name} must be a finite number")
        try:
            seconds = float(value)
        except OverflowError as error:
            raise ValueError(f"queue_timeout_budget.{name} must be finite") from error
        allows_zero = name in {"human_takeover_seconds", "completion_review_seconds"}
        limits = _load_budget_limits()
        maximum = limits["maximum_poll_seconds"] if name == "poll_seconds" else limits["maximum_seconds"]
        if not math.isfinite(seconds) or seconds < 0 or seconds > maximum or (seconds == 0 and not allows_zero):
            raise ValueError(f"queue_timeout_budget.{name} is outside its allowed range")
        normalized[name] = seconds
    return QueueTimeoutBudget(**normalized)


@lru_cache(maxsize=1)
def _load_default_budget() -> QueueTimeoutBudget:
    resource_path = Path(__file__).parent / "resources" / "queue_timeout_budgets.json"
    with resource_path.open(encoding="utf-8") as resource:
        values = json.load(resource)
    if not isinstance(values, dict):
        raise ValueError("queue timeout defaults must be an object")
    return _validate_budget(values)


def load_queue_timeout_budget(config_values: Mapping[str, object]) -> QueueTimeoutBudget:
    """Apply optional queue_timeout_budget overrides without caching caller values."""
    if not isinstance(config_values, Mapping):
        raise ValueError("config_values must be a mapping")
    defaults = _load_default_budget()
    overrides = config_values.get("queue_timeout_budget", {})
    if not isinstance(overrides, Mapping):
        raise ValueError("queue_timeout_budget must be a mapping")
    if not overrides:
        return replace(defaults)
    values = {field.name: getattr(defaults, field.name) for field in fields(defaults)}
    values.update(overrides)
    return _validate_budget(values)
