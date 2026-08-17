"""Risk classification service (M-06, Phase 1 scope).

Two-channel chargeback detection: keyword hit OR LLM verdict => chargeback_risk.
Low-confidence results are downgraded to `unknown` and routed to manual
handling (never auto-sent) - "rather escalate conservatively than let AI send".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.config import Settings, prompts_dir
from app.core.exceptions import LLMError
from app.llm.client import BaseLLMClient

logger = logging.getLogger(__name__)

CATEGORIES = (
    "logistics_inquiry",
    "order_modification",
    "invoice",
    "product_spec",
    "usage",
    "policy",
    "warranty",
    "gratitude",
    "refund_request",
    "other",
)

DEFAULT_CHARGEBACK_KEYWORDS = [
    "chargeback",
    "dispute",
    "credit card company",
    "file a claim",
    "bank claim",
    "lawyer",
    "attorney",
    "legal action",
    "sue",
    "consumer protection",
    "bbb",
    "ftc",
    "platform complaint",
]

# PRD edge case 9: the customer asks not to be contacted again. When detected,
# the customer is silenced for 72h (no auto-replies, mail still ingested).
SILENCE_KEYWORDS = [
    "do not contact",
    "do not reply",
    "don't reply",
    "don't contact",
    "do not email",
    "don't email",
    "stop emailing",
    "stop contacting",
    "stop sending",
    "never email",
    "no more emails",
    "take me off",
    "remove me from your list",
    "opt out",
    "unsubscribe",
    "不要再回复",
    "不要联系",
]

CLASSIFY_SYSTEM_PROMPT = """\
You are the triage classifier of an English after-sales support inbox.
Read the customer email and return ONE strict JSON object with exactly these keys:
{
  "risk_level": "high" | "medium" | "low",
  "confidence": <float 0.0-1.0>,
  "category": "<one of: logistics_inquiry, order_modification, invoice, product_spec,
               usage, policy, warranty, gratitude, refund_request, other>",
  "chargeback_risk": true | false,
  "summary_cn": "<short Chinese summary, 20-40 chars>"
}

Rules:
- high: explicit or implied threats (bad review threat, lawsuit, lawyer, media,
  account ban, chargeback/dispute threats, platform complaint).
- refund/return/exchange requests: category refund_request, risk medium.
- logistics/tracking/order changes/invoices: risk medium (no ERP data).
- product specs, usage questions, policy/warranty info, thanks: risk low.
- If you cannot decide, use confidence < 0.5 and category "other".
- Never invent facts. Do not output anything besides the JSON object.
"""


def requests_silence(text: str | None) -> bool:
    """True when the customer explicitly asks to stop receiving emails."""

    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in SILENCE_KEYWORDS)


def _load_prompt() -> str:
    """Load the classify prompt from docs/prompts when present."""

    prompt_file = prompts_dir() / "classify_chargeback.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return CLASSIFY_SYSTEM_PROMPT


@dataclass
class Classification:
    risk_level: str  # high | medium | low | unknown
    confidence: float
    category: str
    chargeback_risk: bool
    summary_cn: str


RISK_ACTIONS = {
    "high": "escalate",
    "low": "auto_send",
    "medium": "review",
    "medium:logistics_inquiry": "escalate",
    "medium:order_modification": "escalate",
    "medium:invoice": "escalate",
    "medium:policy": "auto_send",
    "medium:warranty": "auto_send",
    "medium:product_spec": "auto_send",
    "medium:usage": "auto_send",
}

# Refund/return/exchange must never auto-send in Phase 1: the retention flow
# (Phase 2) owns that decision. Anything else low-risk may auto-send.
NO_AUTO_SEND_CATEGORIES = {"refund_request"}


def resolve_action(risk_level: str, category: str) -> str:
    """Map a classification to a Phase-1 pipeline action."""

    if risk_level in ("high", "unknown"):
        return "escalate"
    if risk_level == "medium":
        return RISK_ACTIONS.get(f"medium:{category}", RISK_ACTIONS["medium"])
    if risk_level == "low":
        return "auto_send" if category not in NO_AUTO_SEND_CATEGORIES else "escalate"
    return "escalate"


class ClassifierService:
    """Classifies emails with keyword + LLM double channel."""

    def __init__(self, settings: Settings, llm_client: BaseLLMClient) -> None:
        self.settings = settings
        self.llm_client = llm_client
        keywords = settings.chargeback_keyword_list or DEFAULT_CHARGEBACK_KEYWORDS
        self.chargeback_keywords = [k.lower() for k in keywords]
        self.system_prompt = _load_prompt()

    def _keyword_hit(self, text: str) -> bool:
        lowered = text.lower()
        # Word-boundary match: avoid short tokens like "ftc"/"bbb" matching
        # "facts"/"attic"/"abbey" and causing false chargeback escalations.
        return any(
            re.search(rf"\b{re.escape(kw)}\b", lowered)
            for kw in self.chargeback_keywords
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError(f"Classifier returned no JSON object: {text[:200]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Classifier returned invalid JSON: {text[start:end+1][:200]!r}") from exc
        if not isinstance(data, dict):
            raise LLMError("Classifier JSON is not an object")
        return data

    def classify(self, parsed_email) -> Classification:
        """Return the classification for one parsed email."""

        body_text = (parsed_email.body_text or parsed_email.body_html or "").strip()
        if not body_text:
            # PRD edge case 6: empty / image-only / unreadable emails must go to
            # the manual queue, never auto-reply.
            logger.info(
                "Email %s has no extractable text; routing to manual",
                parsed_email.message_id,
            )
            return Classification(
                risk_level="unknown",
                confidence=0.0,
                category="other",
                chargeback_risk=False,
                summary_cn="邮件内容为空或无法提取文本，已标记可疑待人工核查",
            )

        user_content = (
            f"Subject: {parsed_email.subject}\n"
            f"From: {parsed_email.from_email}\n"
            f"Body:\n{body_text}"
        )
        raw = self.llm_client.chat_with_retry(
            messages=[{"role": "user", "content": user_content}],
            system_prompt=self.system_prompt,
        )
        data = self._parse_json(raw)

        risk = str(data.get("risk_level", "")).strip().lower()
        if risk not in ("high", "medium", "low"):
            logger.warning("Classifier returned unknown risk_level=%r; downgrading", risk)
            risk = "unknown"

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        category = str(data.get("category", "other")).strip().lower()
        if category not in CATEGORIES:
            category = "other"

        keyword_hit = self._keyword_hit(user_content)
        llm_flag = bool(data.get("chargeback_risk", False))
        chargeback_risk = keyword_hit or llm_flag

        if chargeback_risk:
            risk = "high"
            logger.warning(
                "Chargeback risk detected (keyword=%s llm=%s) for %s",
                keyword_hit,
                llm_flag,
                parsed_email.message_id,
            )

        if risk != "unknown" and confidence < self.settings.low_confidence_threshold:
            risk = "unknown"
            logger.info(
                "Low confidence (%s) downgraded to manual for %s",
                confidence,
                parsed_email.message_id,
            )

        return Classification(
            risk_level=risk,
            confidence=confidence,
            category=category,
            chargeback_risk=chargeback_risk,
            summary_cn=str(data.get("summary_cn", "")).strip(),
        )
