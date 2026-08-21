"""Quick reply templates CRUD (M-15, replaces the standalone ticket page's
nav slot with a small management UI).

The boss maintains a handful of canned Chinese replies; the conversation reply
box renders them as one-click fill buttons. Templates are the boss's own data
— add / edit / delete here propagates to the editor on the next load.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.db.session import get_db
from app.models.reply_template import (
    DEFAULT_REPLY_TEMPLATES,
    ReplyTemplate,
)
from app.schemas.admin import (
    ReplyTemplateCreateRequest,
    ReplyTemplateUpdateRequest,
)
from app.services.audit import log_action, utcnow

router = APIRouter(prefix="/api/v1", tags=["reply-templates"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _serialize(t: ReplyTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "content": t.content,
        "sort_order": t.sort_order,
    }


@router.get("/reply-templates")
async def list_reply_templates(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    if db.scalar(select(func.count(ReplyTemplate.id))) == 0:
        for order, (name, content) in enumerate(DEFAULT_REPLY_TEMPLATES):
            db.add(
                ReplyTemplate(
                    name=name,
                    content=content,
                    sort_order=order,
                    created_at=utcnow(),
                )
            )
        db.commit()
    rows = (
        db.execute(
            select(ReplyTemplate).order_by(
                ReplyTemplate.sort_order, ReplyTemplate.id
            )
        )
        .scalars()
        .all()
    )
    return ok({"items": [_serialize(t) for t in rows]})


@router.post("/reply-templates")
async def create_reply_template(
    payload: ReplyTemplateCreateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    name = payload.name.strip()
    content = payload.content.strip()
    if not name or not content:
        raise HTTPException(status_code=400, detail="EMPTY_CONTENT")
    max_order = db.scalar(select(func.max(ReplyTemplate.sort_order))) or 0
    template = ReplyTemplate(
        name=name,
        content=content,
        sort_order=max_order + 1,
        created_at=utcnow(),
    )
    db.add(template)
    db.flush()  # assign template.id before the audit row references it
    log_action(
        db,
        "template_created",
        "reply_template",
        template.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(_serialize(template))


@router.patch("/reply-templates/{template_id}")
async def update_reply_template(
    template_id: int,
    payload: ReplyTemplateUpdateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    template = db.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="EMPTY_CONTENT")
        template.name = name
    if payload.content is not None:
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="EMPTY_CONTENT")
        template.content = content
    if payload.sort_order is not None:
        template.sort_order = payload.sort_order
    log_action(
        db,
        "template_updated",
        "reply_template",
        template.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(_serialize(template))


@router.delete("/reply-templates/{template_id}")
async def delete_reply_template(
    template_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    template = db.get(ReplyTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    db.delete(template)
    log_action(
        db,
        "template_deleted",
        "reply_template",
        template_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"template_id": template_id})
