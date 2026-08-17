"""M-17 audit-logs query API tests (TECH 5.8)."""

from __future__ import annotations

from datetime import timedelta

from app.services.audit import log_action, utcnow

from api_helpers import api, close_client, login, make_client, seed_owner


def test_audit_logs_requires_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        resp = api(client, "GET", "/api/v1/audit-logs")
        assert resp.status_code == 401
    finally:
        close_client(client)


def _seed_logs(session_factory) -> list[int]:
    ids = []
    with session_factory() as db:
        for i, action in enumerate(
            [
                "pause",
                "resume",
                "kb_uploaded",
                "reply_sent",
                "ticket_updated",
                "qa_updated",
            ]
        ):
            entry = log_action(
                db,
                action,
                "system" if action in ("pause", "resume") else "misc",
                resource_id=i + 1,
                actor_id=1 if action in ("kb_uploaded", "reply_sent") else None,
                ip="127.0.0.1",
                commit=False,
            )
            ids.append(entry.id)
        db.commit()
    return ids


def test_audit_logs_list_paginated_newest_first(
    settings, session_factory
) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    _seed_logs(session_factory)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)
        resp = api(client, "GET", "/api/v1/audit-logs", params={"page": 1, "size": 4})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 7  # 6 seeded + 1 login entry from this session
        assert len(data["items"]) == 4
        actions = [item["action"] for item in data["items"]]
        assert actions == ["login", "qa_updated", "ticket_updated", "reply_sent"]
        assert data["items"][0]["at"].endswith("Z")

        page2 = api(client, "GET", "/api/v1/audit-logs", params={"page": 2, "size": 4})
        assert [item["action"] for item in page2.json()["data"]["items"]] == [
            "kb_uploaded",
            "resume",
            "pause",
        ]
    finally:
        close_client(client)


def test_audit_logs_filters(settings, session_factory) -> None:
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    _seed_logs(session_factory)
    client = make_client(settings, session_factory)
    try:
        login(client, settings.owner_username, settings.owner_password)

        by_action = api(client, "GET", "/api/v1/audit-logs", params={"action": "pause"})
        assert by_action.json()["data"]["total"] == 1

        by_actor = api(client, "GET", "/api/v1/audit-logs", params={"actor_id": 1})
        assert by_actor.json()["data"]["total"] == 3  # 2 seeded + 1 login

        now = utcnow()
        by_from = api(
            client,
            "GET",
            "/api/v1/audit-logs",
            params={"from": (now - timedelta(seconds=1)).isoformat()},
        )
        assert by_from.json()["data"]["total"] == 7

        by_to = api(
            client,
            "GET",
            "/api/v1/audit-logs",
            params={"to": (now - timedelta(seconds=1)).isoformat()},
        )
        assert by_to.json()["data"]["total"] == 0

        missing = api(client, "GET", "/api/v1/audit-logs", params={"action": "nope"})
        assert missing.json()["data"]["items"] == []
    finally:
        close_client(client)
