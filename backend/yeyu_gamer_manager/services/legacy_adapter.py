from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterable


class LegacyExecutionDisabled(RuntimeError):
    pass


class LegacyAdapterBusy(RuntimeError):
    pass


class LegacyAdapter:
    """Local execution-package locator and explicit execution gate.

    Direct runner launch is retired. ``ManagerAdapterHost`` verifies the v2
    manifest and is the only component allowed to start the fixed Host protocol.
    This class deliberately retains no generic command, path, click, coordinate,
    key, or environment input surface.
    """

    def __init__(
        self,
        *,
        legacy_root: Path,
        runtime_dir: Path,
        allowed_game_ids: Iterable[str],
        execution_enabled: bool,
    ) -> None:
        # LegacyImporter may read this migration source, but execution must not.
        self.import_root = legacy_root.resolve()
        self.runtime_dir = runtime_dir.resolve()
        # Keep the nominal path instead of resolving the final adapter directory.
        # If an attacker later replaces that directory with a junction/symlink,
        # ``script_path`` must detect that the resolved entry escaped runtime.
        self.adapter_root = (
            self.runtime_dir / "adapters" / "legacy-night-rain-gamer"
        )
        self.allowed_game_ids = frozenset(allowed_game_ids)
        self.execution_enabled = execution_enabled
        self._lock = threading.RLock()
        self._active: subprocess.Popen[bytes] | None = None
        self._active_run_id: str | None = None

    def execution_package_root(self, game_id: str) -> Path:
        """Return the Manager-owned package root for one exact GameId.

        A per-game module lives beneath the fixed local runtime hierarchy.  The
        compatibility package remains a deliberately narrow fallback while
        modules are promoted one by one, so adding StarRail cannot overwrite a
        known-good WW/Endfield/GF2 package.  This lookup is internal only: an
        API caller cannot supply either the module name or a filesystem path.
        """
        self.validate_game_id(game_id)
        modules_root = self.runtime_dir / "adapters" / "game-modules"
        nominal = modules_root / game_id.lower()
        if nominal.is_dir() and not nominal.is_symlink():
            candidate = nominal.resolve()
            if (
                candidate.parent == modules_root.resolve()
                and candidate.name == game_id.lower()
                and candidate.is_relative_to(self.runtime_dir)
            ):
                return nominal
        return self.adapter_root

    def active_execution(self) -> dict[str, object] | None:
        """Return the active fixed adapter process without exposing a control handle."""
        with self._lock:
            if self._active is None:
                return None
            return {
                "runId": self._active_run_id,
                "pid": self._active.pid,
                "exited": self._active.poll() is not None,
            }

    @property
    def script_path(self) -> Path:
        nominal = self.adapter_root / "runner.exe"
        candidate = nominal.resolve()
        if (
            candidate.parent != self.adapter_root
            or candidate.name != "runner.exe"
            or not candidate.is_relative_to(self.runtime_dir)
        ):
            raise RuntimeError("local compatibility Adapter invariant failed")
        return candidate

    @property
    def manifest_path(self) -> Path:
        nominal = self.adapter_root / "install-manifest.json"
        candidate = nominal.resolve()
        if (
            candidate.parent != self.adapter_root
            or candidate.name != "install-manifest.json"
            or not candidate.is_relative_to(self.runtime_dir)
        ):
            raise RuntimeError("local compatibility Adapter manifest invariant failed")
        return candidate

    def validate_game_id(self, game_id: str) -> None:
        if game_id not in self.allowed_game_ids:
            raise ValueError(f"GameId is not in the imported allowlist: {game_id}")

    def describe(self, game_id: str) -> dict[str, object]:
        self.validate_game_id(game_id)
        return {
            "adapter": "legacy-night-rain-gamer",
            "gameId": game_id,
            "executionEnabled": self.execution_enabled,
            "entryPoint": "runner.exe",
            "protocolVersion": "1.1",
            "managerHostRequired": True,
            "directRunnerLaunch": False,
            "requestTransport": "bounded-stdin-json",
        }

    def start(
        self,
        run_id: str,
        game_id: str,
        on_complete: Callable[[str, int, str], None],
    ) -> int:
        self.validate_game_id(game_id)
        del run_id, on_complete
        raise LegacyExecutionDisabled(
            "direct legacy runner launch is retired; use ManagerAdapterHost protocol v1.1"
        )
