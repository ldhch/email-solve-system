"""SMTP send service with retry and optional rate limiting (M-11).

After a successful SMTP send, a copy of the message is appended to the
mailbox's sent folder over IMAP. Titan's SMTP does NOT auto-save outbound
copies, so without this the AI's replies would exist only in the local DB and
be invisible in the Hostinger webmail conversation view.
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate

import bleach
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import SMTPError, SMTPRateLimitError
from app.models.reply import Reply
from app.services.audit import utcnow

logger = logging.getLogger(__name__)

_HTML_BODY_TAGS = ["p", "br", "ul", "li", "strong", "em", "a"]
_HTML_BODY_ATTRIBUTES = {"a": ["href"]}
_SIGNATURE_BLOCK_HTML = (
    '<p style="margin:32px 0 0; color:#2E5D86; font-weight:600;">'
    "Best regards,"
    "</p>"
    '<p style="margin:0; color:#2E5D86; font-weight:600;">'
    "The LBORA Team"
    "</p>"
)


def _brand_html_body(content_en: str) -> str:
    """Render reply content as a branded HTML email body."""

    from app.services.replier import markdown_to_html

    body = markdown_to_html(content_en)
    body = bleach.clean(
        body,
        tags=_HTML_BODY_TAGS,
        attributes=_HTML_BODY_ATTRIBUTES,
        strip=True,
    )
    body = body.replace(
        "<p>Best regards,<br>The LBORA Team</p>", _SIGNATURE_BLOCK_HTML
    )
    body = body.replace(
        "<p>Best regards,</p><p>The LBORA Team</p>", _SIGNATURE_BLOCK_HTML
    )
    if "Best regards," not in content_en and "The LBORA Team" not in content_en:
        body += _SIGNATURE_BLOCK_HTML

    body = body.replace('<p>', '<p style="margin:0 0 12px; line-height:1.6;">')
    body = body.replace('<ul>', '<ul style="margin:0 0 12px; padding-left:20px;">')
    body = body.replace('<li>', '<li style="margin:0 0 6px; line-height:1.6;">')
    body = body.replace(
        "<a ", '<a style="color:#2E5D86; text-decoration:underline;" '
    )
    body = body.replace("<strong>", '<strong style="font-weight:600;">')
    body = body.replace("<em>", '<em style="font-style:italic;">')

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0; padding:0; background-color:#ffffff; '
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        'Helvetica,Arial,sans-serif; color:#1F2937;">'
        '<div style="max-width:640px; margin:0 auto; padding:24px;">'
        f"{body}"
        "</div></body></html>"
    )


def build_message(reply, to_email: str, subject: str, settings: Settings) -> EmailMessage:
    """Compose the outbound email, preserving the thread (In-Reply-To)."""

    msg = EmailMessage()
    from_addr = settings.email_username
    if settings.mail_from_name:
        msg["From"] = f"{settings.mail_from_name} <{from_addr}>"
    else:
        msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Message-ID"] = reply.message_id
    if reply.in_reply_to:
        msg["In-Reply-To"] = reply.in_reply_to
    source_email = reply.email
    references = []
    if source_email and source_email.references:
        references.append(source_email.references)
    if reply.in_reply_to:
        references.append(reply.in_reply_to)
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(reply.content_en)
    msg.add_alternative(_brand_html_body(reply.content_en), subtype="html")
    return msg


def build_text_message(
    to_email: str,
    subject: str,
    body: str,
    settings: Settings,
    message_id: str | None = None,
) -> EmailMessage:
    """Compose a plain-text message (used by alert emails, M-18)."""

    msg = EmailMessage()
    from_addr = settings.email_username
    if settings.mail_from_name:
        msg["From"] = f"{settings.mail_from_name} <{from_addr}>"
    else:
        msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg


class MailerService:
    """Sends Reply rows through SMTP; retries 3x (PRD F10)."""

    def __init__(
        self,
        db: Session | None,
        settings: Settings,
        smtp_class: type | None = None,
        imap_class: type | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.smtp_class = smtp_class or smtplib.SMTP_SSL
        self.imap_class = imap_class or imaplib.IMAP4_SSL

    def _check_rate_limit(self) -> None:
        limit = self.settings.smtp_rate_limit_per_hour
        if limit <= 0:
            return
        cutoff = utcnow() - timedelta(hours=1)
        sent_count = self.db.execute(
            select(func.count())
            .select_from(Reply)
            .where(
                Reply.status == "sent",
                Reply.sent_at.is_not(None),
                Reply.sent_at >= cutoff,
            )
        ).scalar_one()
        if sent_count >= limit:
            raise SMTPRateLimitError(
                "SMTP rate limit reached; reply kept as failed for manual retry"
            )

    def _send_once(self, msg: EmailMessage) -> None:
        smtp = self.smtp_class(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.smtp_timeout,
        )
        try:
            smtp.login(self.settings.email_username, self.settings.email_password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 - connection may already be gone
                pass

    def _append_sent_copy(self, msg: EmailMessage) -> None:
        """Store a copy of the sent mail in the mailbox's sent folder (IMAP).

        Best-effort only: the email was already delivered over SMTP, so an
        append failure is logged and never raised. Without this copy Titan's
        webmail conversation view has no trace of the AI's reply (the mailbox
        lost a message the frontend shows as sent).
        """

        folder = self.settings.imap_sent_folder
        if not folder:
            return
        if "Date" not in msg:
            msg["Date"] = formatdate(localtime=True)
        raw = msg.as_string()
        conn = self.imap_class(
            self.settings.imap_host,
            self.settings.imap_port,
            timeout=self.settings.imap_timeout,
        )
        try:
            conn.login(self.settings.email_username, self.settings.email_password)
            conn.append(folder, r"(\Seen)", imaplib.Time2Internaldate(time.time()), raw.encode("utf-8"))
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - already gone
                pass

    def send(self, reply, to_email: str, subject: str) -> None:
        """Send with 3 attempts; raise SMTPError after all retries."""

        self._check_rate_limit()
        msg = build_message(reply, to_email, subject, self.settings)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._send_once(msg)
                break
            except Exception as exc:  # noqa: BLE001 - smtplib raises many types
                last_error = exc
                logger.warning("SMTP send failed (attempt %s/3): %s", attempt, exc)
                if attempt < 3:
                    time.sleep(min(2 ** (attempt - 1), 4))
        else:
            raise SMTPError(f"SMTP send failed after 3 attempts: {last_error}") from last_error
        try:
            self._append_sent_copy(msg)
        except Exception as exc:  # noqa: BLE001 - best effort, never fail a delivered mail
            logger.warning("Failed to store sent copy in mailbox: %s", exc)

    def send_text(
        self,
        to_email: str,
        subject: str,
        body: str,
        message_id: str | None = None,
    ) -> None:
        """Send a plain-text message (alerts) with the same 3-attempt retry.

        Alert mail is intentionally not counted by the outbound reply rate
        limiter: the limiter protects customer-facing sends, while alerts must
        always get through.
        """

        msg = build_text_message(to_email, subject, body, self.settings, message_id)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._send_once(msg)
                return
            except Exception as exc:  # noqa: BLE001 - smtplib raises many types
                last_error = exc
                logger.warning("Alert SMTP send failed (attempt %s/3): %s", attempt, exc)
                if attempt < 3:
                    time.sleep(min(2 ** (attempt - 1), 4))
        raise SMTPError(f"Alert SMTP send failed after 3 attempts: {last_error}") from last_error
