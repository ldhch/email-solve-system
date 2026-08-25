"""System state singleton (TECH.md 4.2 `system_state`), the F9 kill switch."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemState(Base):
    __tablename__ = "system_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_system_state_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ai_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused_reason: Mapped[str | None] = mapped_column(String(512))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Test mode: only whitelisted senders are auto-processed; every other
    # inbound mail is left UNSEEN and untouched on the server (no ingest, no
    # classification, no LLM, no reply). `test_whitelist` is a comma-separated
    # list of sender addresses (empty + test_mode on = isolate everything).
    test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    test_whitelist: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    # Acknowledgment reply template, editable from the Settings page (CN shown
    # in the admin UI, EN is what goes to the customer). Nullable: null means
    # "use the hardcoded defaults in services/acknowledgment.py".
    ack_content_cn: Mapped[str | None] = mapped_column(Text)
    ack_content_en: Mapped[str | None] = mapped_column(Text)
    # True/None: English is auto-managed (re-translated from CN on save). False:
    # the boss hand-tuned the English, so it is kept verbatim and never clobbered.
    ack_content_en_auto: Mapped[bool | None] = mapped_column(Boolean)
    ack_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
