"""F9 emergency switch API tests (M-19, JWT auth from Phase 2)."""

from __future__ import annotations

from app.core.exceptions import LLMError
from app.models.audit import AuditLog
from app.models.user import User
from app.services.translator import TranslatorService

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


# ---------- 测试模式 (test mode / sender whitelist) ----------


def test_test_mode_default_off(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/system/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["test_mode"] is False
        assert data["test_whitelist"] == []
    finally:
        close_client(client)


def test_test_mode_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={"enabled": True, "whitelist": ["a@example.com"]},
        )
        assert resp.status_code == 401
    finally:
        close_client(client)


def test_test_mode_toggle_and_persist(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        assert login(client, settings.owner_username, settings.owner_password).status_code == 200
        resp = api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={
                "enabled": True,
                "whitelist": ["419018463@qq.com", "  Test-B@example.com "],
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["test_mode"] is True
        assert data["test_whitelist"] == ["419018463@qq.com", "test-b@example.com"]

        status = api(client, "GET", "/api/v1/system/status").json()["data"]
        assert status["test_mode"] is True
        assert status["test_whitelist"] == ["419018463@qq.com", "test-b@example.com"]

        off = api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={"enabled": False, "whitelist": []},
        )
        assert off.json()["data"]["test_mode"] is False
        after = api(client, "GET", "/api/v1/system/status").json()["data"]
        assert after["test_mode"] is False
        assert after["test_whitelist"] == []
    finally:
        close_client(client)


def test_test_mode_requires_whitelist_when_enabling(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={"enabled": True, "whitelist": []},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "EMPTY_WHITELIST"
    finally:
        close_client(client)


def test_test_mode_rejects_invalid_email(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={"enabled": True, "whitelist": ["not-an-email"]},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "INVALID_EMAIL"
    finally:
        close_client(client)


def test_test_mode_writes_audit_log(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        api(
            client,
            "PUT",
            "/api/v1/system/test-mode",
            json={"enabled": True, "whitelist": ["a@example.com"]},
        )
        with session_factory() as db:
            actions = {a.action for a in db.query(AuditLog).all()}
        assert "test_mode_changed" in actions
    finally:
        close_client(client)


# ---------- 自动确认回复模板 (acknowledgment template) ----------


def test_ack_template_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/system/ack-template")
        assert resp.status_code == 401
    finally:
        close_client(client)


def test_get_ack_template_defaults(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "GET", "/api/v1/system/ack-template")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "{customer_name}" in data["content_en"]
        assert "{customer_name}" in data["content_cn"]
        assert data["content_en_auto"] is True
        assert data["updated_at"] is None
    finally:
        close_client(client)


def test_put_ack_template_with_explicit_en(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "PUT",
            "/api/v1/system/ack-template",
            json={"content_cn": "感谢您联系。", "content_en": "Thanks for writing."},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["content_cn"] == "感谢您联系。"
        assert data["content_en"] == "Thanks for writing."
        assert data["content_en_auto"] is False
        assert data["updated_at"] is not None

        # Persisted: a fresh GET returns what we saved (manual EN stays manual).
        got = api(client, "GET", "/api/v1/system/ack-template").json()["data"]
        assert got["content_en"] == "Thanks for writing."
        assert got["content_en_auto"] is False

        with session_factory() as db:
            owner = db.query(User).filter(User.username == settings.owner_username).first()
            entries = db.query(AuditLog).filter(
                AuditLog.action == "ack_template_updated"
            ).all()
        assert len(entries) == 1
        assert entries[0].actor_id == owner.id
    finally:
        close_client(client)


def test_put_ack_template_translates_when_en_empty(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "PUT",
            "/api/v1/system/ack-template",
            json={"content_cn": "感谢您联系 LBORA。"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["content_en"]  # auto-translated (Mock LLM -> non-empty)
        assert data["content_en_auto"] is True
    finally:
        close_client(client)


def test_put_ack_template_llm_failure_422(settings, session_factory, monkeypatch) -> None:
    def _boom(self, text: str) -> str:
        raise LLMError("translate down")

    monkeypatch.setattr(TranslatorService, "translate_to_english", _boom)
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "PUT",
            "/api/v1/system/ack-template",
            json={"content_cn": "感谢您联系。"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "LLM_FAILED"
    finally:
        close_client(client)


def test_post_ack_template_translate_preview_not_saved(
    settings, session_factory
) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(
            client,
            "POST",
            "/api/v1/system/ack-template/translate",
            json={"content_cn": "您好，我们正在处理。"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["content_en"]

        # Preview must not persist: updated_at stays None.
        got = api(client, "GET", "/api/v1/system/ack-template").json()["data"]
        assert got["updated_at"] is None
    finally:
        close_client(client)
