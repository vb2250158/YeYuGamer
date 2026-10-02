"""Validation for the fixed public MaaFgo task-options wire contract.

The generated schema is a snapshot of MaaFgo's public interface at the pinned
commit.  This module only validates a saved profile; it deliberately does not
fill defaults or infer an inactive branch.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic_core import PydanticCustomError

from .fgo_maafgo_a9f_task_options_schema import TASK_OPTIONS_SCHEMA


def validate_task_options(value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Accept only MaaFgo entry-keyed options with official value shapes."""

    for entry, options in value.items():
        entry_schema = TASK_OPTIONS_SCHEMA.get(entry)
        if entry_schema is None:
            _invalid(f"FGO taskOptions entry {entry!r} is not an official daily task entry")
        if not isinstance(options, Mapping):
            _invalid(f"FGO taskOptions[{entry!r}] must be an object")
        _validate_entry(entry, options, entry_schema)
    return value


def _validate_entry(
    entry: str,
    options: Mapping[str, Any],
    entry_schema: Mapping[str, Mapping[str, Any]],
) -> None:
    for option_name, option_value in options.items():
        specification = entry_schema.get(option_name)
        if specification is None:
            _invalid(
                f"FGO taskOptions[{entry!r}] option {option_name!r} is not in the pinned official schema"
            )
        kind = specification["kind"]
        if kind in {"select", "switch"}:
            _validate_static_choice(entry, option_name, option_value, specification["values"])
        elif kind == "scan_select":
            if not isinstance(option_value, str):
                raise ValueError(f"FGO taskOptions[{entry!r}][{option_name!r}] must be a string")
        elif kind == "checkbox":
            _validate_checkbox(entry, option_name, option_value, specification["values"])
        elif kind == "input":
            _validate_input(entry, option_name, option_value, specification["fields"])
        else:  # The generated schema is intentionally closed; do not silently accept new kinds.
            _invalid(f"FGO taskOptions schema has unsupported option type {kind!r}")


def _validate_static_choice(entry: str, option: str, value: Any, allowed: list[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        _invalid(f"FGO taskOptions[{entry!r}][{option!r}] is not an official case")


def _validate_checkbox(entry: str, option: str, value: Any, allowed: list[str]) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        _invalid(f"FGO taskOptions[{entry!r}][{option!r}] must contain only official cases")


def _validate_input(
    entry: str,
    option: str,
    value: Any,
    fields: Mapping[str, Mapping[str, str]],
) -> None:
    if not isinstance(value, Mapping):
        _invalid(f"FGO taskOptions[{entry!r}][{option!r}] must be an object")
    for field_name, field_value in value.items():
        field = fields.get(field_name)
        if field is None:
            _invalid(
                f"FGO taskOptions[{entry!r}][{option!r}] field {field_name!r} is not official"
            )
        if not isinstance(field_value, str):
            _invalid(
                f"FGO taskOptions[{entry!r}][{option!r}][{field_name!r}] must be a string"
            )
        verify = field.get("verify")
        if verify and re.fullmatch(verify, field_value) is None:
            _invalid(
                f"FGO taskOptions[{entry!r}][{option!r}][{field_name!r}] does not match its official format"
            )


def _invalid(message: str) -> None:
    """Emit a JSON-safe FastAPI validation context."""

    raise PydanticCustomError("fgo_task_options_invalid", "{message}", {"message": message})
