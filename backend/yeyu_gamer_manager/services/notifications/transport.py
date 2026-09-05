"""Notification transport boundary and an in-memory fake for tests."""

from __future__ import annotations

import base64
import hashlib
import re
import smtplib
import socket
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from html import escape
from html.parser import HTMLParser
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
    report_html: str = ""


class _BodyImageReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cids: set[str] = set()
        self.image_tags: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            source = dict(attrs).get("src") or ""
            if source.startswith("cid:"):
                self.cids.add(source[4:])
                self.image_tags[self.get_starttag_text()] = source[4:]


class _StandaloneReport(HTMLParser):
    """Package frozen markup and resolved image bytes without fetching content."""

    _CSP = (
        '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; '
        'base-uri \'none\'; form-action \'none\'">'
    )
    _ACTIVE_TAGS = frozenset({
        "script", "iframe", "object", "embed", "link", "base", "form", "input",
        "textarea", "button", "svg", "math", "audio", "video", "source", "track",
    })
    _ACTIVE_ATTRIBUTES = frozenset({"srcdoc", "srcset", "background", "action", "formaction", "http-equiv"})
    _CSS_EXTERNAL = re.compile(r"url\s*\(|@import|expression\s*\(|\\", re.IGNORECASE)

    def __init__(self, attachments: tuple[NotificationAttachment, ...]) -> None:
        super().__init__(convert_charrefs=False)
        self.images: dict[str, NotificationAttachment] = {}
        for attachment in attachments:
            cid = f"evidence-{attachment.artifact_id}"
            if cid in self.images and self.images[cid] != attachment:
                raise PermanentNotificationTransportError("HTML report image identity is ambiguous")
            self.images[cid] = attachment
        self.parts: list[str] = []
        self.has_head = False
        self.in_style = False

    @classmethod
    def _check_css(cls, value: str) -> None:
        if cls._CSS_EXTERNAL.search(value):
            raise PermanentNotificationTransportError("HTML report contains active or external styles")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ACTIVE_TAGS:
            raise PermanentNotificationTransportError("HTML report contains active content")
        rendered: list[tuple[str, str | None]] = []
        for key, value in attrs:
            if key.startswith("on") or key in self._ACTIVE_ATTRIBUTES:
                raise PermanentNotificationTransportError("HTML report contains active attributes")
            if key == "href" and value and not value.startswith("#"):
                raise PermanentNotificationTransportError("HTML report contains an external link")
            if key == "style" and value:
                self._check_css(value)
            if key == "src":
                if tag != "img" or not value or not value.startswith("cid:"):
                    raise PermanentNotificationTransportError("HTML report image is not a frozen CID")
                attachment = self.images.get(value[4:])
                if attachment is None:
                    self.parts.append('<span class="evidence-unavailable">此截图未通过本次附件校验，未嵌入。</span>')
                    return
                if attachment.content_type not in {"image/png", "image/jpeg"}:
                    raise PermanentNotificationTransportError("HTML report image type is unsupported")
                value = f"data:{attachment.content_type};base64," + base64.b64encode(attachment.content).decode("ascii")
            rendered.append((key, value))
        self.parts.append("<" + tag + "".join(
            " " + key + (f'="{escape(value, quote=True)}"' if value is not None else "")
            for key, value in rendered
        ) + ">")
        if tag == "head":
            self.has_head = True
            self.parts.append(self._CSP)
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag == "style":
            self.in_style = False

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self._check_css(data)
        self.parts.append(data if self.in_style else escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_decl(self, decl: str) -> None:
        if decl.casefold() == "doctype html":
            self.parts.append("<!doctype html>")

    def render(self, html: str) -> str:
        self.feed(html)
        self.close()
        content = "".join(self.parts)
        if not self.has_head:
            content = "<!doctype html><html><head>" + self._CSP + "</head><body>" + content + "</body></html>"
        return content


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
        body_images = _BodyImageReferences()
        html_body = outbound.html_body
        if outbound.report_html:
            body_images.feed(outbound.html_body)
            body_images.close()
            available = {f"evidence-{item.artifact_id}" for item in outbound.attachments}
            for tag, cid in body_images.image_tags.items():
                if cid not in available:
                    html_body = html_body.replace(tag, '<span style="color:#97551c;font-size:13px">此截图未通过本次附件校验，未嵌入。</span>')
        message.add_alternative(html_body, subtype="html")
        html_part = message.get_payload()[-1]
        for attachment in outbound.attachments:
            if outbound.report_html and f"evidence-{attachment.artifact_id}" not in body_images.cids:
                continue
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
        if outbound.report_html:
            report = _StandaloneReport(outbound.attachments).render(outbound.report_html)
            message.add_attachment(report, subtype="html", charset="utf-8", filename="daily-report.html")
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
