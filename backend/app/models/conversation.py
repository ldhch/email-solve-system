"""Conversation model (TECH.md 4.2 `conversations`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    subject_normalized: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    risk_level: Mapped[str | None] = mapped_column(String(20), index=True)
    retention_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    customer = relationship("Customer", back_populates="conversations")
    emails = relationship("Email", back_populates="conversation")
    replies = relationship("Reply", back_populates="conversation")
    tickets = relationship("Ticket", back_populates="conversation")
