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
    RejectReplyRequest,
)
from app.services.audit import log_action, utcnow
from app.services.acknowledgment import resolve_review_tickets
from app.services.mailer import MailerService
from app.services.replier import ReplierService
from app.services.translator import TranslatorService, contains_cjk

router = APIRouter(prefix="/api/v1", tags=["conversations"])


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


def _has_newer_release(db: Session, reply: Reply) -> bool:
    """True when an auto-release was already sent after this draft.

    Guards against double-sending a compensation offer after the scheduler
    auto-released the return (PRD edge case 22 / review finding).
    """

    return (
        db.execute(
            select(Reply).where(
                Reply.conversation_id == reply.conversation_id,
                Reply.reply_type == "retention_release",
                Reply.status == "sent",
                Reply.created_at > reply.created_at,
            )
        ).scalars().first()
        is not None
    )


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
                "body_html": e.body_html,
                "summary_cn": e.summary_cn,
                "content_cn": e.content_cn,
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
                "source": r.source,
                "low_confidence": r.low_confidence,
                "send_error": r.send_error,
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
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "at": _fmt(a.created_at),
            }
        )
    timeline.sort(key=lambda item: item["at"] or "")

    open_tickets = [t for t in conv.tickets if not t.is_deleted and t.status != "resolved"]
    sla_deadline = max((t.sla_deadline for t in open_tickets), default=None)
    now = utcnow()
    resolved_ticket_count = sum(
        1 for t in conv.tickets if not t.is_deleted and t.status == "resolved"
    )

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
            "is_ad": any(e.is_ad for e in emails if e is not None),
            "retention_attempts": conv.retention_attempts,
            "sla_deadline": _fmt(sla_deadline),
            # Ticket state for the merged ticket bar: open high-risk tickets
            # (with SLA + overdue flag) and a count of resolved ones.
            "open_tickets": [
                {
                    "id": t.id,
                    "status": t.status,
                    "sla_deadline": _fmt(t.sla_deadline),
                    "is_overdue": (
                        t.sla_deadline < now and t.status in ("pending", "in_progress")
                    ),
                }
                for t in open_tickets
            ],
            "resolved_ticket_count": resolved_ticket_count,
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
        content_en = TranslatorService(llm).translate_to_letter(
            payload.content_cn, customer_name=conv.customer.display_name
        )
    except LLMError:
        raise HTTPException(status_code=422, detail="LLM_FAILED") from None

    # A pure-English manual reply passes through translate_to_letter unchanged,
    # which would store the same English text in content_cn and break the
    # CN/EN display toggle in the UI. Back-translate it so content_cn stays
    # genuinely Chinese (non-fatal: on failure keep the original, sending is
    # never blocked).
    content_cn = payload.content_cn
    if not contains_cjk(content_cn):
        try:
            content_cn = TranslatorService(llm).translate_to_chinese(content_en)
        except LLMError:
            content_cn = payload.content_cn

    reply = ReplierService(db, settings, llm).build_reply(
        latest,
        conv,
        content_en,
        reply_type="general",
        status="draft",
        content_cn=content_cn,
    )
    reply.source = "manual"
    try:
        _make_mailer(db, settings).send(
            reply,
            to_email=latest.from_email,
            subject=latest.subject,
            bypass_rate_limit=True,
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

    # 工单合并: 老板回复成功即视为解决该会话未处理的高风险工单
    # (the ticket's "must reply to resolve" discipline, enforced here instead
    # of via a separate ticket page).
    open_tickets = db.execute(
        select(Ticket).where(
            Ticket.conversation_id == conv.id,
            Ticket.is_deleted.is_(False),
            Ticket.status.in_(("pending", "in_progress")),
        )
    ).scalars().all()
    for ticket in open_tickets:
        ticket.status = "resolved"
        ticket.resolved_at = utcnow()
        log_action(
            db,
            "ticket_resolved",
            "ticket",
            ticket.id,
            actor_id=user.id,
            ip=_ip(request),
            commit=False,
        )

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
    if _has_newer_release(db, reply):
        raise HTTPException(status_code=409, detail="SUPERSEDED")
    email = db.get(Email, reply.email_id)
    if email is None:
        raise HTTPException(status_code=409, detail="NOT_REVIEWABLE")

    try:
        _make_mailer(db, settings).send(
            reply,
            to_email=email.from_email,
            subject=email.subject,
            bypass_rate_limit=True,
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
    resolve_review_tickets(
        db,
        reply.conversation_id,
        actor_id=user.id,
        ip=_ip(request),
    )
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
    if reply.status not in ("draft", "pending_review", "failed"):
        raise HTTPException(status_code=409, detail="NOT_EDITABLE")
    customer_name = None
    conv = db.get(Conversation, reply.conversation_id)
    if conv is not None:
        customer_name = conv.customer.display_name
    if payload.content_cn is not None:
        reply.content_cn = payload.content_cn
        try:
            reply.content_en = TranslatorService(
                build_llm_client(settings)
            ).translate_to_letter(payload.content_cn, customer_name=customer_name)
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
    if reply.status not in ("draft", "failed"):
        raise HTTPException(status_code=409, detail="NOT_EDITABLE")
    if _has_newer_release(db, reply):
        raise HTTPException(status_code=409, detail="SUPERSEDED")
    if not reply.content_en and reply.content_cn:
        customer_name = None
        conv = db.get(Conversation, reply.conversation_id)
        if conv is not None:
            customer_name = conv.customer.display_name
        try:
            reply.content_en = TranslatorService(
                build_llm_client(settings)
            ).translate_to_letter(reply.content_cn, customer_name=customer_name)
        except LLMError:
            raise HTTPException(status_code=422, detail="LLM_FAILED") from None
    if not reply.content_en:
        raise HTTPException(status_code=400, detail="EMPTY_CONTENT")

    email = db.get(Email, reply.email_id)
    if email is None:
        raise HTTPException(status_code=409, detail="NO_EMAIL")
    reply.source = "manual"
    try:
        _make_mailer(db, settings).send(
            reply,
            to_email=email.from_email,
            subject=email.subject,
            bypass_rate_limit=True,
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
    reply.send_error = None
    conv = db.get(Conversation, reply.conversation_id)
    if conv is not None:
        conv.last_activity_at = utcnow()
    resolve_review_tickets(
        db,
        reply.conversation_id,
        actor_id=user.id,
        ip=_ip(request),
    )
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
