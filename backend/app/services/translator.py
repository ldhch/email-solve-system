"""Chinese -> English translation service (M-10, PRD F6)."""

from __future__ import annotations

from pathlib import Path

from app.llm.client import BaseLLMClient

TRANSLATE_SYSTEM_PROMPT = """\
You translate the store owner's Chinese reply into professional, polite,
natural English for a customer-support email.
Rules:
- Keep the tone warm, professional and concise (a normal business email length).
- Keep order numbers, amounts, tracking numbers and proper nouns exactly as-is.
- Do not add information that is not present in the original Chinese.
- Output only the English translation; no headers, quotes or explanations.
"""


def _load_prompt() -> str:
    prompt_file = (
        Path(__file__).resolve().parents[2] / "docs" / "prompts" / "translate_reply.md"
    )
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return TRANSLATE_SYSTEM_PROMPT


class TranslatorService:
    """Translates the owner's Chinese reply before sending (F6)."""

    def __init__(self, llm_client: BaseLLMClient) -> None:
        self.llm_client = llm_client

    def translate_to_english(self, text: str) -> str:
        if not text or not text.strip():
            raise ValueError("Empty Chinese reply")
        return self.llm_client.chat_with_retry(
            messages=[{"role": "user", "content": text.strip()}],
            system_prompt=_load_prompt(),
            temperature=0.2,
        ).strip()
