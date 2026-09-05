"""Re-open and verify Manager-owned evidence at decision boundaries.

Artifact import validates bytes once, but completion may be adjudicated much
later.  The ledger is therefore only a claim until this module proves that the
same regular file still exists below the configured local artifact root and
still matches its recorded size and SHA-256 digest.

The verifier deliberately returns a small typed result instead of raising.
Callers can preserve the exact fail-closed reason in an adjudication without
logging internal filesystem paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError

from .adapter_protocol import MAX_ARTIFACT_BYTES


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_IMAGE_PIXELS = 33_177_600  # one 8K frame
_MAX_IMAGE_DIMENSION = 16_384


@dataclass(frozen=True, slots=True)
class ArtifactIntegrityResult:
    valid: bool
    reason_code: str
    content_hash: str = ""
    content: bytes = b""


def _reject_non_standard_json_constant(value: str) -> None:
    """Reject Python's permissive NaN/Infinity JSON extensions."""

    raise ValueError(f"non-standard JSON constant: {value}")


def _result(
    valid: bool,
    reason_code: str,
    *,
    content_hash: str = "",
    content: bytes = b"",
) -> ArtifactIntegrityResult:
    return ArtifactIntegrityResult(
        valid=valid,
        reason_code=reason_code,
        content_hash=content_hash,
        content=content,
    )


def _is_reparse(path: Path, snapshot: os.stat_result | None = None) -> bool:
    try:
        current = snapshot if snapshot is not None else os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(current.st_mode) or bool(
        getattr(current, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    first_identity = (first.st_dev, first.st_ino)
    second_identity = (second.st_dev, second.st_ino)
    if any(first_identity) or any(second_identity):
        return first_identity == second_identity
    return True


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_identity(first, second)
        and stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def read_bounded_file_descriptor(descriptor: int, expected_size: int) -> bytes:
    """Read at most the declared bytes plus one from an open descriptor.

    A single ``os.read`` call may legally return fewer bytes than requested
    before EOF.  Continue on the same descriptor so a transient short read
    cannot make a stable artifact look truncated.
    """

    remaining = expected_size + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _image_validation_failure(content_type: str, content: bytes) -> str | None:
    expected_format = "PNG" if content_type == "image/png" else "JPEG"
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if (
                image.format != expected_format
                or width < 1
                or height < 1
                or width > _MAX_IMAGE_DIMENSION
                or height > _MAX_IMAGE_DIMENSION
                or width * height > _MAX_IMAGE_PIXELS
                or getattr(image, "n_frames", 1) != 1
            ):
                return "artifact_image_dimensions_invalid"
            image.verify()
        # verify() checks the container but does not decode pixels.  Re-open
        # from the same immutable bytes and force a complete decode so a
        # truncated IDAT/scan cannot pass as a screenshot.
        with Image.open(BytesIO(content)) as decoded:
            if decoded.format != expected_format or decoded.size != (width, height):
                return "artifact_image_format_mismatch"
            decoded.load()
    except Image.DecompressionBombError:
        return "artifact_image_dimensions_invalid"
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return "artifact_image_invalid"
    return None


def _content_validation_failure(content_type: str, content: bytes) -> str | None:
    if content_type == "image/png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "artifact_magic_mismatch"
        return _image_validation_failure(content_type, content)
    if content_type == "image/jpeg":
        valid = (
            len(content) >= 4
            and content.startswith(b"\xff\xd8\xff")
            and content.endswith(b"\xff\xd9")
        )
        if not valid:
            return "artifact_magic_mismatch"
        return _image_validation_failure(content_type, content)
    if content_type == "text/plain":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return "artifact_encoding_mismatch"
        return None if "\x00" not in text else "artifact_encoding_mismatch"
    if content_type == "application/json":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return "artifact_encoding_mismatch"
        try:
            json.loads(text, parse_constant=_reject_non_standard_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError):
            return "artifact_json_mismatch"
        return None
    return "artifact_content_type_unsupported"


def verify_artifact_entity(
    artifact_root: Path,
    artifact_id: str,
    document: Mapping[str, Any],
    *,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> ArtifactIntegrityResult:
    """Verify a ledger artifact against its current on-disk entity.

    ``artifact_id`` is accepted only for diagnostics; it is never interpreted
    as a path.  The relative path must come from the Manager ledger and remain
    contained below ``artifact_root`` throughout the bounded read.
    """

    del artifact_id
    root = Path(artifact_root)
    if not root.is_absolute() or str(root).startswith("\\\\"):
        return _result(False, "artifact_root_invalid")
    try:
        root_before = os.lstat(root)
    except OSError:
        return _result(False, "artifact_root_unavailable")
    if not stat.S_ISDIR(root_before.st_mode) or _is_reparse(root, root_before):
        return _result(False, "artifact_root_reparse")
    for component in (root, *root.parents):
        try:
            if _is_reparse(component, os.lstat(component)):
                return _result(False, "artifact_root_reparse")
        except OSError:
            return _result(False, "artifact_root_unavailable")

    relative = document.get("relativePath")
    if not isinstance(relative, str) or not relative:
        return _result(False, "artifact_path_invalid")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        return _result(False, "artifact_path_invalid")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (root / relative_path).resolve(strict=True)
        candidate.relative_to(resolved_root)
    except FileNotFoundError:
        return _result(False, "artifact_file_unavailable")
    except (OSError, ValueError):
        return _result(False, "artifact_path_escape")

    # Resolve containment is not enough on Windows: reject every junction or
    # symlink component before opening the final file.
    current = root
    for part in relative_path.parts:
        current = current / part
        try:
            component_snapshot = os.lstat(current)
        except OSError:
            return _result(False, "artifact_file_unavailable")
        if _is_reparse(current, component_snapshot):
            return _result(False, "artifact_path_reparse")

    expected_size = document.get("sizeBytes")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
        or expected_size > max_bytes
    ):
        return _result(False, "artifact_size_invalid")
    expected_hash = document.get("hash")
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        return _result(False, "artifact_hash_invalid")

    try:
        path_before = os.lstat(candidate)
    except OSError:
        return _result(False, "artifact_file_unavailable")
    if not stat.S_ISREG(path_before.st_mode) or _is_reparse(candidate, path_before):
        return _result(False, "artifact_file_unavailable")
    if path_before.st_size != expected_size:
        return _result(False, "artifact_size_mismatch")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if not _same_file(path_before, opened) or opened.st_size != expected_size:
            return _result(False, "artifact_changed_before_read")
        content = read_bounded_file_descriptor(descriptor, expected_size)
        after_handle = os.fstat(descriptor)
    except OSError:
        return _result(False, "artifact_file_unavailable")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not _same_file(opened, after_handle):
        return _result(False, "artifact_changed_during_read")
    if len(content) != expected_size:
        return _result(False, "artifact_size_mismatch")
    try:
        path_after = os.lstat(candidate)
        root_after = os.lstat(root)
    except OSError:
        return _result(False, "artifact_changed_during_read")
    if (
        not _same_file(opened, path_after)
        or not _same_identity(root_before, root_after)
        or _is_reparse(root, root_after)
    ):
        return _result(False, "artifact_changed_during_read")

    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        return _result(
            False,
            "artifact_hash_mismatch",
            content_hash=actual_hash,
        )
    content_failure = _content_validation_failure(
        str(document.get("contentType") or "").lower(), content
    )
    if content_failure is not None:
        return _result(
            False,
            content_failure,
            content_hash=actual_hash,
        )
    return _result(
        True,
        "verified",
        content_hash=actual_hash,
        content=content,
    )
