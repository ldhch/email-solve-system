"""Owner authentication (M-16, TECH 5.1 / 6.1 / 6.8).

httpOnly `sid` cookie carrying an HS256 JWT (24h, no refresh). Login is
protected by an in-memory rate limiter: 5 attempts/IP/minute and a 30-minute
account lock after 10 failures per hour. CSRF is mitigated by SameSite=Lax.
"""

from __future__ import annotations

import time

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    revoke_access_token,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.services.audit import log_action

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_IP_WINDOW_SECONDS = 60
_IP_MAX_ATTEMPTS = 5
_FAIL_WINDOW_SECONDS = 3600
_FAIL_MAX = 10
_LOCK_SECONDS = 1800

_ip_attempts: dict[str, list[float]] = {}
_account_failures: dict[str, list[float]] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> User:
    """Resolve the owner from the httpOnly `sid` cookie (JWT)."""

    token = request.cookies.get("sid")
    if not token:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    try:
        payload = decode_access_token(settings, token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED") from None
    user = db.get(User, int(payload.get("sub", 0) or 0))
    if user is None:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    return user


async def require_owner(user: User = Depends(get_current_user)) -> User:
    """Owner-only dependency (single-user backend, TECH 6.2 RBAC)."""

    if user.role != "owner":
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return user


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    ip = _client_ip(request)
    now = time.time()

    # 1) per-IP window: at most 5 login attempts per minute
    attempts = [t for t in _ip_attempts.get(ip, []) if now - t < _IP_WINDOW_SECONDS]
    if len(attempts) >= _IP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="RATE_LIMITED")
    attempts.append(now)
    _ip_attempts[ip] = attempts

    # 2) account lockout: 10 failures/hour locks the account for 30 minutes
    failures = [
        t for t in _account_failures.get(payload.username, []) if now - t < _FAIL_WINDOW_SECONDS
    ]
    if len(failures) >= _FAIL_MAX:
        if now - failures[-1] < _LOCK_SECONDS:
            raise HTTPException(status_code=423, detail="ACCOUNT_LOCKED")
        failures = []  # lock window expired

    user = db.execute(
        select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        _account_failures[payload.username] = failures + [now]
        log_action(
            db,
            "login_failed",
            "user",
            user.id if user else 0,
            ip=ip,
            commit=False,
        )
        db.commit()
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")

    token = create_access_token(settings, user.id, user.username, user.role)
    response.set_cookie(
        "sid",
        token,
        max_age=settings.jwt_expire_seconds,
        httponly=True,
        samesite="lax",
        secure=(settings.app_env == "production"),
        path="/",
    )
    log_action(db, "login", "user", user.id, actor_id=user.id, ip=ip)
    return ok(
        {
            "expires_in": settings.jwt_expire_seconds,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    token = request.cookies.get("sid")
    if token:
        try:
            payload = decode_access_token(settings, token)
            revoke_access_token(payload.get("jti"))
        except jwt.PyJWTError:
            pass  # already invalid; clearing the cookie is enough
    response.delete_cookie("sid", path="/")
    log_action(db, "logout", "user", user.id, actor_id=user.id, ip=_client_ip(request))
    return ok(None)


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    return ok({"id": user.id, "username": user.username, "role": user.role})
