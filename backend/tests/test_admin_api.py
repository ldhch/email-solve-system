"""Admin REST API integration tests (M-15, TECH 5.2/5.3/5.4)."""

from __future__ import annotations

from sqlalchemy import select

import app.api.conversations as conversations_module
from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.services.audit import utcnow
from app.services.mailer import MailerService

from api_helpers import api, close_client, login, make_client, seed_owner
from conftest import FakeSMTP


def _seed_conversation(
    session_factory,
    *,
    emails: int = 1,
    reply: dict | None = None,
    ticket: dict | None = None,
    customer_email: str = "c@example.com",
) -> dict:
    """Insert customer/conversation/emails(+optional reply/ticket), return ids."""

    with session_factory() as db:
        customer = Customer(
            email=customer_email, display_name="John", created_at=utcnow()
        )
        db.add(customer)
        db.flush()
        conv = Conversation(
            customer_id=customer.id,
            subject_normalized="order question",
            window_end=utcnow(),
            last_activity_at=utcnow(),
            status="open",
            risk_level="medium",
        )
        db.add(conv)
        db.flush()
        email_ids = []
        uid = customer_email.split("@")[0]
        for i in range(emails):
            email = Email(
                conversation_id=conv.id,
                message_id=f"<admin-{i}-{uid}@example.com>",
                subject=f"Order question {i}",
                from_email="c@example.com",
                to_email="bot@example.com",
                body_text=f"Question body {i}",
                is_inbound=True,
                received_at=utcnow(),
                risk_level="medium",
                category="policy",
                summary_cn="中文摘要",
            )
            db.add(email)
            db.flush()
            email_ids.append(email.id)
        reply_id = None
        if reply is not None:
            reply_row = Reply(
                conversation_id=conv.id,
                email_id=email_ids[-1],
                message_id=f"<out-admin-{uid}@example.com>",
                in_reply_to=email_ids[-1],
                content_cn=reply.get("content_cn"),
                content_en=reply.get("content_en", "English content"),
                status=reply.get("status", "pending_review"),
                reply_type=reply.get("reply_type", "general"),
                created_at=utcnow(),
            )
            db.add(reply_row)
            db.flush()
            reply_id = reply_row.id
        ticket_id = None
        if ticket is not None:
            ticket_row = Ticket(
                conversation_id=conv.id,
                summary_cn="高风险工单摘要",
                risk_level="high",
                status=ticket.get("status", "pending"),
                sla_deadline=utcnow(),
                created_at=utcnow(),
            )
            db.add(ticket_row)
            db.flush()
            ticket_id = ticket_row.id
        db.commit()
        return {
            "customer_id": customer.id,
            "conversation_id": conv.id,
            "email_ids": email_ids,
            "reply_id": reply_id,
            "ticket_id": ticket_id,
        }


def _authed_client(settings, session_factory):
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    resp = login(client, settings.owner_username, settings.owner_password)
    assert resp.status_code == 200, resp.text
    return client


def test_inbox_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        assert api(client, "GET", "/api/v1/inbox").status_code == 401
    finally:
        close_client(client)


def test_inbox_list_and_detail(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=2)
    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/inbox")
        assert resp.status_code == 200
        body = resp.json()["data"]
        # 2 emails fold into a single conversation row
        assert body["total"] == 1
        item = body["items"][0]
        assert item["id"] == ids["conversation_id"]
        assert item["subject"] == "Order question 1"
        assert item["email_count"] == 2
        assert item["unread_count"] == 2
        assert item["is_read"] is False
        assert item["risk_level"] == "medium"

        filtered = api(client, "GET", "/api/v1/inbox", params={"risk_level": "medium"})
        assert filtered.json()["data"]["total"] == 1
        none = api(client, "GET", "/api/v1/inbox", params={"risk_level": "high"})
        assert none.json()["data"]["total"] == 0

        detail = api(client, "GET", f"/api/v1/inbox/{ids['email_ids'][0]}")
        assert detail.status_code == 200
        data = detail.json()["data"]
        assert data["conversation_id"] == ids["conversation_id"]
        assert data["summary_cn"] == "中文摘要"
    finally:
        close_client(client)


