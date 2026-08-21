"""Email-level operations (on-demand full-text translation)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import require_owner
from app.api.common import get_settings_dependency, ok
from app.config import Settings
from app.core.exceptions import LLMError
from app.db.session import get_db
from app.llm.client import build_llm_client
from app.models.email import Email
from app.services.translator import TranslatorService

router = APIRouter(prefix="/api/v1", tags=["emails"])


@router.post("/emails/{email_id}/translate")
def translate_email_full(
    email_id: int,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Translate the email's full body to Simplified Chinese and cache it.

    Idempotent: an already-stored translation is returned as-is, so the boss
    only pays for each email's translation once.

    Sync ``def`` on purpose: the LLM call blocks the thread, and FastAPI runs
    sync endpoints in the worker pool. An ``async def`` here would block the
    whole event loop (freezing every other request) for the translation's
    20-90s.
    """

    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if not email.content_cn:
        if not email.body_text or not email.body_text.strip():
            raise HTTPException(status_code=400, detail="NO_BODY")
        llm = build_llm_client(settings)
        try:
            email.content_cn = TranslatorService(llm).translate_to_chinese(
                email.body_text
            )
        except LLMError:
            raise HTTPException(status_code=422, detail="LLM_FAILED") from None
        db.commit()
    return ok({"email_id": email_id, "content_cn": email.content_cn})


@router.get("/emails/{email_id}/translate/status")
def translate_status(
    email_id: int,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only status of the full-text translation for one email.

    ``done`` means ``content_cn`` is cached and ready to display; ``pending``
    means the on-demand call or the background prefill is still translating.
    The frontend polls this while a translation is in flight, so the Chinese
    appears automatically once the backend finishes (no manual reopen).
    """

    email = db.get(Email, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if email.content_cn:
        return ok({"status": "done", "content_cn": email.content_cn})
    return ok({"status": "pending", "content_cn": None})
