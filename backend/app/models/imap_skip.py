"""Emails deliberately skipped without ingestion (parse failure).

The poll records the server UID so a malformed mail is never re-fetched every
cycle, while the mailbox's \\Seen flag stays untouched.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImapSkip(Base):
    __tablename__ = "imap_skips"

    id: Mapped[int] = mapped_column(primary_key=True)
    uidvalidity: Mapped[str | None] = mapped_column(String(64), index=True)
    uid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
