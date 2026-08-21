"""Emergency pause switch + health endpoints (M-19, F9).

Phase 2: pause/resume are guarded by the JWT owner session (httpOnly cookie).
`AGENT_SERVICE_TOKEN` remains reserved for AI-internal calls (TECH 6.2).
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.db.session import get_db
from app.models.user import User
from app.models.system_state import SystemState
from app.schemas.system import (
    HealthzResponse,
    PauseRequest,
    SystemStatusResponse,
    TestModeRequest,
)
from app.services.audit import log_action, utcnow
from app.services import scheduler as scheduler_module

router = APIRouter(prefix="/api/v1", tags=["system"])

_STARTED_AT = time.time()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def _test_mode_state(state: SystemState | None) -> dict:
    """Serialize the test-mode switch + whitelist for the Settings page."""

    return {
        "test_mode": bool(state and state.test_mode),
        "test_whitelist": [
            w for w in (state.test_whitelist if state else "").split(",") if w
        ],
    }


@router.get("/healthz", response_model=HealthzResponse)
async def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    """Liveness probe (no auth): DB + scheduler heartbeat (TECH 5.6 / N-4).

    Returns 503 when either component is unavailable, for the Docker
    ``healthcheck`` command.
    """

    try:
        db.execute(SystemState.__table__.select().limit(1))  # touch the DB
        db_status = "ok"
    except Exception:  # noqa: BLE001 - report, do not crash the probe
        db_status = "down"

    service = scheduler_module.get_scheduler_service()
    scheduler_status = (
        "ok" if service is not None and service.is_healthy() else "down"
    )
    body = HealthzResponse(
        db=db_status,
        scheduler=scheduler_status,
        uptime_sec=int(time.time() - _STARTED_AT),
    )
    status_code = 200 if db_status == "ok" and scheduler_status == "ok" else 503
    return JSONResponse(status_code=status_code, content=body.model_dump())


@router.get("/system/status")
async def system_status(db: Session = Depends(get_db)) -> dict:
    state = db.get(SystemState, 1)
    return ok(SystemStatusResponse(
        ai_paused=bool(state and state.ai_paused),
        paused_at=_fmt(state.paused_at if state else None),
        paused_reason=state.paused_reason if state else None,
        uptime_sec=int(time.time() - _STARTED_AT),
        **_test_mode_state(state),
    ).model_dump())


@router.put("/system/test-mode")
async def system_test_mode(
    payload: TestModeRequest,
    request: Request,
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Toggle test mode and persist the sender whitelist (owner only).

    Test mode means the pipeline only ingests / replies / translates mail from
    whitelisted senders; every other UNSEEN message is left untouched on the
    server. An empty whitelist is rejected while enabling so the boss can never
    switch it on believing a sender is covered when none is.
    """

    cleaned = [e.strip().lower() for e in payload.whitelist if e.strip()]
    if payload.enabled and not cleaned:
        raise HTTPException(status_code=400, detail="EMPTY_WHITELIST")
    for email in cleaned:
        if not _EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="INVALID_EMAIL")

    state = db.get(SystemState, 1)
    if state is None:
        raise HTTPException(status_code=500, detail="INTERNAL")
    state.test_mode = payload.enabled
    state.test_whitelist = ",".join(dict.fromkeys(cleaned))  # dedupe, keep order
    log_action(
        db,
        "test_mode_changed",
        "system",
        state.id,
        actor_id=user.id,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return ok(_test_mode_state(state))


@router.get("/system/notifications")
async def system_notifications(
    _user=Depends(require_owner),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Read-only alert-channel config status for the Settings page (F-09).

    Only configuration *status* is exposed, never credentials or full
    addresses.
    """

    email = settings.alert_email_to
    masked = None
    if email and "@" in email:
        local, domain = email.split("@", 1)
        masked = f"{local[:1]}***@{domain}" if local else f"***@{domain}"
    return ok(
        {
            "bark_configured": bool(settings.alert_bark_webhook),
            "alert_email_configured": bool(email),
            "alert_email_masked": masked,
        }
    )


@router.post("/system/pause")
async def system_pause(
    payload: PauseRequest,
    request: Request,
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    state = db.get(SystemState, 1)
    if state is None:
        raise HTTPException(status_code=500, detail="INTERNAL")
    state.ai_paused = True
    state.paused_at = utcnow()
    state.paused_reason = payload.reason
    state.resumed_at = None
    log_action(
        db,
        "pause",
        "system",
        state.id,
        actor_id=user.id,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return ok(SystemStatusResponse(
        ai_paused=True,
        paused_at=_fmt(state.paused_at),
        paused_reason=state.paused_reason,
        uptime_sec=int(time.time() - _STARTED_AT),
    ).model_dump())


@router.post("/system/resume")
async def system_resume(
    request: Request,
    user: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    state = db.get(SystemState, 1)
    if state is None:
        raise HTTPException(status_code=500, detail="INTERNAL")
    state.ai_paused = False
    state.paused_at = None
    state.paused_reason = None
    state.resumed_at = utcnow()
    log_action(
        db,
        "resume",
        "system",
        state.id,
        actor_id=user.id,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return ok(SystemStatusResponse(
        ai_paused=False,
        paused_at=None,
        paused_reason=None,
        uptime_sec=int(time.time() - _STARTED_AT),
    ).model_dump())
