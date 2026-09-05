from __future__ import annotations

import base64
from dataclasses import replace
import unittest

from yeyu_gamer_manager.services.notifications.secrets import SmtpProfile
from yeyu_gamer_manager.services.notifications.transport import (
    NotificationAttachment,
    OutboundNotification,
    PermanentNotificationTransportError,
    SmtpNotificationTransport,
)


class NotificationReportTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = SmtpProfile(
            binding_id="test", host="smtp.invalid", port=465, username="", password="",
            sender_address="sender@example.invalid", recipient_address="recipient@example.invalid", security="tls",
        )
        self.body_image = NotificationAttachment("body", "body.png", "image/png", b"resolved body image")
        self.report_image = NotificationAttachment("report", "report.jpg", "image/jpeg", b"resolved report image")
        self.unused_image = NotificationAttachment("unused", "unused.png", "image/png", b"unused image")

    def outbound(self, report: str = "") -> OutboundNotification:
        return OutboundNotification(
            notification_id="notification", message_id="<notification@example.invalid>", subject="每日报告",
            text_body="正文摘要", html_body=(
                '<p>正文摘要 cid:evidence-unused</p><!-- <img src="cid:evidence-report"> -->'
                '<img src="cid:evidence-body">'
            ),
            attachments=(self.body_image, self.report_image, self.unused_image), report_html=report,
        )

    def test_report_is_utf8_attachment_with_resolved_data_images_and_only_body_cids_inline(self) -> None:
        report = '<!doctype html><html><head><style>p{color:#333}</style></head><body><details><summary>详情</summary><img src="cid:evidence-report"></details></body></html>'
        message = SmtpNotificationTransport._message(self.profile, self.outbound(report))
        inline = [p for p in message.walk() if p.get_content_disposition() == "inline"]
        self.assertEqual([p["Content-ID"] for p in inline], ["<evidence-body>"])
        self.assertEqual(inline[0].get_payload(decode=True), self.body_image.content)
        attachments = list(message.iter_attachments())
        self.assertEqual([p.get_filename() for p in attachments], ["daily-report.html"])
        detail = attachments[0]
        self.assertEqual(detail.get_content_type(), "text/html")
        self.assertEqual(detail.get_content_charset(), "utf-8")
        html = detail.get_content()
        self.assertIn("<details><summary>详情</summary>", html)
        self.assertNotIn("<details open", html)
        self.assertIn("data:image/jpeg;base64," + base64.b64encode(self.report_image.content).decode("ascii"), html)
        self.assertNotIn("cid:evidence-report", html)
        self.assertNotIn(base64.b64encode(self.unused_image.content).decode("ascii"), html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("正文摘要", message.get_body(preferencelist=("plain",)).get_content())
        self.assertNotIn("data:image", message.get_body(preferencelist=("html",)).get_content())

    def test_legacy_message_with_empty_report_keeps_all_existing_inline_attachments(self) -> None:
        message = SmtpNotificationTransport._message(self.profile, self.outbound())
        self.assertEqual([p.get_filename() for p in message.walk() if p.get_content_disposition() == "inline"], ["body.png", "report.jpg", "unused.png"])
        self.assertNotIn("daily-report.html", [p.get_filename() for p in message.walk()])

    def test_missing_report_image_is_explicit_without_fetching_or_embedding_other_bytes(self) -> None:
        message = SmtpNotificationTransport._message(self.profile, self.outbound('<p>证据</p><img src="cid:evidence-missing">'))
        html = list(message.iter_attachments())[0].get_content()
        self.assertIn("此截图未通过本次附件校验，未嵌入", html)
        self.assertNotIn("cid:evidence-missing", html)
        self.assertNotIn("data:image", html)
        self.assertIn('<meta charset="utf-8">', html)

    def test_missing_primary_image_is_explained_in_compact_body(self) -> None:
        outbound = replace(self.outbound('<p>详情</p>'), attachments=(self.report_image,))
        message = SmtpNotificationTransport._message(self.profile, outbound)
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertNotIn('<img src="cid:evidence-body">', html)
        self.assertIn("此截图未通过本次附件校验", html)

    def test_report_rejects_active_content_and_external_resources(self) -> None:
        for html in (
            '<script>alert(1)</script>', '<img src="https://example.invalid/pixel">',
            '<img src="cid:evidence-body" onerror="alert(1)">',
            '<link rel="stylesheet" href="https://example.invalid/a.css">',
            '<style>p{background:url(https://example.invalid/pixel)}</style>',
            '<meta http-equiv="refresh" content="0;url=https://example.invalid">',
            '<img src="cid:evidence-body" srcset="https://example.invalid/pixel 2x">',
            '<a href="javascript:alert(1)">link</a>',
        ):
            with self.subTest(html=html), self.assertRaises(PermanentNotificationTransportError):
                SmtpNotificationTransport._message(self.profile, self.outbound(html))

    def test_report_cannot_embed_unapproved_image_type(self) -> None:
        outbound = OutboundNotification(
            "n", "<n@example.invalid>", "subject", "text", "<p>body</p>",
            (NotificationAttachment("svg", "image.svg", "image/svg+xml", b"<svg></svg>"),),
            '<img src="cid:evidence-svg">',
        )
        with self.assertRaises(PermanentNotificationTransportError):
            SmtpNotificationTransport._message(self.profile, outbound)


if __name__ == "__main__":
    unittest.main()
