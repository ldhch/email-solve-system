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
async def translate_email_full(
    email_id: int,
    user=Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    """Translate the email's full body to Simplified Chinese and cache it.

    Idempotent: an already-stored translation is returned as-is, so the boss
    only pays for each email's translation once.
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
