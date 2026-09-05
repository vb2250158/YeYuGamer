"""Resolve only seal-whitelisted screenshot artifacts for mail attachment."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...store.sqlite_store import RecordNotFound, SqliteStore
from .renditions import render_mail_image
from .transport import NotificationAttachment


_ALLOWED_KINDS = frozenset(
    {
        "screenshot",
        "window-screenshot",
        "raw-frame",
        "reward-screenshot",
        "game-ui-claimed-reward",
        "game-ui-reward-screen",
        "game-ui-main-window",
        "game-ui-daily-task-list",
        "game-ui-daily-training-panel",
        # Manager per-step captures and the WW/StarRail reward frames.  The
        # seal only whitelists frames the adjudication already lists for the
        # final run; the kind gate here keeps text logs and bundles out.
        "game-ui-step-before-raw",
        "game-ui-step-after-watermarked",
        "game-ui-daily-reward-before",
        "game-ui-daily-reward-raw",
        "game-ui-daily-reward-watermarked",
    }
)
_ALLOWED_MIME = {"image/png": ".png", "image/jpeg": ".jpg"}
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 12 * 1024 * 1024
# Frames larger than this are transcoded to a bounded JPEG rendition for the
# wire.  The on-disk artifact and its seal hash are never modified.
_RENDITION_THRESHOLD_BYTES = 350 * 1024


@dataclass(frozen=True, slots=True)
class AttachmentDecision:
    artifact_id: str
    accepted: bool
    reason: str


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _has_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return True
    return bool(attributes & 0x400)


def _magic_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return len(data) >= 4 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    return False


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare an opened file with the path snapshot without trusting names."""

    before_identity = (before.st_dev, before.st_ino)
    after_identity = (after.st_dev, after.st_ino)
    identity_matches = (
        before_identity == after_identity
        if any(before_identity) or any(after_identity)
        else True
    )
    return (
        identity_matches
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )


