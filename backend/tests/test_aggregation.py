"""Aggregated auto-send tests (PRD F2 / edge case 3, TECH M-07).

A customer sending several low-risk mails in one poll window must receive ONE
reply that covers every question - no reply spam. Non-auto-send branches
(high-risk, retention, review, manual, silence) keep per-mail semantics.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.exceptions import LLMError
from app.llm.client import MockLLMClient
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.services.ingest import IngestService
from app.services.mailer import MailerService

from conftest import FakeIMAP, FakeSMTP, make_raw_email


class RecordingLLM(MockLLMClient):
    """Captures user prompt contents for aggregation assertions."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.user_contents: list[str] = []

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        for message in messages:
            if message.get("role") == "user":
                self.user_contents.append(message.get("content", ""))
        return super().chat(messages, system_prompt, max_tokens, temperature)


class FailingGenerationLLM(MockLLMClient):
    """Classification succeeds; aggregated generation always fails."""

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        if "risk_level" in (system_prompt or "").lower():
            return super().chat(messages, system_prompt, max_tokens, temperature)
        raise LLMError("simulated generation failure")


def _service(db, settings, imap, llm=None, smtp_class=None) -> IngestService:
    return IngestService(
        db,
        settings,
        llm_client=llm or MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=smtp_class or FakeSMTP),
        imap=imap,
    )


def _raw(uid: str, body: str, message_id: str) -> tuple[str, bytes]:
    return (
        uid,
        make_raw_email(
            subject="Product size question",
            body=body,
            message_id=message_id,
        ),
    )


def test_two_low_risk_mails_one_aggregated_reply(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1, raw2 = (
        _raw("1", "What is the XL size in centimeters?", "<agg-1@example.com>"),
        _raw("2", "And what colors are available?", "<agg-2@example.com>"),
    )
    imap = fake_imap([raw1, raw2])
    llm = RecordingLLM(settings)
    summary = _service(db, settings, imap, llm=llm).fetch_and_process()

    assert summary["fetched"] == 2
    assert summary["auto_sent"] == 1  # ONE reply, no spam
    assert summary["failed"] == 0

    emails = db.execute(select(Email).order_by(Email.id)).scalars().all()
    assert len(emails) == 2
    assert emails[0].conversation_id == emails[1].conversation_id

    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1
    reply = replies[0]
    assert reply.status == "sent"
    assert reply.in_reply_to == "agg-2@example.com"  # newest email in the batch
    assert len(FakeSMTP.instances[0].sent) == 1
    # The server \Seen flag is never touched: webmail keeps the boss's unread
    # state. Processed mail is skipped next poll by its persisted imap_uid.
    assert imap.seen == []
    assert {e.imap_uid for e in emails} == {"1", "2"}

    # The LLM prompt contains BOTH questions (aggregation, not last-mail-only).
    user_content = "\n".join(llm.user_contents)
    assert "What is the XL size in centimeters?" in user_content
    assert "And what colors are available?" in user_content


def test_different_conversations_get_one_reply_each(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Product size question",
        body="What is the XL size?",
        message_id="<topic-a@example.com>",
    )
    raw2 = make_raw_email(
        subject="Color question",
        body="What colors are available for this product?",
        message_id="<topic-b@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["auto_sent"] == 2  # one per conversation
    assert len(db.execute(select(Reply)).scalars().all()) == 2


def test_high_plus_low_same_conversation_blocks_low_auto_send(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Product issue",
        body="I will file a chargeback with my bank.",
        message_id="<mix-high@example.com>",
    )
    raw2 = make_raw_email(
        subject="Product issue",
        body="Also, what is the XL size?",
        message_id="<mix-low@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    summary = _service(db, settings, imap).fetch_and_process()

    # High-risk keeps its per-mail reassurance; the low-risk mail must not be
    # auto-answered while the same conversation has an open high-risk ticket.
    assert summary["reassured"] == 1
    assert summary["manual"] == 1
    reply_types = {r.reply_type for r in db.execute(select(Reply)).scalars().all()}
    assert reply_types == {"reassurance"}


def test_smtp_failure_keeps_aggregated_reply_and_retries_same_draft(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    FakeSMTP.reset(fail_remaining=10)
    raw1, raw2 = (
        _raw("1", "What is the XL size?", "<smtp-agg-1@example.com>"),
        _raw("2", "And the colors?", "<smtp-agg-2@example.com>"),
    )
    imap = fake_imap([raw1, raw2])
    llm = RecordingLLM(settings)
    service = _service(db, settings, imap, llm=llm)

    summary = service.fetch_and_process()
    assert summary["failed"] == 1
    assert summary["auto_sent"] == 0
    # Nothing is flagged \Seen on the server; instead the newest mail's
    # persisted UID is cleared so the next poll re-fetches it for the retry.
    assert imap.seen == []
    rows = db.execute(select(Email).order_by(Email.id)).scalars().all()
    assert {r.imap_uid for r in rows} == {"1", None}

    reply = db.execute(select(Reply)).scalar_one()
    assert reply.status == "failed"
    assert reply.in_reply_to == "smtp-agg-2@example.com"

    FakeSMTP.reset(fail_remaining=0)
    summary2 = service.fetch_and_process()
    assert summary2["fetched"] == 1  # only the cleared uid is re-fetched
    assert summary2["auto_sent"] == 1
    assert imap.fetched == ["1", "2", "2"]
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1  # same draft, no regeneration
    assert replies[0].status == "sent"
    aggregated_prompts = [
        c for c in llm.user_contents if c.strip().startswith("[customer]")
    ]
    assert len(aggregated_prompts) == 1  # generation ran exactly once


def test_generation_failure_removes_batch_and_retries_next_poll(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1, raw2 = (
        _raw("1", "What is the XL size?", "<llm-agg-1@example.com>"),
        _raw("2", "And the colors?", "<llm-agg-2@example.com>"),
    )
    imap = fake_imap([raw1, raw2])
    service = _service(db, settings, imap, llm=FailingGenerationLLM(settings))

    summary = service.fetch_and_process()
    assert summary["failed"] == 1
    assert imap.seen == []  # nothing answered; full retry next poll
    assert db.execute(select(Email)).scalars().all() == []  # batch rolled back
    # No empty batch-created conversation or dangling classified audits.
    assert db.execute(select(Conversation)).scalars().all() == []
    classified = db.execute(
        select(AuditLog).where(AuditLog.action == "classified")
    ).scalars().all()
    assert classified == []
    actions = {a.action for a in db.execute(select(AuditLog)).scalars().all()}
    assert "aggregate_reply_failed" in actions

    # Next poll regenerates and succeeds.
    service.replier.llm_client = MockLLMClient(settings)
    summary2 = service.fetch_and_process()
    assert summary2["auto_sent"] == 1
    assert len(db.execute(select(Email)).scalars().all()) == 2
    assert len(db.execute(select(Conversation)).scalars().all()) == 1
    assert db.execute(select(Reply)).scalars().one().status == "sent"
    assert imap.seen == []  # server \Seen is never written by the poll
