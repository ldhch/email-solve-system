"""Bilingual translation service (ZH->EN for replies, EN->ZH for display)."""

from __future__ import annotations

import re

from app.config import prompts_dir
from app.llm.client import BaseLLMClient

# Matches any CJK Unified Ideograph. If the boss's "Chinese reply" contains
# none, the text is already English (or Latin) — sending it to a "translate
# ZH->EN" prompt makes the LLM echo an instruction back ("Please provide the
# Chinese reply..."). Pass such text through unchanged instead.
_CJK_RE = re.compile(r"[一-鿿]")

TRANSLATE_SYSTEM_PROMPT = """\
You translate the store owner's Chinese reply into professional, polite,
natural English for a customer-support email.
Rules:
- Keep the tone warm, professional and concise (a normal business email length).
- Keep order numbers, amounts, tracking numbers and proper nouns exactly as-is.
- Do not add information that is not present in the original Chinese.
- Output only the English translation; no headers, quotes or explanations.
"""

TRANSLATE_CN_SYSTEM_PROMPT = """\
You translate English customer-support email content into natural, fluent
Simplified Chinese (中文).
Rules:
- Translate the FULL text faithfully; never summarize or drop details.
- Preserve paragraph and list structure, and the original line breaks.
- Keep order numbers, amounts, tracking numbers, brand names and proper nouns
  exactly as-is.
- Output only the Chinese translation; no headers, quotes or explanations.
"""


def _load_prompt() -> str:
    prompt_file = prompts_dir() / "translate_reply.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return TRANSLATE_SYSTEM_PROMPT


class TranslatorService:
    """Translates between the owner's Chinese and the customer's English."""

    def __init__(self, llm_client: BaseLLMClient) -> None:
        self.llm_client = llm_client

    def translate_to_english(self, text: str) -> str:
        if not text or not text.strip():
            raise ValueError("Empty Chinese reply")
        text = text.strip()
        if not _CJK_RE.search(text):
            return text
        return self.llm_client.chat_with_retry(
            messages=[{"role": "user", "content": text}],
            system_prompt=_load_prompt(),
            temperature=0.2,
        ).strip()

    def translate_to_chinese(self, text: str) -> str:
        """Translate the full English text into Simplified Chinese."""
        if not text or not text.strip():
            raise ValueError("Empty English content")
        return self.llm_client.chat_with_retry(
            messages=[{"role": "user", "content": text.strip()}],
            system_prompt=TRANSLATE_CN_SYSTEM_PROMPT,
            temperature=0.2,
            # Full-text translation of long emails needs a larger output budget
            # than the default 2048 (which truncates to an empty response).
            max_tokens=4096,
        ).strip()
