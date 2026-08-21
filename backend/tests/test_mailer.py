"""SMTP mailer tests (M-11)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.exceptions import SMTPError, SMTPRateLimitError
from app.models.reply import Reply
from app.services.audit import utcnow
from app.services.mailer import MailerService, build_message
from app.services.replier import markdown_to_html

from conftest import FakeIMAPAppend, FakeSMTP


def _reply() -> Reply:
    return Reply(
        conversation_id=1,
        email_id=1,
        message_id="<out-1@example.com>",
        in_reply_to="in-1@example.com",
        content_en="Thank you!",
        status="draft",
        reply_type="general",
        created_at=utcnow(),
    )


def test_build_message_headers() -> None:
    settings = Settings(
        email_username="bot@example.com",
        mail_from_name="Support",
        smtp_port=465,
        llm_provider="mock",
    )
    msg = build_message(_reply(), "customer@example.com", "Question", settings)
    assert msg["From"] == "Support <bot@example.com>"
    assert msg["To"] == "customer@example.com"
    assert msg["Subject"] == "Re: Question"
    assert msg["Message-ID"] == "<out-1@example.com>"
    assert msg["In-Reply-To"] == "in-1@example.com"


def test_build_message_keeps_existing_re_prefix() -> None:
    settings = Settings(email_username="bot@example.com", llm_provider="mock")
    msg = build_message(_reply(), "c@example.com", "Re: Question", settings)
    assert msg["Subject"] == "Re: Question"


def test_markdown_to_html_renders_light_markdown() -> None:
    rendered = markdown_to_html(
        "Hello **bold** and *italic*.\n\n"
        "- One\n"
        "- **Two**\n\n"
        "Line 1\nLine 2"
    )
    assert rendered == (
        "<p>Hello <strong>bold</strong> and <em>italic</em>.</p>"
        "<ul><li>One</li><li><strong>Two</strong></li></ul>"
        "<p>Line 1<br>Line 2</p>"
    )


def test_markdown_to_html_escapes_html() -> None:
    assert markdown_to_html("<script>alert(1)</script> & ok") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; ok</p>"
    )


def test_build_message_contains_multipart_alternative() -> None:
    msg = build_message(_reply(), "c@example.com", "Question", Settings(
        email_username="bot@example.com",
        llm_provider="mock",
    ))
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert parts[0].get_content().strip() == "Thank you!"
    html_body = parts[1].get_content()
    assert "background-color:#ffffff" in html_body
    assert "#2E5D86" in html_body
    assert '<p style="margin:0 0 12px; line-height:1.6;">Thank you!</p>' in html_body
    assert "The LBORA Team" in html_body


def test_build_message_html_sanitizes_markdown_injection() -> None:
    reply = _reply()
    reply.content_en = "Hello <script>alert(1)</script><img src=x onerror=alert(1)>"
    msg = build_message(reply, "c@example.com", "Question", Settings(
        email_username="bot@example.com",
        llm_provider="mock",
    ))
    html_body = list(msg.iter_parts())[1].get_content()
    assert "<script" not in html_body
    assert "<img" not in html_body
    assert "alert(1)" in html_body


def test_send_success(db, settings, fake_smtp_class) -> None:
    service = MailerService(db, settings, smtp_class=FakeSMTP)
    service.send(_reply(), "customer@example.com", "Question")
    assert len(FakeSMTP.instances) == 1
    assert FakeSMTP.instances[0].sent[0]["To"] == "customer@example.com"


def test_rate_limit_blocks_automated_but_owner_bypasses(
    db, settings, fake_smtp_class
) -> None:
    """Owner-triggered sends skip the hourly quota; automated sends respect it."""
    settings = settings.model_copy(update={"smtp_rate_limit_per_hour": 1})
    service = MailerService(db, settings, smtp_class=FakeSMTP)
    # One send in the rolling 1h window -> quota exhausted (the rate limiter
    # counts DB rows with status=sent, matching real pipeline behavior).
    sent = _reply()
    sent.status = "sent"
    sent.sent_at = utcnow()
    db.add(sent)
    db.commit()
    with pytest.raises(SMTPRateLimitError):
        service.send(_reply(), "customer2@example.com", "Q2")  # automated blocked
    # explicit owner action goes through even with the quota exhausted
    service.send(
        _reply(), "customer3@example.com", "Q3", bypass_rate_limit=True
    )
    assert len(FakeSMTP.instances) == 1


def test_send_appends_copy_to_sent_folder(db, fake_smtp_class) -> None:
    FakeIMAPAppend.reset()
    settings = Settings(
        email_username="bot@example.com",
        llm_provider="mock",
        imap_host="imap.example.com",
        imap_sent_folder="Sent",
    )
    service = MailerService(db, settings, smtp_class=FakeSMTP, imap_class=FakeIMAPAppend)
    service.send(_reply(), "customer@example.com", "Question")

    assert len(FakeIMAPAppend.instances) == 1
    conn = FakeIMAPAppend.instances[0]
    assert len(conn.append_calls) == 1
    folder, raw = conn.append_calls[0]
    assert folder == "Sent"
    text = raw.decode("utf-8")
    assert "To: customer@example.com" in text
    assert "Subject: Re: Question" in text
    assert "Message-ID: <out-1@example.com>" in text
    assert "In-Reply-To: in-1@example.com" in text


def test_send_skips_append_when_folder_empty(db, fake_smtp_class) -> None:
    FakeIMAPAppend.reset()
    settings = Settings(
        email_username="bot@example.com",
        llm_provider="mock",
        imap_host="imap.example.com",
        imap_sent_folder="",
    )
    service = MailerService(db, settings, smtp_class=FakeSMTP, imap_class=FakeIMAPAppend)
    service.send(_reply(), "customer@example.com", "Question")
    assert FakeIMAPAppend.instances == []


def test_send_survives_append_failure(db, fake_smtp_class) -> None:
    FakeIMAPAppend.reset()
    FakeIMAPAppend.fail_append = True
    settings = Settings(
        email_username="bot@example.com",
        llm_provider="mock",
        imap_host="imap.example.com",
        imap_sent_folder="Sent",
    )
    service = MailerService(db, settings, smtp_class=FakeSMTP, imap_class=FakeIMAPAppend)
    # The email was already delivered via SMTP; a failed copy must not raise.
    service.send(_reply(), "customer@example.com", "Question")
    assert len(FakeSMTP.instances) == 1


def test_send_retries_then_succeeds(db, settings, fake_smtp_class) -> None:
    FakeSMTP.reset(fail_remaining=2)
    service = MailerService(db, settings, smtp_class=FakeSMTP)
    service.send(_reply(), "customer@example.com", "Question")
    assert len(FakeSMTP.instances) == 3
    assert FakeSMTP.instances[-1].sent


def test_send_raises_after_three_failures(db, settings, fake_smtp_class) -> None:
    FakeSMTP.reset(fail_remaining=99)
    service = MailerService(db, settings, smtp_class=FakeSMTP)
    with pytest.raises(SMTPError):
        service.send(_reply(), "customer@example.com", "Question")
    assert len(FakeSMTP.instances) == 3


def test_rate_limit(db, fake_smtp_class) -> None:
    settings = Settings(
        email_username="bot@example.com",
        llm_provider="mock",
        smtp_rate_limit_per_hour=1,
    )
    sent = _reply()
    sent.status = "sent"
    sent.sent_at = utcnow()
    db.add(sent)
    db.commit()

    service = MailerService(db, settings, smtp_class=FakeSMTP)
    with pytest.raises(SMTPRateLimitError):
        service.send(_reply(), "c1@example.com", "Q1")


def test_rate_limit_survives_mailer_recreation(db, fake_smtp_class) -> None:
    settings = Settings(
        email_username="bot@example.com",
        llm_provider="mock",
        smtp_rate_limit_per_hour=1,
    )
    sent = _reply()
    sent.status = "sent"
    sent.sent_at = utcnow()
    db.add(sent)
    db.commit()

    first_service = MailerService(db, settings, smtp_class=FakeSMTP)
    with pytest.raises(SMTPRateLimitError):
        first_service.send(_reply(), "c1@example.com", "Q1")

    second_service = MailerService(db, settings, smtp_class=FakeSMTP)
    with pytest.raises(SMTPRateLimitError):
        second_service.send(_reply(), "c2@example.com", "Q2")
