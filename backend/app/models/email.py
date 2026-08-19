"""Email model (TECH.md 4.2 `emails`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(512), index=True)
    references: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    to_email: Mapped[str | None] = mapped_column(String(320))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    summary_cn: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    risk_level: Mapped[str | None] = mapped_column(String(20), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    is_inbound: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="emails")
    attachments = relationship("Attachment", back_populates="email")
    replies = relationship("Reply", back_populates="email")
