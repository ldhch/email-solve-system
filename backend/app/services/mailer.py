"""SMTP send service with retry and optional rate limiting (M-11)."""

from __future__ import annotations

import logging
import smtplib
import time
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import SMTPError, SMTPRateLimitError
from app.models.reply import Reply
from app.services.audit import utcnow

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.db = db
        self.settings = settings
        self.smtp_class = smtp_class or smtplib.SMTP_SSL

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

    def send(self, reply, to_email: str, subject: str) -> None:
        """Send with 3 attempts; raise SMTPError after all retries."""

        self._check_rate_limit()
        msg = build_message(reply, to_email, subject, self.settings)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._send_once(msg)
                return
            except Exception as exc:  # noqa: BLE001 - smtplib raises many types
                last_error = exc
                logger.warning("SMTP send failed (attempt %s/3): %s", attempt, exc)
                if attempt < 3:
                    time.sleep(min(2 ** (attempt - 1), 4))
        raise SMTPError(f"SMTP send failed after 3 attempts: {last_error}") from last_error

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
