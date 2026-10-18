from __future__ import annotations

import re


SCREENSHOT_UPLOAD_BODY_BYTES = 12 * 1024 * 1024
SCREENSHOT_UPLOAD_PATH = re.compile(r"/api/v1/todo-instances/[^/]+/evidence-uploads")


def operation_read_allowed(path: str) -> bool:
    return re.fullmatch(
        r"/api/v1/(?:todo-instances(?:/[^/]+(?:/attempts)?)?"
        r"|game-runs(?:/[^/]+(?:/attempts)?)?"
        r"|artifacts(?:/[^/]+(?:/content)?)?)",
        path,
    ) is not None


def operation_write_scope(path: str, method: str) -> str | None:
    if method != "POST":
        return None
    if re.fullmatch(
        r"/api/v1/game-runs/[^/]+/(?:cancel|resume|takeover|takeover-release)-requests",
        path,
    ):
        return "execution:control"
    if re.fullmatch(r"/api/v1/todo-instances/[^/]+/transitions", path):
        return "todo:review"
    if SCREENSHOT_UPLOAD_PATH.fullmatch(path) or path == "/api/v1/evidence-reviews":
        return "evidence:write"
    return None
