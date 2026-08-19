"""Replier prompt-context tests (M-07)."""

from __future__ import annotations

from app.config import Settings
from app.llm.client import BaseLLMClient
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.services.audit import utcnow
from app.services.replier import ReplierService


class CapturingLLM(BaseLLMClient):
    def __init__(self) -> None:
        self.settings = Settings(llm_provider="mock", llm_retries=0)
        self.messages: list[dict[str, str]] = []

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        self.messages = list(messages)
        return "Thank you for your question."


def test_current_email_is_injected_into_prompt(db, settings) -> None:
    customer = Customer(email="c@example.com", created_at=utcnow())
    db.add(customer)
    db.flush()
    conversation = Conversation(
        customer_id=customer.id,
        subject_normalized="question",
        window_end=utcnow(),
        last_activity_at=utcnow(),
        status="open",
    )
    db.add(conversation)
    db.flush()
    email = Email(
        conversation_id=conversation.id,
        message_id="<current@example.com>",
        subject="Question",
        from_email="c@example.com",
        to_email="bot@example.com",
        body_text="What is the exact shipping time to Germany?",
        is_inbound=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.commit()

    llm = CapturingLLM()
    ReplierService(db, settings, llm).generate(email, conversation)

    assert llm.messages, "expected at least one user message"
    combined = llm.messages[-1]["content"]
    assert "What is the exact shipping time to Germany?" in combined
    assert combined.count("What is the exact shipping time to Germany?") == 1


class FixedLLM(BaseLLMClient):
    """Returns a fixed answer for every call (used for translation checks)."""

    def __init__(self, answer: str) -> None:
        self.settings = Settings(llm_provider="mock", llm_retries=0)
        self.answer = answer

    def chat(self, messages, system_prompt=None, max_tokens=None, temperature=None) -> str:
        return self.answer


def test_build_reply_auto_translates_content_cn(db, settings) -> None:
    customer = Customer(email="c@example.com", created_at=utcnow())
    db.add(customer)
    db.flush()
    conversation = Conversation(
        customer_id=customer.id,
        subject_normalized="question",
        window_end=utcnow(),
        last_activity_at=utcnow(),
        status="open",
    )
    db.add(conversation)
    db.flush()
    email = Email(
        conversation_id=conversation.id,
        message_id="<current@example.com>",
        subject="Question",
        from_email="c@example.com",
        to_email="bot@example.com",
        body_text="Where is my order?",
        is_inbound=True,
        received_at=utcnow(),
    )
    db.add(email)
    db.flush()

    llm = FixedLLM("这是英文回复的全文中文翻译")
    reply = ReplierService(db, settings, llm).build_reply(email, conversation, "Where is my order?")
    assert reply.content_en == "Where is my order?"
    assert reply.content_cn == "这是英文回复的全文中文翻译"

    # A caller-provided Chinese version is never re-translated.
    reply2 = ReplierService(db, settings, llm).build_reply(
        email, conversation, "Where is my order?", content_cn="老板写的中文"
    )
    assert reply2.content_cn == "老板写的中文"
