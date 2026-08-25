"""M-12 APScheduler job tests (auto-close, SLA, retention timeout, heartbeat)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.exceptions import LLMError
from app.llm.client import MockLLMClient
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.services.audit import utcnow
from app.services.scheduler import (
    SchedulerService,
    _alerted_retention_reply_ids,
    _alerted_sla_ticket_ids,
)

from conftest import FakeSMTP


class FlakyLLM(MockLLMClient):
    """Fails the first N chat calls, then behaves like the normal mock."""

    def __init__(self, settings, fail_calls: int = 3) -> None:
        super().__init__(settings)
        self.fail_calls = fail_calls
        self.calls = 0

    def chat(self, *args, **kwargs) -> str:
        self.calls += 1
        if self.calls <= self.fail_calls:
            raise LLMError("flaky LLM outage")
        return super().chat(*args, **kwargs)


def _capture_alerts(monkeypatch) -> list[tuple[str, str]]:
    from app.services import alerting

    alerts: list[tuple[str, str]] = []

    def fake_send(self, title, message):
        alerts.append((title, message))
        return {"bark": True, "email": True}

    monkeypatch.setattr(alerting.AlertingService, "send_alert", fake_send)
    return alerts


def _conversation(db, customer_id: int, last_activity_at) -> Conversation:
    conv = Conversation(
        customer_id=customer_id,
        subject_normalized="test subject",
        window_end=last_activity_at + timedelta(days=7),
        last_activity_at=last_activity_at,
        status="open",
    )
    db.add(conv)
    db.flush()
    return conv


def _stale_compensation_draft(db, from_email: str) -> tuple[Email, Reply]:
    """Seed one pending_review compensation draft older than 24h."""

    customer = Customer(
        email=from_email, display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow() - timedelta(days=2))
    email = Email(
        conversation_id=conv.id,
        message_id=f"<{from_email}-m@example.com>",
        subject="Refund",
        from_email=from_email,
        to_email="bot@example.com",
        body_text="I want a refund.",
        is_inbound=True,
        received_at=utcnow() - timedelta(days=2),
    )
    db.add(email)
    db.flush()
    draft = Reply(
        conversation_id=conv.id,
        email_id=email.id,
        message_id=f"<{from_email}-draft@example.com>",
        in_reply_to=f"<{from_email}-m@example.com>",
        content_en="We can offer compensation.",
        status="pending_review",
        reply_type="retention_compensation",
        created_at=utcnow() - timedelta(hours=25),
    )
    db.add(draft)
    db.flush()
    return email, draft


def test_auto_close_sessions_job(db, session_factory, settings) -> None:
    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    stale = _conversation(db, customer.id, utcnow() - timedelta(days=40))
    fresh = _conversation(db, customer.id, utcnow() - timedelta(days=1))
    db.commit()

    service = SchedulerService(settings, session_factory=session_factory)
    service._job_auto_close_sessions()

    db.expire_all()  # the job updated rows through another session
    assert db.get(Conversation, stale.id).status == "resolved"
    assert db.get(Conversation, fresh.id).status == "open"
    actions = {
        a.action
        for a in db.execute(
            select(AuditLog).where(AuditLog.resource_id == stale.id)
        ).scalars().all()
    }
    assert "auto_close" in actions


def test_auto_close_sessions_skipped_while_paused(
    db, session_factory, settings
) -> None:
    """Emergency pause makes the job inert: stale conversations are not
    auto-resolved while paused, and the gate releases again on resume."""

    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    stale = _conversation(db, customer.id, utcnow() - timedelta(days=40))
    db.commit()

    state = db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, ai_paused=True)
        db.add(state)
    else:
        state.ai_paused = True
    db.commit()

    service = SchedulerService(settings, session_factory=session_factory)
    service._job_auto_close_sessions()

    db.expire_all()
    assert db.get(Conversation, stale.id).status == "open"  # untouched while paused

    db.get(SystemState, 1).ai_paused = False
    db.commit()
    service._job_auto_close_sessions()

    db.expire_all()
    assert db.get(Conversation, stale.id).status == "resolved"  # gate released


def test_sla_overdue_scan_alerts_once_and_audits(
    db, session_factory, settings, monkeypatch
) -> None:
    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow())
    ticket = Ticket(
        conversation_id=conv.id,
        summary_cn="逾期工单",
        risk_level="high",
        status="pending",
        sla_deadline=utcnow() - timedelta(hours=2),
        created_at=utcnow() - timedelta(days=1),
    )
    db.add(ticket)
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_sla_overdue_scan()
    service._job_sla_overdue_scan()

    assert len(alerts) == 1
    assert "SLA" in alerts[0][0]
    audit_actions = [
        a.action
        for a in db.execute(
            select(AuditLog).where(AuditLog.resource_id == ticket.id)
        ).scalars().all()
    ]
    assert audit_actions.count("sla_overdue") == 1
    _alerted_sla_ticket_ids.clear()


def test_resolved_ticket_not_scanned(db, session_factory, settings, monkeypatch) -> None:
    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow())
    ticket = Ticket(
        conversation_id=conv.id,
        summary_cn="已解决",
        risk_level="high",
        status="resolved",
        sla_deadline=utcnow() - timedelta(hours=2),
        created_at=utcnow() - timedelta(days=1),
    )
    db.add(ticket)
    db.commit()
    alerts = _capture_alerts(monkeypatch)
    SchedulerService(settings, session_factory=session_factory)._job_sla_overdue_scan()
    assert alerts == []


def test_retention_timeout_alerts_and_auto_releases(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow() - timedelta(days=2))
    email = Email(
        conversation_id=conv.id,
        message_id="<m@example.com>",
        subject="Refund",
        from_email="c@example.com",
        to_email="bot@example.com",
        body_text="I want a refund.",
        is_inbound=True,
        received_at=utcnow() - timedelta(days=2),
    )
    db.add(email)
    db.flush()
    draft = Reply(
        conversation_id=conv.id,
        email_id=email.id,
        message_id="<draft@example.com>",
        in_reply_to="<m@example.com>",
        content_en="We can offer compensation.",
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
    assert len(releases) == 1  # second scan must not re-send
    assert releases[0].status == "sent"
    assert releases[0].in_reply_to == "<m@example.com>"
    db.expire_all()
    assert db.get(Reply, draft.id).status == "superseded"

    audit_actions = {a.action for a in db.execute(select(AuditLog)).scalars().all()}
    assert {"retention_timeout_alert", "retention_auto_released"} <= audit_actions
    _alerted_retention_reply_ids.clear()


def test_retention_timeout_skipped_while_paused(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    state = db.get(SystemState, 1)
    state.ai_paused = True
    db.commit()
    _stale_compensation_draft(db, "c@example.com")
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )._job_retention_timeout_scan()

    assert alerts == []
    drafts = db.execute(
        select(Reply).where(Reply.reply_type == "retention_compensation")
    ).scalars().all()
    assert len(drafts) == 1
    assert drafts[0].status == "pending_review"
    assert db.execute(select(Reply).where(Reply.reply_type == "retention_release")).scalars().all() == []
    _alerted_retention_reply_ids.clear()


def test_retention_timeout_respects_test_mode_whitelist(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = "allowed@example.com"
    db.commit()
    _stale_compensation_draft(db, "blocked@example.com")
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )._job_retention_timeout_scan()

    assert alerts == []
    drafts = db.execute(
        select(Reply).where(Reply.reply_type == "retention_compensation")
    ).scalars().all()
    assert len(drafts) == 1
    assert drafts[0].status == "pending_review"
    assert db.execute(select(Reply).where(Reply.reply_type == "retention_release")).scalars().all() == []
    _alerted_retention_reply_ids.clear()


def test_retention_timeout_empty_whitelist_skips_all(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = ""
    db.commit()
    _stale_compensation_draft(db, "c@example.com")
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )._job_retention_timeout_scan()

    assert alerts == []
    assert db.execute(select(Reply).where(Reply.reply_type == "retention_release")).scalars().all() == []
    _alerted_retention_reply_ids.clear()


def test_retention_timeout_allows_whitelisted_sender(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = "c@example.com"
    db.commit()
    _stale_compensation_draft(db, "c@example.com")
    db.commit()

    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )._job_retention_timeout_scan()

    assert any("补偿挽留" in title for title, _ in alerts)
    releases = db.execute(
        select(Reply).where(Reply.reply_type == "retention_release")
    ).scalars().all()
    assert len(releases) == 1
    assert releases[0].status == "sent"
    _alerted_retention_reply_ids.clear()


def test_recent_compensation_draft_not_scanned(
    db, session_factory, settings, monkeypatch
) -> None:
    customer = Customer(
        email="c@example.com", display_name="C", created_at=utcnow()
    )
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow())
    email = Email(
        conversation_id=conv.id,
        message_id="<m2@example.com>",
        subject="Refund",
        from_email="c@example.com",
        to_email="bot@example.com",
        body_text="I want a refund.",
        is_inbound=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.flush()
    draft = Reply(
        conversation_id=conv.id,
        email_id=email.id,
        message_id="<draft2@example.com>",
        in_reply_to="<m2@example.com>",
        content_en="We can offer compensation.",
        status="pending_review",
        reply_type="retention_compensation",
        created_at=utcnow() - timedelta(hours=1),
    )
    db.add(draft)
    db.commit()
    alerts = _capture_alerts(monkeypatch)
    SchedulerService(settings, session_factory=session_factory)._job_retention_timeout_scan()
    assert alerts == []
    assert db.execute(select(Reply)).scalars().all() == [draft]


def test_retention_timeout_one_failure_does_not_block_batch(
    db, session_factory, settings, monkeypatch, fake_smtp_class
) -> None:
    """A failing release for one draft must not lose the alert audit or
    block the remaining drafts (review finding #4)."""

    import app.services.scheduler as scheduler_mod

    drafts: list[Reply] = []
    for index in (1, 2):
        customer = Customer(
            email=f"c{index}@example.com", display_name=f"C{index}", created_at=utcnow()
        )
        db.add(customer)
        db.flush()
        conv = _conversation(db, customer.id, utcnow() - timedelta(days=2))
        email = Email(
            conversation_id=conv.id,
            message_id=f"<m{index}@example.com>",
            subject="Refund",
            from_email=f"c{index}@example.com",
            to_email="bot@example.com",
            body_text="I want a refund.",
            is_inbound=True,
            received_at=utcnow() - timedelta(days=2),
        )
        db.add(email)
        db.flush()
        draft = Reply(
            conversation_id=conv.id,
            email_id=email.id,
            message_id=f"<draft{index}@example.com>",
            in_reply_to=f"<m{index}@example.com>",
            content_en="We can offer compensation.",
            status="pending_review",
            reply_type="retention_compensation",
            created_at=utcnow() - timedelta(hours=25),
        )
        db.add(draft)
        db.flush()
        drafts.append(draft)
    db.commit()

    monkeypatch.setattr(
        scheduler_mod,
        "build_llm_client",
        lambda settings: FlakyLLM(settings, fail_calls=3),
    )
    SchedulerService(
        settings, session_factory=session_factory, smtp_class=FakeSMTP
    )._job_retention_timeout_scan()

    # First draft: generation failed -> no release, still pending_review.
    db.expire_all()
    first = db.get(Reply, drafts[0].id)
    assert first.status == "pending_review"
    # Second draft: processed normally -> release sent + draft superseded.
    second = db.get(Reply, drafts[1].id)
    assert second.status == "superseded"
    releases = db.execute(
        select(Reply).where(Reply.reply_type == "retention_release")
    ).scalars().all()
    assert len(releases) == 1
    assert releases[0].conversation_id == drafts[1].conversation_id

    # Both alert audits were committed before any release attempt, so the
    # failed draft's audit is not lost.
    alerts = db.execute(
        select(AuditLog).where(AuditLog.action == "retention_timeout_alert")
    ).scalars().all()
    assert {a.resource_id for a in alerts} == {drafts[0].id, drafts[1].id}
    _alerted_retention_reply_ids.clear()


def test_heartbeat_and_health(settings, session_factory) -> None:
    service = SchedulerService(settings, session_factory=session_factory)
    assert service.is_healthy() is False  # not running yet
    old = service.last_heartbeat_ts()
    service._job_heartbeat()
    assert service.last_heartbeat_ts() >= old

    service.start()
    try:
        assert service.running is True
        assert service.is_healthy() is True
    finally:
        service.shutdown()
    assert service.is_healthy() is False


def test_fetch_mail_job_invokes_ingest(settings, monkeypatch) -> None:
    calls = []

    class FakeFactory:
        def __call__(self):
            return self

        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    class FakeIngest:
        def __init__(self, db, settings, session_factory=None):
            calls.append((db, settings, session_factory))

        def fetch_and_process(self):
            return {"fetched": 1}

    import app.services.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "IngestService", FakeIngest)
    service = SchedulerService(settings, session_factory=FakeFactory())
    service._job_fetch_mail()
    assert len(calls) == 1


