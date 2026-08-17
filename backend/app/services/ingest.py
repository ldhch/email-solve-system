"""IMAP ingest + full Phase-1 pipeline driver (M-04).

`IngestService` owns the synchronous per-email flow:
    fetch (UNSEEN) -> parse -> dedupe -> conversation merge -> persist
    -> classify -> route -> generate -> SMTP send -> audit
No task queue: one email is fully processed before the next is fetched.
"""

from __future__ import annotations

import hashlib
import imaplib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses, parsedate_tz
from pathlib import Path
from typing import Any

import bleach
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.exceptions import IMAPError, SMTPError
from app.llm.client import BaseLLMClient, build_llm_client
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.services.audit import log_action, utcnow
from app.services.alerting import record_imap_failure, record_imap_success
from app.services.classifier import (
    Classification,
    ClassifierService,
    requests_silence,
    resolve_action,
)
from app.services.conversation import ConversationService
from app.services.mailer import MailerService
from app.services.replier import ReplierService
from app.services.retention import RetentionService

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024  # truncate body beyond 2 MB (TECH N-2)
WARN_RAW_BYTES = 5 * 1024 * 1024  # log warning beyond 5 MB raw email
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # drop oversized email attachments (disk-fill guard)
_MSGID_TAG_RE = re.compile(r"^<|>$")


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    payload: bytes


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    from_email: str
    from_name: str | None
    to_email: str | None
    body_text: str | None
    body_html: str | None
    received_at: datetime
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    has_attachments: bool = False
    attachments: list[ParsedAttachment] = field(default_factory=list)
    raw_bytes: bytes = b""
    uid: str | None = None


@dataclass
class ProcessingResult:
    message_id: str
    action: str  # duplicate | paused | auto_sent | manual | silenced | failed
    email_id: int | None = None
    conversation_id: int | None = None
    reply_id: int | None = None
    risk_level: str | None = None
    category: str | None = None
    error: str | None = None
    uid: str | None = None
    conversation_created: bool = False


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts)


def _strip_msgid(value: str | None) -> str | None:
    if not value:
        return None
    return _MSGID_TAG_RE.sub("", value.strip())


def _sanitize_html(html: str) -> str:
    """Bleach whitelist: only p/br/a survive (TECH 6.6)."""

    return bleach.clean(html, tags=["p", "br", "a"], attributes={"a": ["href"]}, strip=True)


def _html_to_text(html: str) -> str:
    return bleach.clean(html, tags=[], strip=True)


def _truncate(text: str | None, limit: int = MAX_BODY_BYTES) -> str | None:
    if text is None:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _parse_received_at(date_value: str | None) -> datetime:
    """Parse an email Date header into a naive UTC datetime.

    `parsedate_tz` keeps the header's timezone offset; aware times are converted
    to UTC and stripped to naive so `sla_deadline` comparisons against
    `utcnow()` (naive UTC) are correct regardless of the sender's timezone.
    Headers without a zone and unparseable values fall back to UTC.
    """

    if not date_value:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = parsedate_tz(date_value)
    if parsed is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    naive = datetime(*parsed[:6])
    tz_offset = parsed[-1]  # seconds east of UTC, or None
    if tz_offset is not None:
        aware = naive.replace(tzinfo=timezone(timedelta(seconds=tz_offset)))
        return aware.astimezone(timezone.utc).replace(tzinfo=None)
    return naive  # no tz info: treat as UTC


def synthetic_message_id(
    subject: str, from_email: str, received_at: datetime, uid: str | None = None
) -> str:
    """Stable synthetic Message-ID when the incoming mail lacks one.

    `uid` is included so two messages from the same sender/subject/second do
    not collapse into one ID when the real Message-ID header is missing.
    """

    digest = hashlib.sha1(
        f"{subject}|{from_email}|{received_at.isoformat()}|{uid or ''}".encode("utf-8")
    ).hexdigest()[:24]
    return f"gen-{digest}@local"