def test_inbox_unread_and_mark_read(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=2)
    client = _authed_client(settings, session_factory)
    try:
        # unread-count is conversation-level: 1 conversation holds 2 unread emails
        count = api(client, "GET", "/api/v1/inbox/unread-count").json()["data"]
        assert count["unread"] == 1

        email_id = ids["email_ids"][0]
        marked = api(client, "POST", f"/api/v1/inbox/{email_id}/read")
        assert marked.status_code == 200
        assert marked.json()["data"]["is_read"] is True

        # one unread email remains in the conversation -> still counted unread
        after = api(client, "GET", "/api/v1/inbox/unread-count").json()["data"]
        assert after["unread"] == 1

        listing = api(client, "GET", "/api/v1/inbox").json()["data"]["items"]
        assert listing[0]["is_read"] is False
        assert listing[0]["unread_count"] == 1
    finally:
        close_client(client)


def test_inbox_conversation_aggregation(settings, session_factory) -> None:
    ids_a = _seed_conversation(
        session_factory,
        emails=2,
        customer_email="a@example.com",
        reply={
            "status": "sent",
            "content_en": "Reply English",
            "content_cn": "回复内容",
        },
    )
    ids_b = _seed_conversation(session_factory, emails=1, customer_email="b@example.com")
    client = _authed_client(settings, session_factory)
    try:
        body = api(client, "GET", "/api/v1/inbox").json()["data"]
        assert body["total"] == 2
        by_id = {item["id"]: item for item in body["items"]}

        row_a = by_id[ids_a["conversation_id"]]
        assert row_a["email_count"] == 2
        assert row_a["unread_count"] == 2
        assert row_a["latest_status"] == "sent"
        # latest activity is the reply (ties resolve toward the reply)
        assert row_a["summary_cn"] == "回复内容"
        assert row_a["customer_name"] == "John"

        row_b = by_id[ids_b["conversation_id"]]
        assert row_b["email_count"] == 1
        assert row_b["latest_status"] is None
        assert row_b["summary_cn"] == "中文摘要"
    finally:
        close_client(client)


