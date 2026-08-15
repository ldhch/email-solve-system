"""Reply generation + send orchestration (M-07).

Phase 2 adds typed replies for the retention flow (exchange / compensation /
release / acceptance). Knowledge base / standard QA injection arrives in
Phase 3. No fabricated facts are allowed in the prompt.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.exceptions import SMTPError
from app.llm.client import BaseLLMClient
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.services.audit import log_action, utcnow

logger = logging.getLogger(__name__)

GENERATE_SYSTEM_PROMPT = """\
You are a professional customer-support agent writing English replies for a
small online store. Follow these rules strictly:
- Be polite, professional, concise and warm.
- Answer ONLY using information present in the conversation; never invent
  order numbers, tracking numbers, prices, refund amounts or policies.
- If the customer asks for information you do not have, ask them to provide
  their order number instead of guessing.
- For a thank-you note, reply briefly and warmly.
- Output only the final email body, no greetings/headers, no signature block.
"""

RETURN_HANDLING_SYSTEM_PROMPT = """\
You are a customer-support agent for a small online store. The customer asked
to return/refund an item and we are honoring their request (no retention).
Write a short English reply that:
- Apologizes for the inconvenience.
- Confirms we will process the return/refund as requested.
- If return instructions are provided below, include them; otherwise ask the
  customer to reply with their order number so we can arrange the return.
- Never invents order numbers, addresses, refund amounts or policies.
- Output only the email body; no greeting header or signature block.

Return instructions:
{return_policy}
"""

ACCEPTANCE_CONFIRMATION_SYSTEM_PROMPT = """\
You are a customer-support agent for a small online store. The customer just
accepted our retention alternative (exchange or compensation) instead of a
return. Write a short warm English reply that:
- Thanks them and confirms what happens next (the exchange / compensation is
  being arranged).
- Asks them to reply with any missing info (order number) if needed.
- Never invents order numbers, dates or policies.
- Output only the email body; no greeting header or signature block.
"""


def _load_prompt(name: str, fallback: str) -> str:
    prompt_file = (
        Path(__file__).resolve().parents[2] / "docs" / "prompts" / name
    )
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return fallback


RETENTION_PROMPT_FILES = {
    "retention_exchange": "retention_exchange.md",
    "retention_compensation": "retention_compensation.md",
}


def new_outbound_message_id(settings: Settings) -> str:
    domain = "shouhou.local"
    if "@" in settings.email_username:
        domain = settings.email_username.rsplit("@", 1)[1]
    return f"<{uuid.uuid4().hex}@{domain}>"


class ReplierService:
    """Generates aggregated English replies and sends them via SMTP."""

    def __init__(self, db: Session, settings: Settings, llm_client: BaseLLMClient) -> None:
        self.db = db
        self.settings = settings
        self.llm_client = llm_client

    def _conversation_history(self, conversation: Conversation, current: Email) -> list[dict[str, str]]:
        """Build the prompt context: recent history plus the current email.

        The current email is always appended as the final `[customer]` turn,
        otherwise the model would only see older turns and answer blindly
        (PRD F2 requires aggregating every open question in the conversation).
        """

        emails = self.db.execute(
            select(Email)
            .where(
                Email.conversation_id == conversation.id,
                Email.is_inbound.is_(True),
                Email.id != current.id,
            )
            .order_by(Email.received_at.asc())
        ).scalars().all()
        replies = self.db.execute(
            select(Reply)
            .where(Reply.conversation_id == conversation.id, Reply.status == "sent")
            .order_by(Reply.sent_at.asc())
        ).scalars().all()

        timeline: list[tuple] = []
        for email in emails:
            timeline.append((email.received_at, "customer", email.body_text or ""))
        for reply in replies:
            timeline.append((reply.sent_at or reply.created_at, "agent", reply.content_en))
        timeline.sort(key=lambda item: item[0])

        lines = []
        for _, speaker, content in timeline[-6:]:
            lines.append(f"[{speaker}] {content}\n")
        history = "\n".join(lines)
        current_text = f"[customer] {current.body_text or ''}\n"
        content = f"{history}\n{current_text}" if history else current_text
        return [{"role": "user", "content": content}]

    def generate(
        self,
        email_row: Email,
        conversation: Conversation,
        reply_type: str = "general",
        return_policy_text: str = "",
    ) -> str:
        messages = self._conversation_history(conversation, email_row)
        if reply_type in RETENTION_PROMPT_FILES:
            prompt = _load_prompt(RETENTION_PROMPT_FILES[reply_type], GENERATE_SYSTEM_PROMPT)
        elif reply_type == "retention_release":
            prompt = RETURN_HANDLING_SYSTEM_PROMPT.format(
                return_policy=return_policy_text or "(none provided)"
            )
        elif reply_type == "retention_accepted":
            prompt = ACCEPTANCE_CONFIRMATION_SYSTEM_PROMPT
        else:
            prompt = GENERATE_SYSTEM_PROMPT
        return self.llm_client.chat_with_retry(
            messages=messages,
            system_prompt=prompt,
        ).strip()

    def build_reply(
        self,
        email_row: Email,
        conversation: Conversation,
        content_en: str,
        reply_type: str = "general",
        status: str = "draft",
        content_cn: str | None = None,
    ) -> Reply:
        """Persist a Reply row without sending it."""

        reply = Reply(
            conversation_id=conversation.id,
            email_id=email_row.id,
            message_id=new_outbound_message_id(self.settings),
            in_reply_to=email_row.message_id,
            content_en=content_en,
            content_cn=content_cn,
            status=status,
            reply_type=reply_type,
            created_at=utcnow(),
        )
        self.db.add(reply)
        self.db.flush()
        return reply

    def generate_and_send(
        self,
        email_row: Email,
        conversation: Conversation,
        mailer,
        reply_type: str = "general",
        return_policy_text: str = "",
    ) -> Reply:
        """Generate, persist a Reply row and send it. Failed sends stay persisted."""

        content_en = self.generate(
            email_row,
            conversation,
            reply_type=reply_type,
            return_policy_text=return_policy_text,
        )
        reply = self.build_reply(
            email_row,
            conversation,
            content_en,
            reply_type=reply_type,
            status="draft",
        )

        try:
            mailer.send(reply, to_email=email_row.from_email, subject=email_row.subject)
        except SMTPError as exc:
            reply.status = "failed"
            reply.send_error = str(exc)
            log_action(self.db, "reply_failed", "reply", reply.id)
            logger.error("Reply id=%s failed: %s", reply.id, exc)
            return reply

        reply.status = "sent"
        reply.sent_at = utcnow()
        log_action(self.db, "reply_sent", "reply", reply.id)
        logger.info("Reply id=%s sent to %s", reply.id, email_row.from_email)
        return reply
