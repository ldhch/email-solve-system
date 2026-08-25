"""Conversation merge engine (M-05, PRD F2).

Matching priority:
1. In-Reply-To / References header match against stored emails.
2. Sender email + normalized subject: exact equality first, then
   `difflib.SequenceMatcher.ratio() >= threshold`, inside a 7-day window.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.logging import get_logger
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.services.acknowledgment import ACK_REPLY_TYPE
from app.services.audit import utcnow

logger = get_logger(__name__)

_PREFIX_RE = re.compile(r"^\s*(re|fwd?|fw|aw|sv|antw|antwort|vs|res|tr)\s*(\[\d+\])?\s*[:：\-]?\s*", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")

RISK_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0, None: 0}


def followup_count(db: Session, conversation_id: int) -> int:
    """Count customer follow-up emails after the last real sent reply.

    A "real" reply is any sent reply that is not the acknowledgment receipt.
    When the boss has never sent a real reply, every inbound after the first
    mail counts as a follow-up (the first mail triggered the ack). Derived on
    demand — never stored — so there is no counter to keep in sync.
    """

    last_real_sent_at = db.execute(
        select(Reply.sent_at)
        .where(
            Reply.conversation_id == conversation_id,
            Reply.status == "sent",
            Reply.reply_type != ACK_REPLY_TYPE,
        )
        .order_by(Reply.sent_at.desc())
        .limit(1)
    ).scalar()

    inbound = select(func.count(Email.id)).where(
        Email.conversation_id == conversation_id,
        Email.is_inbound.is_(True),
    )
    if last_real_sent_at is not None:
        return db.execute(
            inbound.where(Email.received_at > last_real_sent_at)
        ).scalar() or 0
    # No real reply yet: the first inbound triggered the ack; everything after
    # it is a follow-up while the customer is still waiting.
    return max(0, (db.execute(inbound).scalar() or 0) - 1)


def normalize_subject(subject: str | None) -> str:
    """Strip stacked reply/forward prefixes, lowercase, remove punctuation, squeeze spaces."""

    if not subject:
        return ""
    cleaned = subject
    while True:
        stripped = _PREFIX_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    return " ".join(cleaned.lower().split())


def subject_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two normalized subjects."""

    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class MergeResult:
    conversation: Conversation
    customer: Customer
    created: bool


class ConversationService:
    """Finds or creates the conversation for an inbound email."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def merge(self, parsed_email) -> MergeResult:
        """Route `parsed_email` into an existing or new conversation."""

        window = timedelta(days=self.settings.conversation_window_days)
        received = parsed_email.received_at

        conversation = self._match_by_headers(parsed_email)
        if conversation is None:
            conversation = self._match_by_subject(parsed_email, received, window)

        customer = self._get_or_create_customer(parsed_email)
        if conversation is None:
            conversation = Conversation(
                customer_id=customer.id,
                subject_normalized=normalize_subject(parsed_email.subject),
                window_end=received + window,
                last_activity_at=received,
                status="open",
                risk_level=None,
            )
            self.db.add(conversation)
            self.db.flush()
            logger.info(
                "Created conversation id=%s for %s",
                conversation.id,
                parsed_email.from_email,
            )
            return MergeResult(conversation=conversation, customer=customer, created=True)

        # Reopen closed conversations and slide the window. New mail on an
        # archived conversation means the customer replied -> surface it back
        # in the inbox by clearing the archive flag (M-16 archive UX).
        if conversation.status == "resolved":
            conversation.status = "open"
        conversation.is_archived = False
        conversation.window_end = max(conversation.window_end, received + window)
        conversation.last_activity_at = received
        self.db.flush()
        return MergeResult(conversation=conversation, customer=customer, created=False)

    def _match_by_headers(self, parsed_email) -> Conversation | None:
        """Match via In-Reply-To / References (highest priority)."""

        header_ids = [parsed_email.in_reply_to] if parsed_email.in_reply_to else []
        header_ids += list(parsed_email.references or [])
        for header_id in header_ids:
            if not header_id:
                continue
            existing = self.db.execute(
                select(Email).where(Email.message_id == header_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing.conversation
        return None

    def _match_by_subject(self, parsed_email, received, window) -> Conversation | None:
        """Match by sender email + normalized subject within the window."""

        normalized = normalize_subject(parsed_email.subject)
        if not normalized:
            return None

        customer = self.db.execute(
            select(Customer).where(Customer.email == parsed_email.from_email)
        ).scalar_one_or_none()
        if customer is None:
            return None

        candidates = self.db.execute(
            select(Conversation).where(
                Conversation.customer_id == customer.id,
                Conversation.window_end >= received,
                Conversation.status != "resolved",
            )
        ).scalars().all()

        # 1) exact normalized-subject match
        for conv in candidates:
            if conv.subject_normalized == normalized:
                return conv

        # 2) fuzzy similarity fallback (difflib, threshold from settings)
        threshold = self.settings.conversation_subject_similarity_threshold
        best: tuple[float, Conversation | None] = (0.0, None)
        for conv in candidates:
            score = subject_similarity(conv.subject_normalized, normalized)
            if score > best[0]:
                best = (score, conv)
        if best[0] >= threshold:
            return best[1]
        return None

    def _get_or_create_customer(self, parsed_email) -> Customer:
        customer = self.db.execute(
            select(Customer).where(Customer.email == parsed_email.from_email)
        ).scalar_one_or_none()
        if customer is None:
            customer = Customer(
                email=parsed_email.from_email,
                display_name=parsed_email.from_name,
                created_at=utcnow(),
            )
            self.db.add(customer)
            self.db.flush()
        elif parsed_email.from_name and customer.display_name != parsed_email.from_name:
            logger.info(
                "Customer %s changed display name '%s' -> '%s'",
                customer.email,
                customer.display_name,
                parsed_email.from_name,
            )
            customer.display_name = parsed_email.from_name
        return customer

    def update_risk(self, conversation: Conversation, risk_level: str | None) -> None:
        """Merge risk across the conversation: keep the highest (conservative)."""

        current = RISK_RANK.get(conversation.risk_level, 0)
        incoming = RISK_RANK.get(risk_level, 0)
        if incoming > current:
            conversation.risk_level = risk_level
            self.db.flush()
