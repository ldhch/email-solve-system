"""Reply generation + send orchestration (M-07).

Phase 2 adds typed replies for the retention flow (exchange / compensation /
release / acceptance). Phase 3 adds the high-risk reassurance reply plus
standard-QA / knowledge-base injection for general replies. No fabricated
facts are allowed in the prompt.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, prompts_dir
from app.core.exceptions import SMTPError
from app.llm.client import BaseLLMClient
from app.models.conversation import Conversation
from app.models.email import Email
from app.models.reply import Reply
from app.services.knowledge import KnowledgeService
from app.services.qa import QAService, match_qa
from app.services.audit import log_action, utcnow

logger = logging.getLogger(__name__)

# Every customer-facing reply must be a complete letter-like email: a greeting
# line, the body, a closing and a signature. The customer name is read from the
# conversation when available; inventing one is forbidden.
EMAIL_FORMAT_RULE = (
    "- Write a complete standard business email in letter format: a greeting "
    'line ("Dear [customer first name]," — use the customer\'s name from the '
    'conversation when available, otherwise "Hi there,"), the reply body, '
    'a closing "Best regards,", and a signature "The LBORA Team". '
    "Never invent a customer name."
)

GENERATE_SYSTEM_PROMPT = """\
You are a professional customer-support agent writing English replies for a
small online store. Follow these rules strictly:
- Be polite, professional, concise and warm.
- Answer ONLY using information present in the conversation; never invent
  order numbers, tracking numbers, prices, refund amounts or policies.
- If the customer asks for information you do not have, ask them to provide
  their order number instead of guessing.
- For a thank-you note, reply briefly and warmly.
""" + EMAIL_FORMAT_RULE

RETURN_HANDLING_SYSTEM_PROMPT = """\
You are a customer-support agent for a small online store. The customer asked
to return/refund an item and we are honoring their request (no retention).
Write a short English reply that:
- Apologizes for the inconvenience.
- Confirms we will process the return/refund as requested.
- If return instructions are provided below, include them; otherwise ask the
  customer to reply with their order number so we can arrange the return.
- Never invents order numbers, addresses, refund amounts or policies.
""" + EMAIL_FORMAT_RULE + """

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
""" + EMAIL_FORMAT_RULE

REASSURANCE_SYSTEM_PROMPT = """\
You are a customer-support agent for a small online store. The customer sent a
high-risk or escalated message (possible dispute, complaint or urgent issue).
Write a short, calm English acknowledgment that:
- Thanks the customer for contacting us and apologizes for the frustration.
- Promises that a dedicated support agent will reply within 24 hours.
- Does NOT promise any refund, compensation, replacement or policy outcome.
- Never invents order numbers, dates or facts.
""" + EMAIL_FORMAT_RULE

COMPENSATION_SYSTEM_PROMPT = """\
You are a customer-support agent for a small online store. The customer is
hesitant (changed their mind / bought the wrong item) and asked for a refund.
Write a short English reply that:
- Thanks them for their honesty and apologizes for the inconvenience.
- Offers a goodwill alternative (for example a partial refund or a small
  discount on their next order) so they can keep the item.
- Does NOT promise a specific amount or percentage: the owner reviews this
  draft before sending and may edit it.
- Compensation cap: never suggest a total amount above {compensation_max_usd} USD.
  If the customer explicitly asks for more than the cap, keep the offer
  within the cap; the owner reviews the draft and may adjust it before sending.
