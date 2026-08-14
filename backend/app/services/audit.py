"""Minimal audit logging (M-17, Phase 1 scope)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def utcnow() -> datetime:
    """Naive UTC timestamp used across the app (SQLite-friendly)."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def log_action(
    db: Session,
    action: str,
    resource_type: str,
    resource_id: int = 0,
    actor_id: int | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Persist one audit record. `actor_id=None` means the AI pipeline."""

    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip=ip,
        at=utcnow(),
    )
    db.add(entry)
    db.commit()
    return entry
