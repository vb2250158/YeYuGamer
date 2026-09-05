"""Local platform clients for YeYu Gamer.

This package contains no game automation implementation.  It talks to the
YeYu Gamer Manager through its typed HTTP API and may start the configured
Manager process without creating a console window.
"""

from .agent_client import AgentManagerClient, RabiRouteManagerClient
from .api_client import ManagerApiClient, ManagerApiError
from .config import PlatformConfig

__all__ = [
    "AgentManagerClient",
    "ManagerApiClient",
    "ManagerApiError",
    "PlatformConfig",
    "RabiRouteManagerClient",
]
__version__ = "0.3.3"
