"""Request/response schemas for the F9 emergency switch endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class AckTemplateRequest(BaseModel):
    """Save the editable acknowledgment template (CN shown in the admin UI).

    ``content_en`` is the customer-facing English text. When empty/None the
    backend translates ``content_cn`` once at save time (English stays a fixed
    cached string afterwards — the send path never calls the LLM).
    """

    content_cn: str = Field(min_length=1, max_length=5000)
    content_en: str | None = Field(default=None, max_length=5000)


class AckTemplateTranslateRequest(BaseModel):
    """Preview-only ZH->EN translation of the CN template (never persisted)."""

    content_cn: str = Field(min_length=1, max_length=5000)


class AckTemplateResponse(BaseModel):
    content_cn: str
    content_en: str
    # True: English is auto-managed (re-translated from CN on save).
    # False: the boss hand-tuned English — keep it verbatim.
    content_en_auto: bool = True
    updated_at: str | None = None
