"""Phase 3 admin API tests: knowledge base + standard QA (TECH 5.5/5.7)."""

from __future__ import annotations

from app.models.knowledge_doc import KnowledgeDoc
from app.models.qa_pair import QAPair

from api_helpers import api, close_client, login, make_client, seed_owner

KB_MD = b"# FAQ\nReturn window: 30 days, no restocking fee."
KB_MD_V2 = b"# FAQ v2\nReturn window: 60 days."


def _authed_client(settings, session_factory):
    seed_owner(session_factory, settings.owner_username, settings.owner_password)
    client = make_client(settings, session_factory)
    assert login(client, settings.owner_username, settings.owner_password).status_code == 200
    return client


def test_kb_and_qa_require_login(settings, session_factory) -> None:
    client = make_client(settings, session_factory)
    try:
        assert api(client, "GET", "/api/v1/kb/docs").status_code == 401
        assert api(client, "GET", "/api/v1/qa-pairs").status_code == 401
    finally:
        close_client(client)


def test_kb_upload_list_overwrite_delete(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        up1 = api(
            client,
            "POST",
            "/api/v1/kb/upload",
            files={"file": ("faq.md", KB_MD, "text/markdown")},
        )
        assert up1.status_code == 200, up1.text
        assert up1.json()["data"] == {"doc_id": 1, "version": 1}

        up2 = api(
            client,
            "POST",
            "/api/v1/kb/upload",
            files={"file": ("faq.md", KB_MD_V2, "text/markdown")},
        )
        assert up2.status_code == 200
        assert up2.json()["data"] == {"doc_id": 1, "version": 2}

        listing = api(client, "GET", "/api/v1/kb/docs")
        items = listing.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["filename"] == "faq.md"
        assert items[0]["version"] == 2

        assert api(client, "DELETE", "/api/v1/kb/docs/1").status_code == 200
        assert api(client, "GET", "/api/v1/kb/docs").json()["data"]["items"] == []
        assert api(client, "DELETE", "/api/v1/kb/docs/1").status_code == 404
        assert api(client, "DELETE", "/api/v1/kb/docs/9999").status_code == 404
    finally:
        close_client(client)


def test_kb_upload_rejects_unsupported_and_oversized(
    settings, session_factory, monkeypatch
) -> None:
    client = _authed_client(settings, session_factory)
    try:
        bad_type = api(
            client,
            "POST",
            "/api/v1/kb/upload",
            files={"file": ("note.txt", b"hello", "text/plain")},
        )
        assert bad_type.status_code == 400
        assert bad_type.json()["detail"] == "UNSUPPORTED_TYPE"

        # The ASGITransport test client hangs on ~20MB multipart bodies, so the
        # limit is lowered here and the endpoint's 413 branch is exercised with
        # a small payload. The real 20MB ceiling is covered at the service level.
        import app.api.kb as kb_module

        monkeypatch.setattr(kb_module, "MAX_KB_BYTES", 1024)
        too_large = api(
            client,
            "POST",
            "/api/v1/kb/upload",
            files={"file": ("big.md", b"x" * 2048, "text/markdown")},
        )
        assert too_large.status_code == 413
        assert too_large.json()["detail"] == "TOO_LARGE"
    finally:
        close_client(client)


def test_qa_pairs_crud(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        created = api(
            client,
            "POST",
            "/api/v1/qa-pairs",
            json={"question": "What is your return policy?", "answer": "30 days.", "category": "policy"},
        )
        assert created.status_code == 200, created.text
        pair_id = created.json()["data"]["id"]
        assert created.json()["data"]["enabled"] is True

        listing = api(client, "GET", "/api/v1/qa-pairs")
        assert listing.json()["data"]["items"][0]["question"] == "What is your return policy?"

        patched = api(
            client,
            "PATCH",
            f"/api/v1/qa-pairs/{pair_id}",
            json={"answer": "30 days, no restocking fee.", "enabled": False},
        )
        assert patched.status_code == 200
        assert patched.json()["data"]["answer"] == "30 days, no restocking fee."
        assert patched.json()["data"]["enabled"] is False

        assert api(client, "DELETE", f"/api/v1/qa-pairs/{pair_id}").status_code == 200
        assert api(client, "GET", "/api/v1/qa-pairs").json()["data"]["items"] == []
        assert api(client, "PATCH", f"/api/v1/qa-pairs/{pair_id}", json={"answer": "x"}).status_code == 404
    finally:
        close_client(client)


def test_qa_pairs_validation(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        missing = api(client, "POST", "/api/v1/qa-pairs", json={"question": "", "answer": ""})
        assert missing.status_code == 422
    finally:
        close_client(client)


def test_qa_pairs_bulk_import(settings, session_factory) -> None:
    client = _authed_client(settings, session_factory)
    try:
        payload = {
            "items": [
                {"question": "What is your return policy?", "answer": "30 days.", "category": "policy"},
                {"question": "What is your return policy?", "answer": "dup within batch", "category": "policy"},
                {"question": "How long does shipping take?", "answer": "5-7 days.", "category": "shipping"},
            ]
        }
        resp = api(client, "POST", "/api/v1/qa-pairs/bulk", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == {"created": 2, "skipped": 1}

        # Re-importing the same batch skips everything (DB dedup).
        resp2 = api(client, "POST", "/api/v1/qa-pairs/bulk", json=payload)
        assert resp2.json()["data"] == {"created": 0, "skipped": 3}

        listing = api(client, "GET", "/api/v1/qa-pairs")
        assert len(listing.json()["data"]["items"]) == 2

        empty = api(client, "POST", "/api/v1/qa-pairs/bulk", json={"items": []})
        assert empty.status_code == 422
    finally:
        close_client(client)
