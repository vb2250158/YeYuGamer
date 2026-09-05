from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _discover_legacy_root() -> Path:
    configured = os.getenv("YEYU_GAMER_LEGACY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "daily-gui-config.json").is_file() and (
            candidate / "game-automation-policy.json"
        ).is_file():
            return candidate
    return here.parents[3]


def _default_data_dir() -> Path:
    configured = os.getenv("YEYU_GAMER_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    program_data = os.getenv("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "YeYuGamer" / "runtime"
    return Path.home() / ".local" / "share" / "YeYuGamer"


def _default_web_dist(legacy_root: Path) -> Path:
    configured = os.getenv("YEYU_GAMER_WEB_DIST")
    if configured:
        return Path(configured).expanduser().resolve()
    return legacy_root / "yeyu_gamer" / "webgui" / "dist"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    database_path: Path
    legacy_root: Path
    web_dist: Path
    legacy_execution_enabled: bool
    allow_non_loopback: bool
    actor_tokens_dir: Path
    notification_secrets_dir: Path
    tray_bootstrap_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        legacy_root = _discover_legacy_root()
        data_dir = _default_data_dir()
        host = os.getenv("YEYU_GAMER_HOST", "127.0.0.1").strip()
        allow_non_loopback = False
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "Refusing a non-loopback bind. YeYu Gamer v1 has no remote bind escape hatch."
            )
        database_path = Path(
            os.getenv("YEYU_GAMER_DATABASE", str(data_dir / "manager.sqlite3"))
        ).expanduser().resolve()
        # The module-level ASGI app and the explicit __main__ app are both
        # constructed in the packaged process, so this inherited value must be
        # readable by each construction. It is never serialized or logged.
        tray_bootstrap_secret = os.getenv("YEYU_GAMER_TRAY_BOOTSTRAP_SECRET")
        return cls(
            host=host,
            port=int(os.getenv("YEYU_GAMER_PORT", "8877")),
            data_dir=data_dir,
            database_path=database_path,
            legacy_root=legacy_root,
            web_dist=_default_web_dist(legacy_root),
            legacy_execution_enabled=_env_bool(
                "YEYU_GAMER_LEGACY_EXECUTION_ENABLED", False
            ),
            allow_non_loopback=allow_non_loopback,
            actor_tokens_dir=Path(
                os.getenv(
                    "YEYU_GAMER_ACTOR_TOKENS_DIR",
                    str(data_dir / "secrets" / "actors"),
                )
            ).expanduser().resolve(),
            # This is deliberately not environment-overridable. Production
            # notification secrets live only under the fixed local runtime.
            notification_secrets_dir=(data_dir / "secrets" / "notifications").resolve(),
            tray_bootstrap_secret=tray_bootstrap_secret,
        )

    @classmethod
    def for_test(cls, root: Path) -> "Settings":
        root = root.resolve()
        data_dir = root / "data"
        return cls(
            host="127.0.0.1",
            port=8877,
            data_dir=data_dir,
            database_path=data_dir / "manager.sqlite3",
            legacy_root=root / "legacy",
            web_dist=root / "web-dist",
            legacy_execution_enabled=False,
            allow_non_loopback=False,
            actor_tokens_dir=data_dir / "secrets" / "actors",
            notification_secrets_dir=data_dir / "secrets" / "notifications",
            tray_bootstrap_secret=(
                "test-only-ephemeral-tray-bootstrap-secret-"
                "000000000000000000000000"
            ),
        )
