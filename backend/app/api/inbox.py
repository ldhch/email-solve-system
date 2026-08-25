"""Inbox + attachment APIs (M-15, TECH 5.2).

The inbox list is conversation-level: emails from the same customer that belong
to one conversation are folded into a single row showing the latest activity,
so the owner scans one thread at a time instead of one email at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import func, select
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
from app.services.conversation import followup_count
from app.services.ingest import mark_seen_on_server

router = APIRouter(prefix="/api/v1", tags=["inbox"])

# "unknown" sorts last (lowest) so a conversation that only contains
# unclassifiable mail surfaces as「无法判定」and can be filtered by risk=unknown.
_RISK_ORDER = {"high": 3, "medium": 2, "low": 1, "unknown": 0}


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _latest_reply_per_conversation(db: Session) -> dict[int, Reply]:
    """Map conversation_id -> newest non-deleted reply (SQLite-friendly)."""

    rows = db.execute(
        select(Reply.conversation_id, func.max(Reply.id).label("max_id"))
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
        .where(Ticket.status != "resolved")
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
    ad_only: bool = False,
    archived_only: bool = False,
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
    # Customer follow-ups while waiting for a first real reply, per
    # conversation, so the inbox can badge「追问 N」on conversations that
    # cannot be left sitting (conversation count is small; derived on demand).
    followup_counts = {cid: followup_count(db, cid) for cid in conv_emails}
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

    # Image attachment count per email, so each inbox row can show how many
    # photos the thread carries (the "图×N" badge). Only image attachments
    # count — the badge is about photos, not PDFs.
    all_email_ids = [e.id for e_list in conv_emails.values() for e in e_list]
    image_count_by_email = dict(
        db.execute(
            select(Attachment.email_id, func.count(Attachment.id))
            .where(
                Attachment.email_id.in_(all_email_ids),
                Attachment.content_type.like("image/%"),
            )
            .group_by(Attachment.email_id)
        ).all()
    )

    rows: list[dict] = []
    for cid, e_list in conv_emails.items():
        conv = conversations[cid]
        customer = customers.get(conv.customer_id)
        latest_email = e_list[-1]  # ordered by received_at asc
        reply = latest_replies.get(cid)

        # Ad conversations only surface on the「广告」tab; every other view
        # hides them so marketing mail never pollutes the real inbox.
        is_ad_conv = any(e.is_ad for e in e_list)
        if ad_only and not is_ad_conv:
            continue
        if not ad_only and is_ad_conv:
            continue

        # Archived conversations only surface on the「已归档」tab; every other
        # view hides them so the inbox stays focused on active threads.
        if archived_only and not conv.is_archived:
            continue
        if not archived_only and conv.is_archived:
            continue

        # Highest risk seen in the thread wins, so an early high-risk email is
        # never hidden by later low-key follow-ups. "unknown" outranks "low":
        # a thread that contains an unclassifiable mail surfaces as「无法判定」
        # so the boss never misses the manual item behind a low-risk label.
        risks = [e.risk_level for e in e_list if e.risk_level in _RISK_ORDER]
        if "high" in risks:
            risk = "high"
        elif "medium" in risks:
            risk = "medium"
        elif "unknown" in risks:
            risk = "unknown"
        elif "low" in risks:
            risk = "low"
        else:
            risk = conv.risk_level

        unread = sum(1 for e in e_list if not e.is_read)
        reply_ts = (reply.sent_at or reply.created_at) if reply else None
        email_ts = latest_email.received_at

        # Summary = content of the most recent activity (inbound or outbound).
        if reply_ts is not None and reply_ts >= email_ts:
            summary = reply.content_cn or reply.content_en or latest_email.summary_cn
            latest_status = reply.status
            latest_at = reply_ts
            latest_kind = "reply_sent" if reply.status == "sent" else "reply_pending"
        else:
            summary = (
                latest_email.summary_cn
                or latest_email.body_text
                or latest_email.subject
            )
            # The newest activity is the customer's email, not our reply: a
            # "sent"/"failed" status from an older reply would misread as
            # "this email was answered", so those are suppressed. An older
            # draft / pending-review reply is still the boss's to-do — keep it
            # visible so it never silently drops off the「待审核」worklist.
            latest_status = (
                reply.status
                if reply and reply.status in ("pending_review", "draft")
                else None
            )
            latest_at = email_ts
            latest_kind = "email"

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
        # 「无法判定」 is a to-do list, not an archive: the badge only counts
        # unknown conversations that still carry unread mail, so it shrinks once
        # the boss has looked at the unclassifiable item instead of growing
        # forever.
        if risk_level == "unknown" and unread == 0:
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
                "followup_count": followup_counts.get(cid, 0),
                "has_attachments": any(e.has_attachments for e in e_list),
                "attachment_count": sum(
                    image_count_by_email.get(e.id, 0) for e in e_list
                ),
                "unread_count": unread,
                "is_ad": is_ad_conv,
                "risk_level": risk,
                "summary_cn": summary,
                "latest_status": latest_status,
                "latest_at": _fmt(latest_at),
                "latest_kind": latest_kind,
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
    ad: bool = Query(default=False),
    archived: bool = Query(default=False),
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
        ad_only=ad,
        archived_only=archived,
    )
    total = len(rows)
    start = (page - 1) * size
    return ok({"items": rows[start : start + size], "total": total, "page": page})


@router.get("/inbox/counts")
async def inbox_counts(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """One request returns every inbox-tab badge count (M-15 inbox UX).

    Each count reuses _conversation_rows with the tab's own filter params, so a
    badge always matches exactly what that tab's list shows — no drift between
    the red number and the rows below it. Declared before /inbox/{email_id} so
    the literal "counts" path wins over the {email_id} path parameter.
    """

    return ok(
        {
            "unread": len(
                _conversation_rows(db, risk_level=None, status=None, keyword=None, unread_only=True)
            ),
            "pending_review": len(
                _conversation_rows(db, risk_level=None, status="pending_review", keyword=None)
            ),
            "unknown": len(
                _conversation_rows(db, risk_level="unknown", status=None, keyword=None)
            ),
            "high": len(
                _conversation_rows(db, risk_level="high", status=None, keyword=None)
            ),
            "ad": len(
                _conversation_rows(db, risk_level=None, status=None, keyword=None, ad_only=True)
            ),
            "archived": len(
                _conversation_rows(db, risk_level=None, status=None, keyword=None, archived_only=True)
            ),
        }
    )


@router.get("/inbox/unread-count")
async def inbox_unread_count(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Unread email totals for the navigation badge.

    ``unread_emails`` is the number of individual unread customer emails and
    ``unread`` / ``unread_conversations`` keep the older conversation-level
    meaning for the inbox「未读」tab and any existing API consumers.
    """

    unread_emails = db.scalar(
        select(func.count(Email.id)).where(
            Email.is_read.is_(False), Email.is_ad.is_(False)
        )
    ) or 0
    convs = db.execute(
        select(Email.conversation_id)
        .where(Email.is_read.is_(False), Email.is_ad.is_(False))
        .distinct()
    ).all()
    return ok(
        {
            "unread": len(convs),
            "unread_conversations": len(convs),
            "unread_emails": unread_emails,
        }
    )


