"""Auth API tests (M-16, TECH 5.1 / 6.8)."""

from __future__ import annotations

import app.api.auth as auth_module
from app.models.audit import AuditLog
from app.models.user import User

from api_helpers import api, close_client, login, make_client, seed_owner


def _login_ok(client, username="boss", password="test-owner-password"):
    resp = login(client, username, password)
    assert resp.status_code == 200, resp.text
    return resp


def test_login_sets_http_only_cookie(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        resp = _login_ok(client)
        body = resp.json()["data"]
        assert body["user"]["username"] == "boss"
        assert body["user"]["role"] == "owner"
        set_cookie = resp.headers.get("set-cookie", "")
        assert "sid=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
    finally:
        close_client(client)


def test_login_wrong_password_401_and_audit(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        resp = api(
            client,
            "POST",
            "/api/v1/auth/login",
            json={"username": "boss", "password": "wrong"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "INVALID_CREDENTIALS"
        with session_factory() as db:
            actions = {a.action for a in db.query(AuditLog).all()}
        assert "login_failed" in actions
    finally:
        close_client(client)


def test_me_requires_and_returns_user(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        assert api(client, "GET", "/api/v1/auth/me").status_code == 401
        _login_ok(client)
        resp = api(client, "GET", "/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == "boss"
    finally:
        close_client(client)


def test_logout_revokes_session(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        _login_ok(client)
        assert api(client, "GET", "/api/v1/auth/me").status_code == 200
        resp = api(client, "POST", "/api/v1/auth/logout")
        assert resp.status_code == 200
        assert api(client, "GET", "/api/v1/auth/me").status_code == 401
        with session_factory() as db:
            actions = {a.action for a in db.query(AuditLog).all()}
        assert "logout" in actions
    finally:
        close_client(client)


def test_login_rate_limit_per_ip(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        for _ in range(5):
            api(
                client,
                "POST",
                "/api/v1/auth/login",
                json={"username": "boss", "password": "wrong"},
            )
        resp = api(
            client,
            "POST",
            "/api/v1/auth/login",
            json={"username": "boss", "password": "wrong"},
        )
        assert resp.status_code == 429
        assert resp.json()["detail"] == "RATE_LIMITED"
    finally:
        close_client(client)


def test_account_locked_after_ten_failures(settings, session_factory) -> None:
    seed_owner(session_factory)
    client = make_client(settings, session_factory)
    try:
        for i in range(10):
            resp = api(
                client,
                "POST",
                "/api/v1/auth/login",
                json={"username": "boss", "password": "wrong"},
            )
            assert resp.status_code == 401, i
            # Simulate a new minute so the per-IP window does not trigger first.
            auth_module._ip_attempts.clear()
        resp = api(
            client,
            "POST",
            "/api/v1/auth/login",
            json={"username": "boss", "password": "wrong"},
        )
        assert resp.status_code == 423
        assert resp.json()["detail"] == "ACCOUNT_LOCKED"
    finally:
        close_client(client)
