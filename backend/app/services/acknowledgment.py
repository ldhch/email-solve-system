"""Fixed English acknowledgment replies for owner-review mail.

When an email cannot be answered automatically and must go to the boss, the
system sends one safe acknowledgment so the customer knows it was received.
The reply is a fixed template, never LLM-generated, and each conversation only
receives one. A review ticket carries the promised 1-2 business-day SLA so the
boss is alerted if the customer is still waiting.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.services.audit import log_action, utcnow

ACK_REPLY_TYPE = "acknowledgment"
ACK_SLA_DAYS = 2

ACK_CONTENT_EN = """\
Thank you for contacting LBORA.

We have received your message and are reviewing it. We will reply within 1-2
business days.

Best regards,
The LBORA Team
"""

ACK_CONTENT_CN = """\
感谢您联系 LBORA。

我们已经收到您的邮件，正在处理中，会在 1-2 个工作日内回复。

此致，
LBORA Team
"""


def business_days_from(start: datetime, days: int = ACK_SLA_DAYS) -> datetime:
    """Return a deadline after ``days`` Mon-Fri business days."""

    result = start
    remaining = max(0, days)
    while remaining:
        result = result + timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def resolve_review_tickets(
    db: Session,
    conversation_id: int,
    *,
    actor_id: int | None = None,
    ip: str | None = None,
) -> None:
    """Close open medium-risk review tickets after a real reply is sent."""

    open_tickets = db.execute(
        select(Ticket).where(
            Ticket.conversation_id == conversation_id,
            Ticket.status.in_(("pending", "in_progress")),
            Ticket.risk_level != "high",
        )
    ).scalars().all()
    for ticket in open_tickets:
        ticket.status = "resolved"
        ticket.resolved_at = utcnow()
        log_action(
            db,
            "ticket_resolved",
            "ticket",
            ticket.id,
            actor_id=actor_id,
            ip=ip,
            commit=False,
        )


class AcknowledgmentService:
    """Send the fixed review acknowledgment and create its SLA ticket."""

    def __init__(self, db: Session, settings, mailer, replier) -> None:
        self.db = db
        self.settings = settings
        self.mailer = mailer
        self.replier = replier

    def send_for_email(self, email_row: Email, conversation: Conversation) -> None:
        """Send one acknowledgment per conversation for a review/manual mail."""

        existing = db_execute_sent_ack(self.db, conversation.id)
        if existing:
            self.ensure_review_ticket(email_row, conversation)
            return
        reply = self.replier.build_reply(
            email_row,
            conversation,
            ACK_CONTENT_EN,
            reply_type=ACK_REPLY_TYPE,
            status="draft",
            content_cn=ACK_CONTENT_CN,
        )
        try:
            self.mailer.send(
                reply,
                to_email=email_row.from_email,
                subject=email_row.subject,
                bypass_rate_limit=True,
            )
        except Exception as exc:  # noqa: BLE001 - ack failure must not block review
            reply.status = "failed"
            reply.send_error = str(exc)
            log_action(
                self.db,
                "ack_failed",
                "reply",
                reply.id,
                actor_id=None,
                commit=False,
            )
            self.ensure_review_ticket(email_row, conversation)
            self.db.commit()
            return

        reply.status = "sent"
        reply.sent_at = utcnow()
        log_action(
            self.db,
            "ack_sent",
            "reply",
            reply.id,
            actor_id=None,
            commit=False,
        )
        self.ensure_review_ticket(email_row, conversation)
        self.db.commit()

    def ensure_review_ticket(self, email_row: Email, conversation: Conversation) -> None:
        """Create one pending medium ticket per conversation for SLA alerts."""

        existing = self.db.execute(
            select(Ticket.id)
            .where(
                Ticket.conversation_id == conversation.id,
                Ticket.status.in_(("pending", "in_progress")),
                Ticket.risk_level != "high",
            )
            .limit(1)
        ).scalars().first()
        if existing is not None:
            return
        ticket = Ticket(
            conversation_id=conversation.id,
            summary_cn=email_row.summary_cn or email_row.subject or "待老板审核",
            risk_level="medium",
            status="pending",
            sla_deadline=business_days_from(email_row.received_at, ACK_SLA_DAYS),
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


def db_execute_sent_ack(db: Session, conversation_id: int) -> Reply | None:
    """Return the first sent acknowledgment for a conversation, if any."""

    return db.execute(
        select(Reply)
        .where(
            Reply.conversation_id == conversation_id,
            Reply.reply_type == ACK_REPLY_TYPE,
            Reply.status == "sent",
        )
        .limit(1)
    ).scalars().first()
