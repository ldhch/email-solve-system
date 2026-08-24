"""Conversation merge engine tests (M-05 / PRD F2)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models.conversation import Conversation
from app.models.email import Email
from app.services.audit import utcnow
from app.services.conversation import ConversationService, normalize_subject, subject_similarity
from app.services.ingest import ParsedEmail


def _email(
    subject: str,
    received_at,
    message_id: str,
    from_email: str = "c@example.com",
    in_reply_to: str | None = None,
) -> ParsedEmail:
    return ParsedEmail(
        message_id=message_id,
        subject=subject,
        from_email=from_email,
        from_name="Customer",
        to_email="bot@example.com",
        body_text="body",
        body_html=None,
        received_at=received_at,
        in_reply_to=in_reply_to,
    )


def test_normalize_subject() -> None:
    assert normalize_subject("Re: Hello World!") == "hello world"
    assert normalize_subject("Fwd: RE[2]:  Where  is my order?") == "where is my order"
    assert normalize_subject("  FW: product question  ") == "product question"
    assert normalize_subject(None) == ""


def test_subject_similarity_threshold() -> None:
    assert subject_similarity("where is my order", "where is my order") == 1.0
    assert subject_similarity(
        "question about the tshirt size",
        "question about the tshirt size please",
    ) > 0.85


def test_new_conversation_when_nothing_matches(db, settings) -> None:
    service = ConversationService(db, settings)
    parsed = _email("Hello", utcnow(), "<a@x>")
    result = service.merge(parsed)
    assert result.created is True
    assert result.conversation.id is not None
    assert db.get(Conversation, result.conversation.id) is not None


def test_in_reply_to_matches_same_conversation(db, settings) -> None:
    service = ConversationService(db, settings)
    first = service.merge(_email("Order issue", utcnow(), "<first@x>"))
    second = service.merge(
        _email(
            "Re: Order issue",
            utcnow() + timedelta(hours=1),
            "<second@x>",
            in_reply_to="<first@x>",
        )
    )
    assert second.created is False
    assert second.conversation.id == first.conversation.id


def test_references_header_matches(db, settings) -> None:
    service = ConversationService(db, settings)
    first = service.merge(_email("Bug report", utcnow(), "<r1@x>"))
    parsed = _email("Re: Bug report", utcnow() + timedelta(minutes=5), "<r2@x>")
    parsed.references = ["<r1@x>", "<other@x>"]
    second = service.merge(parsed)
    assert second.conversation.id == first.conversation.id


def test_same_subject_different_customer_new_conversation(db, settings) -> None:
    service = ConversationService(db, settings)
    first = service.merge(_email("Question", utcnow(), "<q1@x>", from_email="a@x.com"))
    second = service.merge(_email("Question", utcnow() + timedelta(minutes=5), "<q2@x>", from_email="b@x.com"))
    assert second.conversation.id != first.conversation.id


def test_same_subject_within_window_matches(db, settings) -> None:
    service = ConversationService(db, settings)
    first = service.merge(_email("Where is my parcel", utcnow(), "<p1@x>"))
    second = service.merge(
        _email("Where is my parcel?", utcnow() + timedelta(days=2), "<p2@x>")
    )
    assert second.created is False
    assert second.conversation.id == first.conversation.id


def test_window_expired_creates_new_conversation(db, settings) -> None:
    service = ConversationService(db, settings)
    now = utcnow()
    first = service.merge(_email("Refund status", now, "<w1@x>"))
    second = service.merge(_email("Refund status", now + timedelta(days=8), "<w2@x>"))
    assert second.created is True
    assert second.conversation.id != first.conversation.id


def test_risk_level_takes_highest(db, settings) -> None:
    service = ConversationService(db, settings)
    conv = service.merge(_email("Topic", utcnow(), "<risk@x>")).conversation
    service.update_risk(conv, "low")
    service.update_risk(conv, "high")
    service.update_risk(conv, "medium")
    assert conv.risk_level == "high"


def test_merge_clears_archive_flag(db, settings) -> None:
    service = ConversationService(db, settings)
    first = service.merge(_email("Order issue", utcnow(), "<arch1@x>"))
    first.conversation.is_archived = True
    db.commit()

    # a reply on an archived thread surfaces it back in the inbox
    second = service.merge(
        _email(
            "Re: Order issue",
            utcnow() + timedelta(hours=1),
            "<arch2@x>",
            in_reply_to="<arch1@x>",
        )
    )
    assert second.created is False
    assert second.conversation.id == first.conversation.id
    assert second.conversation.is_archived is False


def test_window_slides_forward(db, settings) -> None:
    service = ConversationService(db, settings)
    now = utcnow()
    conv = service.merge(_email("Topic", now, "<slide1@x>")).conversation
    initial_end = conv.window_end
    service.merge(_email("Topic", now + timedelta(days=5), "<slide2@x>"))
    assert conv.window_end > initial_end
    emails = db.execute(select(Email).where(Email.conversation_id == conv.id)).scalars().all()
    assert len(emails) == 0  # emails are persisted by the pipeline, not the merge engine
