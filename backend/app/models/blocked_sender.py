"""Blocked-sender blacklist model.

The boss can block a specific sender address (``scope="email"``) or a whole
domain (``scope="domain"``). Mail matching a blocked entry is ingested into the
"广告" tab but never auto-replied or aggregated into a customer conversation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

VALID_SCOPES = ("email", "domain")


class BlockedSender(Base):
    __tablename__ = "blocked_senders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Lowercased email address (scope=email) or domain without "@" (scope=domain).
    value: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
