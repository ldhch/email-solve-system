"""PRD edge cases 1-22, E2E (Phase 4 completion criterion).

Reuses FakeSMTP / FakeIMAP / MockLLM and httpx.ASGITransport; no browser or
real network involved.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.exceptions import IMAPError, LLMError
from app.llm.client import MockLLMClient
from app.models.audit import AuditLog
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.services.audit import utcnow
from app.services.ingest import IngestService
from app.services.mailer import MailerService
from app.services.replier import UNCONFIRMED_MARKER
from app.services.scheduler import SchedulerService

from api_helpers import api, close_client, login, make_client, seed_owner
from conftest import FakeIMAP, FakeSMTP, make_raw_email


# ---------- helpers ----------


class FailingLLM(MockLLMClient):
    def chat(self, *args, **kwargs) -> str:
        raise LLMError("simulated LLM outage")


class CountingLLM(MockLLMClient):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.calls = 0

    def chat(self, *args, **kwargs) -> str:
        self.calls += 1
        raise LLMError("simulated LLM outage")


class LowConfidenceLLM(MockLLMClient):
    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        if "risk_level" in (system_prompt or "").lower():
            return json.dumps(
                {
                    "risk_level": "medium",
                    "confidence": 0.2,
                    "category": "other",
                    "chargeback_risk": False,
                    "summary_cn": "低置信度需人工核查",
                }
            )
        return super().chat(messages, system_prompt, max_tokens, temperature)


class MarkerLLM(MockLLMClient):
    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        if "risk_level" in (system_prompt or "").lower():
            return super().chat(messages, system_prompt, max_tokens, temperature)
        return f"Thank you for your message. {UNCONFIRMED_MARKER}"


class CapturingLLM(MockLLMClient):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.prompts: list[str] = []

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        if system_prompt:
            self.prompts.append(system_prompt)
        return super().chat(messages, system_prompt, max_tokens, temperature)


class ToggleIMAP:
    """Raises for the first N cycles, then delegates to a real FakeIMAP."""

    def __init__(self, inner: FakeIMAP, fail_cycles: int) -> None:
        self.inner = inner
        self.fail_cycles = fail_cycles

    def uid(self, *args):
        if self.fail_cycles > 0:
            self.fail_cycles -= 1
            raise ConnectionError("simulated IMAP outage")
        return self.inner.uid(*args)


def _service(db, settings, imap, llm=None, smtp_class=None) -> IngestService:
    return IngestService(
        db,
        settings,
        llm_client=llm or MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=smtp_class or FakeSMTP),
        imap=imap,
    )


def _capture_alerts(monkeypatch) -> list[tuple[str, str]]:
    from app.services import alerting

    alerts: list[tuple[str, str]] = []

    def fake_send(self, title, message):
        alerts.append((title, message))
        return {"bark": True, "email": True}

    monkeypatch.setattr(alerting.AlertingService, "send_alert", fake_send)
    return alerts


def _audit_actions(db) -> set[str]:
    return {a.action for a in db.execute(select(AuditLog)).scalars().all()}


def make_raw_email_with_attachment(
    subject: str,
    body: str,
    message_id: str,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "John Smith <customer@example.com>"
    msg["To"] = "bot@example.com"
    msg["Message-ID"] = message_id
    msg["Date"] = "Tue, 12 Aug 2026 10:00:00 +0000"
    msg.set_content(body)
    msg.add_attachment(
        b"fake-image-bytes", maintype="image", subtype="png", filename="screenshot.png"
    )
    return bytes(msg)


# ---------- scenario 1: IMAP login failure ----------


def test_exception_01_imap_login_failure_retries_then_alerts(
    db, settings, monkeypatch, fake_smtp_class
) -> None:
    alerts = _capture_alerts(monkeypatch)
    attempts = {"n": 0}

    class BrokenIMAP4:
        def __init__(self, *args, **kwargs) -> None:
            attempts["n"] += 1
            raise ConnectionError("simulated login failure")

    import app.services.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.imaplib, "IMAP4_SSL", BrokenIMAP4)
    service = _service(db, settings, imap=None)
    for _ in range(3):  # 3 failed poll cycles
        with pytest.raises(IMAPError):
            service.fetch_and_process()
    assert attempts["n"] == 9  # 3 connect retries per cycle
    assert len(alerts) == 1
    assert "IMAP" in alerts[0][0]


def test_exception_01b_imap_recovers_and_processes_queue(
    db, settings, monkeypatch, fake_smtp_class
) -> None:
    alerts = _capture_alerts(monkeypatch)
    raw1 = make_raw_email(
        subject="Product size question",
        body="What is the XL size?",
        message_id="<queued-1@example.com>",
    )
    raw2 = make_raw_email(
        subject="Product size question",
        body="And the color options?",
        message_id="<queued-2@example.com>",
    )
    inner = FakeIMAP([("1", raw1), ("2", raw2)])
    toggle = ToggleIMAP(inner, fail_cycles=3)
    service = _service(db, settings, imap=toggle)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            service.fetch_and_process()
    summary = service.fetch_and_process()
    # PRD edge case 3 / F2: both queued mails belong to one conversation and
    # are answered by ONE aggregated reply (no reply spam).
    assert summary["fetched"] == 2
    assert summary["auto_sent"] == 1
    assert len(db.execute(select(Reply)).scalars().all()) == 1
    assert len(alerts) == 1  # alerted once after 3 consecutive failures


# ---------- scenario 2: LLM API failure ----------


def test_exception_02_llm_retries_then_degrades_and_alerts(
    db, settings, fake_smtp_class, fake_imap, monkeypatch
) -> None:
    settings.llm_retries = 2
    alerts = _capture_alerts(monkeypatch)
    raw = make_raw_email(
        subject="Question",
        body="What is the size?",
        message_id="<llm-fail@example.com>",
    )
    imap = fake_imap([("1", raw)])
    counting = CountingLLM(settings)
    summary = _service(db, settings, imap, llm=counting).fetch_and_process()

    assert counting.calls == 3  # 1 initial + 2 retries (PRD F10)
    assert summary["failed"] == 1
    assert imap.seen == []  # stays UNSEEN: retried by the next poll
    assert "pipeline_failed" in _audit_actions(db)
    assert len(alerts) == 0  # 3 failures < threshold 5


def test_exception_02b_five_consecutive_llm_failures_alert(
    db, settings, fake_smtp_class, fake_imap, monkeypatch
) -> None:
    settings.llm_retries = 0
    alerts = _capture_alerts(monkeypatch)
    raws = [
        make_raw_email(
            subject=f"Q{i}",
            body="What is the size?",
            message_id=f"<llm-fail-{i}@example.com>",
        )
        for i in range(5)
    ]
    imap = fake_imap([(str(i), raw) for i, raw in enumerate(raws)])
    summary = _service(db, settings, imap, llm=FailingLLM(settings)).fetch_and_process()
    assert summary["failed"] == 5
    assert len(alerts) == 1
    assert "LLM" in alerts[0][0]
    assert "simulated LLM outage" in alerts[0][1]


# ---------- scenario 3: multiple mails merge, risk takes max ----------


def test_exception_03_multiple_mails_merge_with_max_risk(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Return request",
        body="Hi, I want to return the XL shirt I bought.",
        message_id="<merge-1@example.com>",
    )
    raw2 = make_raw_email(
        subject="Return request",
        body="I will file a chargeback with my bank if you don't refund me now.",
        message_id="<merge-2@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    summary = _service(db, settings, imap).fetch_and_process()

    emails = db.execute(select(Email).order_by(Email.id)).scalars().all()
    assert len(emails) == 2
    assert emails[0].conversation_id == emails[1].conversation_id
    conv = db.get(Conversation, emails[0].conversation_id)
    assert conv.risk_level == "high"  # conservative: keep the highest risk
    assert summary["fetched"] == 2


# ---------- scenario 4: unrelated topics -> independent conversations ----------


def test_exception_04_different_topics_are_separate_conversations(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Product size question",
        body="What size is the XL shirt?",
        message_id="<topic-1@example.com>",
    )
    raw2 = make_raw_email(
        subject="Invoice request",
        body="Please send me an invoice for my order.",
        message_id="<topic-2@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    _service(db, settings, imap).fetch_and_process()
    conversation_ids = {
        e.conversation_id for e in db.execute(select(Email)).scalars().all()
    }
    assert len(conversation_ids) == 2


# ---------- scenario 5: attachments saved and downloadable ----------


def test_exception_05_attachment_saved_and_downloadable(
    db, settings, fake_smtp_class, fake_imap, session_factory
) -> None:
    Path(settings.attachment_dir).mkdir(parents=True, exist_ok=True)
    raw = make_raw_email_with_attachment(
        subject="Photo attached",
        body="Please see the attached photo of the product.",
        message_id="<attach-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["auto_sent"] == 1

    attachment = db.execute(select(Attachment)).scalar_one()
    assert attachment.filename == "screenshot.png"
    assert attachment.content_type == "image/png"
    stored = Path(settings.attachment_dir).parent / attachment.stored_path
    assert stored.read_bytes() == b"fake-image-bytes"

    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "GET", f"/api/v1/attachments/{attachment.id}")
        assert resp.status_code == 200
        assert resp.content == b"fake-image-bytes"
    finally:
        close_client(client)


# ---------- scenario 6: empty / unreadable mail -> manual, marked suspicious ----------


def test_exception_06_empty_mail_goes_to_manual(db, settings, fake_smtp_class, fake_imap) -> None:
    raw = make_raw_email(
        subject="(no subject)",
        body="   ",
        message_id="<empty-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["manual"] == 1
    email = db.execute(select(Email)).scalar_one()
    assert email.risk_level == "unknown"
    assert "可疑" in (email.summary_cn or "")
    ack = db.execute(
        select(Reply).where(Reply.reply_type == "acknowledgment")
    ).scalars().one()
    assert ack.status == "sent"
    assert "requires_manual" in _audit_actions(db)
    # The manual mail is tracked by its persisted UID, never flagged seen.
    assert imap.seen == []
    assert email.imap_uid == "1"


# ---------- scenario 7: poor-language / unclassifiable -> manual ----------


def test_exception_07_low_confidence_downgraded_to_manual(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="pls hlp",
        body="me want thing fix soon ok",
        message_id="<confused-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap, llm=LowConfidenceLLM(settings)).fetch_and_process()
    # Low-confidence mail is never auto-sent; the readable body still gets a
    # low-confidence draft the boss can approve, not a blank manual item.
    assert summary["manual"] == 0
    assert summary["review"] == 1
    email = db.execute(select(Email)).scalar_one()
    assert email.risk_level == "unknown"
    reply = db.execute(
        select(Reply).where(Reply.status == "pending_review")
    ).scalars().one()
    assert reply.status == "pending_review"
    assert reply.low_confidence is True


# ---------- scenario 8: follow-up after reassurance ----------


def test_exception_08_followup_merges_into_original_ticket(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Dispute",
        body="I will file a chargeback with my bank.",
        message_id="<dispute-1@example.com>",
    )
    raw2 = make_raw_email(
        subject="Dispute",
        body="I am still waiting. My lawyer will contact you.",
        message_id="<dispute-2@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    summary = _service(db, settings, imap).fetch_and_process()

    assert summary["reassured"] == 1
    assert summary["followup"] == 1
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1  # no duplicate reassurance
    assert replies[0].reply_type == "reassurance"
    tickets = db.execute(select(Ticket)).scalars().all()
    assert len(tickets) == 1  # no duplicate ticket
    assert "high_risk_followup" in _audit_actions(db)


# ---------- scenario 9: "do not contact" silence ----------


def test_exception_09_silence_request_honored_for_72h(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw1 = make_raw_email(
        subject="Stop emailing me",
        body="Please do not email me again. I will handle this myself.",
        message_id="<silence-1@example.com>",
    )
    imap = fake_imap([("1", raw1)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["silenced"] == 1
    customer = db.execute(select(Customer)).scalar_one()
    assert customer.silenced_until is not None
    assert customer.silenced_until > utcnow()
    assert db.execute(select(Reply)).scalars().all() == []
    assert "silenced_set" in _audit_actions(db)

    raw2 = make_raw_email(
        subject="Size question",
        body="What size is XL?",
        message_id="<silence-2@example.com>",
    )
    imap2 = fake_imap([("2", raw2)])
    summary2 = _service(db, settings, imap2).fetch_and_process()
    assert summary2["silenced"] == 1
    assert db.execute(select(Reply)).scalars().all() == []


# ---------- scenario 10: threats -> forced escalation, no promises ----------


def test_exception_10_threats_escalate_without_promises(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Bad review",
        body="I will leave a terrible review and complain to the BBB.",
        message_id="<threat-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["reassured"] == 1
    reply = db.execute(select(Reply)).scalar_one()
    assert reply.reply_type == "reassurance"
    lowered = reply.content_en.lower()
    assert "24 hours" in lowered
    assert "refund" not in lowered
    assert "compensation" not in lowered
    assert db.execute(select(Ticket)).scalars().one().risk_level == "high"


# ---------- scenario 11: no ERP/KB data -> ask for order number, never invent ----------


def test_exception_11_no_order_data_no_fabrication(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Product size question",
        body="What is the size of the XL shirt?",
        message_id="<warranty-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    llm = CapturingLLM(settings)
    summary = _service(db, settings, imap, llm=llm).fetch_and_process()
    assert summary["auto_sent"] == 1
    prompts = " ".join(llm.prompts)
    assert "order number" in prompts
    assert "never invent" in prompts
    reply = db.execute(select(Reply)).scalar_one()
    assert not re.search(r"order\s*#?\s*\d{5,}", reply.content_en, re.IGNORECASE)


# ---------- scenario 12: SLA overdue -> scheduler alert ----------


def test_exception_12_sla_overdue_alerts(
    db, session_factory, settings, monkeypatch
) -> None:
    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = Conversation(
        customer_id=customer.id,
        subject_normalized="dispute",
        window_end=utcnow() + timedelta(days=7),
        last_activity_at=utcnow() - timedelta(days=1),
        status="open",
    )
    db.add(conv)
    db.flush()
    ticket = Ticket(
        conversation_id=conv.id,
        summary_cn="客户投诉",
        risk_level="high",
        status="pending",
        sla_deadline=utcnow() - timedelta(hours=1),
        created_at=utcnow() - timedelta(days=1),
    )
    db.add(ticket)
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    SchedulerService(settings, session_factory=session_factory)._job_sla_overdue_scan()
    assert len(alerts) == 1
    assert "SLA" in alerts[0][0]
    assert "sla_overdue" in _audit_actions(db)


# ---------- scenario 14: empty KB/QA -> generic reply with marker ----------


def test_exception_14_no_kb_no_qa_uses_fallback_marker(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Product size question",
        body="What is the size of the XL shirt?",
        message_id="<nokb-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap, llm=MarkerLLM(settings)).fetch_and_process()
    assert summary["auto_sent"] == 1
    reply = db.execute(select(Reply)).scalar_one()
    assert UNCONFIRMED_MARKER in reply.content_en
    from app.models.knowledge_doc import KnowledgeDoc

    assert db.execute(select(KnowledgeDoc)).scalars().all() == []


# ---------- scenario 16: duplicate Message-ID dedupe ----------


def test_exception_16_duplicate_message_id_never_resent(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Product size question",
        body="What is the size of the XL shirt?",
        message_id="<dup-1@example.com>",
    )
    imap = fake_imap([("1", raw), ("2", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["auto_sent"] == 1
    assert summary["duplicate"] == 1
    assert len(FakeSMTP.instances[0].sent) == 1
    assert "duplicate_skipped" in _audit_actions(db)


# ---------- scenario 17: SMTP failure -> failed + retried on next poll ----------


def test_exception_17_smtp_failure_queues_and_retries_without_regeneration(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Product size question",
        body="What is the size of the XL shirt?",
        message_id="<smtp-1@example.com>",
    )
    FakeSMTP.reset(fail_remaining=10)
    imap = fake_imap([("1", raw)])
    service = _service(db, settings, imap)
    summary = service.fetch_and_process()
    assert summary["failed"] == 1
    assert imap.seen == []  # stays UNSEEN (待发送队列 = failed reply + UNSEEN mail)
    reply = db.execute(select(Reply)).scalar_one()
    assert reply.status == "failed"
    assert reply.send_error

    FakeSMTP.reset(fail_remaining=0)
    summary2 = service.fetch_and_process()
    assert summary2["auto_sent"] == 1
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1  # same draft, no regeneration
    assert replies[0].status == "sent"
    assert replies[0].sent_at is not None


# ---------- scenario 19: chargeback never enters retention ----------


def test_exception_19_chargeback_never_retention(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Refund",
        body="I want a refund, otherwise I will file a dispute with my credit card company.",
        message_id="<cb-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["reassured"] == 1
    replies = db.execute(select(Reply)).scalars().all()
    assert len(replies) == 1
    assert replies[0].reply_type == "reassurance"
    assert db.execute(select(Ticket)).scalars().one().status == "pending"


# ---------- scenario 20: customer rejects retention -> release within limit ----------


def test_exception_20_rejected_retention_releases_return(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    settings.retention_max_attempts = 1
    raw1 = make_raw_email(
        subject="Wrong size",
        body="I bought the XL shirt but it is too small, I want to exchange it.",
        message_id="<ret-1@example.com>",
    )
    raw2 = make_raw_email(
        subject="Wrong size",
        body="No. The size is too small and I just want my money back.",
        message_id="<ret-2@example.com>",
    )
    imap = fake_imap([("1", raw1), ("2", raw2)])
    summary = _service(db, settings, imap).fetch_and_process()

    replies = db.execute(select(Reply).order_by(Reply.id)).scalars().all()
    assert [r.reply_type for r in replies] == ["retention_exchange", "retention_release"]
    assert replies[-1].status == "sent"
    assert summary["auto_sent"] == 2


# ---------- scenario 21: quality / damaged -> no retention ----------


def test_exception_21_quality_issue_no_retention(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    raw = make_raw_email(
        subject="Defective product",
        body="The product I received is defective and broken. I want a refund.",
        message_id="<quality-1@example.com>",
    )
    imap = fake_imap([("1", raw)])
    summary = _service(db, settings, imap).fetch_and_process()
    assert summary["auto_sent"] == 1
    reply = db.execute(select(Reply)).scalar_one()
    assert reply.reply_type == "retention_release"
    assert "retention_released" in _audit_actions(db)


# ---------- scenario 22: compensation draft review timeout ----------


def test_exception_22_compensation_timeout_alerts_and_auto_releases(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = Conversation(
        customer_id=customer.id,
        subject_normalized="refund",
        window_end=utcnow() + timedelta(days=7),
        last_activity_at=utcnow() - timedelta(days=2),
        status="open",
    )
    db.add(conv)
    db.flush()
    email = Email(
        conversation_id=conv.id,
        message_id="<comp-1@example.com>",
        subject="Refund",
        from_email="c@example.com",
        to_email="bot@example.com",
        body_text="I changed my mind, please refund.",
        is_inbound=True,
        received_at=utcnow() - timedelta(days=2),
    )
    db.add(email)
    db.flush()
    draft = Reply(
        conversation_id=conv.id,
        email_id=email.id,
        message_id="<comp-draft@example.com>",
        in_reply_to="<comp-1@example.com>",
        content_en="We can offer a small compensation to keep the item.",
        status="pending_review",
        reply_type="retention_compensation",
        created_at=utcnow() - timedelta(hours=25),
    )
    db.add(draft)
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    service = SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )
    service._job_retention_timeout_scan()
    service._job_retention_timeout_scan()

    assert any("补偿挽留" in title for title, _ in alerts)
    releases = db.execute(
        select(Reply).where(Reply.reply_type == "retention_release")
    ).scalars().all()
    assert len(releases) == 1
    assert releases[0].status == "sent"
    assert releases[0].in_reply_to == "<comp-1@example.com>"
    actions = _audit_actions(db)
    assert {"retention_timeout_alert", "retention_auto_released"} <= actions
    db.expire_all()
    assert db.get(Reply, draft.id).status == "superseded"

    # The boss can no longer approve the superseded compensation draft.
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "POST", f"/api/v1/replies/{draft.id}/approve")
        assert resp.status_code == 409
    finally:
        close_client(client)


def test_exception_22b_approve_rejected_when_newer_release_exists(
    settings, session_factory
) -> None:
    """Second line of defense: even a pending_review draft is rejected when
    the conversation already has a newer auto-release."""

    now = utcnow()
    with session_factory() as db:
        customer = Customer(
            email="c@example.com", display_name="C", created_at=now
        )
        db.add(customer)
        db.flush()
        conv = Conversation(
            customer_id=customer.id,
            subject_normalized="refund",
            window_end=now + timedelta(days=7),
            last_activity_at=now,
            status="open",
        )
        db.add(conv)
        db.flush()
        email = Email(
            conversation_id=conv.id,
            message_id="<m-x@example.com>",
            subject="Refund",
            from_email="c@example.com",
            to_email="bot@example.com",
            body_text="I want a refund.",
            is_inbound=True,
            received_at=now - timedelta(days=2),
        )
        db.add(email)
        db.flush()
        draft = Reply(
            conversation_id=conv.id,
            email_id=email.id,
            message_id="<draft-x@example.com>",
            in_reply_to="<m-x@example.com>",
            content_en="We can offer compensation.",
            status="pending_review",
            reply_type="retention_compensation",
            created_at=now - timedelta(hours=25),
        )
        release = Reply(
            conversation_id=conv.id,
            email_id=email.id,
            message_id="<release-x@example.com>",
            in_reply_to="<m-x@example.com>",
            content_en="We will process your return.",
            status="sent",
            reply_type="retention_release",
            created_at=now - timedelta(hours=1),
        )
        db.add_all([draft, release])
        db.commit()

    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "POST", f"/api/v1/replies/{draft.id}/approve")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "SUPERSEDED"
    finally:
        close_client(client)
