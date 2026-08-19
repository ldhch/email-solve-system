"""Inbox + attachment APIs (M-15, TECH 5.2).

The inbox list is conversation-level: emails from the same customer that belong
to one conversation are folded into a single row showing the latest activity,
so the owner scans one thread at a time instead of one email at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket

router = APIRouter(prefix="/api/v1", tags=["inbox"])

_RISK_ORDER = {"high": 3, "medium": 2, "low": 1}


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _latest_reply_per_conversation(db: Session) -> dict[int, Reply]:
    """Map conversation_id -> newest non-deleted reply (SQLite-friendly)."""

    rows = db.execute(
        select(Reply.conversation_id, func.max(Reply.id).label("max_id"))
        .where(Reply.is_deleted.is_(False))
        .group_by(Reply.conversation_id)
    ).all()
    if not rows:
        return {}
    ids = [row.max_id for row in rows]
    replies = db.execute(select(Reply).where(Reply.id.in_(ids))).scalars().all()
    return {r.conversation_id: r for r in replies}


def _latest_sla_deadline_per_conversation(db: Session) -> dict[int, datetime]:
    """Map conversation_id -> latest open-ticket SLA deadline."""

    rows = db.execute(
        select(Ticket.conversation_id, func.max(Ticket.sla_deadline))
        .where(Ticket.is_deleted.is_(False), Ticket.status != "resolved")
        .group_by(Ticket.conversation_id)
    ).all()
    return {conversation_id: deadline for conversation_id, deadline in rows}


def _conversation_rows(
    db: Session,
    *,
    risk_level: str | None,
    status: str | None,
    keyword: str | None,
    sort: str = "latest",
    conv_status: str | None = None,
    unread_only: bool = False,
) -> list[dict]:
    """Fold emails into one row per conversation.

    Data volume is small (tens of emails/day), so aggregation happens in Python
    after a single email scan — the same approach the previous email-level
    inbox used for filtering and pagination.
    """

    emails = db.execute(
        select(Email).order_by(Email.conversation_id, Email.received_at)
    ).scalars().all()
    conv_emails: dict[int, list[Email]] = {}
    for e in emails:
        conv_emails.setdefault(e.conversation_id, []).append(e)

    latest_replies = _latest_reply_per_conversation(db)
    sla_deadlines = _latest_sla_deadline_per_conversation(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    conversations = {
        c.id: c
        for c in db.execute(
            select(Conversation).where(Conversation.id.in_(conv_emails))
        ).scalars()
    }
    customers = {
        c.id: c
        for c in db.execute(
            select(Customer).where(
                Customer.id.in_([conversations[cid].customer_id for cid in conversations])
            )
        ).scalars()
    }

    rows: list[dict] = []
    for cid, e_list in conv_emails.items():
        conv = conversations[cid]
        customer = customers.get(conv.customer_id)
        latest_email = e_list[-1]  # ordered by received_at asc
        reply = latest_replies.get(cid)

        # Highest risk seen in the thread wins, so an early high-risk email is
        # never hidden by later low-key follow-ups.
        risk = max(
            (e.risk_level for e in e_list if e.risk_level in _RISK_ORDER),
            key=lambda r: _RISK_ORDER[r],
            default=None,
        ) or conv.risk_level

        unread = sum(1 for e in e_list if not e.is_read)
        reply_ts = (reply.sent_at or reply.created_at) if reply else None
        email_ts = latest_email.received_at

        # Summary = content of the most recent activity (inbound or outbound).
        if reply_ts is not None and reply_ts >= email_ts:
            summary = reply.content_cn or reply.content_en or latest_email.summary_cn
            latest_status = reply.status
            latest_at = reply_ts
        else:
            summary = (
                latest_email.summary_cn
                or latest_email.body_text
                or latest_email.subject
            )
            latest_status = reply.status if reply else None
            latest_at = email_ts

        if keyword:
            kw = keyword.lower()
            if not any(
                kw in (e.subject or "").lower()
                or kw in (e.from_email or "").lower()
                or (e.summary_cn and kw in e.summary_cn.lower())
                for e in e_list
            ) and not (customer and kw in (customer.email or "").lower()):
                continue
        if risk_level and risk != risk_level:
            continue
        if status and latest_status != status:
            continue
        if conv_status and conv.status != conv_status:
            continue
        if unread_only and unread == 0:
            continue

        deadline = sla_deadlines.get(cid)

        rows.append(
            {
                "id": cid,
                "subject": latest_email.subject,
                "from_email": latest_email.from_email,
                "customer_name": customer.display_name if customer else None,
                "email_count": len(e_list),
                "unread_count": unread,
                "risk_level": risk,
                "summary_cn": summary,
                "latest_status": latest_status,
                "latest_at": _fmt(latest_at),
                "sla_deadline": _fmt(deadline),
                "sla_breached": bool(deadline and now > deadline),
                "sla_near": bool(
                    deadline and now <= deadline and deadline - now <= timedelta(hours=2)
                ),
                "_latest_at": latest_at,
                "is_read": unread == 0,
            }
        )

    def _latest_ts(row: dict) -> float:
        latest = row["_latest_at"]
        return latest.timestamp() if latest else 0.0

    if sort == "unread":
        rows.sort(key=lambda r: (-r["unread_count"], -_latest_ts(r)))
    elif sort == "risk":
        rows.sort(
            key=lambda r: (-_RISK_ORDER.get(r["risk_level"] or "", 0), -_latest_ts(r))
        )
    else:
        rows.sort(key=lambda r: -_latest_ts(r))

    for row in rows:
        row.pop("_latest_at", None)
    return rows


@router.get("/inbox")
async def list_inbox(
    risk_level: str | None = Query(default=None),
    status: str | None = Query(default=None),
    sort: str = Query(default="latest"),
    conv_status: str | None = Query(default=None),
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Conversation-level inbox list with risk/status/keyword filters."""

    rows = _conversation_rows(
        db,
        risk_level=risk_level,
        status=status,
        keyword=keyword,
        sort=sort,
        conv_status=conv_status,
        unread_only=unread_only,
    )
    total = len(rows)
    start = (page - 1) * size
    return ok({"items": rows[start : start + size], "total": total, "page": page})


