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
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

import bleach
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import IMAPError
from app.llm.client import BaseLLMClient, build_llm_client
from app.models.attachment import Attachment
from app.models.email import Email
from app.models.system_state import SystemState
from app.services.audit import log_action, utcnow
from app.services.classifier import Classification, ClassifierService, resolve_action
from app.services.conversation import ConversationService
from app.services.mailer import MailerService
from app.services.replier import ReplierService

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024  # truncate body beyond 2 MB (TECH N-2)
WARN_RAW_BYTES = 5 * 1024 * 1024  # log warning beyond 5 MB raw email
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


def synthetic_message_id(subject: str, from_email: str, received_at: datetime) -> str:
    """Stable synthetic Message-ID when the incoming mail lacks one."""

    digest = hashlib.sha1(
        f"{subject}|{from_email}|{received_at.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    return f"gen-{digest}@local"


def parse_email(raw: bytes, uid: str | None = None) -> ParsedEmail:
    """Parse raw RFC822 bytes into a normalized ParsedEmail."""

    msg = message_from_bytes(raw)

    message_id = _strip_msgid(msg.get("Message-ID")) or synthetic_message_id(
        _decode_mime_header(msg.get("Subject")),
        getaddresses([msg.get("From", "")])[0][1] if msg.get("From") else "unknown",
        parsedate_to_datetime(msg.get("Date")) or datetime.now(),
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

    date_value = msg.get("Date")
    received_at = parsedate_to_datetime(date_value) if date_value else None
    if received_at is None:
        received_at = datetime.now()
    if received_at.tzinfo is not None:
        received_at = received_at.astimezone().replace(tzinfo=None)
    received_at = received_at.replace(tzinfo=None)

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
    ) -> None:
        self.db = db
        self.settings = settings
        self.llm_client = llm_client or build_llm_client(settings)
        self.mailer = mailer or MailerService(db, settings)
        self.imap = imap
        self.conversations = ConversationService(db, settings)
        self.classifier = ClassifierService(settings, self.llm_client)
        self.replier = ReplierService(db, settings, self.llm_client)

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

    def process_one(self, parsed: ParsedEmail) -> ProcessingResult:
        """Run the full chain for one parsed email (synchronous, per TECH)."""

        try:
            return self._process_one_inner(parsed)
        except Exception as exc:  # noqa: BLE001 - pipeline must survive single failures
            logger.exception("Pipeline failed for message_id=%s", parsed.message_id)
            self.db.rollback()
            log_action(self.db, "pipeline_failed", "email", 0, ip=None)
            return ProcessingResult(
                message_id=parsed.message_id,
                action="failed",
                error=str(exc),
            )

    def _process_one_inner(self, parsed: ParsedEmail) -> ProcessingResult:
        existing = self.db.execute(
            select(Email).where(Email.message_id == parsed.message_id)
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("Duplicate message_id=%s skipped", parsed.message_id)
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
        )

        customer = merged.customer
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

        action = resolve_action(classification.risk_level, classification.category)
        if action == "auto_send":
            reply = self.replier.generate_and_send(
                email_row=email_row,
                conversation=conversation,
                mailer=self.mailer,
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

        # Phase 1 has no review queue / ticket table (Phase 2/3). Record and stop.
        log_action(
            self.db,
            "requires_manual",
            "email",
            email_row.id,
            actor_id=None,
        )
        self.db.commit()
        return ProcessingResult(
            message_id=parsed.message_id,
            action="manual",
            email_id=email_row.id,
            conversation_id=conversation.id,
            risk_level=classification.risk_level,
            category=classification.category,
        )

    def _persist_attachments(
        self, email_row: Email, attachments: list[ParsedAttachment]
    ) -> None:
        """Write attachment files under `data/attachments` and record rows."""

        for att in attachments:
            safe_name = os.path.basename(att.filename).replace("/", "_").replace("\\", "_")
            if not safe_name:
                safe_name = "attachment.bin"
            target = Path(self.settings.attachment_dir) / f"{uuid.uuid4().hex[:12]}_{safe_name}"
            target.write_bytes(att.payload)
            self.db.add(
                Attachment(
                    email_id=email_row.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=len(att.payload),
                    stored_path=str(target),
                    created_at=utcnow(),
                )
            )

    def fetch_and_process(self) -> dict[str, int]:
        """Fetch all UNSEEN emails and process each one synchronously."""

        conn = self.imap or self._connect()
        items = self.fetch_unseen(conn)
        summary = {
            "fetched": len(items),
            "auto_sent": 0,
            "manual": 0,
            "paused": 0,
            "duplicate": 0,
            "silenced": 0,
            "failed": 0,
        }
        for uid, raw in items:
            parsed = parse_email(raw, uid=uid)
            result = self.process_one(parsed)
            if result.action in summary:
                summary[result.action] += 1
            # Persisted emails are marked SEEN so they are not re-fetched.
            # Paused/failed emails stay UNSEEN and are processed after resume/retry.
            if result.action in ("auto_sent", "manual", "silenced"):
                self.mark_seen(conn, uid)
        return summary