- Never invents order numbers, prices or policies.
""" + EMAIL_FORMAT_RULE

UNCONFIRMED_MARKER = (
    'Please note: some information is not confirmed and requires '
    "manual verification."
)


def _load_prompt(name: str, fallback: str) -> str:
    prompt_file = prompts_dir() / name
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    return fallback


RETENTION_PROMPT_FILES = {
    "retention_exchange": "retention_exchange.md",
}


def build_general_system_prompt(qa_pairs: list, kb_text: str) -> str:
    """Compose the general-reply system prompt (QA + KB full injection)."""

    sections = [GENERATE_SYSTEM_PROMPT]
    if qa_pairs:
        lines = [
            "Standard Q&A (if the customer's question matches one of these, "
            "output that standard answer VERBATIM; do not rewrite or embellish):"
        ]
        for pair in qa_pairs:
            lines.append(f"Q: {pair.question}\nA: {pair.answer}")
        sections.append("\n".join(lines))
    if kb_text:
        sections.append(
            "Company knowledge base (use ONLY this information; never invent "
            f"facts):\n{kb_text}"
        )
    sections.append(
        "If the customer's question is NOT covered by the standard Q&A or the "
        "knowledge base, reply with a general, polite message that asks for the "
        f"order number if needed, and include this exact sentence: "
        f'"{UNCONFIRMED_MARKER}"'
    )
    return "\n\n".join(sections)


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

    def _timeline_context(
        self,
        conversation: Conversation,
        exclude_email_ids: set[int],
        current_emails: list[Email],
    ) -> list[dict[str, str]]:
        """Build the prompt context: recent history plus current email(s).

        The current email(s) are always appended as the final `[customer]`
        turns, otherwise the model would answer blindly (PRD F2 requires
        aggregating every open question in the conversation).
        """

        history_emails = self.db.execute(
            select(Email)
            .where(
                Email.conversation_id == conversation.id,
                Email.is_inbound.is_(True),
                Email.id.not_in(exclude_email_ids),
            )
            .order_by(Email.received_at.asc())
        ).scalars().all()
        replies = self.db.execute(
            select(Reply)
            .where(Reply.conversation_id == conversation.id, Reply.status == "sent")
            .order_by(Reply.sent_at.asc())
        ).scalars().all()

        timeline: list[tuple] = []
        for email in history_emails:
            timeline.append((email.received_at, "customer", email.body_text or ""))
        for reply in replies:
            timeline.append((reply.sent_at or reply.created_at, "agent", reply.content_en))
        timeline.sort(key=lambda item: item[0])

        lines = []
        for _, speaker, content in timeline[-6:]:
            lines.append(f"[{speaker}] {content}\n")
        for email in sorted(current_emails, key=lambda e: (e.received_at, e.id)):
            lines.append(f"[customer] {email.body_text or ''}\n")
        content = "\n".join(lines)
        return [{"role": "user", "content": content}]

    def _conversation_history(self, conversation: Conversation, current: Email) -> list[dict[str, str]]:
        return self._timeline_context(conversation, {current.id}, [current])

    def _batch_context(
        self, conversation: Conversation, new_emails: list[Email]
    ) -> list[dict[str, str]]:
        """Prompt context for one aggregated reply over a poll batch."""

        return self._timeline_context(
            conversation, {e.id for e in new_emails}, new_emails
        )

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
        elif reply_type == "retention_compensation":
            prompt = _load_prompt(
                "retention_compensation.md", COMPENSATION_SYSTEM_PROMPT
            ).format(compensation_max_usd=f"{self.settings.compensation_max_usd:.0f}")
        elif reply_type == "retention_release":
            prompt = RETURN_HANDLING_SYSTEM_PROMPT.format(
                return_policy=return_policy_text or "(none provided)"
            )
        elif reply_type == "retention_accepted":
            prompt = ACCEPTANCE_CONFIRMATION_SYSTEM_PROMPT
        elif reply_type == "reassurance":
            prompt = _load_prompt("reassurance.md", REASSURANCE_SYSTEM_PROMPT)
        else:
            qa_pairs = QAService(self.db).list_active(limit=100)
            matched = match_qa(email_row.body_text or "", qa_pairs)
            if matched is not None:
                logger.info(
                    "QA hit (id=%s) for email id=%s; using stored answer",
                    matched.id,
                    email_row.id,
                )
                return matched.answer
            prompt = build_general_system_prompt(qa_pairs, KnowledgeService(self.db).full_text())
        return self.llm_client.chat_with_retry(
            messages=messages,
            system_prompt=prompt,
        ).strip()

    def generate_aggregated(
        self,
        new_emails: list[Email],
        conversation: Conversation,
    ) -> str:
        """Generate ONE English reply covering every new email in the batch.

        PRD F2 / edge case 3: a customer sending N mails in a short window gets
        a single reply that aggregates all open questions (no reply spam).
        QA hits use the combined batch body; otherwise the general prompt with
        QA + knowledge-base injection is used.
        """

        messages = self._batch_context(conversation, new_emails)
        combined_body = "\n".join(
            e.body_text or ""
            for e in sorted(new_emails, key=lambda e: (e.received_at, e.id))
        )
        qa_pairs = QAService(self.db).list_active(limit=100)
        matched = match_qa(combined_body, qa_pairs)
        if matched is not None:
            logger.info(
                "QA hit (id=%s) for aggregated batch conversation=%s",
                matched.id,
                conversation.id,
            )
            return matched.answer
        prompt = build_general_system_prompt(qa_pairs, KnowledgeService(self.db).full_text())
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
