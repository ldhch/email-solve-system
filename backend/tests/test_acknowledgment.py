"""Acknowledgment reply + review SLA ticket tests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.llm.client import MockLLMClient
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.services.acknowledgment import (
    AcknowledgmentService,
    business_days_from,
    resolve_review_tickets,
)
from app.services.audit import utcnow
from app.services.mailer import MailerService
from app.services.replier import ReplierService

from conftest import FakeSMTP


def _conversation_with_email(db, email: str) -> tuple[Conversation, Email]:
    customer = Customer(email=email, display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = Conversation(
        customer_id=customer.id,
        subject_normalized="review",
        window_end=utcnow(),
        last_activity_at=utcnow(),
        status="open",
    )
    db.add(conv)
    db.flush()
    row = Email(
        conversation_id=conv.id,
        message_id=f"<{email}-m@example.com>",
        subject="Review",
        from_email=email,
        to_email="bot@example.com",
        body_text="Please help me.",
        is_inbound=True,
        received_at=utcnow(),
        summary_cn="待审核邮件",
    )
    db.add(row)
    db.flush()
    return conv, row


def test_business_days_from_weekday() -> None:
    start = datetime(2026, 8, 24, 10, 0)  # Monday
    assert business_days_from(start, 2) == datetime(2026, 8, 26, 10, 0)


def test_business_days_from_friday_skips_weekend() -> None:
    start = datetime(2026, 8, 28, 10, 0)  # Friday
    assert business_days_from(start, 2) == datetime(2026, 9, 1, 10, 0)


def test_ack_sent_once_and_creates_review_ticket(
    db, settings, fake_smtp_class
) -> None:
    conv, first_email = _conversation_with_email(db, "first@example.com")
    second_email = Email(
        conversation_id=conv.id,
        message_id="<second@example.com>",
        subject="Review 2",
        from_email="first@example.com",
        to_email="bot@example.com",
        body_text="Follow-up.",
        is_inbound=True,
        received_at=utcnow(),
        summary_cn="待审核邮件 2",
    )
    db.add(second_email)
    db.commit()

    service = AcknowledgmentService(
        db,
        settings,
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        replier=ReplierService(db, settings, MockLLMClient(settings)),
    )
    service.send_for_email(first_email, conv)
    service.send_for_email(second_email, conv)

    acks = db.execute(
        select(Reply).where(Reply.reply_type == "acknowledgment")
    ).scalars().all()
    assert len(acks) == 1
    assert acks[0].status == "sent"
    assert len(FakeSMTP.instances[0].sent) == 1
    tickets = db.execute(select(Ticket)).scalars().all()
    assert len(tickets) == 1
    assert tickets[0].risk_level == "medium"


def test_resolve_review_tickets_only_closes_medium(
    db, settings
) -> None:
    conv, _ = _conversation_with_email(db, "c@example.com")
    db.add(
        Ticket(
            conversation_id=conv.id,
            summary_cn="review",
            risk_level="medium",
            status="pending",
            sla_deadline=utcnow(),
            created_at=utcnow(),
        )
    )
    db.add(
        Ticket(
            conversation_id=conv.id,
            summary_cn="high risk",
            risk_level="high",
            status="pending",
            sla_deadline=utcnow(),
            created_at=utcnow(),
        )
    )
    db.commit()

    resolve_review_tickets(db, conv.id)

    tickets = db.execute(select(Ticket).order_by(Ticket.id)).scalars().all()
    assert tickets[0].status == "resolved"
    assert tickets[1].status == "pending"
