"""Batch-seal notification rendering and delivery primitives."""

from .dispatcher import NotificationDispatcher
from .secrets import (
    DpapiNotificationSecretProvider,
    FakeNotificationSecretProvider,
    NotificationSecretError,
    NotificationSecretMissing,
    SmtpProfile,
)
from .template import RenderedBatchNotification, render_batch_notification
from .transport import (
    FakeNotificationTransport,
    NotificationAttachment,
    NotificationTransportReceipt,
    OutboundNotification,
    SmtpNotificationTransport,
)

__all__ = [
    "DpapiNotificationSecretProvider",
    "FakeNotificationSecretProvider",
    "FakeNotificationTransport",
    "NotificationAttachment",
    "NotificationDispatcher",
    "NotificationSecretError",
    "NotificationSecretMissing",
    "NotificationTransportReceipt",
    "OutboundNotification",
    "RenderedBatchNotification",
    "SmtpNotificationTransport",
    "SmtpProfile",
    "render_batch_notification",
]
