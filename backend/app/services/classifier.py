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

# Deterministic fallback for the full high-risk set, not just chargeback.
# The LLM is still the primary classifier, but any explicit threat keyword must
# never be downgraded into an auto-send path by a mislabeled risk_level.
DEFAULT_HIGH_RISK_KEYWORDS = [
    "bad review",
    "negative review",
    "lawsuit",
    "sue",
    "lawyer",
    "attorney",
    "legal action",
    "to the media",  # threat context only ("go to / talk to / contact the media")
    "to the press",  # threat context only ("go to / contact the press")
    "bad press",
    "news outlet",
    "account ban",
    "ban my account",
    "platform complaint",
    "consumer protection",
    "bbb",
    "ftc",
    "chargeback",
    "dispute",
    "credit card company",
    "file a claim",
    "bank claim",
]

# Marketing/newsletter/promotional markers for the keyword channel. Deliberately
# avoids bare "unsubscribe" (a customer asking "please unsubscribe me" must hit
# SILENCE_KEYWORDS instead, not be archived as an ad); the LLM channel catches
# unsubscribe-style ad mail the keyword list cannot.
AD_KEYWORDS = [
    "newsletter",
    "promo code",
    "coupon",
    "flash sale",
    "limited time",
    "you're receiving this",
    "you are receiving this",
    "marketing email",
    "opt-out",
    "opt out",
    "to unsubscribe",
    "unsubscribe at",
    "unsubscribe link",
    "unsubscribe",
    "退订",
    "广告邮件",
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
  "is_advertisement": true | false,
  "summary_cn": "<short Chinese summary, 20-40 chars>"
}

Rules:
- high: explicit or implied threats (bad review threat, lawsuit, lawyer, media,
  account ban, chargeback/dispute threats, platform complaint).
- refund/return/exchange requests: category refund_request, risk medium.
- logistics/tracking/order changes/invoices: risk medium (no ERP data).
- product specs, usage questions, policy/warranty info, thanks: risk low.
- is_advertisement: true for marketing/newsletter/promotional or spam mail
  (coupon codes, "you're receiving this email", unsubscribe links, sales promos),
  for third-party app notifications / stats / reports / product-update broadcasts
  that carry no customer-service request (checkout stats, review prompts,
  upsells), for guest-post / sponsored-content / media-outlet pitch emails
  (PR placement, link building, "opportunity for your website"), and for app /
  TestFlight test invites and cold sales outreach.
- Order / tracking / payment transactional emails are NOT advertisements even
  though they are automated.
- A customer email raising a support issue is NOT an advertisement even when it
  comes from an automated system (a bad-review alert is high risk, never an ad).
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
    is_ad: bool = False  # marketing/newsletter/promotional mail


# Only clearly safe consultation categories are eligible for auto-send.
AUTO_SEND_CATEGORIES = {"product_spec", "usage", "gratitude"}
MANUAL_CATEGORIES = {"logistics_inquiry", "order_modification", "invoice"}


def resolve_action(
    risk_level: str,
    category: str,
    confidence: float | None = None,
    min_confidence: float = 0.8,
) -> str:
    """Map a classification to a pipeline action (conservative by default)."""

    if risk_level in ("high", "unknown"):
        return "escalate"
    if risk_level == "medium":
        if category in MANUAL_CATEGORIES or category == "refund_request":
            return "escalate"
        return "review"
    if risk_level == "low":
        if confidence is not None and confidence < min_confidence:
            return "review"
        if category in AUTO_SEND_CATEGORIES:
            return "auto_send"
        if category in MANUAL_CATEGORIES or category == "refund_request":
            return "escalate"
        return "review"
    return "escalate"


class ClassifierService:
    """Classifies emails with keyword + LLM double channel."""

    def __init__(self, settings: Settings, llm_client: BaseLLMClient) -> None:
        self.settings = settings
        self.llm_client = llm_client
        keywords = settings.chargeback_keyword_list or DEFAULT_CHARGEBACK_KEYWORDS
        self.chargeback_keywords = [k.lower() for k in keywords]
        high_risk_keywords = (
            settings.high_risk_keyword_list or DEFAULT_HIGH_RISK_KEYWORDS
        )
        self.high_risk_keywords = [k.lower() for k in high_risk_keywords]
        self.ad_keywords = [k.lower() for k in AD_KEYWORDS]
        self.system_prompt = _load_prompt()

    def _keyword_hit(self, text: str, keywords: list[str]) -> bool:
        lowered = text.lower()
        # Word-boundary match: avoid short tokens like "ftc"/"bbb" matching
        # "facts"/"attic"/"abbey" and causing false chargeback escalations.
        return any(
            re.search(rf"\b{re.escape(kw)}\b", lowered)
            for kw in keywords
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

        keyword_hit = self._keyword_hit(user_content, self.chargeback_keywords)
        high_risk_hit = self._keyword_hit(user_content, self.high_risk_keywords)
        llm_flag = bool(data.get("chargeback_risk", False))
        chargeback_risk = keyword_hit or llm_flag

        if high_risk_hit or llm_flag or risk == "high":
            risk = "high"
            logger.warning(
                "High risk detected (keyword=%s llm=%s) for %s",
                high_risk_hit,
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

        # The ad channel is advisory only: keyword/LLM hits propose, but
        # high-risk or unclassifiable mail (complaints, bad-review threats,
        # chargebacks) must never be archived into the ad tab. Automated system
        # notifications carrying an unsubscribe footer (e.g. judge.me review
        # alerts) are a classic trap for a bare "unsubscribe" keyword hit.
        ad_hit = self._keyword_hit(user_content, self.ad_keywords)
        llm_ad = bool(data.get("is_advertisement", False))
        is_ad = (ad_hit or llm_ad) and risk not in ("high", "unknown")
        if is_ad:
            logger.info(
                "Advertisement detected (keyword=%s llm=%s) for %s",
                ad_hit,
                llm_ad,
                parsed_email.message_id,
            )

        return Classification(
            risk_level=risk,
            confidence=confidence,
            category=category,
            chargeback_risk=chargeback_risk,
            summary_cn=str(data.get("summary_cn", "")).strip(),
            is_ad=is_ad,
        )