@router.get("/inbox/unread-count")
async def inbox_unread_count(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Conversations with at least one unread email (not raw email count)."""

    convs = db.execute(
        select(Email.conversation_id).where(Email.is_read.is_(False)).distinct()
    ).all()
    return ok({"unread": len(convs)})


@router.post("/inbox/{email_id}/read")
async def mark_email_read(
    email_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    email.is_read = True
    db.commit()
    return ok({"id": email.id, "is_read": True})


@router.post("/inbox/conversations/{conversation_id}/read")
async def mark_conversation_read(
    conversation_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Mark every unread email in a conversation as read."""

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    db.execute(
        update(Email)
        .where(Email.conversation_id == conversation_id, Email.is_read.is_(False))
        .values(is_read=True)
    )
    db.commit()
    return ok({"id": conversation_id, "is_read": True})


@router.get("/inbox/{email_id}")
async def get_inbox_email(
    email_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    attachments = db.execute(
        select(Attachment).where(Attachment.email_id == email.id)
    ).scalars().all()
    return ok(
        {
            "id": email.id,
            "conversation_id": email.conversation_id,
            "subject": email.subject,
            "from_email": email.from_email,
            "to_email": email.to_email,
            "body_text": email.body_text,
            "body_html": email.body_html,
            "summary_cn": email.summary_cn,
            "category": email.category,
            "risk_level": email.risk_level,
            "confidence": email.confidence,
            "received_at": _fmt(email.received_at),
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in attachments
            ],
        }
    )


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> Response:
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    data_dir = Path(settings.attachment_dir).parent
    path = data_dir / attachment.stored_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="FILE_MISSING")
    # Plain bytes response: FileResponse streams via anyio's thread pool, which
    # is unavailable in some sandboxed environments (see Phase 1 notes). Files
    # are capped at 20MB, so in-memory reading is acceptable for this admin UI.
    return Response(
        content=path.read_bytes(),
        media_type=attachment.content_type,
        headers={"Content-Disposition": f'attachment; filename="{attachment.filename}"'},
    )