def test_inbox_conversation_read(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=2)
    client = _authed_client(settings, session_factory)
    try:
        resp = api(
            client,
            "POST",
            f"/api/v1/inbox/conversations/{ids['conversation_id']}/read",
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["is_read"] is True

        count = api(client, "GET", "/api/v1/inbox/unread-count").json()["data"]
        assert count["unread"] == 0
        listing = api(client, "GET", "/api/v1/inbox").json()["data"]["items"]
        assert listing[0]["is_read"] is True

        missing = api(client, "POST", "/api/v1/inbox/conversations/9999/read")
        assert missing.status_code == 404
    finally:
        close_client(client)


def test_conversation_detail_timeline(settings, session_factory) -> None:
    ids = _seed_conversation(
        session_factory,
        emails=2,
        reply={"status": "sent", "content_en": "Thank you!"},
    )
    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", f"/api/v1/conversations/{ids['conversation_id']}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "open"
        assert data["customer"]["email"] == "c@example.com"
        types = [t["type"] for t in data["timeline"]]
        assert types.count("email") == 2
        assert types.count("reply") == 1
        reply_item = next(t for t in data["timeline"] if t["type"] == "reply")
        assert reply_item["status"] == "sent"
        assert reply_item["content_en"] == "Thank you!"
    finally:
        close_client(client)


def test_conversation_status_escalated_with_open_ticket(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, ticket={"status": "pending"})
    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", f"/api/v1/conversations/{ids['conversation_id']}")
        data = resp.json()["data"]
        assert data["status"] == "escalated"
        assert data["sla_deadline"] is not None
    finally:
        close_client(client)


def test_manual_reply_translates_and_sends(
    settings, session_factory, monkeypatch
) -> None:
    ids = _seed_conversation(session_factory)
    monkeypatch.setattr(
        conversations_module,
        "_make_mailer",
        lambda db, s: MailerService(db, s, smtp_class=FakeSMTP),
    )
    client = _authed_client(settings, session_factory)
    try:
        resp = api(
            client,
            "POST",
            f"/api/v1/conversations/{ids['conversation_id']}/reply",
            json={"content_cn": "请提供您的订单号，我们马上为您处理。"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["reply_id"] is not None
        assert data["content_en"].startswith("(Mock translation)")
        assert FakeSMTP.instances and FakeSMTP.instances[0].sent
        with session_factory() as db:
            reply = db.get(Reply, data["reply_id"])
            assert reply.status == "sent"
            actions = {a.action for a in db.query(AuditLog).all()}
        assert "manual_reply_sent" in actions
    finally:
        close_client(client)


def test_approve_reject_edit_send_draft(
    settings, session_factory, monkeypatch
) -> None:
    ids = _seed_conversation(session_factory, reply={"status": "pending_review"})
    monkeypatch.setattr(
        conversations_module,
        "_make_mailer",
        lambda db, s: MailerService(db, s, smtp_class=FakeSMTP),
    )
    client = _authed_client(settings, session_factory)
    try:
        # reject -> draft
        reject = api(
            client,
            "POST",
            f"/api/v1/replies/{ids['reply_id']}/reject",
            json={"reason": "tone too strong"},
        )
        assert reject.status_code == 200
        assert reject.json()["data"]["status"] == "draft"

        # edit -> re-translate
        edit = api(
            client,
            "PATCH",
            f"/api/v1/replies/{ids['reply_id']}",
            json={"content_cn": "很抱歉给您带来不便。"},
        )
        assert edit.status_code == 200
        assert "(Mock translation)" in edit.json()["data"]["content_en"]

        # send
        send = api(client, "POST", f"/api/v1/replies/{ids['reply_id']}/send")
        assert send.status_code == 200, send.text
        with session_factory() as db:
            reply = db.get(Reply, ids["reply_id"])
            assert reply.status == "sent"

        # approve an already-sent reply must conflict
        conflict = api(client, "POST", f"/api/v1/replies/{ids['reply_id']}/approve")
        assert conflict.status_code == 409
    finally:
        close_client(client)


def test_approve_pending_review_sends(settings, session_factory, monkeypatch) -> None:
    ids = _seed_conversation(session_factory, reply={"status": "pending_review"})
    monkeypatch.setattr(
        conversations_module,
        "_make_mailer",
        lambda db, s: MailerService(db, s, smtp_class=FakeSMTP),
    )
    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "POST", f"/api/v1/replies/{ids['reply_id']}/approve")
        assert resp.status_code == 200, resp.text
        assert FakeSMTP.instances and FakeSMTP.instances[0].sent
        with session_factory() as db:
            reply = db.get(Reply, ids["reply_id"])
            assert reply.status == "sent"
            assert reply.review_user_id is not None
    finally:
        close_client(client)


def test_reply_trash_and_restore(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, reply={"status": "sent"})
    client = _authed_client(settings, session_factory)
    try:
        delete = api(client, "DELETE", f"/api/v1/replies/{ids['reply_id']}")
        assert delete.status_code == 200
        trash = api(client, "GET", "/api/v1/replies/trash")
        assert trash.json()["data"]["total"] == 1
        restore = api(client, "POST", f"/api/v1/replies/{ids['reply_id']}/restore")
        assert restore.status_code == 200
        trash2 = api(client, "GET", "/api/v1/replies/trash")
        assert trash2.json()["data"]["total"] == 0
    finally:
        close_client(client)


def test_tickets_list_and_update(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, ticket={"status": "pending"})
    client = _authed_client(settings, session_factory)
    try:
        listing = api(client, "GET", "/api/v1/tickets")
        assert listing.status_code == 200
        data = listing.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["risk_level"] == "high"

        # resolving without an owner reply must be rejected
        bad = api(
            client,
            "PATCH",
            f"/api/v1/tickets/{ids['ticket_id']}",
            json={"status": "resolved"},
        )
        assert bad.status_code == 400
        assert bad.json()["detail"] == "OWNER_REPLY_REQUIRED"

        ok = api(
            client,
            "PATCH",
            f"/api/v1/tickets/{ids['ticket_id']}",
            json={"status": "resolved", "owner_reply_cn": "已联系客户解决"},
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["status"] == "resolved"

        resolved = api(client, "GET", "/api/v1/tickets", params={"status": "resolved"})
        assert resolved.json()["data"]["total"] == 1
    finally:
        close_client(client)


def test_ticket_trash_and_restore(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, ticket={"status": "pending"})
    client = _authed_client(settings, session_factory)
    try:
        assert api(client, "DELETE", f"/api/v1/tickets/{ids['ticket_id']}").status_code == 200
        trash = api(client, "GET", "/api/v1/tickets/trash")
        assert trash.json()["data"]["total"] == 1
        assert api(client, "POST", f"/api/v1/tickets/{ids['ticket_id']}/restore").status_code == 200
        assert api(client, "GET", "/api/v1/tickets/trash").json()["data"]["total"] == 0
    finally:
        close_client(client)


def test_split_and_merge_conversations(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=2)
    client = _authed_client(settings, session_factory)
    try:
        split = api(
            client,
            "POST",
            f"/api/v1/conversations/{ids['conversation_id']}/split",
            json={"at_email_id": ids["email_ids"][1]},
        )
        assert split.status_code == 200, split.text
        new_id = split.json()["data"]["new_conversation_id"]
        assert new_id != ids["conversation_id"]

        with session_factory() as db:
            original_count = len(
                db.execute(
                    select(Email).where(Email.conversation_id == ids["conversation_id"])
                ).scalars().all()
            )
            new_count = len(
                db.execute(select(Email).where(Email.conversation_id == new_id)).scalars().all()
            )
        assert (original_count, new_count) == (1, 1)

        merge = api(
            client,
            "POST",
            f"/api/v1/conversations/{ids['conversation_id']}/merge",
            json={"other_conversation_id": new_id},
        )
        assert merge.status_code == 200
        with session_factory() as db:
            count = len(
                db.execute(
                    select(Email).where(Email.conversation_id == ids["conversation_id"])
                ).scalars().all()
            )
        assert count == 2
    finally:
        close_client(client)


def test_merge_moves_tickets_to_target_conversation(
    settings, session_factory
) -> None:
    """Phase 3: merging a conversation that owns a Ticket must not 500."""

    with session_factory() as db:
        customer = Customer(
            email="merge-ticket@example.com", display_name="Merge", created_at=utcnow()
        )
        db.add(customer)
        db.flush()
        conv_a = Conversation(
            customer_id=customer.id,
            subject_normalized="thread a",
            window_end=utcnow(),
            last_activity_at=utcnow(),
            status="open",
        )
        conv_b = Conversation(
            customer_id=customer.id,
            subject_normalized="thread b",
            window_end=utcnow(),
            last_activity_at=utcnow(),
            status="open",
        )
        db.add_all([conv_a, conv_b])
        db.flush()
        for conv in (conv_a, conv_b):
            db.add(
                Email(
                    conversation_id=conv.id,
                    message_id=f"<merge-ticket-{conv.id}@example.com>",
                    subject="Order question",
                    from_email="merge-ticket@example.com",
                    to_email="bot@example.com",
                    body_text="body",
                    is_inbound=True,
                    received_at=utcnow(),
                )
            )
        db.flush()
        ticket = Ticket(
            conversation_id=conv_b.id,
            summary_cn="高风险工单",
            risk_level="high",
            status="pending",
            sla_deadline=utcnow(),
            created_at=utcnow(),
        )
        db.add(ticket)
        db.commit()
        target_id, other_id, ticket_id = conv_a.id, conv_b.id, ticket.id

    client = _authed_client(settings, session_factory)
    try:
        resp = api(
            client,
            "POST",
            f"/api/v1/conversations/{target_id}/merge",
            json={"other_conversation_id": other_id},
        )
        assert resp.status_code == 200, resp.text
        with session_factory() as db:
            moved = db.get(Ticket, ticket_id)
            assert moved is not None
            assert moved.conversation_id == target_id
            assert db.get(Conversation, other_id) is None
    finally:
        close_client(client)


def test_attachment_download(settings, session_factory, tmp_path) -> None:
    settings = settings.model_copy(update={"attachment_dir": str(tmp_path)})
    attach_dir = tmp_path
    attach_dir.mkdir(parents=True, exist_ok=True)
    (attach_dir / "photo.png").write_bytes(b"PNG-DATA")

    ids = _seed_conversation(session_factory)
    with session_factory() as db:
        email_id = ids["email_ids"][0]
        attachment = Attachment(
            email_id=email_id,
            filename="photo.png",
            content_type="image/png",
            size_bytes=8,
            stored_path=f"{attach_dir.name}/photo.png",
            created_at=utcnow(),
        )
        db.add(attachment)
        db.commit()
        attachment_id = attachment.id

    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", f"/api/v1/attachments/{attachment_id}")
        assert resp.status_code == 200
        assert resp.content == b"PNG-DATA"
        assert resp.headers["content-type"] == "image/png"
    finally:
        close_client(client)
