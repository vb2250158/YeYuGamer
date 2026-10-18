from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import uuid
import warnings
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PIL import Image

from ..domain.models import ScreenshotUploadRequest, utc_now
from .manager_errors import ManagerConflict, ManagerValidation
from .notifications.artifacts import _has_reparse_point

if TYPE_CHECKING:
    from .manager import ManagerService


MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_SCREENSHOT_PIXELS = 24_000_000


def prepare_review_screenshot(
    manager: ManagerService, todo_id: str, request: ScreenshotUploadRequest, *,
    idempotency_key: str, request_id: str | None, path: str,
) -> tuple[dict[str, Any], Callable[[], dict[str, Any]], list[Path]]:
    try:
        content = base64.b64decode(request.content_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ManagerValidation("Screenshot must be strict base64") from error
    if not content or len(content) > MAX_SCREENSHOT_BYTES:
        raise ManagerValidation("Screenshot must be at most 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in {"PNG", "JPEG"} or width * height > MAX_SCREENSHOT_PIXELS:
                    raise ValueError("Only bounded PNG/JPEG screenshots are accepted")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ManagerValidation("Invalid or oversized PNG/JPEG screenshot") from error
    digest = hashlib.sha256(content).hexdigest()
    created_paths: list[Path] = []

    def operation() -> dict:
        current = manager.store.get_todo_instance(todo_id)
        if current["status"] in {"in_progress", "completed"}:
            raise ManagerConflict("Cannot replace active or completed Todo results with manual evidence")
        now = utc_now()
        if not datetime.fromisoformat(current["period_starts_at"]) <= now < datetime.fromisoformat(current["period_ends_at"]):
            raise ManagerConflict("Manual evidence requires a current-period Todo")
        artifact_id = str(uuid.uuid4())
        root = manager.settings.data_dir / "artifacts"
        if str(root).startswith("\\\\"):
            raise ManagerValidation("Screenshot storage must be local")
        root.mkdir(parents=True, exist_ok=True)
        if any(_has_reparse_point(directory) for directory in (root, *root.parents)):
            raise ManagerValidation("Screenshot storage contains a link")
        suffix = ".png" if image_format == "PNG" else ".jpg"
        file_name = f"operator-upload-{artifact_id}{suffix}"
        destination = root / file_name
        with destination.open("xb") as stream:
            created_paths.append(destination)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        artifact = manager.store.create_resource(
            "artifact", resource_id=artifact_id, state="review_required", document={
                "kind": "operator-screenshot", "source": "operator-upload",
                "capturedAt": now.isoformat(), "capturedAtMeaning": "upload-received-at",
                "frameCaptureTimeKnown": False, "accountIdentityConfirmed": False,
                "raw": True, "diagnosticOnly": True, "verdict": "unknown",
                "contentType": "image/png" if image_format == "PNG" else "image/jpeg",
                "gameId": current["game_id"], "accountId": current["account_id"],
                "todoInstanceId": todo_id, "gameDayKey": current["period_key"],
                "hash": digest, "sizeBytes": len(content), "fileName": file_name,
                "relativePath": file_name, "originalFileName": request.file_name,
                "uploadedBy": request.requested_by, "note": request.note,
                "width": width, "height": height,
            },
        )
        todo = manager.store.transition_todo_instance(
            todo_id, status="review_required", reason=request.note, evidence_refs=[artifact_id],
            run_id=current["run_id"], increment_attempt=False, requested_by=request.requested_by,
        )
        return manager._receipt(
            command_id=str(uuid.uuid4()), idempotency_key=idempotency_key, request_id=request_id,
            status_url=f"/api/v1/todo-instances/{todo_id}",
            message="人工截图已保存并关联待审核事项；不代表程序验收完成。",
            result={"evidenceId": artifact_id, "artifact": manager._artifact_record(artifact).model_dump(mode="json", by_alias=True),
                    "todoInstance": manager._todo_instance_record(todo, runtime_cache={}).model_dump(mode="json", by_alias=True)},
        )

    identity = {"todoInstanceId": todo_id, "contentSha256": digest, "fileName": request.file_name,
                "note": request.note, "requestedBy": request.requested_by}
    return identity, operation, created_paths
