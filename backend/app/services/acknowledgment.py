"""Fixed English acknowledgment replies for owner-review mail.

When an email cannot be answered automatically and must go to the boss, the
system sends one safe acknowledgment so the customer knows it was received.
The reply is a fixed template, never LLM-generated, and each conversation only
receives one. A review ticket carries the promised 1-2 business-day SLA so the
boss is alerted if the customer is still waiting.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.services.audit import log_action, utcnow

ACK_REPLY_TYPE = "acknowledgment"
ACK_SLA_DAYS = 2

ACK_CONTENT_EN = """\
Hi {customer_name},

Thank you for contacting LBORA.

We have received your message and are reviewing it. We will reply within 1-2
business days.

Best regards,
The LBORA Team
"""

ACK_CONTENT_CN = """\
{customer_name}，您好！

感谢您联系 LBORA。

我们已经收到您的邮件，正在处理中，会在 1-2 个工作日内回复。

此致，
LBORA Team
"""

# Follow-up "still working on it" acknowledgment. The first ack promises a
# 1-2 business-day reply; if the customer keeps writing while the boss has not
# replied yet, one follow-up keeps the conversation from going silent. Same
# {customer_name} placeholder replacement as the first ack.
ACK_FOLLOWUP_CONTENT_EN = """\
Hi {customer_name},

Thank you for your patience and for keeping in touch.

We have received your latest message — nothing has been overlooked. Your
request is still being processed by our team, and you can expect our final
reply within the next 1-2 business days.

We appreciate your understanding.

Best regards,
The LBORA Team
"""

ACK_FOLLOWUP_CONTENT_CN = """\
{customer_name}，您好！

感谢您的耐心等待和持续联系。

您的最新消息我们已收到，没有被遗漏。我们的团队仍在处理您的请求，预计将在
接下来 1-2 个工作日内给您最终答复。

感谢您的理解。

此致，
LBORA Team
"""

# Only re-acknowledge once the customer has waited this long since the last ack
# (a same-day follow-up is not "waiting" yet), and cap the total acks per
# conversation so a patient customer is never bombarded.
ACK_FOLLOWUP_MIN_HOURS = 24
ACK_MAX_PER_CONVERSATION = 2


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
        """Send one acknowledgment per conversation for a review/manual mail.

        First mail gets the standard ack. If the customer keeps writing while
        the boss has not replied yet, one follow-up "still working on it"
        acknowledgment is sent (>= ACK_FOLLOWUP_MIN_HOURS later, capped at
        ACK_MAX_PER_CONVERSATION acks per conversation) so the customer is
        never left in silence while waiting for the first real reply.
        """

        existing = db_execute_sent_ack(self.db, conversation.id)
        if existing:
            if self._should_send_followup_ack(existing, conversation):
                self._send_ack(
                    email_row,
                    conversation,
                    ACK_FOLLOWUP_CONTENT_EN,
                    ACK_FOLLOWUP_CONTENT_CN,
                )
            else:
                self.ensure_review_ticket(email_row, conversation)
            return

        # Editable template (Settings page): DB value wins, else the hardcoded
        # default. The {customer_name} placeholder is replaced in _send_ack.
        state = self.db.get(SystemState, 1)
        content_en = (
            state.ack_content_en if state and state.ack_content_en else ACK_CONTENT_EN
        )
        content_cn = (
            state.ack_content_cn if state and state.ack_content_cn else ACK_CONTENT_CN
        )
        self._send_ack(email_row, conversation, content_en, content_cn)

    def _should_send_followup_ack(
        self, last_ack: Reply, conversation: Conversation
    ) -> bool:
        """True when the customer is still waiting and deserves one more ack.

        All three must hold: the boss never sent a real reply yet (this is
        still the first-wait), enough time passed since the last ack, and the
        per-conversation ack cap is not reached.
        """

        # A real (non-ack) reply was already sent -> this is a new round of
        # conversation, not a wait; "still working on it" would be wrong.
        real_reply_sent = self.db.execute(
            select(Reply.id)
            .where(
                Reply.conversation_id == conversation.id,
                Reply.status == "sent",
                Reply.reply_type != ACK_REPLY_TYPE,
            )
            .limit(1)
        ).scalars().first()
        if real_reply_sent is not None:
            return False

        if last_ack.sent_at is None:
            return False
        since = utcnow() - last_ack.sent_at
        if since.total_seconds() < ACK_FOLLOWUP_MIN_HOURS * 3600:
            return False

        ack_count = self.db.execute(
            select(func.count(Reply.id)).where(
                Reply.conversation_id == conversation.id,
                Reply.reply_type == ACK_REPLY_TYPE,
                Reply.status == "sent",
            )
        ).scalar()
        return ack_count < ACK_MAX_PER_CONVERSATION

    def _send_ack(
        self,
        email_row: Email,
        conversation: Conversation,
        content_en: str,
        content_cn: str,
    ) -> None:
        """Build and send one acknowledgment reply (first or follow-up).

        Replaces the {customer_name} placeholder, persists the reply, sends via
        SMTP and ensures the review ticket exists. A send failure marks the
        reply failed but never blocks the review flow.
        """

        name = (
            (conversation.customer.display_name or "").strip()
            if conversation.customer
            else ""
        )
        placeholder = name or "there"
        content_en = content_en.replace("{customer_name}", placeholder)
        content_cn = content_cn.replace("{customer_name}", placeholder)

        reply = self.replier.build_reply(
            email_row,
            conversation,
            content_en,
            reply_type=ACK_REPLY_TYPE,
            status="draft",
            content_cn=content_cn,
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
