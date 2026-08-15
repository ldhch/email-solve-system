"""Retention service unit tests (M-13, PRD F3.1 / TECH B-3)."""

from __future__ import annotations

from app.config import Settings
from app.llm.client import BaseLLMClient
from app.services.retention import RetentionService


class StubLLM(BaseLLMClient):
    def __init__(self, response: str) -> None:
        self.response = response
        self.settings = Settings(llm_provider="mock", llm_retries=0)

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        return self.response


def _service(db, response: str = '{"reason": "other"}') -> RetentionService:
    return RetentionService(
        db,
        Settings(llm_provider="mock", retention_max_attempts=2),
        StubLLM(response),
    )


def test_reason_keywords() -> None:
    cases = {
        "it arrived damaged": "damaged",
        "the product is defective": "quality",
        "I bought the wrong color": "bought_wrong",
        "the shirt is too small": "size",
        "I changed my mind": "not_wanted",
    }
    for text, expected in cases.items():
        assert _service(None).classify_reason(text) == expected


def test_reason_llm_fallback(db) -> None:
    service = _service(db, '{"reason": "size"}')
    assert service.classify_reason("please refund this order") == "size"


def test_reason_unknown_when_llm_garbage(db) -> None:
    service = _service(db, "not json at all")
    assert service.classify_reason("please refund this order") == "other"


def test_acceptance_positive_phrases_take_priority() -> None:
    # "no need refund" contains "no" but is an explicit acceptance (B-3 fix).
    assert _service(None).is_customer_accepted("no need refund, keep it") is True
    assert _service(None).is_customer_accepted("ok, send the replacement please") is True


def test_acceptance_negative_wins_over_yes() -> None:
    assert _service(None).is_customer_accepted("yes, but I still want a refund") is False
    assert _service(None).is_customer_accepted("no thanks, please refund") is False


def test_acceptance_llm_fallback(db) -> None:
    assert _service(db, '{"verdict": "accept_retention"}').is_customer_accepted("whatever") is True
    assert _service(db, '{"verdict": "reject_retention"}').is_customer_accepted("whatever") is False


def test_acceptance_default_false_on_uncertain(db) -> None:
    assert _service(db, '{"verdict": "uncertain"}').is_customer_accepted("hmm") is False


def test_resolve_strategy_map() -> None:
    service = _service(None)
    assert service.resolve_strategy("quality", 0) == "none"
    assert service.resolve_strategy("damaged", 0) == "none"
    assert service.resolve_strategy("size", 0) == "exchange"
    assert service.resolve_strategy("not_wanted", 0) == "compensation"
    assert service.resolve_strategy("bought_wrong", 0) == "compensation"
    assert service.resolve_strategy("other", 0) == "review"


def test_resolve_strategy_attempt_limit() -> None:
    service = _service(None)
    assert service.resolve_strategy("size", 1) == "exchange"
    assert service.resolve_strategy("size", 2) == "release"
    assert service.resolve_strategy("not_wanted", 2) == "release"
    # quality/damaged never retains, even below the limit
    assert service.resolve_strategy("quality", 1) == "none"


def test_unexpected_reason_defaults_no_retention() -> None:
    service = _service(None)
    assert service.resolve_strategy("made_up", 0) == "none"
