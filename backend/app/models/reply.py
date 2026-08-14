"""Reply model (TECH.md 4.2 `replies`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    content_cn: Mapped[str | None] = mapped_column(Text)
    content_en: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    reply_type: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    review_user_id: Mapped[int | None] = mapped_column(Integer)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    send_error: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    conversation = relationship("Conversation", back_populates="replies")
    email = relationship("Email", back_populates="replies")
