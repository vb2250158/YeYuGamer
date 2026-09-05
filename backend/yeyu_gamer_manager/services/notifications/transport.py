"""Notification transport boundary and an in-memory fake for tests."""

from __future__ import annotations

import hashlib
import smtplib
import socket
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

from .secrets import SmtpProfile


class NotificationTransportError(RuntimeError):
    classification = "permanent"
    error_class = "transport_failure"


class TransientNotificationTransportError(NotificationTransportError):
    classification = "transient"
    error_class = "transport_transient"


class PermanentNotificationTransportError(NotificationTransportError):
    classification = "permanent"
    error_class = "transport_permanent"


class AmbiguousNotificationTransportError(NotificationTransportError):
    classification = "ambiguous"
    error_class = "transport_ambiguous"


@dataclass(frozen=True, slots=True)
class NotificationAttachment:
    artifact_id: str
    file_name: str
    content_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class OutboundNotification:
    notification_id: str
    message_id: str
    subject: str
    text_body: str
    html_body: str
    attachments: tuple[NotificationAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class NotificationTransportReceipt:
    receipt_hash: str


class NotificationTransport(Protocol):
    def send(
        self, profile: SmtpProfile, message: OutboundNotification
    ) -> NotificationTransportReceipt: ...


class SmtpNotificationTransport:
    """SMTP implementation that only raises redacted, classified errors."""

    @staticmethod
    def _message(profile: SmtpProfile, outbound: OutboundNotification) -> EmailMessage:
        message = EmailMessage()
        message["Message-ID"] = outbound.message_id
        message["Subject"] = outbound.subject
        message["From"] = profile.sender_address
        message["To"] = profile.recipient_address
        message.set_content(outbound.text_body)
        message.add_alternative(outbound.html_body, subtype="html")
        html_part = message.get_payload()[-1]
        for attachment in outbound.attachments:
            major, minor = attachment.content_type.split("/", 1)
            # Frames are related to the HTML body (``cid:evidence-<id>``) so
            # mail clients render them inline next to their game section, and
            # they remain downloadable as named attachments.
            html_part.add_related(
                attachment.content,
                maintype=major,
                subtype=minor,
                cid=f"<evidence-{attachment.artifact_id}>",
                filename=attachment.file_name,
                disposition="inline",
            )
        return message

    def send(
        self, profile: SmtpProfile, message: OutboundNotification
    ) -> NotificationTransportReceipt:
        email = self._message(profile, message)
        client: smtplib.SMTP | smtplib.SMTP_SSL | None = None
        try:
            if profile.security == "tls":
                client = smtplib.SMTP_SSL(
                    profile.host,
                    profile.port,
                    timeout=profile.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(
                    profile.host, profile.port, timeout=profile.timeout_seconds
                )
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if profile.username:
                client.login(profile.username, profile.password)
            client.send_message(email)
        except smtplib.SMTPAuthenticationError as error:
            raise PermanentNotificationTransportError(
                "SMTP authentication was rejected"
            ) from error
        except smtplib.SMTPRecipientsRefused as error:
            raise PermanentNotificationTransportError(
                "SMTP recipient binding was rejected"
            ) from error
        except smtplib.SMTPDataError as error:
            classification = error.smtp_code // 100
            if classification == 4:
                raise TransientNotificationTransportError(
                    "SMTP temporarily rejected the message"
                ) from error
            raise PermanentNotificationTransportError(
                "SMTP permanently rejected the message"
            ) from error
        except smtplib.SMTPServerDisconnected as error:
            raise AmbiguousNotificationTransportError(
                "SMTP connection ended with an ambiguous delivery result"
            ) from error
        except (TimeoutError, socket.timeout, ConnectionError, OSError) as error:
            raise TransientNotificationTransportError(
                "SMTP transport is temporarily unavailable"
            ) from error
        except smtplib.SMTPException as error:
            raise PermanentNotificationTransportError(
                "SMTP transport rejected the request"
            ) from error
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass
        digest = hashlib.sha256(
            f"{message.notification_id}:{message.message_id}".encode("utf-8")
        ).hexdigest()
        return NotificationTransportReceipt(receipt_hash=digest)


class FakeNotificationTransport:
    """Deterministic transport used by unit and integration tests."""

    def __init__(self, outcomes: list[str] | None = None) -> None:
        self._outcomes = list(outcomes or ["sent"])
        self.sent: list[OutboundNotification] = []
        self._lock = threading.Lock()

    def send(
        self, profile: SmtpProfile, message: OutboundNotification
    ) -> NotificationTransportReceipt:
        del profile
        with self._lock:
            outcome = self._outcomes.pop(0) if self._outcomes else "sent"
            if outcome == "transient":
                raise TransientNotificationTransportError("fake transient failure")
            if outcome == "permanent":
                raise PermanentNotificationTransportError("fake permanent failure")
            if outcome == "ambiguous":
                raise AmbiguousNotificationTransportError("fake ambiguous failure")
            self.sent.append(message)
        return NotificationTransportReceipt(
            receipt_hash=hashlib.sha256(message.message_id.encode("ascii")).hexdigest()
        )
