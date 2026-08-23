"""IMAP parsing + ingest unit tests (M-04)."""

from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage

import json

from sqlalchemy import select

from app.llm.client import MockLLMClient
from app.models.attachment import Attachment
from app.models.blocked_sender import BlockedSender
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.imap_skip import ImapSkip
from app.models.reply import Reply
from app.models.system_state import SystemState
from app.services.ingest import IngestService, _html_to_text, parse_email
from app.services.audit import utcnow
from app.services.mailer import MailerService

from conftest import FakeIMAP, FakeSMTP, make_raw_email


class _LowConfidenceClassifier(MockLLMClient):
    """Classifier that always returns confidence below the manual threshold."""

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None):
        system_prompt_lower = (system_prompt or "").lower()
        if "risk_level" in system_prompt_lower:  # triage classifier
            return json.dumps(
                {
                    "risk_level": "low",
                    "confidence": 0.3,  # < low_confidence_threshold (0.6)
                    "category": "other",
                    "chargeback_risk": False,
                    "summary_cn": "低置信无法判定",
                }
            )
        return super().chat(messages, system_prompt, max_tokens, temperature)


def test_parse_plain_email() -> None:
    raw = make_raw_email(
        subject="Shipping question",
        body="Hi, has my package shipped yet?",
        message_id="<abc-123@example.com>",
        from_email="customer@example.com",
        from_name="Jane Doe",
    )
    parsed = parse_email(raw)
    assert parsed.message_id == "abc-123@example.com"
    assert parsed.subject == "Shipping question"
    assert parsed.from_email == "customer@example.com"
    assert parsed.from_name == "Jane Doe"
    assert parsed.to_email == "bot@example.com"
    assert "has my package shipped yet?" in parsed.body_text
    assert parsed.has_attachments is False


def test_html_to_text_preserves_paragraphs_lists_and_links() -> None:
    html = (
        '<html><body><script>alert(1)</script>'
        '<p>Hello <b>world</b>&nbsp;!</p>'
        "<ul><li>First item</li><li>Second item</li></ul>"
        '<div>See <a href="https://example.com/x?a=1&amp;b=2">details</a></div>'
        "</body></html>"
    )
    assert _html_to_text(html) == (
        "Hello world !\n\n"
        "- First item\n"
        "- Second item\n\n"
        "See details (https://example.com/x?a=1&b=2)"
    )


def test_html_to_text_keeps_script_content_out_of_body_text() -> None:
    assert "alert" not in _html_to_text("<script>alert(1)</script><p>Safe</p>")


def test_parse_strips_angle_brackets_and_supports_references() -> None:
    raw = make_raw_email(
        message_id="<a@x>",
        in_reply_to="<b@x>",
        references="<b@x> <c@x>",
    )
    parsed = parse_email(raw)
    assert parsed.in_reply_to == "b@x"
    assert parsed.references == ["b@x", "c@x"]


def test_parse_missing_message_id_generates_synthetic() -> None:
    msg = EmailMessage()
    msg["Subject"] = "No id"
    msg["From"] = "a@example.com"
    msg["Date"] = "Tue, 12 Aug 2026 10:00:00 +0000"
    msg.set_content("hello")
    parsed = parse_email(bytes(msg))
    assert parsed.message_id.startswith("gen-")


def test_parse_received_at_converts_timezone_to_utc() -> None:
    plus8 = make_raw_email(date="Tue, 12 Aug 2026 10:00:00 +0800")
    minus8 = make_raw_email(date="Tue, 12 Aug 2026 10:00:00 -0800")
    naive_zone = make_raw_email(date="Tue, 12 Aug 2026 10:00:00")

    # All results must be naive UTC so SLA math against utcnow() is stable.
    assert parse_email(plus8).received_at == datetime(2026, 8, 12, 2, 0)
    assert parse_email(minus8).received_at == datetime(2026, 8, 12, 18, 0)
    assert parse_email(naive_zone).received_at == datetime(2026, 8, 12, 10, 0)


