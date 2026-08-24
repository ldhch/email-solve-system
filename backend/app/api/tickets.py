"""Ticket admin APIs (M-15, TECH 5.4).

Phase 2 ships the management API + page; automatic ticket creation from
high-risk emails is Phase 3.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.db.session import get_db
from app.models.ticket import Ticket
from app.schemas.admin import TicketUpdateRequest
from app.services.audit import log_action, utcnow

router = APIRouter(prefix="/api/v1", tags=["tickets"])

TICKET_STATUSES = {"pending", "in_progress", "resolved"}


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/tickets")
async def list_tickets(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    now = utcnow()
    filters: list = []
    if status and status != "all":
        filters.append(Ticket.status == status)
    tickets = db.execute(
        select(Ticket).where(*filters).order_by(Ticket.sla_deadline.asc())
    ).scalars().all()
    total = len(tickets)
    page_tickets = tickets[(page - 1) * size : (page - 1) * size + size]
    items = [
        {
            "id": t.id,
            "conversation_id": t.conversation_id,
            "summary_cn": t.summary_cn,
            "sla_deadline": _fmt(t.sla_deadline),
            "risk_level": t.risk_level,
            "status": t.status,
            "age_minutes": int((now - t.created_at).total_seconds() // 60),
            "is_overdue": t.sla_deadline < now and t.status in ("pending", "in_progress"),
        }
        for t in page_tickets
    ]
    return ok({"items": items, "total": total, "page": page})


@router.patch("/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    payload: TicketUpdateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if payload.status is not None:
        if payload.status not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail="BAD_STATUS")
        if payload.status == "resolved" and not payload.owner_reply_cn:
            raise HTTPException(status_code=400, detail="OWNER_REPLY_REQUIRED")
        ticket.status = payload.status
        ticket.resolved_at = utcnow() if payload.status == "resolved" else None
    if payload.owner_reply_cn is not None:
        ticket.owner_reply_cn = payload.owner_reply_cn
    log_action(
        db,
        "ticket_updated",
        "ticket",
        ticket.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(
        {
            "id": ticket.id,
            "status": ticket.status,
            "resolved_at": _fmt(ticket.resolved_at),
        }
    )
