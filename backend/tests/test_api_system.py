"""F9 emergency switch API tests (M-19)."""

from __future__ import annotations

import asyncio

import httpx

from app.config import Settings, get_settings
from app.db.session import get_db
from app.main import create_app
from app.models.audit import AuditLog
from app.models.system_state import SystemState
from app.api.system import get_settings_dependency


_RUNNER = asyncio.Runner()  # single event loop for the whole module (see below)


def _client(settings, session_factory):
    app = create_app()

    async def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db

    async def settings_override() -> Settings:
        return settings

    app.dependency_overrides[get_settings_dependency] = settings_override
    # Use httpx ASGITransport directly: starlette's TestClient is broken in this
    # stack (httpx2 portal hang) and would also trigger lifespan against the
    # real DB file.
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _request(client_factory, method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        async with client_factory() as client:
            return await client.request(method, path, **kwargs)

    # Reuse ONE event loop across the module: anyio binds its thread pool to
    # the first loop, so repeated asyncio.run() calls would starve it.
    return _RUNNER.run(run())


def _make_client(settings, session_factory):
    return lambda: _client(settings, session_factory)


def test_healthz_ok(settings, session_factory) -> None:
    resp = _request(_make_client(settings, session_factory), "GET", "/api/v1/healthz")
    assert resp.status_code == 200
    assert resp.json()["db"] == "ok"


def test_status_initial(settings, session_factory) -> None:
    resp = _request(_make_client(settings, session_factory), "GET", "/api/v1/system/status")
    assert resp.status_code == 200
    assert resp.json()["ai_paused"] is False


def test_pause_requires_service_token(settings, session_factory) -> None:
    client = _make_client(settings, session_factory)
    assert _request(client, "POST", "/api/v1/system/pause", json={"reason": "x"}).status_code == 401
    bad = _request(
        client,
        "POST",
        "/api/v1/system/pause",
        json={"reason": "x"},
        headers={"X-Service-Token": "wrong"},
    )
    assert bad.status_code == 401


def test_pause_and_resume_flow(settings, session_factory) -> None:
    client = _make_client(settings, session_factory)
    headers = {"X-Service-Token": settings.agent_service_token}

    resp = _request(client, "POST", "/api/v1/system/pause", json={"reason": "going offline"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_paused"] is True
    assert body["paused_reason"] == "going offline"
    assert body["paused_at"] is not None

    status = _request(client, "GET", "/api/v1/system/status").json()
    assert status["ai_paused"] is True

    resume = _request(client, "POST", "/api/v1/system/resume", headers=headers)
    assert resume.status_code == 200
    assert resume.json()["ai_paused"] is False
    assert resume.json()["paused_at"] is None

    after = _request(client, "GET", "/api/v1/system/status").json()
    assert after["ai_paused"] is False
    assert after["paused_at"] is None
    assert after["paused_reason"] is None


def test_pause_writes_audit_logs(settings, session_factory) -> None:
    client = _make_client(settings, session_factory)
    headers = {"X-Service-Token": settings.agent_service_token}
    _request(client, "POST", "/api/v1/system/pause", json={"reason": "audit"}, headers=headers)
    _request(client, "POST", "/api/v1/system/resume", headers=headers)

    with session_factory() as db:
        actions = {a.action for a in db.query(AuditLog).all()}
    assert {"pause", "resume"} <= actions


def test_pause_returns_503_when_token_unconfigured(session_factory) -> None:
    settings = get_settings(agent_service_token="")
    resp = _request(_make_client(settings, session_factory), "POST", "/api/v1/system/pause", json={"reason": "x"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "SERVICE_NOT_CONFIGURED"
