"""Stable Manager API error types shared by composed services."""

from __future__ import annotations

from typing import Any


class ManagerConflict(RuntimeError):
    pass


class ManagerValidation(ValueError):
    pass


class ExecutionUnavailable(ManagerConflict):
    """The requested execute scope has no verified Todo binding."""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__("No selected Todo has a verified executable binding.")
        self.details = details