# ---------- full-text translation prefill (concurrent) ----------


import itertools

_message_seq = itertools.count(1)


def _inbound_email(db, customer_id: int, body: str) -> Email:
    """Seed one inbound email inside its own conversation."""
    conv = Conversation(
        customer_id=customer_id,
        subject_normalized="Re: test",
        window_end=utcnow() + timedelta(days=7),
        last_activity_at=utcnow(),
        status="open",
    )
    db.add(conv)
    db.flush()
    email = Email(
        conversation_id=conv.id,
        message_id=f"<prefill-{next(_message_seq)}>",
        subject="Re: test",
        from_email="c@example.com",
        to_email="support@shoplbora.com",
        body_text=body,
        is_inbound=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.flush()
    return email


def test_prefill_translations_writes_pending_inbound(
    db, session_factory, settings, monkeypatch
) -> None:
    import app.services.scheduler as scheduler_mod

    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    emails = [
        _inbound_email(db, customer.id, "Where is my order? #1"),
        _inbound_email(db, customer.id, "The hat is damaged. #2"),
        _inbound_email(db, customer.id, "Please refund me. #3"),
    ]
    db.commit()

    monkeypatch.setattr(
        scheduler_mod, "build_llm_client", lambda s: MockLLMClient(s)
    )
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_prefill_translations()

    db.expire_all()
    for em in emails:
        row = db.get(Email, em.id)
        assert row.content_cn is not None
        assert "(Mock translation)" in row.content_cn


def test_prefill_handles_all_failures_without_crashing(
    db, session_factory, settings, monkeypatch
) -> None:
    """A permanently failing LLM must not raise out of the job or block others."""
    import app.services.scheduler as scheduler_mod

    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    emails = [
        _inbound_email(db, customer.id, f"Refund request #{i}") for i in range(3)
    ]
    db.commit()

    monkeypatch.setattr(
        scheduler_mod,
        "build_llm_client",
        lambda s: FlakyLLM(s, fail_calls=100),  # every call fails (even retries)
    )
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_prefill_translations()  # must not raise

    db.expire_all()
    for em in emails:
        assert db.get(Email, em.id).content_cn is None


def test_prefill_skips_empty_body(
    db, session_factory, settings, monkeypatch
) -> None:
    import app.services.scheduler as scheduler_mod

    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    empty = _inbound_email(db, customer.id, "   ")
    normal = _inbound_email(db, customer.id, "Please help.")
    db.commit()

    monkeypatch.setattr(
        scheduler_mod, "build_llm_client", lambda s: MockLLMClient(s)
    )
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_prefill_translations()

    db.expire_all()
    assert db.get(Email, empty.id).content_cn is None
    assert db.get(Email, normal.id).content_cn is not None


def test_prefill_respects_test_mode_whitelist(
    db, session_factory, settings, monkeypatch
) -> None:
    """Test mode: prefill only translates whitelisted senders."""
    import app.services.scheduler as scheduler_mod

    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = "c@example.com"
    db.commit()

    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    allowed = _inbound_email(db, customer.id, "Where is my order? #1")
    blocked = _inbound_email(db, customer.id, "Another question #2")
    blocked.from_email = "d@example.com"
    db.commit()

    monkeypatch.setattr(
        scheduler_mod, "build_llm_client", lambda s: MockLLMClient(s)
    )
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_prefill_translations()

    db.expire_all()
    assert db.get(Email, allowed.id).content_cn is not None
    assert db.get(Email, blocked.id).content_cn is None


def test_prefill_test_mode_empty_whitelist_translates_nothing(
    db, session_factory, settings, monkeypatch
) -> None:
    """Test mode with an empty whitelist must not spend LLM tokens at all."""
    import app.services.scheduler as scheduler_mod

    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = ""
    db.commit()

    customer = Customer(email="c@example.com", display_name="C", created_at=utcnow())
    db.add(customer)
    db.flush()
    email = _inbound_email(db, customer.id, "Please help.")
    db.commit()

    monkeypatch.setattr(
        scheduler_mod, "build_llm_client", lambda s: MockLLMClient(s)
    )
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_prefill_translations()

    db.expire_all()
    assert db.get(Email, email.id).content_cn is None


def _conversation_with_followups(db, n_emails: int) -> Conversation:
    """Seed a conversation with n inbound emails and an open medium ticket."""
    customer = Customer(email="f@example.com", display_name="F", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = _conversation(db, customer.id, utcnow())
    for i in range(n_emails):
        db.add(
            Email(
                conversation_id=conv.id,
                message_id=f"<f-{i}@example.com>",
                subject="Follow up",
                from_email="f@example.com",
                to_email="bot@example.com",
                body_text=f"Mail {i}",
                is_inbound=True,
                received_at=utcnow(),
            )
        )
    db.add(
        Ticket(
            conversation_id=conv.id,
            summary_cn="审核工单",
            risk_level="medium",
            status="pending",
            sla_deadline=utcnow(),
            created_at=utcnow(),
        )
    )
    db.commit()
    return conv


def test_followup_alert_scan_alerts_once_and_audits(
    db, session_factory, settings, monkeypatch
) -> None:
    """Customer followed up twice without a reply -> alert once, not every scan."""
    conv = _conversation_with_followups(db, 3)
    alerts = _capture_alerts(monkeypatch)
    service = SchedulerService(settings, session_factory=session_factory)
    service._job_followup_alert_scan()
    service._job_followup_alert_scan()

    assert len(alerts) == 1
    assert "客户追问中" in alerts[0][0]
    assert str(conv.id) in alerts[0][1]
    audit_actions = [
        a.action
        for a in db.execute(
            select(AuditLog).where(AuditLog.resource_id == conv.id)
        ).scalars().all()
    ]
    assert audit_actions.count("followup_alert") == 1


def test_followup_alert_scan_skips_few_followups(
    db, session_factory, settings, monkeypatch
) -> None:
    """One follow-up is normal waiting, not yet an alert."""
    _conversation_with_followups(db, 2)
    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory
    )._job_followup_alert_scan()
    assert alerts == []


def test_followup_alert_scan_skips_resolved_ticket(
    db, session_factory, settings, monkeypatch
) -> None:
    """A resolved review ticket means the wait is over: no follow-up alert."""
    conv = _conversation_with_followups(db, 3)
    ticket = db.execute(
        select(Ticket).where(Ticket.conversation_id == conv.id)
    ).scalars().one()
    ticket.status = "resolved"
    db.commit()
    alerts = _capture_alerts(monkeypatch)
    SchedulerService(
        settings, session_factory=session_factory
    )._job_followup_alert_scan()
    assert alerts == []
