"""Shared helpers for admin API tests (httpx ASGITransport + JWT login)."""

from __future__ import annotations

import asyncio

import httpx

from app.api.common import get_settings_dependency
from app.core.security import hash_password
from app.db.session import get_db
from app.main import create_app
from app.models.user import User
from app.services.audit import utcnow

_RUNNER = asyncio.Runner()  # one event loop for all API tests (see Phase 1 notes)


def run_async(coro):
    return _RUNNER.run(coro)


def seed_owner(session_factory, username: str = "boss", password: str = "test-owner-password") -> None:
    """Insert/refresh the owner user for login tests."""

    with session_factory() as db:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role="owner",
                    created_at=utcnow(),
                )
            )
        else:
            user.password_hash = hash_password(password)
        db.commit()


def make_client(settings, session_factory) -> httpx.AsyncClient:
    """Build an app bound to the in-memory DB and the test settings."""

    app = create_app()

    async def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    async def settings_override():
        return settings

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings_dependency] = settings_override
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    return run_async(client.request(method, path, **kwargs))


def close_client(client: httpx.AsyncClient) -> None:
    run_async(client.aclose())


def login(client: httpx.AsyncClient, username: str = "boss", password: str = "test-owner-password") -> httpx.Response:
    return api(
        client,
        "POST",
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