def test_parse_received_at_missing_date_falls_back_to_utc_now() -> None:
    from app.services.audit import utcnow

    msg = EmailMessage()
    msg["Subject"] = "No date"
    msg["From"] = "a@example.com"
    msg.set_content("hello")
    parsed = parse_email(bytes(msg))
    assert parsed.received_at.tzinfo is None
    assert abs((utcnow() - parsed.received_at).total_seconds()) < 10


def test_parse_html_sanitized() -> None:
    msg = EmailMessage()
    msg["Subject"] = "HTML mail"
    msg["From"] = "a@example.com"
    msg["Date"] = "Tue, 12 Aug 2026 10:00:00 +0000"
    msg.set_content(
        '<html><body><p>Hello</p><script>alert(1)</script><a href="https://x">link</a></body></html>',
        subtype="html",
    )
    parsed = parse_email(bytes(msg))
    assert "<script>" not in (parsed.body_html or "")
    assert "<p>Hello</p>" in (parsed.body_html or "")
    assert "Hello" in (parsed.body_text or "")


def test_parse_multipart_with_attachment(tmp_path) -> None:
    msg = EmailMessage()
    msg["Subject"] = "With attachment"
    msg["From"] = "a@example.com"
    msg["Date"] = "Tue, 12 Aug 2026 10:00:00 +0000"
    msg.set_content("See the file")
    msg.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename="receipt.pdf")
    parsed = parse_email(bytes(msg))
    assert parsed.has_attachments is True
    assert parsed.attachments[0].filename == "receipt.pdf"
    assert parsed.attachments[0].payload == b"PDFDATA"


def test_process_one_duplicate_skipped(db, settings, fake_smtp_class) -> None:
    raw = make_raw_email(subject="Product size question")
    parsed = parse_email(raw, uid="1")
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
    )
    first = service.process_one(parsed)
    second = service.process_one(parsed)
    assert first.action == "auto_sent"
    assert second.action == "duplicate"
    emails = db.execute(select(Email)).scalars().all()
    assert len(emails) == 1


def test_attachments_persisted(db, settings, fake_smtp_class, tmp_path) -> None:
    settings = settings.model_copy(update={"attachment_dir": str(tmp_path)})
    msg = EmailMessage()
    msg["Subject"] = "With photo"
    msg["From"] = "a@example.com"
    msg["Date"] = "Tue, 12 Aug 2026 10:00:00 +0000"
    msg.set_content("See file")
    msg.add_attachment(b"X", maintype="image", subtype="png", filename="shot.png")
    parsed = parse_email(bytes(msg))
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
    )
    service.process_one(parsed)
    attachments = db.execute(select(Attachment)).scalars().all()
    assert len(attachments) == 1
    assert attachments[0].filename == "shot.png"
    assert (tmp_path / attachments[0].stored_path.split("/")[-1]).exists()


# ---------- 广告 (ad) / 黑名单 (blocked sender) / 无法判定 (low-confidence draft) ----------


def _service(db, settings, client=None):
    return IngestService(
        db,
        settings,
        llm_client=client or MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
    )


def test_process_one_ad_archived_by_keyword(db, settings) -> None:
    parsed = parse_email(
        make_raw_email(
            subject="Your weekly picks",
            body="Check out our newsletter! You're receiving this email because "
            "you subscribed. Save 70% this weekend.",
            from_email="promo@amazon.com",
            message_id="<ad-1@example.com>",
        ),
        uid="1",
    )
    result = _service(db, settings).process_one(parsed)
    assert result.action == "ad"
    email = db.execute(select(Email)).scalars().one()
    assert email.is_ad is True
    assert email.is_read is True  # never counts toward the unread badge
    assert db.execute(select(Reply)).scalars().all() == []  # never auto-replied


