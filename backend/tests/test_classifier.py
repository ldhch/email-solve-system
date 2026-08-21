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
    assert resolve_action("low", "product_spec") == "auto_send"
    assert resolve_action("low", "gratitude") == "auto_send"
    assert resolve_action("low", "refund_request") == "escalate"
    # Boss decision (方案 A): pure consultations auto-send even if medium.
    assert resolve_action("medium", "product_spec") == "auto_send"
    assert resolve_action("medium", "policy") == "auto_send"
    assert resolve_action("medium", "warranty") == "auto_send"
    assert resolve_action("medium", "usage") == "auto_send"
    assert resolve_action("medium", "gratitude") == "review"
    assert resolve_action("medium", "logistics_inquiry") == "escalate"
    assert resolve_action("medium", "invoice") == "escalate"
    assert resolve_action("medium", "order_modification") == "escalate"
    assert resolve_action("high", "refund_request") == "escalate"
    assert resolve_action("unknown", "other") == "escalate"


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
