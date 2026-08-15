"""Inbox + attachment APIs (M-15, TECH 5.2)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.db.session import get_db
from app.models.attachment import Attachment
from app.models.email import Email
from app.models.reply import Reply

router = APIRouter(prefix="/api/v1", tags=["inbox"])


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _latest_reply_status(db: Session) -> dict[int, str]:
    """Map email_id -> status of its newest reply (SQLite-friendly)."""

    rows = db.execute(
        select(Reply.email_id, func.max(Reply.id).label("max_id")).group_by(Reply.email_id)
    ).all()
    if not rows:
        return {}
    ids = [row.max_id for row in rows]
    status_map = dict(
        db.execute(select(Reply.id, Reply.status).where(Reply.id.in_(ids))).all()
    )
    return {row.email_id: status_map[row.max_id] for row in rows}


@router.get("/inbox")
async def list_inbox(
    risk_level: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated email list with risk filter and latest-reply status filter."""

    filters = []
    if risk_level:
        filters.append(Email.risk_level == risk_level)
    if keyword:
        like = f"%{keyword}%"
        filters.append(
            or_(
                Email.subject.ilike(like),
                Email.from_email.ilike(like),
                Email.summary_cn.ilike(like),
            )
        )

    emails = db.execute(
        select(Email).where(*filters).order_by(Email.received_at.desc())
    ).scalars().all()

    latest = _latest_reply_status(db)
    if status and status != "all":
        emails = [e for e in emails if latest.get(e.id) == status]

    total = len(emails)
    start = (page - 1) * size
    page_emails = emails[start : start + size]
    items = [
        {
            "id": e.id,
            "conversation_id": e.conversation_id,
            "subject": e.subject,
            "from_email": e.from_email,
            "risk_level": e.risk_level,
            "confidence": e.confidence,
            "summary_cn": e.summary_cn,
            "received_at": _fmt(e.received_at),
            "status": latest.get(e.id),
        }
        for e in page_emails
    ]
    return ok({"items": items, "total": total, "page": page})


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
