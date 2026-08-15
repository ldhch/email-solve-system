"""Password hashing (bcrypt) and JWT session helpers (M-16, TECH 6.1/6.7)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import Settings
from app.core.exceptions import ConfigurationError

# In-memory logout denylist: stateless JWTs are otherwise impossible to revoke.
# Single-process app, so this is enough; the list clears on restart (the
# session cookie is short-lived anyway, 24h).
_REVOKED_JTIS: set[str] = set()


def hash_password(password: str) -> str:
    """Return a bcrypt hash with cost 12 (TECH 6.7)."""

    if not password:
        raise ConfigurationError("Owner password must not be empty")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(settings: Settings, user_id: int, username: str, role: str) -> str:
    """Create an HS256 JWT with a `jti` so logout can revoke it."""

    if not settings.secret_key:
        raise ConfigurationError("SECRET_KEY is required for owner login (set it in .env)")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(8),
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_expire_seconds),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Decode + validate a JWT; raises `jwt.PyJWTError` on any failure."""

    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    if payload.get("jti") in _REVOKED_JTIS:
        raise jwt.InvalidTokenError("token has been revoked by logout")
    return payload


def revoke_access_token(jti: str | None) -> None:
    if jti:
        _REVOKED_JTIS.add(jti)
