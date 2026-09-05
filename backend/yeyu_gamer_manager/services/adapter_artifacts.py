"""Import Manager Adapter artifacts through one fail-closed local boundary."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..store.sqlite_store import RecordNotFound, SqliteStore
from .adapter_protocol import (
    ALLOWED_ARTIFACT_MIME_TYPES,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_TODO,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    AdapterExecutionPlan,
    AdapterProtocolError,
    run_artifact_byte_limit,
)
from .artifact_integrity import read_bounded_file_descriptor


_LEAF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TODO_INSTANCE_ID = re.compile(
    r"^todo-instance-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "text/plain": ".txt",
}
_EVENT_FIELDS = frozenset(
    {
        "schemaVersion",
        "protocolVersion",
        "eventType",
        "sequence",
        "runId",
        "runAttemptId",
        "fencingToken",
        "gameId",
        "at",
        "todoInstanceId",
        "todoAttemptId",
        "artifactId",
        "kind",
        "fileName",
        "mimeType",
        "sizeBytes",
        "sha256",
        "capturedAt",
    }
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class AdapterArtifactImportError(AdapterProtocolError):
    """An Adapter artifact failed the Manager-owned import boundary."""


def _fail(code: str, message: str) -> AdapterArtifactImportError:
    return AdapterArtifactImportError(code, message)


def _canonical_uuid(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise _fail("invalid_artifact_event", f"{field} is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise _fail("invalid_artifact_event", f"{field} is invalid") from error
    canonical = str(parsed)
    if canonical != value:
        raise _fail("invalid_artifact_event", f"{field} is not canonical")
    return canonical


def _aware_timestamp(value: object, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise _fail("invalid_artifact_event", f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _fail("invalid_artifact_event", f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail("invalid_artifact_event", f"{field} must include an offset")
    return value, parsed.astimezone(timezone.utc)


def _is_reparse_point(path: Path, snapshot: os.stat_result | None = None) -> bool:
    try:
        current = snapshot if snapshot is not None else os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(current.st_mode):
        return True
    attributes = getattr(current, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    first_identity = (first.st_dev, first.st_ino)
    second_identity = (second.st_dev, second.st_ino)
    if any(first_identity) or any(second_identity):
        return first_identity == second_identity
    return True


def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_identity(first, second)
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _same_directory_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Keep the containment object stable without rejecting sibling writes.

    Adapter runners legitimately stage the next immutable artifact in the same
    run directory while Manager imports the preceding event.  Directory size
    and mtime therefore are not stable-file properties.  Identity, type and
    reparse checks still prevent the containing directory from being swapped.
    """
    return (
        _same_identity(first, second)
        and first.st_mode == second.st_mode
        and stat.S_ISDIR(second.st_mode)
    )


def _components(path: Path) -> tuple[Path, ...]:
    return tuple(reversed((path, *path.parents)))


