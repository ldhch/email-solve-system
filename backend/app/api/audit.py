"""Audit log query API (M-17, TECH 5.8)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.db.session import get_db
from app.models.audit import AuditLog
from app.models.user import User

router = APIRouter(prefix="/api/v1", tags=["audit"])


def _fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds") + "Z"


@router.get("/audit-logs")
async def list_audit_logs(
    action: str | None = Query(default=None),
    actor_id: int | None = Query(default=None),
    from_at: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated audit logs, newest first, with optional filters."""

    filters = []
    if action:
        filters.append(AuditLog.action == action)
    if actor_id is not None:
        filters.append(AuditLog.actor_id == actor_id)
    if from_at is not None:
        filters.append(AuditLog.at >= from_at)
    if to is not None:
        filters.append(AuditLog.at <= to)

    total = db.execute(
        select(func.count()).select_from(AuditLog).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()
    items = [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "actor_name": (
                db.get(User, row.actor_id).username
                if row.actor_id is not None and db.get(User, row.actor_id) is not None
                else None
            ),
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "ip": row.ip,
            "at": _fmt(row.at),
        }
        for row in rows
    ]
    return ok({"items": items, "total": total, "page": page})
