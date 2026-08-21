"""Blocked-sender lookups for the ingest pipeline (M-XX).

``is_blocked`` is called before an email is merged into a conversation: a hit
marks the email ``is_ad`` (归档到「广告」tab) and skips the whole reply/retention
flow. The list is small (tens of entries), so a full scan per check is fine.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.blocked_sender import BlockedSender

logger = __import__("logging").getLogger(__name__)


def normalize_blocked_value(value: str, scope: str) -> str:
    """Normalize a blacklist entry to its canonical stored form.

    Domains are stored without a leading ``@`` (``amazon.com``, not
    ``@amazon.com``); emails are lowercased. Both scopes reject empty input.
    """

    value = value.strip().lower()
    if scope == "domain":
        value = value.lstrip("@")
    if not value:
        raise ValueError("EMPTY_VALUE")
    return value


def sender_is_blocked(db: Session, from_email: str) -> bool:
    """True when the sender address or its domain is blacklisted."""

    email = (from_email or "").strip().lower()
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1]
    rows = db.execute(select(BlockedSender)).scalars().all()
    for entry in rows:
        if entry.scope == "email" and entry.value == email:
            return True
        if entry.scope == "domain" and entry.value == domain:
            return True
    return False
