"""Conversation + reply admin APIs (M-15, TECH 5.3)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.core.exceptions import LLMError, SMTPError
from app.db.session import get_db
from app.llm.client import build_llm_client
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.models.ticket import Ticket
from app.schemas.admin import (
    EditReplyRequest,
    ManualReplyRequest,
    MergeConversationRequest,
    RejectReplyRequest,
    SplitConversationRequest,
)
from app.services.audit import log_action, utcnow
from app.services.conversation import normalize_subject
from app.services.mailer import MailerService
from app.services.replier import ReplierService
from app.services.translator import TranslatorService

router = APIRouter(prefix="/api/v1", tags=["conversations"])

RISK_RANK = {"high": 3, "medium": 2, "low": 1}


def _fmt(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _make_mailer(db: Session, settings: Settings) -> MailerService:
    return MailerService(db, settings)


def _conversation_status(conv: Conversation) -> str:
    tickets = [t for t in conv.tickets if not t.is_deleted]
    if tickets and all(t.status == "resolved" for t in tickets):
        return "resolved"
    if any(t.status in ("pending", "in_progress") for t in tickets):
        return "escalated"
    return "open"


def _recompute_risk(db: Session, conv: Conversation) -> None:
    levels = [
        risk
        for risk in db.execute(
            select(Email.risk_level).where(Email.conversation_id == conv.id)
        ).scalars().all()
        if risk in RISK_RANK
    ]
    conv.risk_level = max(levels, key=RISK_RANK.get) if levels else None


@router.get("/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: int,
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    emails = db.execute(
        select(Email).where(Email.conversation_id == conv.id).order_by(Email.received_at)
    ).scalars().all()
    replies = db.execute(
        select(Reply)
        .where(Reply.conversation_id == conv.id, Reply.is_deleted.is_(False))
        .order_by(Reply.created_at)
    ).scalars().all()
    email_ids = [e.id for e in emails]
    attachments = (
        db.execute(
            select(Attachment)
            .where(Attachment.email_id.in_(email_ids))
            .order_by(Attachment.id)
        ).scalars().all()
        if email_ids
        else []
    )

    timeline: list[dict] = []
    for e in emails:
        timeline.append(
            {
                "type": "email",
                "direction": "inbound",
                "email_id": e.id,
                "content": e.body_text or "",
                "at": _fmt(e.received_at),
            }
        )
    for r in replies:
        timeline.append(
            {
                "type": "reply",
                "direction": "outbound",
                "reply_id": r.id,
                "content_en": r.content_en,
                "content_cn": r.content_cn,
                "status": r.status,
                "reply_type": r.reply_type,
                "at": _fmt(r.sent_at or r.created_at),
            }
        )
    for a in attachments:
        timeline.append(
            {
                "type": "attachment",
                "filename": a.filename,
                "attachment_id": a.id,
                "email_id": a.email_id,
                "at": _fmt(a.created_at),
            }
        )
    timeline.sort(key=lambda item: item["at"] or "")

    open_tickets = [t for t in conv.tickets if not t.is_deleted and t.status != "resolved"]
    sla_deadline = max((t.sla_deadline for t in open_tickets), default=None)

    return ok(
        {
            "id": conv.id,
            "subject": conv.subject_normalized,
            "customer": {
                "email": conv.customer.email,
                "display_name": conv.customer.display_name,
            },
            "status": _conversation_status(conv),
            "risk_level": conv.risk_level,
            "retention_attempts": conv.retention_attempts,
            "suggested_merge_conversation_id": None,
            "sla_deadline": _fmt(sla_deadline),
            "timeline": timeline,
        }
    )


@router.post("/conversations/{conversation_id}/reply")
async def manual_reply(
    conversation_id: int,
    payload: ManualReplyRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Boss writes Chinese -> system translates to English -> sends (PRD F6)."""

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    latest = db.execute(
        select(Email)
        .where(Email.conversation_id == conv.id, Email.is_inbound.is_(True))
        .order_by(Email.received_at.desc(), Email.id.desc())
    ).scalars().first()
    if latest is None:
        raise HTTPException(status_code=400, detail="NO_EMAIL")

    llm = build_llm_client(settings)
    try:
        content_en = TranslatorService(llm).translate_to_english(payload.content_cn)
    except LLMError:
        raise HTTPException(status_code=422, detail="LLM_FAILED") from None

    reply = ReplierService(db, settings, llm).build_reply(
        latest,
        conv,
        content_en,
        reply_type="general",
        status="draft",
        content_cn=payload.content_cn,
    )
    try:
        _make_mailer(db, settings).send(
            reply, to_email=latest.from_email, subject=latest.subject
        )
    except SMTPError as exc:
        reply.status = "failed"
        reply.send_error = str(exc)
        log_action(
            db,
            "reply_failed",
            "reply",
            reply.id,
            actor_id=user.id,
            ip=_ip(request),
            commit=False,
        )
        db.commit()
        raise HTTPException(status_code=502, detail="SMTP_FAILED") from None

    reply.status = "sent"
    reply.sent_at = utcnow()
    conv.last_activity_at = utcnow()
    log_action(
        db,
        "manual_reply_sent",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(
        {
            "reply_id": reply.id,
            "sent_at": _fmt(reply.sent_at),
            "content_en": reply.content_en,
        }
    )


@router.post("/replies/{reply_id}/approve")
async def approve_reply(
    reply_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if reply.status != "pending_review":
        raise HTTPException(status_code=409, detail="NOT_REVIEWABLE")
    email = db.get(Email, reply.email_id)
    if email is None:
        raise HTTPException(status_code=409, detail="NOT_REVIEWABLE")

    try:
        _make_mailer(db, settings).send(
            reply, to_email=email.from_email, subject=email.subject
        )
    except SMTPError as exc:
        reply.status = "failed"
        reply.send_error = str(exc)
        log_action(
            db,
            "reply_failed",
            "reply",
            reply.id,
            actor_id=user.id,
            ip=_ip(request),
            commit=False,
        )
        db.commit()
        raise HTTPException(status_code=502, detail="SMTP_FAILED") from None

    reply.status = "sent"
    reply.sent_at = utcnow()
    reply.review_user_id = user.id
    reply.reviewed_at = utcnow()
    conv = db.get(Conversation, reply.conversation_id)
    if conv is not None:
        conv.last_activity_at = utcnow()
    log_action(
        db,
        "reply_approved",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"reply_id": reply.id, "sent_at": _fmt(reply.sent_at)})


@router.post("/replies/{reply_id}/reject")
async def reject_reply(
    reply_id: int,
    payload: RejectReplyRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if reply.status != "pending_review":
        raise HTTPException(status_code=409, detail="NOT_REVIEWABLE")
    reply.status = "draft"
    log_action(
        db,
        "reply_rejected",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"reply_id": reply.id, "status": reply.status})


@router.patch("/replies/{reply_id}")
async def edit_reply(
    reply_id: int,
    payload: EditReplyRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if reply.status not in ("draft", "pending_review"):
        raise HTTPException(status_code=409, detail="NOT_EDITABLE")
    if payload.content_cn is not None:
        reply.content_cn = payload.content_cn
        try:
            reply.content_en = TranslatorService(
                build_llm_client(settings)
            ).translate_to_english(payload.content_cn)
        except LLMError:
            raise HTTPException(status_code=422, detail="LLM_FAILED") from None
    log_action(
        db,
        "reply_edited",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok(
        {
            "reply_id": reply.id,
            "status": reply.status,
            "content_cn": reply.content_cn,
            "content_en": reply.content_en,
        }
    )


@router.post("/replies/{reply_id}/send")
async def send_draft(
    reply_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Send an edited draft (boss rejected a review draft, fixed it, sends)."""

    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if reply.status != "draft":
        raise HTTPException(status_code=409, detail="NOT_EDITABLE")
    if not reply.content_en and reply.content_cn:
        try:
            reply.content_en = TranslatorService(
                build_llm_client(settings)
            ).translate_to_english(reply.content_cn)
        except LLMError:
            raise HTTPException(status_code=422, detail="LLM_FAILED") from None
    if not reply.content_en:
        raise HTTPException(status_code=400, detail="EMPTY_CONTENT")

    email = db.get(Email, reply.email_id)
    if email is None:
        raise HTTPException(status_code=409, detail="NO_EMAIL")
    try:
        _make_mailer(db, settings).send(
            reply, to_email=email.from_email, subject=email.subject
        )
    except SMTPError as exc:
        reply.status = "failed"
        reply.send_error = str(exc)
        log_action(
            db,
            "reply_failed",
            "reply",
            reply.id,
            actor_id=user.id,
            ip=_ip(request),
            commit=False,
        )
        db.commit()
        raise HTTPException(status_code=502, detail="SMTP_FAILED") from None

    reply.status = "sent"
    reply.sent_at = utcnow()
    conv = db.get(Conversation, reply.conversation_id)
    if conv is not None:
        conv.last_activity_at = utcnow()
    log_action(
        db,
        "reply_sent",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"reply_id": reply.id, "sent_at": _fmt(reply.sent_at)})


@router.delete("/replies/{reply_id}")
async def soft_delete_reply(
    reply_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if reply.is_deleted:
        raise HTTPException(status_code=409, detail="ALREADY_DELETED")
    reply.is_deleted = True
    log_action(
        db,
        "reply_deleted",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"reply_id": reply.id})


@router.get("/replies/trash")
async def reply_trash(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    _user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    base = select(Reply).where(Reply.is_deleted.is_(True))
    total = len(db.execute(base).scalars().all())
    replies = db.execute(
        base.order_by(Reply.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()
    items = [
        {
            "id": r.id,
            "conversation_id": r.conversation_id,
            "email_id": r.email_id,
            "subject": (db.get(Email, r.email_id).subject if r.email_id else None),
            "content_en": r.content_en,
            "reply_type": r.reply_type,
            "created_at": _fmt(r.created_at),
        }
        for r in replies
    ]
    return ok({"items": items, "total": total, "page": page})


@router.post("/replies/{reply_id}/restore")
async def restore_reply(
    reply_id: int,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if not reply.is_deleted:
        raise HTTPException(status_code=409, detail="NOT_DELETED")
    if utcnow() - reply.created_at > timedelta(days=30):
        raise HTTPException(status_code=410, detail="DELETED_EXPIRED")
    reply.is_deleted = False
    log_action(
        db,
        "reply_restored",
        "reply",
        reply.id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"reply_id": reply.id})


@router.post("/conversations/{conversation_id}/split")
async def split_conversation(
    conversation_id: int,
    payload: SplitConversationRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Move emails (and their replies) at/after `at_email_id` to a new thread."""

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    at_email = db.get(Email, payload.at_email_id)
    if at_email is None or at_email.conversation_id != conv.id:
        raise HTTPException(status_code=400, detail="EMAIL_NOT_IN_CONVERSATION")

    moved = db.execute(
        select(Email)
        .where(Email.conversation_id == conv.id, Email.id >= payload.at_email_id)
        .order_by(Email.id)
    ).scalars().all()
    if len(moved) == len(db.execute(select(Email).where(Email.conversation_id == conv.id)).scalars().all()):
        raise HTTPException(status_code=400, detail="NOTHING_TO_SPLIT")

    new_conv = Conversation(
        customer_id=conv.customer_id,
        subject_normalized=normalize_subject(moved[0].subject),
        window_end=moved[-1].received_at
        + timedelta(days=settings.conversation_window_days),
        last_activity_at=moved[-1].received_at,
        status="open",
    )
    db.add(new_conv)
    db.flush()
    moved_ids = [e.id for e in moved]
    for e in moved:
        e.conversation = new_conv  # relationship assignment keeps both sides in sync
    for r in db.execute(
        select(Reply).where(Reply.email_id.in_(moved_ids))
    ).scalars().all():
        r.conversation = new_conv
    _recompute_risk(db, conv)
    _recompute_risk(db, new_conv)
    log_action(
        db,
        "conversation_split",
        "conversation",
        conversation_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"new_conversation_id": new_conv.id})


@router.post("/conversations/{conversation_id}/merge")
async def merge_conversations(
    conversation_id: int,
    payload: MergeConversationRequest,
    request: Request,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Merge `other_conversation_id` into this one (same customer only)."""

    conv = db.get(Conversation, conversation_id)
    other = db.get(Conversation, payload.other_conversation_id)
    if conv is None or other is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if conv.id == other.id:
        raise HTTPException(status_code=400, detail="SAME_CONVERSATION")
    if other.customer_id != conv.customer_id:
        raise HTTPException(status_code=409, detail="DIFFERENT_CUSTOMER")

    for e in list(other.emails):
        e.conversation = conv
    for r in list(other.replies):
        r.conversation = conv
    db.flush()
    conv.window_end = max(conv.window_end, other.window_end)
    conv.last_activity_at = max(conv.last_activity_at, other.last_activity_at)
    _recompute_risk(db, conv)
    db.delete(other)
    log_action(
        db,
        "conversation_merge",
        "conversation",
        conversation_id,
        actor_id=user.id,
        ip=_ip(request),
        commit=False,
    )
    db.commit()
    return ok({"conversation_id": conversation_id})
