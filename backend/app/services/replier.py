"""Reply generation + send orchestration (M-07, Phase 1 scope).

Phase 1 injects the conversation history only; knowledge base / standard QA
injection arrives in Phase 3. No fabricated facts are allowed in the prompt.
"""

from __future__ import annotations

import logging
import uuid

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
        """Last 6 inbound emails + sent replies of the conversation, oldest first."""

        emails = self.db.execute(
            select(Email)
            .where(Email.conversation_id == conversation.id, Email.is_inbound.is_(True))
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
        return [{"role": "user", "content": "\n".join(lines)}]

    def generate(self, email_row: Email, conversation: Conversation) -> str:
        messages = self._conversation_history(conversation, email_row)
        return self.llm_client.chat_with_retry(
            messages=messages,
            system_prompt=GENERATE_SYSTEM_PROMPT,
        ).strip()

    def generate_and_send(self, email_row: Email, conversation: Conversation, mailer) -> Reply:
        """Generate, persist a Reply row and send it. Failed sends stay persisted."""

        content_en = self.generate(email_row, conversation)
        reply = Reply(
            conversation_id=conversation.id,
            email_id=email_row.id,
            message_id=new_outbound_message_id(self.settings),
            in_reply_to=email_row.message_id,
            content_en=content_en,
            status="draft",
            reply_type="general",
            created_at=utcnow(),
        )
        self.db.add(reply)
        self.db.flush()

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