def parse_email(raw: bytes, uid: str | None = None) -> ParsedEmail:
    """Parse raw RFC822 bytes into a normalized ParsedEmail."""

    msg = message_from_bytes(raw)

    message_id = _strip_msgid(msg.get("Message-ID")) or synthetic_message_id(
        _decode_mime_header(msg.get("Subject")),
        getaddresses([msg.get("From", "")])[0][1] if msg.get("From") else "unknown",
        _parse_received_at(msg.get("Date")),
        uid=uid,
    )

    references: list[str] = []
    for ref_header in msg.get_all("References") or []:
        for token in ref_header.split():
            ref_id = _strip_msgid(token)
            if ref_id:
                references.append(ref_id)

    from_name, from_email = "", ""
    if msg.get("From"):
        addr = getaddresses([msg["From"]])
        if addr:
            from_name, from_email = addr[0]
    to_name, to_email = "", ""
    if msg.get("To"):
        addr = getaddresses([msg["To"]])
        if addr:
            to_name, to_email = addr[0]

    subject = _decode_mime_header(msg.get("Subject"))
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[ParsedAttachment] = []

    for part in msg.walk():
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                ParsedAttachment(
                    filename=filename or f"attachment-{len(attachments) + 1}",
                    content_type=ctype,
                    payload=payload,
                )
            )
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain" and body_text is None:
            body_text = text
        elif ctype == "text/html" and body_html is None:
            body_html = _sanitize_html(text)
            if body_text is None:
                body_text = _html_to_text(text)

    received_at = _parse_received_at(msg.get("Date"))

    if len(raw) > WARN_RAW_BYTES:
        logger.warning("Oversized raw email (%s bytes) for %s", len(raw), message_id)

    return ParsedEmail(
        message_id=message_id,
        subject=subject,
        from_email=from_email.lower() or "unknown@local",
        from_name=from_name or None,
        to_email=to_email.lower() or None,
        body_text=_truncate(body_text),
        body_html=_truncate(body_html),
        received_at=received_at,
        in_reply_to=_strip_msgid(msg.get("In-Reply-To")),
        references=references,
        has_attachments=bool(attachments),
        attachments=attachments,
        raw_bytes=raw,
        uid=uid,
    )