class SealArtifactResolver:
    def __init__(self, store: SqliteStore, artifact_root: Path) -> None:
        self.store = store
        self.configured_artifact_root = Path(artifact_root)
        if str(self.configured_artifact_root).startswith("\\\\"):
            raise ValueError("notification artifact root must be local")
        self.artifact_root = self.configured_artifact_root.resolve()

    def preflight_reference(
        self,
        artifact_id: str,
        *,
        allowed_games: set[str],
        allowed_runs: set[str],
        started_at: datetime,
        sealed_at: datetime,
    ) -> AttachmentDecision:
        """Validate one future seal allowlist entry without mutating state.

        The immutable seal records this coarse decision. A later dispatch still
        repeats every check because a local file may disappear after sealing.
        """

        if (
            not self.configured_artifact_root.is_dir()
            or _has_reparse_point(self.configured_artifact_root)
        ):
            return AttachmentDecision(artifact_id, False, "artifact_root_invalid")
        result = self._validate_reference(
            artifact_id,
            whitelist={artifact_id},
            allowed_games=allowed_games,
            allowed_runs=allowed_runs,
            started_at=started_at,
            sealed_at=sealed_at,
        )
        return AttachmentDecision(
            artifact_id,
            not isinstance(result, str),
            result if isinstance(result, str) else "accepted",
        )

    def resolve(
        self, delivery: dict[str, Any]
    ) -> tuple[tuple[NotificationAttachment, ...], tuple[AttachmentDecision, ...]]:
        if (
            not self.configured_artifact_root.is_dir()
            or _has_reparse_point(self.configured_artifact_root)
        ):
            return (), tuple(
                AttachmentDecision(str(item), False, "artifact_root_invalid")
                for item in delivery.get("attachment_refs", [])
            )
        if delivery.get("outcome") not in {"blocked", "completed"}:
            return (), tuple(
                AttachmentDecision(str(item), False, "notification_outcome_rejected")
                for item in delivery.get("attachment_refs", [])
            )
        batch = self.store.get_batch(str(delivery["batch_id"]))
        sealed_result = batch.get("result", {})
        whitelist = {
            str(item)
            for item in sealed_result.get("sealEvidenceArtifactIds", [])
            if isinstance(item, str)
        }
        allowed_games = set(str(item) for item in batch.get("game_ids", []))
        allowed_runs = {
            str(item)
            for item in sealed_result.get("finalGameRunIds", [])
            if isinstance(item, str)
        }
        started_at = _timestamp(batch.get("created_at"))
        sealed_at = _timestamp(sealed_result.get("sealedAt"))
        attachments: list[NotificationAttachment] = []
        decisions: list[AttachmentDecision] = []
        total_bytes = 0
        for raw_artifact_id in delivery.get("attachment_refs", []):
            artifact_id = str(raw_artifact_id)
            reason = self._validate_reference(
                artifact_id,
                whitelist=whitelist,
                allowed_games=allowed_games,
                allowed_runs=allowed_runs,
                started_at=started_at,
                sealed_at=sealed_at,
            )
            if isinstance(reason, str):
                decisions.append(AttachmentDecision(artifact_id, False, reason))
                continue
            document, candidate, data = reason
            content_type = str(document["contentType"])
            wire_data, wire_type = self._wire_rendition(candidate, data, content_type)
            if total_bytes + len(wire_data) > _MAX_TOTAL_BYTES:
                decisions.append(
                    AttachmentDecision(artifact_id, False, "attachment_total_limit")
                )
                continue
            total_bytes += len(wire_data)
            attachments.append(
                NotificationAttachment(
                    artifact_id=artifact_id,
                    file_name=f"evidence-{artifact_id}{_ALLOWED_MIME[wire_type]}",
                    content_type=wire_type,
                    content=wire_data,
                )
            )
            decisions.append(AttachmentDecision(artifact_id, True, "accepted"))
        return tuple(attachments), tuple(decisions)

    @staticmethod
    def _wire_rendition(
        candidate: Path, data: bytes, content_type: str
    ) -> tuple[bytes, str]:
        """Return the verified bytes, or a bounded JPEG rendition for large frames.

        The hash/size verification above already accepted ``data``.  Only the
        transmitted copy is reduced; a failed transcode keeps the original.
        """

        if len(data) <= _RENDITION_THRESHOLD_BYTES:
            return data, content_type
        try:
            rendition = render_mail_image(candidate)
        except Exception:
            rendition = None
        if rendition is None or len(rendition.content) >= len(data):
            return data, content_type
        return rendition.content, rendition.content_type

    def missing_contract_games(
        self,
        delivery: dict[str, Any],
        decisions: tuple[AttachmentDecision, ...],
    ) -> tuple[str, ...]:
        """Return CompletionContract games without an accepted screenshot.

        Older sealed batches that predate CompletionContract integration do not
        acquire a new dispatch requirement retroactively.  A completed notice
        requires one resolver-accepted screenshot per named game.  A blocked
        notice may substitute only the Manager-frozen typed explanation in
        ``notificationBlockers[].screenshotUnavailableReason``.  This is checked
        again after the delivery lease is claimed so a removed or changed file
        cannot silently turn into a text-only report.
        """

        batch = self.store.get_batch(str(delivery["batch_id"]))
        sealed_result = batch.get("result", {})
        contracts = sealed_result.get("completionContracts")
        if not isinstance(contracts, (dict, list)) or not contracts:
            return ()
        if isinstance(contracts, dict):
            required_games = {
                str(game_id)
                for game_id, contract in contracts.items()
                if isinstance(game_id, str)
                and game_id
                and isinstance(contract, dict)
            }
        else:
            required_games = {
                str(contract.get("gameId"))
                for contract in contracts
                if isinstance(contract, dict)
                and isinstance(contract.get("gameId"), str)
                and contract.get("gameId")
            }
        accepted_ids = {
            decision.artifact_id for decision in decisions if decision.accepted
        }
        covered_games: set[str] = set()
        for artifact_id in accepted_ids:
            try:
                resource = self.store.get_resource("artifact", artifact_id)
            except RecordNotFound:
                continue
            document = resource.get("document", {})
            game_id = document.get("gameId")
            if isinstance(game_id, str):
                covered_games.add(game_id)
        missing_games = required_games - covered_games
        if delivery.get("outcome") != "blocked":
            return tuple(sorted(missing_games))

        # A blocked run can fail before capture becomes possible.  It may still
        # notify the operator, but only when the immutable seal explicitly says
        # why each game's screenshot is unavailable.  The template surfaces
        # the same typed blocker; the dispatcher never invents an excuse.
        typed_unavailable_games = {
            str(item.get("gameId"))
            for item in sealed_result.get("notificationBlockers", [])
            if isinstance(item, dict)
            and isinstance(item.get("gameId"), str)
            and isinstance(item.get("screenshotUnavailableReason"), str)
            and item.get("screenshotUnavailableReason", "").strip()
        }
        return tuple(sorted(missing_games - typed_unavailable_games))

    def _validate_reference(
        self,
        artifact_id: str,
        *,
        whitelist: set[str],
        allowed_games: set[str],
        allowed_runs: set[str],
        started_at: datetime | None,
        sealed_at: datetime | None,
    ) -> str | tuple[dict[str, Any], Path, bytes]:
        if artifact_id not in whitelist:
            return "not_in_seal_whitelist"
        try:
            resource = self.store.get_resource("artifact", artifact_id)
        except RecordNotFound:
            return "artifact_not_found"
        document = resource.get("document", {})
        if document.get("kind") not in _ALLOWED_KINDS:
            return "artifact_kind_rejected"
        content_type = document.get("contentType")
        if content_type not in _ALLOWED_MIME:
            return "artifact_mime_rejected"
        if document.get("gameId") not in allowed_games:
            return "artifact_game_mismatch"
        if document.get("runId") not in allowed_runs:
            return "artifact_run_mismatch"
        captured_at = _timestamp(document.get("capturedAt"))
        if (
            captured_at is None
            or started_at is None
            or sealed_at is None
            or captured_at < started_at
            or captured_at > sealed_at
        ):
            return "artifact_time_mismatch"
        relative = document.get("relativePath")
        if not isinstance(relative, str) or not relative:
            return "artifact_path_invalid"
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return "artifact_path_invalid"
        candidate = (self.artifact_root / relative_path).resolve()
        try:
            candidate.relative_to(self.artifact_root)
        except ValueError:
            return "artifact_path_escape"
        if not candidate.is_file() or _has_reparse_point(candidate):
            return "artifact_file_unavailable"
        for parent in candidate.parents:
            if parent == self.artifact_root:
                break
            if _has_reparse_point(parent):
                return "artifact_path_reparse"
        try:
            expected_size = int(document.get("sizeBytes", -1))
        except (TypeError, ValueError):
            return "artifact_size_invalid"
        if expected_size < 1 or expected_size > _MAX_ATTACHMENT_BYTES:
            return "artifact_size_rejected"
        try:
            path_snapshot = candidate.stat()
        except OSError:
            return "artifact_file_unavailable"
        if not stat.S_ISREG(path_snapshot.st_mode):
            return "artifact_file_unavailable"
        if path_snapshot.st_size != expected_size:
            return "artifact_size_mismatch"
        try:
            # Validate the opened handle against the path snapshot, then bound
            # the read to the sealed size plus one byte. This keeps a path swap
            # or concurrent growth from becoming an unbounded allocation.
            with candidate.open("rb") as stream:
                opened_snapshot = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened_snapshot.st_mode)
                    or not _same_file_snapshot(path_snapshot, opened_snapshot)
                    or opened_snapshot.st_size != expected_size
                ):
                    return "artifact_changed_before_read"
                data = stream.read(expected_size + 1)
                final_snapshot = os.fstat(stream.fileno())
        except OSError:
            return "artifact_file_unavailable"
        if not _same_file_snapshot(opened_snapshot, final_snapshot):
            return "artifact_changed_during_read"
        if len(data) != expected_size:
            return "artifact_size_mismatch"
        digest = document.get("hash")
        if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest:
            return "artifact_hash_mismatch"
        if not _magic_matches(str(content_type), data):
            return "artifact_magic_mismatch"
        return document, candidate, data
