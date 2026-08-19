"""Bilingual TranslatorService tests (EN<->ZH)."""

from __future__ import annotations

import pytest

from app.llm.client import MockLLMClient
from app.services.translator import TranslatorService


def test_translate_to_english_with_mock(settings) -> None:
    llm = MockLLMClient(settings)
    out = TranslatorService(llm).translate_to_english("你好，我的订单在哪里？")
    assert "Mock translation" in out
    assert "你好" in out


def test_translate_to_english_passes_through_non_chinese(settings) -> None:
    # A reply written in English (or any text without CJK characters) must be
    # sent to the customer as-is, not fed to the ZH->EN translator (which
    # would make the LLM echo an instruction back).
    llm = MockLLMClient(settings)
    svc = TranslatorService(llm)
    assert svc.translate_to_english("Thanks for your patience, Chris.") == (
        "Thanks for your patience, Chris."
    )
    assert svc.translate_to_english("Order #12345 is on the way.") == (
        "Order #12345 is on the way."
    )


def test_translate_to_chinese_with_mock(settings) -> None:
    llm = MockLLMClient(settings)
    out = TranslatorService(llm).translate_to_chinese("Where is my order?")
    assert "Mock translation" in out
    assert "Where is my order?" in out


def test_translate_rejects_empty_input(settings) -> None:
    llm = MockLLMClient(settings)
    svc = TranslatorService(llm)
    with pytest.raises(ValueError):
        svc.translate_to_chinese("   ")