def _is_remote_windows_path(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        # DRIVE_REMOTE = 4. This also closes the mapped-drive variant of a UNC
        # path, which a simple leading-backslash check cannot detect.
        return ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4
    except (AttributeError, OSError):
        return True


def _validate_local_directory(path: Path, *, create: bool, code: str) -> None:
    if (
        str(path).startswith("\\\\")
        or not path.is_absolute()
        or _is_remote_windows_path(path)
    ):
        raise _fail(code, "artifact storage must use an absolute local path")
    if create:
        try:
            nearest = path
            while not nearest.exists() and nearest != nearest.parent:
                nearest = nearest.parent
            for component in _components(nearest):
                if component.exists() and _is_reparse_point(component):
                    raise _fail(code, "artifact storage contains a reparse point")
            path.mkdir(parents=True, exist_ok=True)
        except AdapterArtifactImportError:
            raise
        except OSError:
            raise _fail(code, "artifact storage is unavailable") from None
    try:
        snapshot = os.lstat(path)
    except OSError:
        raise _fail(code, "artifact storage is unavailable") from None
    if not stat.S_ISDIR(snapshot.st_mode) or _is_reparse_point(path, snapshot):
        raise _fail(code, "artifact storage root is unsafe")
    for component in _components(path):
        if component.exists() and _is_reparse_point(component):
            raise _fail(code, "artifact storage contains a reparse point")


def _validate_leaf_name(value: object) -> str:
    if not isinstance(value, str) or not _LEAF_NAME.fullmatch(value):
        raise _fail("unsafe_artifact_path", "fileName is not a safe leaf")
    if Path(value).name != value or value in {".", ".."}:
        raise _fail("unsafe_artifact_path", "fileName is not a safe leaf")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise _fail("unsafe_artifact_path", "fileName is reserved")
    return value


def _validate_content(content_type: str, data: bytes) -> None:
    if content_type == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif content_type == "image/jpeg":
        valid = (
            len(data) >= 4
            and data.startswith(b"\xff\xd8\xff")
            and data.endswith(b"\xff\xd9")
        )
    elif content_type == "text/plain":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _fail("artifact_encoding_mismatch", "text artifact is not UTF-8") from error
        valid = "\x00" not in text
    else:
        raise _fail("artifact_mime_denied", "artifact MIME type is not allowed")
    if not valid:
        raise _fail("artifact_magic_mismatch", "artifact bytes do not match their MIME type")


class AdapterArtifactImporter:
    """Move one validated Adapter artifact into Manager-owned local storage."""

    def __init__(
        self,
        store: SqliteStore,
        staging_root: Path,
        artifact_root: Path,
    ) -> None:
        self.store = store
        self.staging_root = Path(staging_root).absolute()
        self.artifact_root = Path(artifact_root).absolute()
        if (
            self.staging_root == self.artifact_root
            or self.staging_root in self.artifact_root.parents
            or self.artifact_root in self.staging_root.parents
        ):
            raise _fail(
                "artifact_root_invalid",
                "staging and Manager artifact roots must be separate",
            )
        _validate_local_directory(
            self.staging_root, create=True, code="artifact_staging_root_invalid"
        )
        _validate_local_directory(
            self.artifact_root, create=True, code="artifact_root_invalid"
        )
        self._staging_root_identity = os.lstat(self.staging_root)
        self._artifact_root_identity = os.lstat(self.artifact_root)

    def import_staged(
        self,
        plan: AdapterExecutionPlan,
        event_document: Mapping[str, Any],
        *,
        allowed_mime_types: Sequence[str],
        max_artifacts_per_todo: int,
        max_artifact_bytes: int,
    ) -> str:
        """Validate, copy, and register one ``artifact_staged`` event."""

        allowed_mime_policy = self._validate_manifest_policy(
            allowed_mime_types=allowed_mime_types,
            max_artifacts_per_todo=max_artifacts_per_todo,
            max_artifact_bytes=max_artifact_bytes,
        )
        final_path: Path | None = None
        try:
            with self.store.atomic():
                artifact_id, final_path = self._import_staged_atomic(
                    plan,
                    event_document,
                    allowed_mime_types=allowed_mime_policy,
                    max_artifacts_per_todo=max_artifacts_per_todo,
                    max_artifact_bytes=max_artifact_bytes,
                )
        except Exception:
            if final_path is not None:
                self._remove_new_target(final_path)
            raise
        return artifact_id

    @staticmethod
    def _validate_manifest_policy(
        *,
        allowed_mime_types: Sequence[str],
        max_artifacts_per_todo: int,
        max_artifact_bytes: int,
    ) -> frozenset[str]:
        if isinstance(allowed_mime_types, (str, bytes)):
            raise _fail("artifact_mime_denied", "artifact MIME policy is invalid")
        allowed = tuple(allowed_mime_types)
        if (
            not allowed
            or len(set(allowed)) != len(allowed)
            or any(
                not isinstance(value, str)
                or value not in ALLOWED_ARTIFACT_MIME_TYPES
                for value in allowed
            )
        ):
            raise _fail("artifact_mime_denied", "artifact MIME policy is invalid")
        if (
            isinstance(max_artifacts_per_todo, bool)
            or not isinstance(max_artifacts_per_todo, int)
            or not 1 <= max_artifacts_per_todo <= MAX_ARTIFACTS_PER_TODO
            or isinstance(max_artifact_bytes, bool)
            or not isinstance(max_artifact_bytes, int)
            or not 1 <= max_artifact_bytes <= MAX_ARTIFACT_BYTES
        ):
            raise _fail("artifact_limit_invalid", "artifact limits are invalid")
        return frozenset(allowed)

    def _import_staged_atomic(
        self,
        plan: AdapterExecutionPlan,
        event_document: Mapping[str, Any],
        *,
        allowed_mime_types: frozenset[str],
        max_artifacts_per_todo: int,
        max_artifact_bytes: int,
    ) -> tuple[str, Path]:
        """Import one artifact inside the Store's immediate transaction."""

        event = self._validate_event(
            plan,
            event_document,
            allowed_mime_types=allowed_mime_types,
            max_artifact_bytes=max_artifact_bytes,
        )
        self._validate_quota(
            plan,
            event,
            max_artifacts_per_todo=max_artifacts_per_todo,
            max_artifact_bytes=max_artifact_bytes,
        )
        source = self._source_path(plan, str(event["fileName"]))
        data = self._read_verified_source(
            source,
            expected_size=int(event["sizeBytes"]),
        )
        digest = hashlib.sha256(data).hexdigest()
        if digest != event["sha256"]:
            raise _fail("artifact_hash_mismatch", "artifact SHA-256 differs")
        content_type = str(event["mimeType"])
        _validate_content(content_type, data)

        artifact_id = str(event["artifactId"])
        try:
            todo_instance = self.store.get_todo_instance(
                str(event["todoInstanceId"])
            )
        except RecordNotFound as error:
            raise _fail(
                "event_scope_mismatch",
                "artifact Todo is no longer registered",
            ) from error
        game_day_key = str(todo_instance.get("period_key") or "")
        if not game_day_key:
            raise _fail(
                "event_scope_mismatch",
                "artifact Todo lacks a frozen game-day key",
            )
        final_path, file_name = self._write_atomic(
            data, extension=_MIME_EXTENSIONS[content_type]
        )
        try:
            artifact_kind = str(event["kind"])
            document = {
                "schemaVersion": 1,
                "kind": artifact_kind,
                "contentType": content_type,
                "gameId": plan.game_id,
                "accountId": plan.account_id,
                "runId": plan.run_id,
                "runAttemptId": plan.run_attempt_id,
                "todoAttemptId": str(event["todoAttemptId"]),
                "todoInstanceId": str(event["todoInstanceId"]),
                "gameDayKey": game_day_key,
                "capturedAt": str(event["capturedAt"]),
                "hash": digest,
                "sizeBytes": len(data),
                "relativePath": file_name,
                "source": "manager-adapter-v1.1",
                "verdict": "unreviewed",
                # A watermarked derivative must never impersonate the original
                # capture. Its kind is fixed by the promoted Adapter manifest;
                # both files remain run-scoped immutable evidence.
                "raw": not artifact_kind.endswith("-watermarked"),
                "fileName": file_name,
            }
            created = self.store.create_resource(
                "artifact",
                resource_id=artifact_id,
                state="captured",
                document=document,
            )
            if created.get("resource_id") != artifact_id:
                raise RuntimeError("artifact ledger returned a different resource ID")
        except Exception as error:
            self._remove_new_target(final_path)
            if isinstance(error, AdapterArtifactImportError):
                raise
            raise _fail(
                "artifact_ledger_write_failed",
                "artifact ledger insert failed",
            ) from None
        return artifact_id, final_path

    def _validate_quota(
        self,
        plan: AdapterExecutionPlan,
        event: Mapping[str, Any],
        *,
        max_artifacts_per_todo: int,
        max_artifact_bytes: int,
    ) -> None:
        todo_instance_id = str(event["todoInstanceId"])
        usage = self.store.get_adapter_artifact_usage(
            run_attempt_id=plan.run_attempt_id,
            todo_instance_id=todo_instance_id,
        )
        if usage["invalid_size_count"]:
            raise _fail(
                "artifact_quota_state_invalid",
                "existing run artifact has an invalid size",
            )

        if usage["todo_artifact_count"] >= max_artifacts_per_todo:
            raise _fail(
                "artifact_count_exceeded", "Todo artifact count exceeds its limit"
            )
        maximum_run_bytes = run_artifact_byte_limit(
            executable_todo_count=len(plan.executable_todo_instance_ids),
            max_artifacts_per_todo=max_artifacts_per_todo,
            max_artifact_bytes=max_artifact_bytes,
        )
        if usage["run_artifact_bytes"] + int(event["sizeBytes"]) > maximum_run_bytes:
            raise _fail(
                "artifact_run_bytes_exceeded", "Run artifact bytes exceed their limit"
            )

    def _validate_event(
        self,
        plan: AdapterExecutionPlan,
        event_document: Mapping[str, Any],
        *,
        allowed_mime_types: frozenset[str],
        max_artifact_bytes: int,
    ) -> dict[str, Any]:
        if not isinstance(event_document, Mapping):
            raise _fail("invalid_artifact_event", "artifact event must be an object")
        event = dict(event_document)
        if set(event) != _EVENT_FIELDS:
            raise _fail("invalid_artifact_event", "artifact event fields differ")
        if (
            event["schemaVersion"] != SCHEMA_VERSION
            or event["protocolVersion"] != PROTOCOL_VERSION
            or event["eventType"] != "artifact_staged"
        ):
            raise _fail("invalid_artifact_event", "artifact event version or type differs")
        sequence = event["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise _fail("invalid_artifact_event", "artifact event sequence is invalid")
        if (
            event["runId"] != plan.run_id
            or event["runAttemptId"] != plan.run_attempt_id
            or event["fencingToken"] != plan.fencing_token
            or event["gameId"] != plan.game_id
        ):
            raise _fail("event_scope_mismatch", "artifact event differs from its plan")

        _, issued_at = _aware_timestamp(plan.issued_at, field="issuedAt")
        _, expires_at = _aware_timestamp(plan.expires_at, field="expiresAt")
        _, event_at = _aware_timestamp(event["at"], field="at")
        captured_text, captured_at = _aware_timestamp(
            event["capturedAt"], field="capturedAt"
        )
        if not (issued_at <= event_at <= expires_at):
            raise _fail("event_time_out_of_scope", "artifact event is outside its lease")
        if not (issued_at <= captured_at <= expires_at):
            raise _fail(
                "artifact_time_out_of_scope", "artifact capture is outside its lease"
            )
        event["capturedAt"] = captured_text

        todo_instance_id = event["todoInstanceId"]
        if (
            not isinstance(todo_instance_id, str)
            or not _TODO_INSTANCE_ID.fullmatch(todo_instance_id)
        ):
            raise _fail("invalid_artifact_event", "todoInstanceId is invalid")
        targets = {
            target.todo_instance_id: target
            for target in plan.todos
            if target.todo_instance_id in plan.executable_todo_instance_ids
        }
        target = targets.get(todo_instance_id)
        if target is None:
            raise _fail("event_scope_mismatch", "artifact Todo is not in the plan")

        todo_attempt_id = _canonical_uuid(
            event["todoAttemptId"], field="todoAttemptId"
        )
        artifact_id = _canonical_uuid(event["artifactId"], field="artifactId")
        event["todoAttemptId"] = todo_attempt_id
        event["artifactId"] = artifact_id
        kind = event["kind"]
        if not isinstance(kind, str) or not _IDENTIFIER.fullmatch(kind):
            raise _fail("invalid_artifact_event", "artifact kind is invalid")
        event["fileName"] = _validate_leaf_name(event["fileName"])
        if event["mimeType"] not in allowed_mime_types:
            raise _fail("artifact_mime_denied", "artifact MIME type is not allowed")
        size = event["sizeBytes"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or size > max_artifact_bytes
        ):
            raise _fail("artifact_size_rejected", "artifact size is outside its limit")
        digest = event["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise _fail("invalid_artifact_event", "artifact sha256 is invalid")

        try:
            run_attempt = self.store.get_run_attempt(plan.run_attempt_id)
            todo_attempt = self.store.get_todo_attempt(todo_attempt_id)
        except RecordNotFound as error:
            raise _fail("event_scope_mismatch", "artifact attempt is not registered") from error
        expected_digest = hashlib.sha256(
            plan.fencing_token.encode("utf-8")
        ).hexdigest()
        expected_fencing_hashes = {
            expected_digest,  # read compatibility for pre-v8 attempts
            f"sha256:{expected_digest}",
        }
        if (
            run_attempt.get("run_id") != plan.run_id
            or run_attempt.get("game_id") != plan.game_id
            or run_attempt.get("cadence") != plan.cadence
            or run_attempt.get("fencing_token_hash") not in expected_fencing_hashes
            # Cooperative cancellation is still a live fenced protocol phase:
            # the Host may emit its final diagnostic artifact before the
            # run_terminal acknowledgement. Rejecting it at `cancelling`
            # discards evidence and turns a clean cancel into scope mismatch.
            or run_attempt.get("state") not in {"starting", "running", "cancelling"}
        ):
            raise _fail("event_scope_mismatch", "artifact RunAttempt is not active")
        if (
            todo_attempt.get("run_attempt_id") != plan.run_attempt_id
            or todo_attempt.get("todo_instance_id") != todo_instance_id
            or todo_attempt.get("operation") != target.operation
            or todo_attempt.get("state") != "running"
        ):
            raise _fail("todo_attempt_mismatch", "artifact TodoAttempt is not active")
        return event

    def _source_path(self, plan: AdapterExecutionPlan, file_name: str) -> Path:
        self._validate_bound_root(
            self.staging_root,
            self._staging_root_identity,
            code="artifact_staging_root_invalid",
        )
        run_directory = self.staging_root / plan.run_attempt_id
        try:
            run_snapshot = os.lstat(run_directory)
        except OSError:
            raise _fail(
                "artifact_file_unavailable",
                "artifact staging directory is unavailable",
            ) from None
        if (
            not stat.S_ISDIR(run_snapshot.st_mode)
            or _is_reparse_point(run_directory, run_snapshot)
        ):
            raise _fail("artifact_path_reparse", "artifact staging directory is unsafe")
        source = run_directory / file_name
        try:
            source_snapshot = os.lstat(source)
        except OSError:
            raise _fail("artifact_file_unavailable", "artifact file is unavailable") from None
        if _is_reparse_point(source, source_snapshot):
            raise _fail("artifact_path_reparse", "artifact file is a reparse point")
        try:
            resolved_root = self.staging_root.resolve(strict=True)
            resolved_run = run_directory.resolve(strict=True)
            resolved_source = source.resolve(strict=True)
            resolved_run.relative_to(resolved_root)
            resolved_source.relative_to(resolved_run)
        except (OSError, ValueError):
            raise _fail("unsafe_artifact_path", "artifact path escaped staging") from None
        if resolved_source.parent != resolved_run:
            raise _fail("unsafe_artifact_path", "artifact must be directly staged")
        return source

    def _read_verified_source(self, source: Path, *, expected_size: int) -> bytes:
        try:
            parent_before = os.lstat(source.parent)
            path_before = os.lstat(source)
        except OSError:
            raise _fail("artifact_file_unavailable", "artifact file is unavailable") from None
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or _is_reparse_point(source.parent, parent_before)
        ):
            raise _fail("artifact_path_reparse", "artifact parent is unsafe")
        if _is_reparse_point(source, path_before):
            raise _fail("artifact_path_reparse", "artifact file is a reparse point")
        if not stat.S_ISREG(path_before.st_mode):
            raise _fail("artifact_file_unavailable", "artifact is not a regular file")
        if path_before.st_size != expected_size:
            raise _fail("artifact_size_mismatch", "artifact size differs before read")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_snapshot(path_before, opened)
                or opened.st_size != expected_size
            ):
                raise _fail(
                    "artifact_changed_before_read",
                    "artifact changed while its handle opened",
                )
            data = read_bounded_file_descriptor(descriptor, expected_size)
            after_handle = os.fstat(descriptor)
        except AdapterArtifactImportError:
            raise
        except OSError:
            raise _fail("artifact_file_unavailable", "artifact file could not be read") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if len(data) != expected_size:
            raise _fail("artifact_size_mismatch", "artifact size differs during read")
        if not _same_snapshot(opened, after_handle):
            raise _fail("artifact_changed_during_read", "artifact changed during read")
        try:
            parent_after = os.lstat(source.parent)
            path_after = os.lstat(source)
        except OSError:
            raise _fail(
                "artifact_changed_during_read", "artifact path changed after read"
            ) from None
        if (
            _is_reparse_point(source.parent, parent_after)
            or not _same_directory_identity(parent_before, parent_after)
            or _is_reparse_point(source, path_after)
            or not _same_snapshot(opened, path_after)
        ):
            raise _fail("artifact_changed_during_read", "artifact path changed after read")
        self._validate_bound_root(
            self.staging_root,
            self._staging_root_identity,
            code="artifact_staging_root_invalid",
        )
        return data

    def _write_atomic(self, data: bytes, *, extension: str) -> tuple[Path, str]:
        self._validate_bound_root(
            self.artifact_root,
            self._artifact_root_identity,
            code="artifact_root_invalid",
        )
        opaque = uuid.uuid4().hex
        file_name = f"artifact-{opaque}{extension}"
        final_path = self.artifact_root / file_name
        temp_path = self.artifact_root / f".{opaque}-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOINHERIT", 0)
            descriptor = os.open(temp_path, flags, 0o600)
            view = memoryview(data)
            written = 0
            while written < len(data):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("short artifact write")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temp_path, final_path)
            self._fsync_directory(self.artifact_root)
            self._validate_bound_root(
                self.artifact_root,
                self._artifact_root_identity,
                code="artifact_root_invalid",
            )
            final_snapshot = os.lstat(final_path)
            if (
                not stat.S_ISREG(final_snapshot.st_mode)
                or _is_reparse_point(final_path, final_snapshot)
                or final_snapshot.st_size != len(data)
            ):
                raise _fail("artifact_target_invalid", "Manager artifact target is unsafe")
        except Exception as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for candidate in (temp_path, final_path):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(error, AdapterArtifactImportError):
                raise
            raise _fail(
                "artifact_target_unavailable",
                "Manager artifact target could not be written",
            ) from None
        return final_path, file_name

    def _remove_new_target(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            self._fsync_directory(self.artifact_root)
        except OSError:
            pass

    @staticmethod
    def _validate_bound_root(
        path: Path,
        expected_identity: os.stat_result,
        *,
        code: str,
    ) -> None:
        _validate_local_directory(path, create=False, code=code)
        try:
            current = os.lstat(path)
        except OSError:
            raise _fail(code, "artifact storage root is unavailable") from None
        if not _same_identity(expected_identity, current):
            raise _fail(code, "artifact storage root identity changed")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            # Windows does not provide a portable directory fsync. The file
            # itself has already been flushed before the atomic replacement.
            pass
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