class IngestService:
    """Fetches UNSEEN mail and drives the Phase-1 processing pipeline."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        llm_client: BaseLLMClient | None = None,
        mailer: MailerService | None = None,
        imap: Any | None = None,
        session_factory: sessionmaker | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.llm_client = llm_client or build_llm_client(settings)
        self.mailer = mailer or MailerService(db, settings)
        self.imap = imap
        self.session_factory = session_factory
        self.conversations = ConversationService(db, settings)
        self.classifier = ClassifierService(settings, self.llm_client)
        self.replier = ReplierService(db, settings, self.llm_client)
        self.retention = RetentionService(db, settings, self.llm_client)

    # ---------- IMAP transport ----------

    def _connect(self) -> Any:
        """Open an IMAP4 SSL connection. Login failures are retried 3x."""

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                conn = imaplib.IMAP4_SSL(
                    self.settings.imap_host,
                    self.settings.imap_port,
                    timeout=self.settings.imap_timeout,
                )
                conn.login(self.settings.email_username, self.settings.email_password)
                conn.select("INBOX")
                return conn
            except Exception as exc:  # noqa: BLE001 - connection/login errors
                last_error = exc
                logger.warning("IMAP connect/login failed (attempt %s/3): %s", attempt, exc)
                if attempt < 3:
                    time.sleep(2 * attempt)
        raise IMAPError(f"IMAP login failed after 3 attempts: {last_error}") from last_error

    def fetch_unseen(self, conn: Any | None = None) -> list[tuple[str, bytes]]:
        """Return [(uid, raw_bytes)] for all UNSEEN messages in INBOX."""

        conn = conn or self.imap or self._connect()
        _, data = conn.uid("SEARCH", None, "UNSEEN")
        uids = (data[0] or b"").split()
        results: list[tuple[str, bytes]] = []
        for uid in uids:
            uid = uid.decode("ascii")
            status, fetch_data = conn.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not fetch_data:
                logger.warning("IMAP FETCH failed for uid=%s (status=%s)", uid, status)
                continue
            raw = fetch_data[0][1]
            results.append((uid, bytes(raw)))
        return results

    def mark_seen(self, conn: Any | None, uid: str) -> None:
        """Mark one message SEEN after successful processing (idempotent)."""

        try:
            (conn or self.imap).uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except Exception as exc:  # noqa: BLE001 - non-fatal
            logger.warning("Failed to mark uid=%s seen: %s", uid, exc)

    # ---------- pipeline ----------

    def process_one(
        self,
        parsed: ParsedEmail,
        auto_send_mode: str = "send",
    ) -> ProcessingResult:
        """Run the full chain for one parsed email (synchronous, per TECH)."""

        try:
            return self._process_one_inner(parsed, auto_send_mode=auto_send_mode)
        except Exception as exc:  # noqa: BLE001 - pipeline must survive single failures
            logger.exception("Pipeline failed for message_id=%s", parsed.message_id)
            self.db.rollback()
            # Write the failure audit in a fresh session so a rolled-back
            # business transaction can never pollute or swallow the record.
            if self.session_factory is not None:
                with self.session_factory() as audit_db:
                    log_action(audit_db, "pipeline_failed", "email", 0, ip=None)
            else:
                log_action(self.db, "pipeline_failed", "email", 0, ip=None)
            return ProcessingResult(
                message_id=parsed.message_id,
                action="failed",
                error=str(exc),
            )

    def _process_one_inner(
        self,
        parsed: ParsedEmail,
        auto_send_mode: str = "send",
    ) -> ProcessingResult:
        existing = self.db.execute(
            select(Email).where(Email.message_id == parsed.message_id)
        ).scalar_one_or_none()
        if existing is not None:
            resend_result = self._resend_failed_reply(existing)
            if resend_result is not None:
                return resend_result
            logger.info("Duplicate message_id=%s skipped", parsed.message_id)
            log_action(self.db, "duplicate_skipped", "email", existing.id)
            return ProcessingResult(message_id=parsed.message_id, action="duplicate")

        state = self.db.get(SystemState, 1)
        if state is not None and state.ai_paused:
            log_action(self.db, "paused_skipped", "email", 0)
            logger.info("System paused; email %s fetched but not processed", parsed.message_id)
            return ProcessingResult(message_id=parsed.message_id, action="paused")

        merged = self.conversations.merge(parsed)
        conversation = merged.conversation

        email_row = Email(
            conversation_id=conversation.id,
            message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references=" ".join(parsed.references) if parsed.references else None,
            subject=parsed.subject,
            from_email=parsed.from_email,
            to_email=parsed.to_email,
            body_text=parsed.body_text,
            body_html=parsed.body_html,
            is_inbound=True,
            has_attachments=parsed.has_attachments,
            received_at=parsed.received_at,
        )
        self.db.add(email_row)
        self.db.flush()
        self._persist_attachments(email_row, parsed.attachments)

        classification: Classification = self.classifier.classify(parsed)
        email_row.risk_level = classification.risk_level
        email_row.confidence = classification.confidence
        email_row.category = classification.category
        email_row.summary_cn = classification.summary_cn
        self.conversations.update_risk(conversation, classification.risk_level)
        log_action(
            self.db,
            "classified",
            "email",
            email_row.id,
            actor_id=None,
            commit=False,
        )

        customer = merged.customer
        if requests_silence(parsed.body_text):
            # PRD edge case 9: honor "do not contact" requests (72h silence).
            customer.silenced_until = utcnow() + timedelta(hours=72)
            log_action(
                self.db,
                "silenced_set",
                "customer",
                customer.id,
                actor_id=None,
                commit=False,
            )
            self.db.commit()
            return ProcessingResult(
                message_id=parsed.message_id,
                action="silenced",
                email_id=email_row.id,
                conversation_id=conversation.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )
        if customer.silenced_until and customer.silenced_until > utcnow():
            log_action(self.db, "silenced_skipped", "email", email_row.id)
            self.db.commit()
            return ProcessingResult(
                message_id=parsed.message_id,
                action="silenced",
                email_id=email_row.id,
                conversation_id=conversation.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )

        # Phase 3 routing (TECH 九 Phase 3):
        #   high   -> reassurance email + auto-created ticket with 24h SLA
        #             (chargeback was already forced to high by the classifier
        #             and never enters retention)
        #   unknown-> manual only, no reassurance (PRD edge cases 6/7)
        #   refund/return/exchange -> retention flow (M-13)
        #   medium -> pending_review draft (boss approves first)
        #   low    -> auto_send
        if classification.risk_level == "high":
            return self._handle_high_risk(parsed, email_row, conversation, classification)
        if classification.risk_level == "unknown":
            return self._manual(parsed, email_row, conversation, classification)

        if classification.category == "refund_request":
            return self._run_retention_flow(parsed, email_row, conversation, classification)

        action = resolve_action(classification.risk_level, classification.category)
        if action == "review":
            return self._draft_for_review(parsed, email_row, conversation, classification)
        if action == "auto_send":
            if auto_send_mode == "defer":
                return self._defer_auto_send(
                    parsed,
                    email_row,
                    conversation,
                    classification,
                    conversation_created=merged.created,
                )
            return self._auto_send(parsed, email_row, conversation, classification)
        return self._manual(parsed, email_row, conversation, classification)

    def _handle_high_risk(
        self, parsed, email_row, conversation, classification
    ) -> ProcessingResult:
        """M-06 enhancement (Phase 3): send reassurance + create a ticket.

        The reassurance email is sent synchronously (SMTP retries 3x inside
        MailerService); a send failure marks the reply `failed` + audits it but
        never blocks ticket creation. The email stays UNSEEN on failure so the
        next poll retries the same draft (no regeneration, no duplicate ticket).
        """

        # PRD edge case 8: a follow-up to an already-escalated conversation must
        # not re-send the reassurance or create a second ticket. The message is
        # merged into the existing ticket; the boss sees it in the timeline.
        existing_open_ticket = self.db.execute(
            select(Ticket)
            .where(
                Ticket.conversation_id == conversation.id,
                Ticket.is_deleted.is_(False),
                Ticket.status.in_(("pending", "in_progress")),
            )
            .order_by(Ticket.id.asc())
        ).scalars().first()
        if existing_open_ticket is not None:
            log_action(
                self.db,
                "high_risk_followup",
                "ticket",
                existing_open_ticket.id,
                actor_id=None,
                commit=False,
            )
            self.db.commit()
            return ProcessingResult(
                message_id=parsed.message_id,
                action="followup",
                email_id=email_row.id,
                conversation_id=conversation.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )

        reply = self.replier.generate_and_send(
            email_row=email_row,
            conversation=conversation,
            mailer=self.mailer,
            reply_type="reassurance",
        )
        ticket = Ticket(
            conversation_id=conversation.id,
            summary_cn=classification.summary_cn or parsed.subject or "High-risk email",
            risk_level="high",
            status="pending",
            sla_deadline=email_row.received_at + timedelta(hours=24),  # 24x7, no weekends
            created_at=utcnow(),
        )
        self.db.add(ticket)
        self.db.flush()
        log_action(
            self.db,
            "ticket_created",
            "ticket",
            ticket.id,
            actor_id=None,
            commit=False,
        )
        self.db.commit()
        sent = reply.status == "sent"
        return ProcessingResult(
            message_id=parsed.message_id,
            action="reassured" if sent else "failed",
            email_id=email_row.id,
            conversation_id=conversation.id,
            reply_id=reply.id,
            risk_level=classification.risk_level,
            category=classification.category,
            error=reply.send_error if not sent else None,
        )

    def _manual(self, parsed, email_row, conversation, classification) -> ProcessingResult:
        log_action(self.db, "requires_manual", "email", email_row.id, actor_id=None)
        self.db.commit()
        return ProcessingResult(
            message_id=parsed.message_id,
            action="manual",
            email_id=email_row.id,
            conversation_id=conversation.id,
            risk_level=classification.risk_level,
            category=classification.category,
        )

    def _auto_send(
        self,
        parsed,
        email_row,
        conversation,
        classification,
        reply_type: str = "general",
    ) -> ProcessingResult:
        """Generate + SMTP-send a reply (low-risk or direct retention)."""

        reply = self.replier.generate_and_send(
            email_row=email_row,
            conversation=conversation,
            mailer=self.mailer,
            reply_type=reply_type,
            return_policy_text=self.settings.return_policy_text,
        )
        self.db.commit()
        sent = reply.status == "sent"
        return ProcessingResult(
            message_id=parsed.message_id,
            action="auto_sent" if sent else "failed",
            email_id=email_row.id,
            conversation_id=conversation.id,
            reply_id=reply.id,
            risk_level=classification.risk_level,
            category=classification.category,
            error=reply.send_error if not sent else None,
        )

    def _defer_auto_send(
        self,
        parsed,
        email_row,
        conversation,
        classification,
        conversation_created: bool,
    ) -> ProcessingResult:
        """Batch mode (PRD F2 / edge case 3): persist only, no reply yet.

        The reply for this email is generated once per conversation after the
        whole poll batch is ingested (aggregation), see `_send_aggregated_group`.
        """

        self.db.commit()
        return ProcessingResult(
            message_id=parsed.message_id,
            action="pending_auto",
            email_id=email_row.id,
            conversation_id=conversation.id,
            risk_level=classification.risk_level,
            category=classification.category,
            uid=parsed.uid,
            conversation_created=conversation_created,
        )

    def _draft_for_review(
        self, parsed, email_row, conversation, classification
    ) -> ProcessingResult:
        """Medium-risk (or unknown-reason refund): draft waiting for the boss."""

        content_en = self.replier.generate(email_row, conversation)
        reply = self.replier.build_reply(
            email_row,
            conversation,
            content_en,
            reply_type="general",
            status="pending_review",
        )
        log_action(self.db, "requires_review", "reply", reply.id)
        self.db.commit()
        return ProcessingResult(
            message_id=parsed.message_id,
            action="review",
            email_id=email_row.id,
            conversation_id=conversation.id,
            reply_id=reply.id,
            risk_level=classification.risk_level,
            category=classification.category,
        )

    def _run_retention_flow(
        self, parsed, email_row, conversation, classification
    ) -> ProcessingResult:
        """M-13: route refund/return/exchange requests into save-the-sale."""

        body = parsed.body_text or ""

        # The customer is answering a retention offer we already sent.
        if self.retention.has_open_offer(conversation.id):
            if self.retention.is_customer_accepted(body):
                reply = self.replier.generate_and_send(
                    email_row=email_row,
                    conversation=conversation,
                    mailer=self.mailer,
                    reply_type="retention_accepted",
                )
                self.db.commit()
                log_action(
                    self.db,
                    "retention_accepted",
                    "conversation",
                    conversation.id,
                )
                return ProcessingResult(
                    message_id=parsed.message_id,
                    action="auto_sent" if reply.status == "sent" else "failed",
                    email_id=email_row.id,
                    conversation_id=conversation.id,
                    reply_id=reply.id,
                    risk_level=classification.risk_level,
                    category=classification.category,
                    error=reply.send_error if reply.status != "sent" else None,
                )
            # rejected/uncertain: the pending offer already counts toward the
            # attempt limit, so the next round may already release the return.
        elif self.retention.is_released(conversation.id):
            # Return instructions were already sent; further refund requests go
            # to the boss instead of spamming the same reply.
            return self._manual(parsed, email_row, conversation, classification)

        reason = self.retention.classify_reason(body)
        strategy = self.retention.resolve_strategy(reason, conversation.retention_attempts)

        if strategy == "none":  # quality / damaged: honor the return directly
            reply = self.replier.generate_and_send(
                email_row=email_row,
                conversation=conversation,
                mailer=self.mailer,
                reply_type="retention_release",
                return_policy_text=self.settings.return_policy_text,
            )
            self.db.commit()
            log_action(self.db, "retention_released", "conversation", conversation.id)
            return ProcessingResult(
                message_id=parsed.message_id,
                action="auto_sent" if reply.status == "sent" else "failed",
                email_id=email_row.id,
                conversation_id=conversation.id,
                reply_id=reply.id,
                risk_level=classification.risk_level,
                category=classification.category,
                error=reply.send_error if reply.status != "sent" else None,
            )

        if strategy == "review":  # unknown reason: conservative, boss reviews
            return self._draft_for_review(parsed, email_row, conversation, classification)

        if strategy == "exchange":  # size issue: AI sends the exchange offer
            content_en = self.replier.generate(
                email_row, conversation, reply_type="retention_exchange"
            )
            reply = self.replier.build_reply(
                email_row,
                conversation,
                content_en,
                reply_type="retention_exchange",
                status="draft",
            )
            try:
                self.mailer.send(
                    reply, to_email=email_row.from_email, subject=email_row.subject
                )
            except SMTPError as exc:
                reply.status = "failed"
                reply.send_error = str(exc)
                log_action(self.db, "reply_failed", "reply", reply.id)
                self.db.commit()
                return ProcessingResult(
                    message_id=parsed.message_id,
                    action="failed",
                    email_id=email_row.id,
                    conversation_id=conversation.id,
                    reply_id=reply.id,
                    risk_level=classification.risk_level,
                    category=classification.category,
                    error=str(exc),
                )
            reply.status = "sent"
            reply.sent_at = utcnow()
            conversation.retention_attempts += 1
            log_action(self.db, "retention_offer_sent", "conversation", conversation.id)
            self.db.commit()
            return ProcessingResult(
                message_id=parsed.message_id,
                action="auto_sent",
                email_id=email_row.id,
                conversation_id=conversation.id,
                reply_id=reply.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )

        if strategy == "compensation":  # money involved: boss approves first
            content_en = self.replier.generate(
                email_row, conversation, reply_type="retention_compensation"
            )
            reply = self.replier.build_reply(
                email_row,
                conversation,
                content_en,
                reply_type="retention_compensation",
                status="pending_review",
            )
            conversation.retention_attempts += 1
            log_action(self.db, "retention_draft_created", "reply", reply.id)
            self.db.commit()
            return ProcessingResult(
                message_id=parsed.message_id,
                action="review",
                email_id=email_row.id,
                conversation_id=conversation.id,
                reply_id=reply.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )

        # strategy == "release": attempts exhausted, honor the return
        reply = self.replier.generate_and_send(
            email_row=email_row,
            conversation=conversation,
            mailer=self.mailer,
            reply_type="retention_release",
            return_policy_text=self.settings.return_policy_text,
        )
        self.db.commit()
        log_action(self.db, "retention_released", "conversation", conversation.id)
        return ProcessingResult(
            message_id=parsed.message_id,
            action="auto_sent" if reply.status == "sent" else "failed",
            email_id=email_row.id,
            conversation_id=conversation.id,
            reply_id=reply.id,
            risk_level=classification.risk_level,
            category=classification.category,
            error=reply.send_error if reply.status != "sent" else None,
        )

    def _resend_failed_reply(self, email_row: Email) -> ProcessingResult | None:
        """Retry the latest failed reply for an already-ingested email.

        Keeps the original draft (no LLM regeneration) and only re-runs SMTP,
        which matches PRD F10 "SMTP 失败重试 3 次，失败后进入待发送" at the
        Phase-1 level: the email stays UNSEEN until a send finally succeeds.
        """

        reply = self.db.execute(
            select(Reply)
            .where(Reply.email_id == email_row.id, Reply.status == "failed")
            .order_by(Reply.created_at.desc())
        ).scalars().first()
        if reply is None:
            return None

        try:
            self.mailer.send(reply, to_email=email_row.from_email, subject=email_row.subject)
        except SMTPError as exc:
            reply.send_error = str(exc)
            log_action(self.db, "reply_failed", "reply", reply.id)
            self.db.commit()
            return ProcessingResult(
                message_id=email_row.message_id,
                action="failed",
                email_id=email_row.id,
                conversation_id=email_row.conversation_id,
                reply_id=reply.id,
                error=str(exc),
            )

        reply.status = "sent"
        reply.sent_at = utcnow()
        reply.send_error = None
        log_action(self.db, "reply_sent", "reply", reply.id)
        self.db.commit()
        return ProcessingResult(
            message_id=email_row.message_id,
            action="auto_sent",
            email_id=email_row.id,
            conversation_id=email_row.conversation_id,
            reply_id=reply.id,
        )

    def _persist_attachments(
        self, email_row: Email, attachments: list[ParsedAttachment]
    ) -> None:
        """Write attachment files under `data/attachments` and record rows."""

        for att in attachments:
            if len(att.payload) > MAX_ATTACHMENT_BYTES:
                logger.warning(
                    "Dropping oversized attachment %r (%s bytes) for email id=%s",
                    att.filename,
                    len(att.payload),
                    email_row.id,
                )
                continue
            safe_name = os.path.basename(att.filename).replace("/", "_").replace("\\", "_")
            if not safe_name:
                safe_name = "attachment.bin"
            target = Path(self.settings.attachment_dir) / f"{uuid.uuid4().hex[:12]}_{safe_name}"
            target.write_bytes(att.payload)
            # Store a path relative to the data directory so the DB survives
            # machine moves / redeploys (absolute paths would all 404 later).
            data_dir = Path(self.settings.attachment_dir).parent
            stored_path = str(target.relative_to(data_dir))
            self.db.add(
                Attachment(
                    email_id=email_row.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=len(att.payload),
                    stored_path=stored_path,
                    created_at=utcnow(),
                )
            )

    # ---------- aggregated auto-send (PRD F2 / edge case 3) ----------

    def _send_aggregated_group(
        self, conversation_id: int, results: list[ProcessingResult]
    ) -> tuple[str, str | None]:
        """Send ONE aggregated reply covering a conversation's pending batch.

        Returns ``(outcome, keep_unseen_uid)``:
        - ``sent``: the batch is fully answered; caller marks every UID seen.
        - ``smtp_failed``: a failed reply row was kept for the newest email;
          the caller keeps that email UNSEEN so the next poll re-sends the same
          draft, and marks the older batch emails seen.
        - ``generation_failed``: the batch email rows were removed so the next
          poll regenerates the reply (mirrors single-mail rollback semantics);
          the caller keeps every UID unseen.
        """

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return "generation_failed", None
        emails = [
            self.db.get(Email, r.email_id)
            for r in results
            if r.email_id is not None
        ]
        emails = [e for e in emails if e is not None]
        if not emails:
            return "generation_failed", None
        emails.sort(key=lambda e: (e.received_at, e.id))
        latest = emails[-1]
        uid_by_message_id = {r.message_id: r.uid for r in results}
        latest_uid = uid_by_message_id.get(latest.message_id)

        try:
            content = self.replier.generate_aggregated(emails, conversation)
        except Exception as exc:  # noqa: BLE001 - LLM/provider errors
            logger.exception(
                "Aggregated reply generation failed for conversation=%s: %s",
                conversation_id,
                exc,
            )
            self._remove_ingested_batch(results)
            log_action(
                self.db,
                "aggregate_reply_failed",
                "conversation",
                conversation_id,
                actor_id=None,
            )
            return "generation_failed", None

        reply = self.replier.build_reply(
            latest,
            conversation,
            content,
            reply_type="general",
            status="draft",
        )
        try:
            self.mailer.send(reply, to_email=latest.from_email, subject=latest.subject)
        except SMTPError as exc:
            reply.status = "failed"
            reply.send_error = str(exc)
            log_action(self.db, "reply_failed", "reply", reply.id)
            self.db.commit()
            logger.error("Aggregated reply id=%s failed: %s", reply.id, exc)
            return "smtp_failed", latest_uid

        reply.status = "sent"
        reply.sent_at = utcnow()
        log_action(self.db, "reply_sent", "reply", reply.id)
        logger.info(
            "Aggregated reply id=%s sent to %s (covers %s batch emails)",
            reply.id,
            latest.from_email,
            len(emails),
        )
        self.db.commit()
        return "sent", None

    def _remove_ingested_batch(self, results: list[ProcessingResult]) -> None:
        """Delete this poll batch's email rows (generation-failure retry).

        The next poll re-ingests these emails from scratch and regenerates the
        aggregated reply; attachments become orphan files (harmless, small).
        Conversations that were created by this batch and are now empty are
        removed too, and the batch's `classified` audit rows are dropped so no
        dangling rows point at the removed emails (historical conversations are
        left untouched).
        """

        deleted_email_ids: list[int] = []
        conversation_ids = {
            r.conversation_id for r in results if r.conversation_id is not None
        }
        newly_created = {
            r.conversation_id
            for r in results
            if r.conversation_created and r.conversation_id is not None
        }
        for r in results:
            email = self.db.get(Email, r.email_id)
            if email is None:
                continue
            for attachment in list(email.attachments):
                self.db.delete(attachment)
            for reply in list(email.replies):
                self.db.delete(reply)
            deleted_email_ids.append(email.id)
            self.db.delete(email)
        self.db.flush()  # make the deletions visible to the count queries below

        if deleted_email_ids:
            dangling = self.db.execute(
                select(AuditLog).where(
                    AuditLog.action == "classified",
                    AuditLog.resource_type == "email",
                    AuditLog.resource_id.in_(deleted_email_ids),
                )
            ).scalars().all()
            for entry in dangling:
                self.db.delete(entry)

        for conversation_id in conversation_ids:
            remaining = self.db.execute(
                select(func.count())
                .select_from(Email)
                .where(Email.conversation_id == conversation_id)
            ).scalar_one()
            if remaining == 0 and conversation_id in newly_created:
                conversation = self.db.get(Conversation, conversation_id)
                if conversation is not None:
                    self.db.delete(conversation)

    def fetch_and_process(self) -> dict[str, int]:
        """Fetch all UNSEEN emails and process each one synchronously.

        Two-phase batch processing (PRD F2 / edge case 3, TECH M-07):
        1. Every mail is parsed, deduped, merged and classified; only
           non-auto-send branches act immediately (high-risk reassurance /
           tickets, retention, review, manual, silence...).
        2. Low-risk auto-send mails are answered with ONE aggregated reply per
           conversation covering every new question, avoiding reply spam.

        Still fully synchronous and serial per email - no queue/worker.
        """

        owns_connection = self.imap is None
        conn = self.imap
        try:
            conn = self.imap or self._connect()
            items = self.fetch_unseen(conn)
            summary = {
                "fetched": len(items),
                "auto_sent": 0,
                "reassured": 0,
                "review": 0,
                "manual": 0,
                "paused": 0,
                "duplicate": 0,
                "silenced": 0,
                "failed": 0,
                "followup": 0,
            }
            pending_groups: dict[int, list[ProcessingResult]] = {}
            for uid, raw in items:
                try:
                    parsed = parse_email(raw, uid=uid)
                except Exception as exc:  # noqa: BLE001 - one malformed mail must not stall the inbox
                    logger.exception("Failed to parse email uid=%s; marking seen and skipping", uid)
                    log_action(self.db, "parse_failed", "email", 0, ip=None)
                    self.mark_seen(conn, uid)
                    summary["failed"] += 1
                    continue
                result = self.process_one(parsed, auto_send_mode="defer")
                if result.action == "pending_auto":
                    pending_groups.setdefault(result.conversation_id or 0, []).append(
                        result
                    )
                    continue
                if result.action in summary:
                    summary[result.action] += 1
                # Persisted emails are marked SEEN so they are not re-fetched.
                # Paused/failed emails stay UNSEEN and are processed after resume/retry.
                if result.action in (
                    "auto_sent",
                    "reassured",
                    "review",
                    "manual",
                    "silenced",
                    "followup",
                ):
                    self.mark_seen(conn, uid)
            # Phase 2: one aggregated reply per conversation (synchronous).
            for conversation_id, results in pending_groups.items():
                outcome, keep_unseen_uid = self._send_aggregated_group(
                    conversation_id, results
                )
                if outcome == "sent":
                    summary["auto_sent"] += 1
                    for r in results:
                        if r.uid:
                            self.mark_seen(conn, r.uid)
                elif outcome == "smtp_failed":
                    summary["failed"] += 1
                    for r in results:
                        if r.uid and r.uid != keep_unseen_uid:
                            self.mark_seen(conn, r.uid)
                else:  # generation_failed: batch removed, retry everything
                    summary["failed"] += 1
            record_imap_success()
            return summary
        except Exception as exc:  # noqa: BLE001 - transport-level failures
            record_imap_failure(self.settings, error=str(exc))
            raise
        finally:
            # Release the IMAP session we created; otherwise a long-running loop
            # leaks half-open connections and can be treated as abuse by the host.
            if owns_connection and conn is not None:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 - best effort
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001 - already gone
                        pass
