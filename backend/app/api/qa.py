"""Standard QA admin APIs (M-15/M-14, TECH 5.7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import ok
from app.db.session import get_db
from app.schemas.admin import (
    QAPairBulkRequest,
    QAPairCreateRequest,
    QAPairUpdateRequest,
)
from app.services.audit import log_action
from app.services.qa import QAService

router = APIRouter(prefix="/api/v1", tags=["qa-pairs"])


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _item(pair) -> dict:
    return {
        "id": pair.id,
        "question": pair.question,
        "answer": pair.answer,
        "category": pair.category,
        "enabled": pair.enabled,
        "updated_at": _fmt(pair.updated_at),
    }


@router.get("/qa-pairs")
async def list_qa_pairs(
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    pairs = QAService(db).list_all()
    return ok({"items": [_item(p) for p in pairs]})


@router.post("/qa-pairs")
async def create_qa_pair(
    payload: QAPairCreateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    pair = QAService(db).create(
        question=payload.question,
        answer=payload.answer,
        category=payload.category,
    )
    log_action(
        db,
        "qa_created",
        "qa",
        pair.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(_item(pair))


@router.post("/qa-pairs/bulk")
async def bulk_create_qa_pairs(
    payload: QAPairBulkRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Import many QA pairs at once; duplicates are skipped (M-14)."""

    items = [(i.question, i.answer, i.category) for i in payload.items]
    created, skipped = QAService(db).bulk_create(items)
    log_action(
        db,
        "qa_bulk_import",
        "qa",
        0,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"created": created, "skipped": skipped})


@router.patch("/qa-pairs/{pair_id}")
async def update_qa_pair(
    pair_id: int,
    payload: QAPairUpdateRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    pair = QAService(db).update(pair_id, **fields)
    if pair is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    log_action(
        db,
        "qa_updated",
        "qa",
        pair.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(_item(pair))


@router.delete("/qa-pairs/{pair_id}")
async def delete_qa_pair(
    pair_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    if not QAService(db).soft_delete(pair_id):
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    log_action(
        db,
        "qa_deleted",
        "qa",
        pair_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"pair_id": pair_id})