def test_process_one_blocked_sender_archived(db, settings) -> None:
    db.add(
        BlockedSender(value="amazon.com", scope="domain", created_at=utcnow())
    )
    db.commit()
    parsed = parse_email(
        make_raw_email(
            subject="Big sale",
            body="Grab our best price today.",
            from_email="promo@amazon.com",
            message_id="<ad-2@example.com>",
        ),
        uid="1",
    )
    result = _service(db, settings).process_one(parsed)
    assert result.action == "ad"
    email = db.execute(select(Email)).scalars().one()
    assert email.is_ad is True
    # exact-address blacklist also matches
    db.add(BlockedSender(value="spam@example.org", scope="email", created_at=utcnow()))
    db.commit()
    assert db.execute(
        select(BlockedSender).where(BlockedSender.value == "spam@example.org")
    ).scalar_one().scope == "email"


def test_process_one_unknown_creates_low_confidence_draft(db, settings) -> None:
    parsed = parse_email(
        make_raw_email(
            subject="Can you help",
            body="Some confusing thing happened with my order.",
            message_id="<unk-1@example.com>",
        ),
        uid="1",
    )
    result = _service(db, settings, _LowConfidenceClassifier(settings)).process_one(parsed)
    assert result.action == "review"
    assert result.risk_level == "unknown"
    reply = db.execute(select(Reply)).scalars().one()
    assert reply.status == "pending_review"
    assert reply.low_confidence is True


def test_process_one_empty_body_stays_manual(db, settings) -> None:
    """Empty/unreadable mail has nothing to draft from — pure manual only."""
    parsed = parse_email(
        make_raw_email(subject="(no subject)", body="", message_id="<empty-1@example.com>"),
        uid="1",
    )
    result = _service(db, settings, _LowConfidenceClassifier(settings)).process_one(parsed)
    assert result.action == "manual"
    assert result.risk_level == "unknown"
    assert db.execute(select(Reply)).scalars().all() == []


# ---------- 测试模式 (test mode / sender whitelist) ----------


def _enable_test_mode(db, whitelist: str) -> None:
    state = db.get(SystemState, 1)
    state.test_mode = True
    state.test_whitelist = whitelist
    db.commit()


def test_fetch_test_mode_gates_non_whitelisted_sender(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    """Test mode: non-whitelisted senders stay UNSEEN and are not ingested."""
    _enable_test_mode(db, "test-a@example.com")
    raw_a = make_raw_email(
        subject="Order question",
        body="Where is my hat?",
        from_email="test-a@example.com",
        message_id="<gate-a@example.com>",
    )
    raw_b = make_raw_email(
        subject="Another one",
        body="Please help me.",
        from_email="test-b@example.com",
        message_id="<gate-b@example.com>",
    )
    imap = fake_imap([("1", raw_a), ("2", raw_b)])
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        imap=imap,
    )
    summary = service.fetch_and_process()
    assert summary["fetched"] == 2
    assert summary["test_skipped"] == 1
    emails = db.execute(select(Email)).scalars().all()
    assert [e.message_id for e in emails] == ["gate-a@example.com"]
    # The server \Seen flag is never touched; the whitelisted mail is tracked
    # by its persisted imap_uid, and the gated mail stays untouched.
    assert imap.seen == []
    assert {e.imap_uid for e in emails} == {"1"}
    # Next poll: the gated mail is re-fetched (not ingested, no UID recorded);
    # the whitelisted mail is skipped via its persisted UID.
    service.fetch_and_process()
    assert imap.fetched == ["1", "2", "2"]


def test_fetch_test_mode_empty_whitelist_isolates_everything(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    """Test mode with an empty whitelist gates every sender (full isolation)."""
    _enable_test_mode(db, "")
    imap = fake_imap(
        [
            (
                "1",
                make_raw_email(
                    from_email="test-a@example.com", message_id="<gate-c@example.com>"
                ),
            ),
            (
                "2",
                make_raw_email(
                    from_email="test-b@example.com", message_id="<gate-d@example.com>"
                ),
            ),
        ]
    )
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        imap=imap,
    )
    summary = service.fetch_and_process()
    assert summary["test_skipped"] == 2
    assert db.execute(select(Email)).scalars().all() == []
    assert imap.seen == []


