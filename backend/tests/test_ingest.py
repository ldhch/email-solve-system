"""IMAP parsing + ingest unit tests (M-04)."""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage

import json

from sqlalchemy import select

from app.llm.client import MockLLMClient
from app.models.attachment import Attachment
from app.models.blocked_sender import BlockedSender
from app.models.email import Email
from app.models.reply import Reply
from app.services.ingest import IngestService, _html_to_text, parse_email
from app.services.audit import utcnow
from app.services.mailer import MailerService

from conftest import FakeSMTP, make_raw_email


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
