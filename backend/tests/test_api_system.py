"""F9 emergency switch API tests (M-19, JWT auth from Phase 2)."""

from __future__ import annotations

from app.models.audit import AuditLog
from app.models.user import User

from api_helpers import api, close_client, login, make_client, seed_owner


class _StubScheduler:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    def is_healthy(self) -> bool:
        return self.healthy


def _inject_scheduler(monkeypatch, healthy: bool = True) -> None:
    from app.services import scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module, "get_scheduler_service", lambda: _StubScheduler(healthy)
    )


def test_healthz_ok(settings, session_factory, monkeypatch) -> None:
    _inject_scheduler(monkeypatch, healthy=True)
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["db"] == "ok"
        assert body["scheduler"] == "ok"
        assert body["uptime_sec"] >= 0
    finally:
        close_client(client)


def test_healthz_503_when_scheduler_stale(settings, session_factory, monkeypatch) -> None:
    _inject_scheduler(monkeypatch, healthy=False)
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/healthz")
        assert resp.status_code == 503
        assert resp.json()["scheduler"] == "down"
    finally:
        close_client(client)


def test_healthz_503_when_db_down(settings, session_factory, monkeypatch) -> None:
    from app.db.session import get_db

    _inject_scheduler(monkeypatch, healthy=True)
    client = make_client(settings, session_factory)
    try:

        async def broken_db():
            db = session_factory()
            try:
                db.execute = lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("db unavailable")
                )
                yield db
            finally:
                db.close()

        client._transport.app.dependency_overrides[get_db] = broken_db
        resp = api(client, "GET", "/api/v1/healthz")
        assert resp.status_code == 503
        assert resp.json()["db"] == "down"
    finally:
        close_client(client)


def test_notifications_status(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "GET", "/api/v1/system/notifications")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["bark_configured"] is False
        assert data["alert_email_configured"] is False
        assert data["alert_email_masked"] is None

        settings.alert_email_to = "boss@example.com"
        settings.alert_bark_webhook = "https://api.day.app/abc"
        resp2 = api(client, "GET", "/api/v1/system/notifications")
        data2 = resp2.json()["data"]
        assert data2["bark_configured"] is True
        assert data2["alert_email_configured"] is True
        assert data2["alert_email_masked"] == "b***@example.com"
    finally:
        close_client(client)


def test_status_initial(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/system/status")
        assert resp.status_code == 200
        assert resp.json()["data"]["ai_paused"] is False
    finally:
        close_client(client)


def test_pause_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "POST", "/api/v1/system/pause", json={"reason": "x"})
        assert resp.status_code == 401
    finally:
        close_client(client)


def test_pause_and_resume_flow(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        assert login(client, settings.owner_username, settings.owner_password).status_code == 200

        resp = api(client, "POST", "/api/v1/system/pause", json={"reason": "going offline"})
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["ai_paused"] is True
        assert body["paused_reason"] == "going offline"
        assert body["paused_at"] is not None

        status = api(client, "GET", "/api/v1/system/status").json()["data"]
        assert status["ai_paused"] is True

        resume = api(client, "POST", "/api/v1/system/resume")
        assert resume.status_code == 200
        assert resume.json()["data"]["ai_paused"] is False

        after = api(client, "GET", "/api/v1/system/status").json()["data"]
        assert after["ai_paused"] is False
        assert after["paused_at"] is None
        assert after["paused_reason"] is None
    finally:
        close_client(client)


def test_pause_writes_audit_logs_with_actor(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        api(client, "POST", "/api/v1/system/pause", json={"reason": "audit"})
        api(client, "POST", "/api/v1/system/resume")

        with session_factory() as db:
            owner = db.query(User).filter(User.username == settings.owner_username).first()
            actions = {a.action for a in db.query(AuditLog).all()}
            pause_actors = [
                a.actor_id
                for a in db.query(AuditLog).filter(AuditLog.action == "pause").all()
            ]
        assert {"pause", "resume"} <= actions
        assert pause_actors == [owner.id]
    finally:
        close_client(client)
