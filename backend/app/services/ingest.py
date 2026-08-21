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
from html.parser import HTMLParser
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
from app.services.blocked_senders import sender_is_blocked
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
_IGNORED_TAG_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)


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


class _HtmlToTextParser(HTMLParser):
    """Convert the sanitized email subset into structured plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._anchor_href: str | None = None

    def _ensure_single_newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def _ensure_blank_line(self) -> None:
        if not self.parts:
            return
        if self.parts[-1].endswith("\n\n"):
            return
        if self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        else:
            self.parts.append("\n\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag in ("p", "div", "section"):
            self._ensure_blank_line()
        elif tag == "br":
            self._ensure_single_newline()
        elif tag == "li":
            self._ensure_single_newline()
            self.parts.append("- ")
        elif tag == "a":
            self._anchor_href = dict(attrs).get("href", "")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self._ensure_single_newline()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("p", "div", "section"):
            self._ensure_blank_line()
        elif tag == "li":
            self._ensure_single_newline()
        elif tag == "a" and self._anchor_href is not None:
            if self._anchor_href:
                self.parts.append(f" ({self._anchor_href})")
            self._anchor_href = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data.replace("\xa0", " "))
        if text and text != " ":
            self.parts.append(text)

    def text(self) -> str:
        return "".join(self.parts).strip()


def _html_to_text(html: str) -> str:
    """Convert HTML email bodies into paragraph-separated plain text."""

    html = _IGNORED_TAG_RE.sub(" ", html)
    cleaned = bleach.clean(
        html,
        tags=["p", "div", "section", "br", "ul", "ol", "li", "b", "strong", "a"],
        attributes={"a": ["href"]},
        strip=True,
    )
    parser = _HtmlToTextParser()
    parser.feed(cleaned)
    parser.close()
    return parser.text()


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
    # RFC2047 encoded-words in display names (e.g. "=?utf-8?B?...?=") must be
    # decoded before storing, otherwise the name renders as raw MIME in the UI.
    from_name = _decode_mime_header(from_name)
    to_name = _decode_mime_header(to_name)

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

    # ---------- test mode (sender whitelist) ----------

    def _test_mode_gate(self) -> set[str] | None:
        """Return the effective whitelist when test mode is on, else None.

        ``None`` means test mode is off — every sender is processed. An empty
        set means test mode is on with an empty whitelist — every sender is
        gated (full isolation). A non-empty set is the exact set of sender
        addresses allowed through, normalized to lowercase.
        """

        state = self.db.get(SystemState, 1)
        if state is None or not state.test_mode:
            return None
        return {
            w.strip().lower()
            for w in (state.test_whitelist or "").split(",")
            if w.strip()
        }

    def _is_gated(self, gate: set[str] | None, from_email: str) -> bool:
        """True when ``from_email`` must be skipped under test mode.

        Gated mail is NOT ingested, classified, translated or replied to, and
        it stays UNSEEN on the server so a later non-test poll picks it up.
        """

        return gate is not None and from_email.lower() not in gate

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

        # Blocked senders (blacklist) are archived to the「广告」tab without any
        # LLM call — no classification, no reply, no retention, no ticket.
        if sender_is_blocked(self.db, parsed.from_email):
            return self._handle_ad(parsed, email_row, conversation, blocked=True)

        classification: Classification = self.classifier.classify(parsed)
        email_row.risk_level = classification.risk_level
        email_row.confidence = classification.confidence
        email_row.category = classification.category
        email_row.summary_cn = classification.summary_cn

        # Marketing/newsletter mail detected by the classifier: same archiving
        # path as a blacklist hit, but the classification is persisted first.
        if classification.is_ad:
            return self._handle_ad(parsed, email_row, conversation, blocked=False)

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

        # Emergency pause (M-19): the mail is already ingested (visible + unread
        # in the inbox) but no reply is generated or sent. The flag makes
        # `_process_pending_after_pause` re-route it in received order after the
        # boss resumes, and it is marked SEEN so the poll never re-fetches it.
        state = self.db.get(SystemState, 1)
        if state is not None and state.ai_paused:
            email_row.pending_after_pause = True
            log_action(self.db, "paused_skipped", "email", email_row.id, actor_id=None)
            logger.info(
                "System paused; email %s ingested and queued (pending_after_pause)",
                parsed.message_id,
            )
            return ProcessingResult(
                message_id=parsed.message_id,
                action="paused",
                email_id=email_row.id,
                conversation_id=conversation.id,
                risk_level=classification.risk_level,
                category=classification.category,
            )

        return self._route_email(
            parsed,
            email_row,
            conversation,
            classification,
            auto_send_mode=auto_send_mode,
            conversation_created=merged.created,
        )

    def _route_email(
        self,
        parsed,
        email_row,
        conversation,
        classification,
        *,
        auto_send_mode: str = "send",
        conversation_created: bool = False,
    ) -> ProcessingResult:
        """Phase 3 routing (TECH 九 Phase 3): pick one reply action per mail.

        Shared by the normal pipeline and `_process_pending_after_pause`, which
        re-routes emails ingested while the system was emergency-paused.
        """

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
            # Readable body -> low-confidence draft the boss can edit and send
            # instead of writing from scratch; empty/unreadable -> pure manual.
            if (parsed.body_text or "").strip():
                return self._unknown_draft(parsed, email_row, conversation, classification)
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
                    conversation_created=conversation_created,
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

    def _unknown_draft(
        self, parsed, email_row, conversation, classification
    ) -> ProcessingResult:
        """Low-confidence pending_review draft for unclassifiable mail.

        The model was unsure, so the draft is marked ``low_confidence`` and the
        UI shows an explicit「置信度低」warning — it is never auto-sent, only the
        boss can approve it. Saves rewriting the reply from scratch.
        """

        content_en = self.replier.generate(email_row, conversation)
        reply = self.replier.build_reply(
            email_row,
            conversation,
            content_en,
            reply_type="general",
            status="pending_review",
        )
        reply.low_confidence = True
        log_action(self.db, "requires_review_low_confidence", "reply", reply.id)
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

    def _handle_ad(
        self, parsed, email_row, conversation, *, blocked: bool
    ) -> ProcessingResult:
        """Archive a marketing/newsletter email to the「广告」tab.

        The mail is ingested (visible + readable) but never auto-replied, never
        aggregated, never ticketed. ``blocked`` distinguishes a blacklist hit
        from a classifier-detected ad for the audit trail.
        """

        email_row.is_ad = True
        # Ad mail never counts toward the unread badge — the boss reads it on
        # the「广告」tab only when they choose to.
        email_row.is_read = True
        log_action(
            self.db,
            "ad_blocked" if blocked else "ad_archived",
            "email",
            email_row.id,
            actor_id=None,
        )
        self.db.commit()
        logger.info(
            "Advertisement %s archived to ad tab (blocked=%s)",
            parsed.message_id,
            blocked,
        )
        return ProcessingResult(
            message_id=parsed.message_id,
            action="ad",
            email_id=email_row.id,
            conversation_id=conversation.id,
            risk_level="low",
            category="other",
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

    # ---------- resume-after-pause backlog (M-19) ----------

    def _process_pending_after_pause(self) -> dict[str, int]:
        """Re-route emails ingested while the system was paused, oldest first.

        Called at the top of every poll while NOT paused. Failed sends retry
        their existing draft (no regeneration); everything else re-enters Phase 3
        routing. Low-risk auto-sends aggregate per conversation exactly like a
        normal poll, so the backlog is answered in received order.
        """

        summary = {
            key: 0
            for key in (
                "auto_sent",
                "reassured",
                "review",
                "manual",
                "paused",
                "duplicate",
                "silenced",
                "failed",
                "followup",
                "test_skipped",
            )
        }
        state = self.db.get(SystemState, 1)
        if state is not None and state.ai_paused:
            return summary
        gate = self._test_mode_gate()
        pending = self.db.execute(
            select(Email)
            .where(Email.pending_after_pause.is_(True))
            .order_by(Email.received_at.asc(), Email.id.asc())
        ).scalars().all()
        if not pending:
            return summary
        logger.info("Resuming %s paused email(s) in received order", len(pending))
        pending_groups: dict[int, list[ProcessingResult]] = {}
        for email in pending:
            if self._is_gated(gate, email.from_email):
                # Test mode: keep the mail queued untouched until it is off.
                summary["test_skipped"] += 1
                continue
            conversation = self.db.get(Conversation, email.conversation_id)
            if conversation is None:
                email.pending_after_pause = False  # orphaned row: drop the flag
                continue
            resend = self._resend_failed_reply(email)
            if resend is not None:
                if resend.action == "auto_sent":
                    email.pending_after_pause = False
                summary[resend.action] = summary.get(resend.action, 0) + 1
                continue
            classification = self._classification_from_email(email)
            parsed = ParsedEmail(
                message_id=email.message_id,
                subject=email.subject,
                from_email=email.from_email,
                from_name=None,
                to_email=email.to_email,
                body_text=email.body_text,
                body_html=email.body_html,
                received_at=email.received_at,
                in_reply_to=email.in_reply_to,
            )
            result = self._route_email(
                parsed,
                email,
                conversation,
                classification,
                auto_send_mode="defer",
                conversation_created=False,
            )
            if result.action == "pending_auto":
                pending_groups.setdefault(result.conversation_id or 0, []).append(
                    result
                )
                continue
            if result.action in (
                "auto_sent",
                "reassured",
                "review",
                "manual",
                "silenced",
                "followup",
            ):
                email.pending_after_pause = False
            if result.action in summary:
                summary[result.action] += 1
        for conversation_id, results in pending_groups.items():
            outcome, _keep_unseen_uid = self._send_aggregated_group(
                conversation_id, results, remove_on_generation_failure=False
            )
            if outcome == "sent":
                summary["auto_sent"] += 1
                self._set_pending_flag(results, False)
            elif outcome == "smtp_failed":
                # Only the newest batch email stays queued; the next round
                # re-sends its failed reply instead of regenerating.
                summary["failed"] += 1
                self._set_pending_flag(results, False)
                latest = self._latest_of(results)
                if latest is not None:
                    latest.pending_after_pause = True
            else:  # generation_failed: keep every batch email queued for retry
                summary["failed"] += 1
        self.db.commit()
        return summary

    def _classification_from_email(self, email: Email) -> Classification:
        """Rebuild the classification for an already-ingested email.

        Reuses the pause-time classification when present; otherwise re-runs the
        classifier (covers rows where classification failed before persisting).
        """

        if email.risk_level:
            return Classification(
                risk_level=email.risk_level,
                confidence=email.confidence or 0.0,
                category=email.category or "",
                chargeback_risk=False,
                summary_cn=email.summary_cn or "",
            )
        parsed = ParsedEmail(
            message_id=email.message_id,
            subject=email.subject,
            from_email=email.from_email,
            from_name=None,
            to_email=email.to_email,
            body_text=email.body_text,
            body_html=email.body_html,
            received_at=email.received_at,
            in_reply_to=email.in_reply_to,
        )
        classification = self.classifier.classify(parsed)
        email.risk_level = classification.risk_level
        email.confidence = classification.confidence
        email.category = classification.category
        email.summary_cn = classification.summary_cn
        return classification

    def _emails_of(self, results: list[ProcessingResult]) -> list[Email]:
        emails = [
            self.db.get(Email, r.email_id) for r in results if r.email_id is not None
        ]
        return [e for e in emails if e is not None]

    def _latest_of(self, results: list[ProcessingResult]) -> Email | None:
        emails = self._emails_of(results)
        return max(emails, key=lambda e: (e.received_at, e.id)) if emails else None

    def _set_pending_flag(self, results: list[ProcessingResult], value: bool) -> None:
        for email in self._emails_of(results):
            email.pending_after_pause = value

    # ---------- aggregated auto-send (PRD F2 / edge case 3) ----------

    def _send_aggregated_group(
        self,
        conversation_id: int,
        results: list[ProcessingResult],
        *,
        remove_on_generation_failure: bool = True,
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
            # Resume-backlog batches must NOT be deleted: those email rows were
            # ingested while paused and stay queued for a retry instead.
            if remove_on_generation_failure:
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
            summary = {
                "fetched": 0,
                "auto_sent": 0,
                "reassured": 0,
                "review": 0,
                "manual": 0,
                "paused": 0,
                "duplicate": 0,
                "silenced": 0,
                "ad": 0,
                "failed": 0,
                "followup": 0,
                "test_skipped": 0,
            }
            # Backlog first: emails ingested while paused are re-routed in
            # received order, then fresh UNSEEN mail is processed (M-19).
            pending_summary = self._process_pending_after_pause()
            for key, value in pending_summary.items():
                summary[key] += value
            items = self.fetch_unseen(conn)
            summary["fetched"] = len(items)
            gate = self._test_mode_gate()
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
                if self._is_gated(gate, parsed.from_email):
                    # Test mode: the mail stays UNSEEN and untouched; it will be
                    # re-fetched (and skipped again) until test mode is off.
                    summary["test_skipped"] += 1
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
                # Paused emails are already ingested and re-routed from the DB
                # backlog after resume; failed emails stay UNSEEN and retried.
                if result.action in (
                    "auto_sent",
                    "reassured",
                    "review",
                    "manual",
                    "silenced",
                    "followup",
                    "paused",
                    "ad",
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
