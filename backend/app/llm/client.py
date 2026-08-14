"""DeepSeek LLM client with a provider switch (M-09).

Core interface (TECH.md section 12):
    `client.chat(messages, system_prompt) -> str`

Supported providers:
- `deepseek` (default): OpenAI-compatible endpoint at api.deepseek.com
- `openai`          : OpenAI-compatible endpoint (drop-in)
- `mock`            : deterministic local responses for tests / offline demo

Adding another provider (e.g. Anthropic) means adding one class here and one
branch in `build_llm_client`; `config.LLM_PROVIDER` selects it.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from app.config import Settings
from app.core.exceptions import ConfigurationError, LLMError

logger = logging.getLogger(__name__)

CHAT_ROLE_SYSTEM = "system"
CHAT_ROLE_USER = "user"


class BaseLLMClient(ABC):
    """Provider-agnostic chat interface."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return the assistant's text answer for the given messages."""

    def chat_with_retry(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Call `chat` with retries and exponential backoff (PRD: retry 2)."""

        retries = max(0, self.settings.llm_retries)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self.chat(
                    messages=messages,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:  # noqa: BLE001 - retry covers all provider errors
                last_error = exc
                logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, retries + 1, exc)
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"LLM call failed after {retries + 1} attempt(s): {last_error}") from last_error


class OpenAIClient(BaseLLMClient):
    """OpenAI-compatible client (used for both `deepseek` and `openai`)."""

    def __init__(self, settings: Settings, base_url: str, api_key: str) -> None:
        super().__init__(settings)
        if not api_key:
            raise ConfigurationError(
                f"Missing API key for LLM provider '{settings.llm_provider}' "
                "(set DEEPSEEK_API_KEY / OPENAI_API_KEY in .env)"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
            raise ConfigurationError("The 'openai' package is not installed") from exc
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)

    def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        full_messages = list(messages)
        if system_prompt:
            full_messages.insert(0, {"role": CHAT_ROLE_SYSTEM, "content": system_prompt})
        response = self._client.chat.completions.create(
            model=self.settings.llm_model,
            messages=full_messages,
            temperature=(
                temperature if temperature is not None else self.settings.llm_temperature
            ),
            max_tokens=max_tokens or self.settings.llm_max_tokens,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "LLM usage: prompt=%s completion=%s total=%s",
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("LLM returned an empty response")
        return content


class MockLLMClient(BaseLLMClient):
    """Deterministic local client for tests and offline demos (no network)."""

    def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        user_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
        lower = user_text.lower()
        system_prompt_lower = (system_prompt or "").lower()
        if (
            "classify" in system_prompt_lower
            or "classifier" in system_prompt_lower
            or "risk_level" in system_prompt_lower
        ):
            if any(word in lower for word in ("chargeback", "dispute", "sue", "lawyer")):
                return json.dumps(
                    {
                        "risk_level": "high",
                        "confidence": 0.98,
                        "category": "refund_request",
                        "chargeback_risk": True,
                        "summary_cn": "客户威胁发起拒付（模拟判定）",
                    }
                )
            if any(word in lower for word in ("refund", "return", "exchange", "退货", "退款")):
                return json.dumps(
                    {
                        "risk_level": "medium",
                        "confidence": 0.9,
                        "category": "refund_request",
                        "chargeback_risk": False,
                        "summary_cn": "客户要求退换货（模拟判定）",
                    }
                )
            return json.dumps(
                {
                    "risk_level": "low",
                    "confidence": 0.95,
                    "category": "product_spec",
                    "chargeback_risk": False,
                    "summary_cn": "客户咨询产品规格（模拟判定）",
                }
            )
        return (
            "Thank you for reaching out to us. (Mock reply - replace LLM_PROVIDER=deepseek "
            "with a real API key for production.)"
        )


def build_llm_client(settings: Settings) -> BaseLLMClient:
    """Factory for the configured provider."""

    provider = (settings.llm_provider or "deepseek").strip().lower()
    if provider == "deepseek":
        return OpenAIClient(
            settings,
            base_url="https://api.deepseek.com",
            api_key=settings.deepseek_api_key,
        )
    if provider == "openai":
        return OpenAIClient(
            settings,
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
        )
    if provider == "mock":
        return MockLLMClient(settings)
    raise ConfigurationError(
        f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
        "Supported: deepseek | openai | mock. Add new providers in llm/client.py."
    )
