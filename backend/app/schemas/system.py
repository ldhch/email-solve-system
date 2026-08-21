"""Request/response schemas for the F9 emergency switch endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class PauseRequest(BaseModel):
    reason: str = ""


class SystemStatusResponse(BaseModel):
    ai_paused: bool
    paused_at: str | None = None
    paused_reason: str | None = None
    uptime_sec: int
    test_mode: bool = False
    test_whitelist: list[str] = []


class TestModeRequest(BaseModel):
    """Test-mode switch: only whitelisted senders are auto-processed."""

    enabled: bool
    whitelist: list[str] = []


class HealthzResponse(BaseModel):
    db: str = "ok"
    scheduler: str = "ok"
    uptime_sec: int