@router.post("/inbox/{email_id}/read")
async def mark_email_read(
    email_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    email.is_read = True
    db.commit()
    # Mirror the read to the mailbox so webmail unread state follows the boss.
    if email.imap_uid:
        mark_seen_on_server(settings, [email.imap_uid])
    return ok({"id": email.id, "is_read": True})


@router.post("/inbox/conversations/{conversation_id}/read")
async def mark_conversation_read(
    conversation_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Mark every unread email in a conversation as read."""

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    unread = db.execute(
        select(Email).where(
            Email.conversation_id == conversation_id, Email.is_read.is_(False)
        )
    ).scalars().all()
    uids = [e.imap_uid for e in unread if e.imap_uid]
    for e in unread:
        e.is_read = True
    db.commit()
    # Mirror the read to the mailbox so webmail unread state follows the boss.
    mark_seen_on_server(settings, uids)
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


# Thumbnail cache: tiny JPEGs (tens of KB) keyed by attachment id, so the
# inbox thumbnail grid does not re-decode 3-4MB phone photos on every render
# or 5s poll. Bounded so a long-lived process cannot grow it unboundedly.
_thumb_cache: dict[int, bytes] = {}


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: int,
    thumb: bool = False,
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

    # Thumbnails: downscale the image to a ~256px square so the attachment grid
    # downloads tens of KB instead of the full multi-megabyte original. Non-
    # image files are served unchanged regardless of the flag.
    if thumb and (attachment.content_type or "").startswith("image/"):
        cached = _thumb_cache.get(attachment_id)
        if cached is not None:
            return Response(content=cached, media_type="image/jpeg")
        try:
            with Image.open(path) as im:
                im.thumbnail((256, 256))
                buf = BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=82)
                data = buf.getvalue()
        except Exception:
            # Corrupt / un-decodable image: fall back to the original rather
            # than failing the whole thumbnail grid.
            return Response(
                content=path.read_bytes(),
                media_type=attachment.content_type,
            )
        if len(_thumb_cache) < 512:
            _thumb_cache[attachment_id] = data
        return Response(content=data, media_type="image/jpeg")

    # Plain bytes response: FileResponse streams via anyio's thread pool, which
    # is unavailable in some sandboxed environments (see Phase 1 notes). Files
    # are capped at 20MB, so in-memory reading is acceptable for this admin UI.
    return Response(
        content=path.read_bytes(),
        media_type=attachment.content_type,
        headers={"Content-Disposition": f'attachment; filename="{attachment.filename}"'},
    )
