"""Credential-bound execution entry points; request body labels are untrusted."""
import re

LAUNCH_ACTORS = frozenset({"webgui", "agent", "agent-launch", "scheduler"})


def is_launch_request(path: str, method: str) -> bool:
    return method == "POST" and bool(re.fullmatch(
        r"/api/v1/(?:batches|game-runs|batches/[^/]+/resume-requests|"
        r"game-runs/[^/]+/resume-requests|games/[^/]+/(?:run|launch)-requests)", path
    ))
