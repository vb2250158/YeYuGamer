"""Candidate-only passive telemetry bootstrap for OpenKuro DailyTask runs."""
from __future__ import annotations

from functools import wraps
import os


def _install_logging_after_official_configuration() -> None:
    """Append the observer handler after `ok` replaces its own handlers."""
    if not os.environ.get("YEYU_GAMER_UPSTREAM_LOG_FILE"):
        return
    try:
        import ok.util.logger as logger_module
        original = logger_module.config_logger
        if getattr(original, "_yeyu_logging_observer", False):
            return

        @wraps(original)
        def configured(*args, **kwargs):
            result = original(*args, **kwargs)
            try:
                from upstream_logging import install
                install(os.environ.get("YEYU_GAMER_UPSTREAM_LOG_FILE", ""))
            except Exception:
                pass
            return result

        configured._yeyu_logging_observer = True
        logger_module.config_logger = configured
    except Exception:
        # Import/configuration observation is never allowed to affect startup.
        pass


def _install_task_observer() -> None:
    game = os.environ.get("YEYU_GAMER_GAME_ID")
    if game == "Endfield":
        from EndfieldYeYuBridge import install_yeyu_gamer_bridge
        install_yeyu_gamer_bridge()
    elif game == "WW":
        from WwYeYuObserver import install
        install()
    elif game == "GF2":
        from Gf2YeYuObserver import install
        install()


_install_logging_after_official_configuration()
_install_task_observer()
