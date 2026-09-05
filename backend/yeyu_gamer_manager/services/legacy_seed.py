from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

from .legacy_import import SAFE_CONFIG_KEYS, _read_json, _safe_projection


def _write_json_once(path: Path, value: dict[str, Any]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def export_safe_legacy_seed(source_root: Path, destination_root: Path) -> dict[str, Any]:
    """Copy only Manager-importable legacy configuration into local runtime.

    This migration helper deliberately excludes paths, commands and credentials.
    It never copies scripts and never overwrites an existing local seed.
    """

    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    config, _ = _read_json(source_root / "daily-gui-config.json")
    policy, _ = _read_json(source_root / "game-automation-policy.json")
    order = config.get("order")
    enabled = config.get("enabled")
    if not isinstance(order, list) or not isinstance(enabled, dict):
        raise ValueError("legacy order/enabled fields have invalid types")
    if not all(
        isinstance(game_id, str) and game_id in enabled for game_id in order
    ):
        raise ValueError("every ordered GameId must have an enabled flag")

    safe_config = {
        key: _safe_projection(value)
        for key, value in config.items()
        if key in SAFE_CONFIG_KEYS
    }
    safe_policy = _safe_projection(policy)
    if not isinstance(safe_policy, dict):
        raise ValueError("legacy policy must remain an object after sanitization")

    config_written = _write_json_once(
        destination_root / "daily-gui-config.json", safe_config
    )
    policy_written = _write_json_once(
        destination_root / "game-automation-policy.json", safe_policy
    )
    return {
        "sourceRead": True,
        "destination": str(destination_root),
        "configWritten": config_written,
        "policyWritten": policy_written,
        "gameCount": len(order),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a one-time sanitized YeYu Gamer migration seed"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    report = export_safe_legacy_seed(Path(args.source), Path(args.destination))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
