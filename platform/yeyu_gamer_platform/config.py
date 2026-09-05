"""Configuration and fixed local path rules for YeYu Gamer clients."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


FIXED_MANAGER_BASE_URL = "http://127.0.0.1:8877/api/v1"
FIXED_WEB_URL = "http://127.0.0.1:8877/"
DEFAULT_BASE_URL = FIXED_MANAGER_BASE_URL
DEFAULT_WEB_URL = FIXED_WEB_URL
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MANAGER_MODULE = "yeyu_gamer_manager"


def _windows_known_folder(csidl: int) -> Path:
    """Read a Windows known folder without trusting mutable process variables."""

    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError(f"Windows known-folder lookup failed for CSIDL {csidl:#x}")
    return Path(buffer.value)


def default_install_root() -> Path:
    if os.name == "nt":
        local_app_data = _windows_known_folder(0x001C)  # CSIDL_LOCAL_APPDATA
    else:
        local_app_data = Path.home() / ".local" / "share"
    return local_app_data / "Programs" / "YeYuGamer"


def default_runtime_root() -> Path:
    if os.name == "nt":
        program_data = _windows_known_folder(0x0023)  # CSIDL_COMMON_APPDATA
    else:
        program_data = Path("/var/lib")
    return program_data / "YeYuGamer" / "runtime"


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.expanduser())))


def _locked_path(data: Mapping[str, Any], key: str, expected: Path) -> Path:
    configured = data.get(key)
    if configured is not None and _path_key(Path(configured)) != _path_key(expected):
        raise ValueError(f"{key} is fixed by the installed YeYu Gamer layout")
    return expected


def ensure_loopback_url(url: str, *, name: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{name} must use http or https")
    if parsed.hostname is None or parsed.hostname.lower() not in LOOPBACK_HOSTS:
        raise ValueError(f"{name} must be loopback-only; got {parsed.hostname!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials")
    return url.rstrip("/")


def ensure_fixed_url(url: str, *, expected: str, name: str) -> str:
    if url != expected:
        raise ValueError(f"{name} is fixed at {expected}; overrides are disabled")
    return expected


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration root must be an object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """Immutable client configuration; executable paths are derived, never parsed."""

    install_root: Path
    runtime_root: Path
    manager_base_url: str
    web_url: str
    legacy_root: Path | None
    web_dist: Path
    actor_token_file: Path | None
    request_timeout_seconds: float = 3.0
    startup_timeout_seconds: float = 150.0
    health_refresh_seconds: float = 10.0
    launch_at_windows_logon: bool = False
    _test_only_allow_variable_urls: bool = False

    @property
    def log_directory(self) -> Path:
        return self.runtime_root / "logs"

    @property
    def state_directory(self) -> Path:
        return self.runtime_root / "state"

    @property
    def config_file(self) -> Path:
        return self.runtime_root / "config" / "platform.json"

    @property
    def manager_executable(self) -> Path:
        if os.name == "nt":
            return self.install_root / ".venv" / "Scripts" / "pythonw.exe"
        return self.install_root / ".venv" / "bin" / "python"

    @property
    def manager_command(self) -> tuple[str, ...]:
        return (
            str(self.manager_executable),
            "-I",
            "-B",
            "-m",
            MANAGER_MODULE,
        )

    @property
    def manager_working_directory(self) -> Path:
        return self.install_root / "app"

    @property
    def actor_token_directory(self) -> Path:
        configured = self.actor_token_file
        if (
            configured is None
            or configured.suffix.lower() == ".token"
            or (configured.exists() and configured.is_file())
        ):
            return self.runtime_root / "secrets" / "actors"
        return configured

    @classmethod
    def load(
        cls,
        config_path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "PlatformConfig":
        env = dict(os.environ if environ is None else environ)
        install_root = default_install_root()
        runtime_root = default_runtime_root()
        selected_config = Path(
            config_path or runtime_root / "config" / "platform.json"
        ).expanduser()
        data = _read_json_object(selected_config)

        # These paths are installation policy, not mutable configuration. Old
        # managerCommand/managerWorkingDirectory keys are deliberately ignored.
        install_root = _locked_path(data, "installRoot", install_root)
        runtime_root = _locked_path(data, "runtimeRoot", runtime_root)
        base_url = env.get(
            "YEYU_GAMER_MANAGER_BASE_URL",
            str(data.get("managerBaseUrl", DEFAULT_BASE_URL)),
        )
        web_url = env.get(
            "YEYU_GAMER_WEB_URL", str(data.get("webUrl", DEFAULT_WEB_URL))
        )
        legacy_root = _locked_path(
            data,
            "legacyRoot",
            runtime_root / "import" / "legacy-config",
        )
        web_dist = _locked_path(
            data,
            "webDist",
            install_root / "app" / "webgui" / "dist",
        )
        actor_token_file = _locked_path(
            data,
            "actorTokenFile",
            runtime_root / "secrets" / "actors",
        )

        config = cls(
            install_root=install_root,
            runtime_root=runtime_root,
            manager_base_url=ensure_fixed_url(
                base_url,
                expected=FIXED_MANAGER_BASE_URL,
                name="managerBaseUrl",
            ),
            web_url=ensure_fixed_url(
                web_url,
                expected=FIXED_WEB_URL,
                name="webUrl",
            ),
            legacy_root=legacy_root,
            web_dist=web_dist,
            actor_token_file=actor_token_file,
            request_timeout_seconds=float(data.get("requestTimeoutSeconds", 3.0)),
            startup_timeout_seconds=float(data.get("startupTimeoutSeconds", 150.0)),
            health_refresh_seconds=float(data.get("healthRefreshSeconds", 10.0)),
            # Login startup remains deliberately disabled by the product contract.
            launch_at_windows_logon=False,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self._test_only_allow_variable_urls:
            ensure_loopback_url(self.manager_base_url, name="managerBaseUrl")
            ensure_loopback_url(self.web_url, name="webUrl")
            production_token_root = default_runtime_root() / "secrets" / "actors"
            if (
                self.manager_base_url != FIXED_MANAGER_BASE_URL
                and self.actor_token_file is not None
                and _path_key(self.actor_token_directory) == _path_key(production_token_root)
            ):
                raise ValueError(
                    "test-only endpoint overrides must not read production actor tokens"
                )
        else:
            ensure_fixed_url(
                self.manager_base_url,
                expected=FIXED_MANAGER_BASE_URL,
                name="managerBaseUrl",
            )
            ensure_fixed_url(
                self.web_url,
                expected=FIXED_WEB_URL,
                name="webUrl",
            )
        if self.request_timeout_seconds <= 0:
            raise ValueError("requestTimeoutSeconds must be positive")
        if self.startup_timeout_seconds <= 0:
            raise ValueError("startupTimeoutSeconds must be positive")
        if self.health_refresh_seconds < 1:
            raise ValueError("healthRefreshSeconds must be at least one second")
        if self.launch_at_windows_logon:
            raise ValueError("launchAtWindowsLogon is disabled by the v3 contract")

    def with_base_url(self, base_url: str) -> "PlatformConfig":
        if not self._test_only_allow_variable_urls:
            ensure_fixed_url(
                base_url,
                expected=FIXED_MANAGER_BASE_URL,
                name="managerBaseUrl",
            )
        return replace(
            self,
            manager_base_url=ensure_loopback_url(base_url, name="managerBaseUrl"),
        )

    @classmethod
    def for_test(cls, **values: Any) -> "PlatformConfig":
        """Construct an explicitly marked loopback-only test endpoint seam."""

        config = cls(**values, _test_only_allow_variable_urls=True)
        config.validate()
        return config
