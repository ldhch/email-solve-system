"""Blocked-sender blacklist CRUD.

The boss blocks a marketing sender (``scope=email``) or a whole domain
(``scope=domain``); matching inbound mail is then archived to the「广告」tab and
never auto-replied. Duplicate entries are rejected.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.db.session import get_db
from app.models.blocked_sender import BlockedSender
from app.schemas.admin import BlockedSenderCreateRequest
from app.services.audit import log_action, utcnow
from app.services.blocked_senders import normalize_blocked_value

router = APIRouter(prefix="/api/v1", tags=["blocked-senders"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _serialize(b: BlockedSender) -> dict:
    return {"id": b.id, "value": b.value, "scope": b.scope, "created_at": _fmt(b.created_at)}


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


@router.get("/blocked-senders")
async def list_blocked_senders(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    rows = (
        db.execute(select(BlockedSender).order_by(BlockedSender.id.desc()))
        .scalars()
        .all()
    )
    return ok({"items": [_serialize(b) for b in rows]})


@router.post("/blocked-senders")
async def create_blocked_sender(
    payload: BlockedSenderCreateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    try:
        value = normalize_blocked_value(payload.value, payload.scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="EMPTY_VALUE") from None
    if payload.scope == "email" and not _EMAIL_RE.match(value):
        raise HTTPException(status_code=400, detail="INVALID_EMAIL")
    if payload.scope == "domain" and (
        not value
        or "@" in value
        or "." not in value
        or not re.match(r"^[a-z0-9.-]+$", value)
    ):
        raise HTTPException(status_code=400, detail="INVALID_DOMAIN")
    existing = db.execute(
        select(BlockedSender).where(
            BlockedSender.value == value, BlockedSender.scope == payload.scope
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="ALREADY_BLOCKED")
    entry = BlockedSender(
        value=value,
        scope=payload.scope,
        created_at=utcnow(),
    )
    db.add(entry)
    db.flush()  # assign entry.id before the audit row references it
    log_action(
        db,
        "blocked_sender_created",
        "blocked_sender",
        entry.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(_serialize(entry))


@router.delete("/blocked-senders/{sender_id}")
async def delete_blocked_sender(
    sender_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    entry = db.get(BlockedSender, sender_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    db.delete(entry)
    log_action(
        db,
        "blocked_sender_deleted",
        "blocked_sender",
        sender_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"sender_id": sender_id})
