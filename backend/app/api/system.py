"""Emergency pause switch + health endpoints (M-19, F9).

Phase 1 has no owner login (Phase 2), so pause/resume are guarded by the
`X-Service-Token` header matching `AGENT_SERVICE_TOKEN`. Phase 2 replaces this
guard with the JWT-based owner auth and adds the Settings-page UI.
"""

from __future__ import annotations

import hmac
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_db
from app.models.system_state import SystemState
from app.schemas.system import HealthzResponse, PauseRequest, SystemStatusResponse
from app.services.audit import log_action, utcnow

router = APIRouter(prefix="/api/v1", tags=["system"])

_STARTED_AT = time.time()


async def get_settings_dependency() -> Settings:
    """Async settings dependency (sync deps would run in a thread pool)."""

    return get_settings()


async def _require_service_token(
    x_service_token: str | None = Header(default=None, alias="X-Service-Token"),
    settings: Settings = Depends(get_settings_dependency),
) -> None:
    expected = settings.agent_service_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SERVICE_NOT_CONFIGURED",
        )
    if not x_service_token or not hmac.compare_digest(x_service_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="UNAUTHORIZED",
        )


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


@router.get("/healthz", response_model=HealthzResponse)
async def healthz(db: Session = Depends(get_db)) -> HealthzResponse:
    """Liveness probe: DB reachable + process uptime (no auth)."""

    db.execute(SystemState.__table__.select().limit(1))  # touch the DB
    return HealthzResponse(db="ok", uptime_sec=int(time.time() - _STARTED_AT))


@router.get("/system/status", response_model=SystemStatusResponse)
async def system_status(db: Session = Depends(get_db)) -> SystemStatusResponse:
    state = db.get(SystemState, 1)
    return SystemStatusResponse(
        ai_paused=bool(state and state.ai_paused),
        paused_at=_fmt(state.paused_at if state else None),
        paused_reason=state.paused_reason if state else None,
        uptime_sec=int(time.time() - _STARTED_AT),
    )


@router.post("/system/pause", response_model=SystemStatusResponse)
async def system_pause(
    payload: PauseRequest,
    request: Request,
    _: None = Depends(_require_service_token),
    db: Session = Depends(get_db),
) -> SystemStatusResponse:
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
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return SystemStatusResponse(
        ai_paused=True,
        paused_at=_fmt(state.paused_at),
        paused_reason=state.paused_reason,
        uptime_sec=int(time.time() - _STARTED_AT),
    )


@router.post("/system/resume", response_model=SystemStatusResponse)
async def system_resume(
    request: Request,
    _: None = Depends(_require_service_token),
    db: Session = Depends(get_db),
) -> SystemStatusResponse:
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
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return SystemStatusResponse(
        ai_paused=False,
        paused_at=None,
        paused_reason=None,
        uptime_sec=int(time.time() - _STARTED_AT),
    )
