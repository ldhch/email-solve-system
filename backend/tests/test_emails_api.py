"""Email on-demand full-translation API tests."""

from __future__ import annotations

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.services.audit import utcnow

from api_helpers import api, close_client, login, make_client, seed_owner


def _authed_client(settings, session_factory):
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    assert login(client, settings.owner_username, settings.owner_password).status_code == 200
    return client


def _seed_email(session_factory) -> int:
    with session_factory() as db:
        customer = Customer(email="c@example.com", created_at=utcnow())
        db.add(customer)
        db.flush()
        conv = Conversation(
            customer_id=customer.id,
            subject_normalized="question",
            window_end=utcnow(),
            last_activity_at=utcnow(),
            status="open",
        )
        db.add(conv)
        db.flush()
        email = Email(
            conversation_id=conv.id,
            message_id="<email@example.com>",
            subject="Question",
            from_email="c@example.com",
            to_email="bot@example.com",
            body_text="Where is my order?",
            is_inbound=True,
            received_at=utcnow(),
        )
        db.add(email)
        db.commit()
        return email.id


def test_translate_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        assert api(client, "POST", "/api/v1/emails/1/translate").status_code == 401
    finally:
        close_client(client)


def test_translate_email_full_and_idempotent(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        email_id = _seed_email(session_factory)
        r1 = api(client, "POST", f"/api/v1/emails/{email_id}/translate")
        assert r1.status_code == 200, r1.text
        cn1 = r1.json()["data"]["content_cn"]
        assert cn1.startswith("(Mock translation)"), cn1
        # Second call is a cache hit: same value, no re-translation.
        r2 = api(client, "POST", f"/api/v1/emails/{email_id}/translate")
        assert r2.status_code == 200
        assert r2.json()["data"]["content_cn"] == cn1
    finally:
        close_client(client)


def test_translate_email_missing(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        assert api(client, "POST", "/api/v1/emails/9999/translate").status_code == 404
    finally:
        close_client(client)


def test_conversation_detail_includes_email_content_cn(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        email_id = _seed_email(session_factory)
        api(client, "POST", f"/api/v1/emails/{email_id}/translate")
        with session_factory() as db:
            conv_id = db.get(Email, email_id).conversation_id
        resp = api(client, "GET", f"/api/v1/conversations/{conv_id}")
        assert resp.status_code == 200, resp.text
        timeline = resp.json()["data"]["timeline"]
        mail = next(t for t in timeline if t["type"] == "email")
        assert mail["content_cn"]
    finally:
        close_client(client)
