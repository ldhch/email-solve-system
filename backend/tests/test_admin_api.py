"""Admin REST API integration tests (M-15, TECH 5.2/5.3/5.4)."""

from __future__ import annotations

from datetime import timedelta

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
from app.llm.client import MockLLMClient
from app.services.replier import ReplierService, sanitize_reply_text
from app.services.translator import TranslatorService

from api_helpers import api, close_client, login, make_client, seed_owner
from conftest import FakeSMTP


def _seed_conversation(
    session_factory,
    *,
    emails: int = 1,
    reply: dict | None = None,
    ticket: dict | None = None,
    customer_email: str = "c@example.com",
    email_risk: str = "medium",
    is_ad: bool = False,
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
                from_email=customer_email,
                to_email="bot@example.com",
                body_text=f"Question body {i}",
                is_inbound=True,
                received_at=utcnow(),
                risk_level=email_risk,
                is_ad=is_ad,
                is_read=is_ad,  # ad mail never counts toward unread
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
                source=reply.get("source", "system"),
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


def test_inbox_has_attachments_flag(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=1)
    with session_factory() as db:
        email = db.get(Email, ids["email_ids"][0])
        email.has_attachments = True
        db.add(
            Attachment(
                email_id=email.id,
                filename="photo.png",
                content_type="image/png",
                size_bytes=8,
                stored_path="attachments/photo.png",
                created_at=utcnow(),
            )
        )
        db.commit()
    client = _authed_client(settings, session_factory)
    try:
        # the inbox row exposes the paperclip flag + image count so the boss
        # sees which conversations carry photos before opening them
        listing = api(client, "GET", "/api/v1/inbox").json()["data"]["items"]
        assert listing[0]["has_attachments"] is True
        assert listing[0]["attachment_count"] == 1

        # and the conversation timeline carries the attachment entry for the
        # frontend to render thumbnails / download chips
        data = api(
            client, "GET", f"/api/v1/conversations/{ids['conversation_id']}"
        ).json()["data"]
        attachment_item = next(
            t for t in data["timeline"] if t["type"] == "attachment"
        )
        assert attachment_item["attachment_id"] is not None
        assert attachment_item["filename"] == "photo.png"
        assert attachment_item["content_type"] == "image/png"
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


def test_inbox_unread_sort_and_conv_status(settings, session_factory) -> None:
    ids_a = _seed_conversation(session_factory, customer_email="a@example.com")
    ids_b = _seed_conversation(session_factory, customer_email="b@example.com")
    with session_factory() as db:
        conv_a = db.get(Conversation, ids_a["conversation_id"])
        conv_a.status = "resolved"
        email_a = db.get(Email, ids_a["email_ids"][0])
        email_a.is_read = True
        db.commit()

    client = _authed_client(settings, session_factory)
    try:
        unread = api(client, "GET", "/api/v1/inbox", params={"sort": "unread"})
        assert unread.status_code == 200
        items = unread.json()["data"]["items"]
        assert [i["id"] for i in items] == [
            ids_b["conversation_id"],
            ids_a["conversation_id"],
        ]

        resolved = api(
            client, "GET", "/api/v1/inbox", params={"conv_status": "resolved"}
        )
        items = resolved.json()["data"]["items"]
        assert [i["id"] for i in items] == [ids_a["conversation_id"]]

        unread_only = api(
            client, "GET", "/api/v1/inbox", params={"unread_only": True}
        )
        items = unread_only.json()["data"]["items"]
        assert [i["id"] for i in items] == [ids_b["conversation_id"]]
    finally:
        close_client(client)


def test_inbox_risk_sort(settings, session_factory) -> None:
    ids_low = _seed_conversation(session_factory, customer_email="low@example.com")
    ids_high = _seed_conversation(session_factory, customer_email="high@example.com")
    with session_factory() as db:
        db.get(Conversation, ids_low["conversation_id"]).risk_level = "low"
        db.get(Conversation, ids_high["conversation_id"]).risk_level = "high"
        db.get(Email, ids_low["email_ids"][0]).risk_level = "low"
        db.get(Email, ids_high["email_ids"][0]).risk_level = "high"
        db.commit()

    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/inbox", params={"sort": "risk"})
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert [i["id"] for i in items] == [
            ids_high["conversation_id"],
            ids_low["conversation_id"],
        ]
    finally:
        close_client(client)


def test_inbox_sla_flags(settings, session_factory) -> None:
    overdue = _seed_conversation(
        session_factory,
        customer_email="overdue@example.com",
        ticket={"status": "pending"},
    )
    near = _seed_conversation(
        session_factory,
        customer_email="near@example.com",
        ticket={"status": "pending"},
    )
    plain = _seed_conversation(
        session_factory,
        customer_email="plain@example.com",
    )
    with session_factory() as db:
        db.get(Ticket, overdue["ticket_id"]).sla_deadline = utcnow() - timedelta(
            hours=1
        )
        db.get(Ticket, near["ticket_id"]).sla_deadline = utcnow() + timedelta(hours=1)
        db.commit()

    client = _authed_client(settings, session_factory)
    try:
        body = api(client, "GET", "/api/v1/inbox").json()["data"]
        by_id = {item["id"]: item for item in body["items"]}

        overdue_row = by_id[overdue["conversation_id"]]
        assert overdue_row["sla_breached"] is True
        assert overdue_row["sla_near"] is False

        near_row = by_id[near["conversation_id"]]
        assert near_row["sla_breached"] is False
        assert near_row["sla_near"] is True

        plain_row = by_id[plain["conversation_id"]]
        assert plain_row["sla_deadline"] is None
        assert plain_row["sla_breached"] is False
        assert plain_row["sla_near"] is False
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
        assert reply_item["source"] == "system"
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
            assert reply.source == "manual"
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
            assert reply.source == "manual"

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
            assert reply.source == "system"
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


# ---------- quick reply templates + ticket merge ----------


def test_reply_templates_seed_and_crud(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        # first GET seeds the four defaults
        listing = api(client, "GET", "/api/v1/reply-templates")
        assert listing.status_code == 200, listing.text
        items = listing.json()["data"]["items"]
        assert [t["name"] for t in items] == ["退货", "物流", "补偿", "通用"]
        assert all(t["content"] for t in items)

        # create
        created = api(
            client,
            "POST",
            "/api/v1/reply-templates",
            json={"name": "发票", "content": "发票已通过邮件发送给您。"},
        )
        assert created.status_code == 200, created.text
        new_id = created.json()["data"]["id"]

        # update
        updated = api(
            client,
            "PATCH",
            f"/api/v1/reply-templates/{new_id}",
            json={"name": "发票重发", "content": "我们重新给您发送发票，请查收。"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["name"] == "发票重发"

        # delete
        deleted = api(client, "DELETE", f"/api/v1/reply-templates/{new_id}")
        assert deleted.status_code == 200
        after = api(client, "GET", "/api/v1/reply-templates").json()["data"]["items"]
        assert all(t["id"] != new_id for t in after)
    finally:
        close_client(client)


def test_conversation_detail_includes_open_tickets(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, ticket={"status": "pending"})
    client = _authed_client(settings, session_factory)
    try:
        resp = api(client, "GET", f"/api/v1/conversations/{ids['conversation_id']}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["open_tickets"]) == 1
        assert data["open_tickets"][0]["status"] == "pending"
        assert data["open_tickets"][0]["sla_deadline"] is not None
        assert data["resolved_ticket_count"] == 0
    finally:
        close_client(client)


def test_manual_reply_auto_resolves_open_ticket(
    settings, session_factory, monkeypatch
) -> None:
    ids = _seed_conversation(
        session_factory,
        emails=1,
        ticket={"status": "pending"},
    )
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
            json={"content_cn": "非常抱歉，我们马上为您处理。"},
        )
        assert resp.status_code == 200, resp.text
        with session_factory() as db:
            ticket = db.get(Ticket, ids["ticket_id"])
            assert ticket.status == "resolved"
            assert ticket.resolved_at is not None
            actions = {a.action for a in db.query(AuditLog).all()}
        assert "ticket_resolved" in actions
    finally:
        close_client(client)


# ---------- reply letter format (sanitize + translate-to-letter) ----------


def test_sanitize_reply_text_strips_quotes_and_preamble() -> None:
    text = "Here is your reply:\n> > quoted\nPlease send order #1."
    assert sanitize_reply_text(text) == "Please send order #1."

    cn = "好的，这是完整的回复：\n请提供订单号。"
    assert sanitize_reply_text(cn) == "请提供订单号。"

    # a clean letter must pass through untouched
    normal = "Dear John,\nWe have processed your refund."
    assert sanitize_reply_text(normal) == normal


def test_translate_to_letter_passthrough_and_mock(settings) -> None:
    service = TranslatorService(MockLLMClient(settings))
    # no CJK -> pass through unchanged (boss already wrote English)
    assert service.translate_to_letter("Thanks for your help.") == "Thanks for your help."
    # CJK -> mock "translates" through the normal client path
    out = service.translate_to_letter("请提供订单号", customer_name="John")
    assert out.startswith("(Mock translation)")


def test_build_reply_sanitizes_content_en(settings, session_factory) -> None:
    ids = _seed_conversation(session_factory, emails=1)
    with session_factory() as db:
        conv = db.get(Conversation, ids["conversation_id"])
        email = db.get(Email, ids["email_ids"][0])
        reply = ReplierService(db, settings, MockLLMClient(settings)).build_reply(
            email,
            conv,
            "Here is your reply:\n> > quoted\nDear John,\nPlease provide order #1.",
        )
        db.commit()
        assert reply.content_en == "Dear John,\nPlease provide order #1."


# ---------- 无法判定 (unknown) / 广告 (ad) / 黑名单 (blocked senders) ----------


def test_inbox_unknown_filter(settings, session_factory) -> None:
    _seed_conversation(
        session_factory, customer_email="weird@example.com", email_risk="unknown"
    )
    _seed_conversation(session_factory, customer_email="a@example.com", email_risk="low")
    client = _authed_client(settings, session_factory)
    try:
        unknown = api(client, "GET", "/api/v1/inbox", params={"risk_level": "unknown"})
        body = unknown.json()["data"]
        assert body["total"] == 1
        assert body["items"][0]["from_email"] == "weird@example.com"
        assert body["items"][0]["risk_level"] == "unknown"
        low = api(client, "GET", "/api/v1/inbox", params={"risk_level": "low"})
        assert low.json()["data"]["total"] == 1
    finally:
        close_client(client)


def test_inbox_unknown_outranks_low_in_thread(settings, session_factory) -> None:
    """A thread mixing low-risk + unclassifiable mail surfaces as「无法判定」."""
    ids = _seed_conversation(
        session_factory, customer_email="mix@example.com", email_risk="low"
    )
    with session_factory() as db:
        db.add(
            Email(
                conversation_id=ids["conversation_id"],
                message_id="<mix-weird@example.com>",
                subject="weird",
                from_email="mix@example.com",
                body_text="gibberish body",
                is_inbound=True,
                received_at=utcnow(),
                risk_level="unknown",
                category="other",
                summary_cn="无法判定",
            )
        )
        db.commit()
    client = _authed_client(settings, session_factory)
    try:
        unknown = api(client, "GET", "/api/v1/inbox", params={"risk_level": "unknown"})
        assert unknown.json()["data"]["total"] == 1
        assert unknown.json()["data"]["items"][0]["risk_level"] == "unknown"
        low = api(client, "GET", "/api/v1/inbox", params={"risk_level": "low"})
        assert low.json()["data"]["total"] == 0
    finally:
        close_client(client)


def test_inbox_ad_filter_and_exclusion(settings, session_factory) -> None:
    _seed_conversation(
        session_factory, customer_email="promo@amazon.com", is_ad=True
    )
    _seed_conversation(session_factory, customer_email="real@example.com")
    client = _authed_client(settings, session_factory)
    try:
        all_rows = api(client, "GET", "/api/v1/inbox").json()["data"]
        assert all_rows["total"] == 1  # ad conversation hidden from the main list
        assert all_rows["items"][0]["from_email"] == "real@example.com"

        ad_rows = api(client, "GET", "/api/v1/inbox", params={"ad": "true"}).json()["data"]
        assert ad_rows["total"] == 1
        assert ad_rows["items"][0]["is_ad"] is True
        assert ad_rows["items"][0]["from_email"] == "promo@amazon.com"

        unread = api(client, "GET", "/api/v1/inbox/unread-count").json()["data"]
        assert unread["unread"] == 1  # ad mail never counts toward unread
    finally:
        close_client(client)


def test_blocked_senders_crud(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        r1 = api(
            client,
            "POST",
            "/api/v1/blocked-senders",
            json={"value": "spam@bad.com", "scope": "email"},
        )
        assert r1.status_code == 200
        assert r1.json()["data"]["scope"] == "email"

        dup = api(
            client,
            "POST",
            "/api/v1/blocked-senders",
            json={"value": "spam@bad.com", "scope": "email"},
        )
        assert dup.status_code == 409

        r2 = api(
            client,
            "POST",
            "/api/v1/blocked-senders",
            json={"value": "@amazon.com", "scope": "domain"},
        )
        assert r2.status_code == 200
        # "@" prefix is stripped for domains
        assert r2.json()["data"]["value"] == "amazon.com"

        bad_domain = api(
            client,
            "POST",
            "/api/v1/blocked-senders",
            json={"value": "@bad", "scope": "domain"},
        )
        assert bad_domain.status_code == 400
        bad_email = api(
            client,
            "POST",
            "/api/v1/blocked-senders",
            json={"value": "not-an-email", "scope": "email"},
        )
        assert bad_email.status_code == 400

        items = api(client, "GET", "/api/v1/blocked-senders").json()["data"]["items"]
        assert len(items) == 2

        deleted = api(client, "DELETE", f"/api/v1/blocked-senders/{items[0]['id']}")
        assert deleted.status_code == 200
        remaining = api(client, "GET", "/api/v1/blocked-senders").json()["data"]["items"]
        assert len(remaining) == 1
    finally:
        close_client(client)


def test_conversation_detail_marks_ad(settings, session_factory) -> None:
    ids = _seed_conversation(
        session_factory, customer_email="promo@shop.com", is_ad=True
    )
    client = _authed_client(settings, session_factory)
    try:
        detail = api(client, "GET", f"/api/v1/conversations/{ids['conversation_id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["is_ad"] is True
    finally:
        close_client(client)
