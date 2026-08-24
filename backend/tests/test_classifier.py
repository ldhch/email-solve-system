"""Classifier + routing tests (M-06)."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.llm.client import BaseLLMClient
from app.services.classifier import ClassifierService, resolve_action
from app.services.ingest import ParsedEmail


class StubLLM(BaseLLMClient):
    def __init__(self, response: str) -> None:
        self.response = response
        self.settings = Settings(llm_provider="mock", llm_retries=0)

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        return self.response


def _parsed(body: str) -> ParsedEmail:
    return ParsedEmail(
        message_id="<c1@x>",
        subject="Question",
        from_email="c@example.com",
        from_name=None,
        to_email="bot@example.com",
        body_text=body,
        body_html=None,
        received_at=datetime.now(),
    )


def test_chargeback_keyword_forced_high() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"low","confidence":0.95,"category":"product_spec",'
        '"chargeback_risk":false,"summary_cn":"普通咨询"}'
    )
    parsed = _parsed("I will file a chargeback with my bank.")
    result = ClassifierService(settings, llm).classify(parsed)
    assert result.risk_level == "high"
    assert result.chargeback_risk is True


def test_keyword_boundary_avoids_false_positive() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"low","confidence":0.95,"category":"product_spec",'
        '"chargeback_risk":false,"summary_cn":"普通咨询"}'
    )
    service = ClassifierService(settings, llm)
    # "softcover" contains the letters f-t-c, but must NOT trigger "ftc".
    assert service._keyword_hit("I bought a softcover book", service.chargeback_keywords) is False
    assert service.classify(_parsed("I bought a softcover book")).chargeback_risk is False


def test_llm_chargeback_flag_forced_high() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"medium","confidence":0.9,"category":"refund_request",'
        '"chargeback_risk":true,"summary_cn":"疑似拒付"}'
    )
    parsed = _parsed("I want my money back and I will involve my bank.")
    result = ClassifierService(settings, llm).classify(parsed)
    assert result.risk_level == "high"
    assert result.chargeback_risk is True


def test_bad_review_keyword_forced_high() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"low","confidence":0.95,"category":"product_spec",'
        '"chargeback_risk":false,"summary_cn":"普通咨询"}'
    )
    parsed = _parsed("I will leave a terrible review and complain to the BBB.")
    result = ClassifierService(settings, llm).classify(parsed)
    assert result.risk_level == "high"
    assert result.chargeback_risk is True


def test_lawyer_keyword_forced_high() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"low","confidence":0.95,"category":"gratitude",'
        '"chargeback_risk":false,"summary_cn":"普通邮件"}'
    )
    parsed = _parsed("My lawyer will contact you about this order.")
    result = ClassifierService(settings, llm).classify(parsed)
    assert result.risk_level == "high"


def test_low_confidence_downgraded_to_unknown() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"low","confidence":0.3,"category":"product_spec",'
        '"chargeback_risk":false,"summary_cn":"不确定"}'
    )
    result = ClassifierService(settings, llm).classify(_parsed("confusing text"))
    assert result.risk_level == "unknown"


def test_refund_request_parsed() -> None:
    settings = Settings(low_confidence_threshold=0.6)
    llm = StubLLM(
        '{"risk_level":"medium","confidence":0.9,"category":"refund_request",'
        '"chargeback_risk":false,"summary_cn":"客户要求退款"}'
    )
    result = ClassifierService(settings, llm).classify(_parsed("Please refund my order"))
    assert result.risk_level == "medium"
    assert result.category == "refund_request"


def test_resolve_action_mapping() -> None:
    assert resolve_action("low", "product_spec", confidence=0.9) == "auto_send"
    assert resolve_action("low", "gratitude", confidence=0.9) == "auto_send"
    assert resolve_action("low", "refund_request") == "escalate"
    # Conservative routing: medium always reviews, even for pure consultations.
    assert resolve_action("medium", "product_spec") == "review"
    assert resolve_action("medium", "policy") == "review"
    assert resolve_action("medium", "warranty") == "review"
    assert resolve_action("medium", "usage") == "review"
    assert resolve_action("medium", "gratitude") == "review"
    assert resolve_action("medium", "logistics_inquiry") == "escalate"
    assert resolve_action("medium", "invoice") == "escalate"
    assert resolve_action("medium", "order_modification") == "escalate"
    assert resolve_action("high", "refund_request") == "escalate"
    assert resolve_action("unknown", "other") == "escalate"
    # Category guard: LLM says low but category needs manual handling.
    assert resolve_action("low", "invoice", confidence=0.95) == "escalate"
    assert resolve_action("low", "logistics_inquiry", confidence=0.95) == "escalate"
    assert resolve_action("low", "other", confidence=0.95) == "review"
    # Confidence guard: a not-confident low-risk product question reviews.
    assert resolve_action("low", "product_spec", confidence=0.7) == "review"


def test_llm_failure_raises() -> None:
    class FailingLLM(BaseLLMClient):
        def __init__(self) -> None:
            self.settings = Settings(llm_provider="mock", llm_retries=0)

        def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
            raise RuntimeError("api down")

    from app.core.exceptions import LLMError

    settings = Settings(low_confidence_threshold=0.6)
    try:
        ClassifierService(settings, FailingLLM()).classify(_parsed("hello"))
    except LLMError:
        return
    raise AssertionError("expected LLMError")
