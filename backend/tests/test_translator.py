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
