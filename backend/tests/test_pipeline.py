"""End-to-end Phase-1 pipeline tests (fake IMAP + fake SMTP + mock LLM)."""

from __future__ import annotations

from sqlalchemy import select

from app.llm.client import MockLLMClient
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.services.ingest import IngestService
from app.services.mailer import MailerService

from conftest import FakeIMAP, FakeSMTP, make_raw_email


def _service(db, settings, imap, smtp_class=None) -> IngestService:
    return IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=smtp_class or FakeSMTP),
        imap=imap,
    )


def test_low_risk_auto_reply_end_to_end(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Product size question",
        body="Hi, what is the length of the XL t-shirt in centimeters?",
        message_id="<e2e-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap, FakeSMTP).fetch_and_process()

    assert summary["auto_sent"] == 1
    assert summary["fetched"] == 1
    assert imap.seen == ["1"]

    email = db.execute(select(Email)).scalar_one()
    assert email.message_id == "e2e-1@example.com"
    assert email.risk_level == "low"
    assert email.category == "product_spec"
    assert email.summary_cn

    conversation = db.get(Conversation, email.conversation_id)
    assert conversation.risk_level == "low"
    assert conversation.status == "open"

    reply = db.execute(select(Reply)).scalar_one()
    assert reply.status == "sent"
    assert reply.in_reply_to == "e2e-1@example.com"
    assert "Thank you" in reply.content_en
    sent_msg = FakeSMTP.instances[0].sent[0]
    assert sent_msg["In-Reply-To"] == "e2e-1@example.com"

    actions = {a.action for a in db.execute(select(AuditLog)).scalars().all()}
    assert {"classified", "reply_sent"} <= actions


def test_chargeback_email_not_auto_replied(db, settings, fake_smtp_class, fake_imap) -> None:
    raw = make_raw_email(
        subject="I am filing a dispute",
        body="I will file a chargeback with my bank if you don't refund me today.",
        message_id="<e2e-high@example.com>",
    )
    imap = fake_imap([("7", raw)])
    summary = _service(db, settings, imap, FakeSMTP).fetch_and_process()

    assert summary["manual"] == 1
    email = db.execute(select(Email)).scalar_one()
    assert email.risk_level == "high"
    conversation = db.get(Conversation, email.conversation_id)
    assert conversation.risk_level == "high"
    assert db.execute(select(Reply)).scalars().all() == []


def test_refund_request_not_auto_replied_in_phase1(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Refund",
        body="Please refund my order #123, it does not fit.",
        message_id="<e2e-refund@example.com>",
    )
    imap = fake_imap([("3", raw)])
    summary = _service(db, settings, imap, FakeSMTP).fetch_and_process()
    assert summary["manual"] == 1
    email = db.execute(select(Email)).scalar_one()
    assert email.category == "refund_request"
    assert db.execute(select(Reply)).scalars().all() == []


def test_paused_system_fetches_but_does_not_process(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    state = db.get(SystemState, 1)
    state.ai_paused = True
    db.commit()

    raw = make_raw_email(message_id="<paused-1@example.com>")
    imap = fake_imap([("9", raw)])
    summary = _service(db, settings, imap, FakeSMTP).fetch_and_process()

    assert summary["paused"] == 1
    assert db.execute(select(Email)).scalars().all() == []
    assert imap.seen == []  # stays UNSEEN, processed after resume
    actions = {a.action for a in db.execute(select(AuditLog)).scalars().all()}
    assert "paused_skipped" in actions


def test_send_failure_marks_reply_failed_and_keeps_email_unseen(
    db, settings, fake_imap
) -> None:
    FakeSMTP.reset(fail_remaining=99)
    raw = make_raw_email(message_id="<fail-1@example.com>")
    imap = fake_imap([("5", raw)])
    summary = _service(db, settings, imap, FakeSMTP).fetch_and_process()

    assert summary["failed"] == 1
    reply = db.execute(select(Reply)).scalar_one()
    assert reply.status == "failed"
    assert reply.send_error
    assert imap.seen == []  # retry on the next poll cycle


def test_failed_reply_is_resent_without_regeneration(
    db, settings, fake_imap
) -> None:
    FakeSMTP.reset(fail_remaining=99)
    raw = make_raw_email(message_id="<resend-1@example.com>")
    imap = fake_imap([("4", raw)])
    service = _service(db, settings, imap, FakeSMTP)

    first = service.fetch_and_process()
    assert first["failed"] == 1
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1
    assert replies[0].status == "failed"
    original_content = replies[0].content_en

    FakeSMTP.reset(fail_remaining=0)
    second = service.fetch_and_process()
    assert second["auto_sent"] == 1
    assert second["fetched"] == 1

    db.expire_all()
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1  # reused draft, no LLM regeneration / new row
    assert replies[0].status == "sent"
    assert replies[0].content_en == original_content
    assert imap.seen == ["4"]


def test_duplicate_uid_second_cycle_skipped(db, settings, fake_smtp_class, fake_imap) -> None:
    raw = make_raw_email(message_id="<dup-1@example.com>")
    imap = fake_imap([("2", raw)])
    service = _service(db, settings, imap, FakeSMTP)
    first = service.fetch_and_process()
    second = service.fetch_and_process()
    assert first["auto_sent"] == 1
    assert second["fetched"] == 0  # already marked SEEN
