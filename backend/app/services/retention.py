"""Save-the-sale retention service (M-13, PRD F3.1).

Hard-coded rules (TECH.md 4.2, no config table):
  quality / damaged  -> no retention, handle the return directly
  size               -> exchange offer (AI sends directly)
  not_wanted / bought_wrong -> compensation offer (money => owner review)
  unknown reason     -> conservative: owner review instead of auto-send
  attempts >= RETENTION_MAX_ATTEMPTS -> stop retention, release the return

Chargeback signals are intercepted by the classifier before this service is
ever reached (M-06 -> J2); retention never runs on chargeback risk.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, prompts_dir
from app.core.exceptions import LLMError
from app.llm.client import BaseLLMClient
from app.models.reply import Reply

logger = logging.getLogger(__name__)

RETENTION_STRATEGIES = {
    "quality": "none",
    "damaged": "none",
    "size": "exchange",
    "not_wanted": "compensation",
    "bought_wrong": "compensation",
    "other": "none",
}

REASON_KEYWORDS: dict[str, list[str]] = {
    "damaged": [
        "damaged",
        "cracked",
        "scratched",
        "torn",
        "dented",
        "arrived broken",
        "arrived damaged",
    ],
    "quality": [
        "quality",
        "defective",
        "faulty",
        "broken",
        "not working",
        "doesn't work",
        "does not work",
        "poor quality",
        "fell apart",
        "falling apart",
        "cheap",
    ],
    "bought_wrong": [
        "bought the wrong",
        "ordered the wrong",
        "wrong item",
        "wrong color",
        "wrong model",
        "wrong product",
        "wrong one",
        "by mistake",
        "accidentally ordered",
        "duplicate order",
        "double order",
    ],
    "size": [
        "size",
        "too small",
        "too big",
        "too large",
        "too tight",
        "doesn't fit",
        "does not fit",
        "don't fit",
        "fit issue",
        "smaller than expected",
        "larger than expected",
        "wrong size",
    ],
    "not_wanted": [
        "changed my mind",
        "no longer want",
        "don't want",
        "do not want",
        "not what i expected",
        "didn't like",
        "not satisfied",
        "don't need",
        "do not need",
        "not needed",
    ],
}

# Multi-word acceptance phrases are checked FIRST because some contain
# negative tokens ("no need refund"). This is a deliberate refinement of
# TECH B-3: an explicit acceptance must not be treated as a rejection, or we
# would keep sending retention offers and delay the customer's legal return.
POSITIVE_PHRASES = [
    "no need refund",
    "no refund needed",
    "send the replacement",
    "that works",
    "sounds good",
    "i'll take",
    "keep it",
    "i accept",
    "accepted",
]
NEGATIVE_KEYWORDS = [
    "no",
    "refund",
    "return",
    "cancel",
    "give me my money back",
    "still want",
    "money back",
    "full refund",
]
POSITIVE_KEYWORDS = ["ok", "yes", "agree", "fine"]

REASON_SYSTEM_PROMPT = """\
You are classifying why a customer wants to return/refund/exchange an online
order. Read the email and return ONE strict JSON object:
{"reason": "quality | damaged | size | not_wanted | bought_wrong | other"}
Pick the best fit; if unclear, use "other". Output only the JSON object.
"""

RETENTION_OFFER_TYPES = {"retention_exchange", "retention_compensation"}
RETENTION_RELEASE_TYPES = {"retention_release", "retention_accepted"}


def _load_acceptance_prompt() -> str:
    prompt_file = prompts_dir() / "retention_acceptance.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return ACCEPTANCE_SYSTEM_PROMPT


ACCEPTANCE_SYSTEM_PROMPT = """\
The store offered the customer a retention alternative (exchange or small
compensation) instead of a return. Read the customer's latest reply and decide:
- accept_retention: they clearly accept the alternative.
- reject_retention: they clearly still want the refund/return.
- uncertain: unclear or silent about the offer.
Return ONE strict JSON object: {"verdict": "accept_retention | reject_retention | uncertain"}
Output only the JSON object.
"""


def _keyword_hit(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in keywords)


class RetentionService:
    """Routes refund/return/exchange requests into the save-the-sale flow."""

    def __init__(
        self, db: Session, settings: Settings, llm_client: BaseLLMClient
    ) -> None:
        self.db = db
        self.settings = settings
        self.llm_client = llm_client

    # ---------- reason / acceptance classification ----------

    def classify_reason(self, text: str) -> str:
        """Keyword-first, LLM fallback. Unknown => "other" (owner review)."""

        lowered = (text or "").lower()
        for reason in ("damaged", "quality", "bought_wrong", "size", "not_wanted"):
            if _keyword_hit(lowered, REASON_KEYWORDS[reason]):
                return reason
        try:
            raw = self.llm_client.chat_with_retry(
                messages=[{"role": "user", "content": text or ""}],
                system_prompt=REASON_SYSTEM_PROMPT,
            )
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                data = json.loads(raw[start : end + 1])
                reason = str(data.get("reason", "other")).strip().lower()
                if reason in RETENTION_STRATEGIES:
                    return reason
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Retention reason LLM fallback failed, using 'other': %s", exc)
        return "other"

    def is_customer_accepted(self, text: str) -> bool:
        """TECH B-3 with phrase-priority refinement (see module docstring)."""

        lowered = (text or "").lower()
        if any(phrase in lowered for phrase in POSITIVE_PHRASES):
            return True
        if _keyword_hit(lowered, NEGATIVE_KEYWORDS):
            return False
        if _keyword_hit(lowered, POSITIVE_KEYWORDS):
            return True
        try:
            raw = self.llm_client.chat_with_retry(
                messages=[{"role": "user", "content": text or ""}],
                system_prompt=_load_acceptance_prompt(),
            )
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                data = json.loads(raw[start : end + 1])
                return str(data.get("verdict", "")).strip() == "accept_retention"
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Retention acceptance LLM fallback failed, defaulting to False: %s", exc)
        return False

    # ---------- routing ----------

    def resolve_strategy(self, reason: str, attempts: int) -> str:
        """Return none | exchange | compensation | release | review."""

        if reason == "other":
            return "review"
        strategy = RETENTION_STRATEGIES.get(reason, "none")
        if strategy == "none":
            return "none"
        if attempts >= self.settings.retention_max_attempts:
            return "release"
        return strategy

    # ---------- conversation state ----------

    def latest_sent_reply(self, conversation_id: int) -> Reply | None:
        return self.db.execute(
            select(Reply)
            .where(
                Reply.conversation_id == conversation_id,
                Reply.status == "sent",
            )
            .order_by(Reply.sent_at.desc(), Reply.id.desc())
        ).scalars().first()

    def has_open_offer(self, conversation_id: int) -> bool:
        latest = self.latest_sent_reply(conversation_id)
        return latest is not None and latest.reply_type in RETENTION_OFFER_TYPES

    def is_released(self, conversation_id: int) -> bool:
        latest = self.latest_sent_reply(conversation_id)
        return latest is not None and latest.reply_type in RETENTION_RELEASE_TYPES
