"""System state singleton (TECH.md 4.2 `system_state`), the F9 kill switch."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String
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
