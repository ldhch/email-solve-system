"""Request schemas for Phase 2 admin APIs (M-15)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ManualReplyRequest(BaseModel):
    content_cn: str = Field(min_length=1, max_length=5000)


class RejectReplyRequest(BaseModel):
    reason: str = ""


class EditReplyRequest(BaseModel):
    content_cn: str | None = Field(default=None, max_length=5000)


class SendReplyRequest(BaseModel):
    pass


class SplitConversationRequest(BaseModel):
    at_email_id: int


class MergeConversationRequest(BaseModel):
    other_conversation_id: int


class TicketUpdateRequest(BaseModel):
    status: str | None = None
    owner_reply_cn: str | None = None


class QAPairCreateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=10000)
    category: str | None = Field(default=None, max_length=100)


class QAPairUpdateRequest(BaseModel):
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    answer: str | None = Field(default=None, min_length=1, max_length=10000)
    category: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class QAPairBulkItem(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=10000)
    category: str | None = Field(default=None, max_length=100)


class QAPairBulkRequest(BaseModel):
    items: list[QAPairBulkItem] = Field(min_length=1, max_length=200)