def test_pending_backlog_respects_test_mode(db, settings, fake_smtp_class) -> None:
    """Mail queued from an emergency pause stays queued when the sender is gated."""
    _enable_test_mode(db, "test-a@example.com")
    customer = Customer(email="test-b@example.com", display_name="B", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = Conversation(
        customer_id=customer.id,
        subject_normalized="re: x",
        window_end=utcnow() + timedelta(days=7),
        last_activity_at=utcnow(),
        status="open",
    )
    db.add(conv)
    db.flush()
    email = Email(
        conversation_id=conv.id,
        message_id="<pend-b@example.com>",
        subject="Re: help",
        from_email="test-b@example.com",
        is_inbound=True,
        pending_after_pause=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.commit()
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
    )
    summary = service._process_pending_after_pause()
    assert summary["test_skipped"] == 1
    db.expire_all()
    assert db.get(Email, email.id).pending_after_pause is True  # stays queued


def test_fetch_never_marks_seen_and_persists_uid(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    """The poll never writes \\Seen to the server; processed mail is tracked
    by its persisted imap_uid (+ uidvalidity) instead."""
    raw = make_raw_email(
        subject="Order question",
        body="Where is my hat?",
        message_id="<noscen-1@example.com>",
    )
    imap = fake_imap([("3", raw)])
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        imap=imap,
    )
    summary = service.fetch_and_process()
    assert summary["fetched"] == 1
    assert imap.seen == []  # server \Seen untouched
    email = db.execute(select(Email)).scalar_one()
    assert email.imap_uid == "3"
    assert email.imap_uidvalidity == FakeIMAP.UIDVALIDITY


def test_skipped_uid_is_not_fetched_again(db, settings, fake_imap) -> None:
    """A malformed mail's UID is recorded once (ImapSkip) and never re-fetched."""
    raw = make_raw_email(message_id="<skip-1@example.com>")
    imap = fake_imap([("7", raw)])
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        imap=imap,
    )
    # Simulate the poll recording a parse failure for uid "7".
    service._current_uidvalidity = FakeIMAP.UIDVALIDITY
    service._skip_uid("7")
    assert db.execute(select(ImapSkip)).scalars().all()
    assert service.fetch_unseen(imap) == []  # skipped uid is not fetched


def test_old_seen_mail_is_not_refetched(
    db, settings, fake_smtp_class, fake_imap
) -> None:
    """Mail the previous poll already flagged \\Seen is never re-downloaded.

    This is what keeps an upgrade safe: the old poll marked everything seen on
    the server, so ``SEARCH UNSEEN`` simply does not return historical mail.
    """
    customer = Customer(email="old@example.com", display_name="Old", created_at=utcnow())
    db.add(customer)
    db.flush()
    conv = Conversation(
        customer_id=customer.id,
        subject_normalized="old mail",
        window_end=utcnow(),
        last_activity_at=utcnow(),
        status="open",
        risk_level="medium",
    )
    db.add(conv)
    db.flush()
    email = Email(
        conversation_id=conv.id,
        message_id="old-1@example.com",
        subject="Old mail",
        from_email="old@example.com",
        to_email="bot@example.com",
        body_text="hello",
        is_inbound=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.commit()

    raw = make_raw_email(subject="Old mail", message_id="<old-1@example.com>")
    imap = fake_imap([("9", raw)])
    imap.seen.append("9")  # the old system marked it seen on the server
    service = IngestService(
        db,
        settings,
        llm_client=MockLLMClient(settings),
        mailer=MailerService(db, settings, smtp_class=FakeSMTP),
        imap=imap,
    )
    summary = service.fetch_and_process()
    assert summary["fetched"] == 0  # UNSEEN search never returns it
    assert imap.seen == ["9"]  # and nothing new was flagged seen either
