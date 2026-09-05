"""Recoverable notification outbox worker.

The dispatcher owns transport only.  Its store calls are restricted to the
notification tables/events; a delivery failure can never mutate a Batch,
GameRun, Todo instance, or invoke an Adapter.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ...store.sqlite_store import SqliteStore
from .artifacts import SealArtifactResolver
from .secrets import (
    NotificationSecretError,
    NotificationSecretMissing,
    NotificationSecretProvider,
)
from .template import render_batch_notification
from .transport import (
    AmbiguousNotificationTransportError,
    NotificationTransport,
    NotificationTransportError,
    OutboundNotification,
    PermanentNotificationTransportError,
    TransientNotificationTransportError,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationDispatcher:
    def __init__(
        self,
        *,
        store: SqliteStore,
        artifact_root,
        secret_provider: NotificationSecretProvider,
        transport: NotificationTransport,
        clock: Callable[[], datetime] = _utc_now,
        poll_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.secret_provider = secret_provider
        self.transport = transport
        self.clock = clock
        self.poll_seconds = max(0.05, poll_seconds)
        self.artifacts = SealArtifactResolver(store, artifact_root)
        self.worker_id = f"manager-{uuid.uuid4()}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def build_draft(
        self,
        *,
        game_day: str,
        batch_id: str,
        seal_version: int,
        sealed_result: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        rendered = render_batch_notification(
            game_day=game_day,
            batch_id=batch_id,
            seal_version=seal_version,
            sealed_result=sealed_result,
        )
        binding_id = str(policy["recipient_binding_id"])
        secret_state = self.secret_provider.state(binding_id)
        # Both successful and blocked round reports carry the immutable seal's
        # whitelisted screenshot references. The resolver still enforces
        # kind/hash/size/run/time bounds before any bytes reach SMTP.
        attachment_refs = list(sealed_result.get("sealEvidenceArtifactIds", []))
        return {
            "outcome": rendered.outcome,
            "subject": rendered.subject,
            "text_body": rendered.text_body,
            "html_body": rendered.html_body,
            "report_html": rendered.report_html,
            "attachment_refs": attachment_refs,
            "secret_state": secret_state,
            "dispatch_disposition": self._dispatch_disposition(batch_id),
        }

    # A batch that only gets sealed this long after it was created (recovered
    # review phase, Manager restart) is history rather than a round report.
    STALE_BATCH_SECONDS = 20 * 60 * 60

    def _dispatch_disposition(self, batch_id: str) -> str:
        try:
            created_raw = str(self.store.get_batch(batch_id).get("created_at", ""))
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            return ""
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = self.clock().astimezone(timezone.utc)
        if (now - created).total_seconds() >= self.STALE_BATCH_SECONDS:
            return "stale_batch"
        return ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="YeYuGamer.NotificationDispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # No raw exception is logged or persisted. The next loop can
                # recover a lease; process liveness is more valuable here than
                # allowing an unexpected transport error to kill the worker.
                pass
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def run_once(self, limit: int = 10) -> int:
        now = self.clock().astimezone(timezone.utc)
        self.store.recover_expired_notification_leases(now.isoformat())
        processed = 0
        for candidate in self.store.list_dispatchable_notifications(
            now.isoformat(), limit
        ):
            notification_id = str(candidate["notification_id"])
            binding_id = str(candidate["recipient_binding_id"])
            _, preflight_decisions = self.artifacts.resolve(candidate)
            missing_games = self.artifacts.missing_contract_games(
                candidate, preflight_decisions
            )
            if missing_games:
                self.store.set_notification_dispatch_gate(
                    notification_id,
                    gate="manual_review",
                    error_class="attachment_contract_missing",
                )
                continue
            try:
                profile = self.secret_provider.load(binding_id)
            except NotificationSecretMissing:
                self.store.set_notification_dispatch_gate(
                    notification_id,
                    gate="secret_missing",
                    error_class="secret_missing",
                )
                continue
            except NotificationSecretError:
                self.store.set_notification_dispatch_gate(
                    notification_id,
                    gate="manual_review",
                    error_class="secret_invalid",
                )
                continue
            # A transport call earlier in this batch may have consumed most of
            # the lease window. Anchor each claim to a fresh clock reading.
            claim_now = self.clock().astimezone(timezone.utc)
            claimed = self.store.claim_notification_delivery(
                notification_id,
                owner=self.worker_id,
                now=claim_now.isoformat(),
                lease_seconds=90,
            )
            if claimed is None:
                continue
            processed += 1
            attachments, attachment_decisions = self.artifacts.resolve(claimed)
            missing_games = self.artifacts.missing_contract_games(
                claimed, attachment_decisions
            )
            if missing_games:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="permanent_failure",
                    error_class="attachment_contract_changed",
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
                continue
            outbound = OutboundNotification(
                notification_id=notification_id,
                message_id=str(claimed["message_id"]),
                subject=str(claimed["subject"]),
                text_body=str(claimed["text_body"]),
                html_body=str(claimed["html_body"]),
                report_html=str(claimed["report_html"]),
                attachments=attachments,
            )
            try:
                receipt = self.transport.send(profile, outbound)
            except TransientNotificationTransportError as error:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="transient_failure",
                    error_class=error.error_class,
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
            except AmbiguousNotificationTransportError as error:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="ambiguous",
                    error_class=error.error_class,
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
            except PermanentNotificationTransportError as error:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="permanent_failure",
                    error_class=error.error_class,
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
            except NotificationTransportError as error:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="permanent_failure",
                    error_class=error.error_class,
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
            except Exception:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="ambiguous",
                    error_class="worker_internal",
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
            else:
                self.store.finish_notification_attempt(
                    notification_id,
                    lease_token=str(claimed["lease_token"]),
                    outcome="sent",
                    error_class="",
                    transport_receipt_hash=receipt.receipt_hash,
                    now=self.clock().astimezone(timezone.utc).isoformat(),
                )
        return processed
