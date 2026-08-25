"""Acknowledgment reply + review SLA ticket tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.llm.client import MockLLMClient
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.services.acknowledgment import (
    ACK_CONTENT_EN,
    ACK_FOLLOWUP_CONTENT_EN,
    AcknowledgmentService,
    business_days_from,
    resolve_review_tickets,
)
from app.services.audit import utcnow
from app.services.mailer import MailerService
from app.services.replier import ReplierService

from conftest import FakeSMTP


def _plain_body(msg) -> str:
    """Text/plain payload of a built EmailMessage (multipart alternative)."""

    body = msg.get_body(preferencelist=("plain",))
    return body.get_payload() if body is not None else msg.get_payload()


def _conversation_with_email(
    db, email: str, display_name: str = "C"
) -> tuple[Conversation, Email]:
    customer = Customer(email=email, display_name=display_name, created_at=utcnow())
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


def test_ack_uses_db_template_and_substitutes_name(
    db, settings, fake_smtp_class
) -> None:
    """A saved template wins over the default and {customer_name} is replaced."""
    conv, email = _conversation_with_email(db, "sarah@example.com", display_name="Sarah")
    state = db.get(SystemState, 1)
    assert state is not None
    state.ack_content_en = "Hello {customer_name}, custom note."
    state.ack_content_cn = "你好，{customer_name}"
    db.commit()

    service = AcknowledgmentService(
        db,
        settings,
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        replier=ReplierService(db, settings, MockLLMClient(settings)),
    )
    service.send_for_email(email, conv)

    body = _plain_body(FakeSMTP.instances[0].sent[0])
    assert "Hello Sarah, custom note." in body
    assert "{customer_name}" not in body


def test_ack_falls_back_to_default_when_db_null(
    db, settings, fake_smtp_class
) -> None:
    """Null template fields fall back to the default, still personalized."""
    conv, email = _conversation_with_email(db, "n@example.com", display_name="Nina")
    state = db.get(SystemState, 1)
    assert state is not None
    state.ack_content_cn = None
    state.ack_content_en = None
    db.commit()

    service = AcknowledgmentService(
        db,
        settings,
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        replier=ReplierService(db, settings, MockLLMClient(settings)),
    )
    service.send_for_email(email, conv)

    body = _plain_body(FakeSMTP.instances[0].sent[0])
    assert body == ACK_CONTENT_EN.replace("{customer_name}", "Nina")


def test_ack_default_with_no_customer_name_falls_back_to_there(
    db, settings, fake_smtp_class
) -> None:
    """Empty display_name degrades to 'there' instead of a dangling comma."""
    conv, email = _conversation_with_email(db, "x@example.com", display_name="")
    service = AcknowledgmentService(
        db,
        settings,
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        replier=ReplierService(db, settings, MockLLMClient(settings)),
    )
    service.send_for_email(email, conv)

    body = _plain_body(FakeSMTP.instances[0].sent[0])
    assert body.startswith("Hi there,")


def _ack_service(db, settings) -> AcknowledgmentService:
    return AcknowledgmentService(
        db,
        settings,
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        replier=ReplierService(db, settings, MockLLMClient(settings)),
    )


def _acks(db, conv) -> list[Reply]:
    return db.execute(
        select(Reply)
        .where(
            Reply.conversation_id == conv.id,
            Reply.reply_type == "acknowledgment",
        )
        .order_by(Reply.id)
    ).scalars().all()


def _second_email(db, conv, msg_id: str, body: str) -> Email:
    row = Email(
        conversation_id=conv.id,
        message_id=f"<{msg_id}>",
        subject="Follow up",
        from_email=conv.customer.email,
        to_email="bot@example.com",
        body_text=body,
        is_inbound=True,
        received_at=utcnow(),
        summary_cn="追问邮件",
    )
    db.add(row)
    db.commit()
    return row


def _backdate_first_ack(db, conv, hours: int = 25) -> None:
    first_ack = db.execute(
        select(Reply)
        .where(
            Reply.conversation_id == conv.id,
            Reply.reply_type == "acknowledgment",
        )
        .order_by(Reply.id)
        .limit(1)
    ).scalars().first()
    assert first_ack is not None
    first_ack.sent_at = utcnow() - timedelta(hours=hours)
    db.commit()


def test_ack_no_followup_before_24h(db, settings, fake_smtp_class) -> None:
    """Same-day follow-up is not "waiting" yet: no second ack under 24h."""
    conv, first_email = _conversation_with_email(db, "soon@example.com")
    second = _second_email(db, conv, "soon-2@example.com", "Quick note.")
    service = _ack_service(db, settings)

    service.send_for_email(first_email, conv)
    service.send_for_email(second, conv)  # immediately, < 24h

    assert len(_acks(db, conv)) == 1
    assert len(FakeSMTP.instances[0].sent) == 1


def test_ack_followup_sent_after_24h_without_real_reply(
    db, settings, fake_smtp_class
) -> None:
    """Second mail >24h after the first ack, no real reply yet, gets a
    follow-up ack so the customer is not left in silence while waiting."""
    conv, first_email = _conversation_with_email(
        db, "follow@example.com", display_name="F"
    )
    second = _second_email(db, conv, "follow-2@example.com", "Still waiting.")
    service = _ack_service(db, settings)

    service.send_for_email(first_email, conv)
    _backdate_first_ack(db, conv)
    service.send_for_email(second, conv)

    acks = _acks(db, conv)
    assert len(acks) == 2
    assert acks[1].status == "sent"
    # Each mailer.send() opens its own SMTP connection -> messages are spread
    # across FakeSMTP instances; collect them all.
    sent_msgs = [m for inst in FakeSMTP.instances for m in inst.sent]
    assert len(sent_msgs) == 2
    body = _plain_body(sent_msgs[-1])
    assert body == ACK_FOLLOWUP_CONTENT_EN.replace("{customer_name}", "F")


def test_ack_no_followup_when_real_reply_sent(
    db, settings, fake_smtp_class
) -> None:
    """A real reply was already sent -> the wait is over, no follow-up ack."""
    conv, first_email = _conversation_with_email(
        db, "answered@example.com", display_name="A"
    )
    second = _second_email(db, conv, "answered-2@example.com", "Still waiting.")
    db.add(
        Reply(
            conversation_id=conv.id,
            email_id=first_email.id,
            message_id="<answered-reply@example.com>",
            content_en="We are on it.",
            status="sent",
            reply_type="general",
            created_at=utcnow(),
            sent_at=utcnow(),
        )
    )
    db.commit()
    service = _ack_service(db, settings)

    service.send_for_email(first_email, conv)  # first ack
    _backdate_first_ack(db, conv)
    service.send_for_email(second, conv)

    assert len(_acks(db, conv)) == 1
    assert len(FakeSMTP.instances[0].sent) == 1


def test_ack_followup_capped_at_two(db, settings, fake_smtp_class) -> None:
    """The per-conversation ack cap stops a third ack from being sent."""
    conv, first_email = _conversation_with_email(db, "cap@example.com", display_name="K")
    second = _second_email(db, conv, "cap-2@example.com", "Still waiting 1.")
    third = _second_email(db, conv, "cap-3@example.com", "Still waiting 2.")
    service = _ack_service(db, settings)

    service.send_for_email(first_email, conv)  # ack #1
    _backdate_first_ack(db, conv)
    service.send_for_email(second, conv)  # follow-up ack #2

    # Backdate the newest ack: the third mail must still not trigger a #3.
    newest = db.execute(
        select(Reply)
        .where(
            Reply.conversation_id == conv.id,
            Reply.reply_type == "acknowledgment",
        )
        .order_by(Reply.sent_at.desc())
        .limit(1)
    ).scalars().first()
    newest.sent_at = utcnow() - timedelta(hours=25)
    db.commit()

    service.send_for_email(third, conv)

    assert len(_acks(db, conv)) == 2
    sent_msgs = [m for inst in FakeSMTP.instances for m in inst.sent]
    assert len(sent_msgs) == 2
