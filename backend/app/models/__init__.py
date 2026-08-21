"""ORM models. Importing this package registers all models on Base.metadata."""

from app.models.attachment import Attachment
from app.models.audit import AuditLog
from app.models.blocked_sender import BlockedSender
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.email import Email
from app.models.knowledge_doc import KnowledgeDoc
from app.models.qa_pair import QAPair
from app.models.reply import Reply
from app.models.reply_template import ReplyTemplate
from app.models.system_state import SystemState
from app.models.ticket import Ticket
from app.models.user import User

__all__ = [
    "Attachment",
    "AuditLog",
    "BlockedSender",
    "Conversation",
    "Customer",
    "Email",
    "KnowledgeDoc",
    "QAPair",
    "Reply",
    "ReplyTemplate",
    "SystemState",
    "Ticket",
    "User",
]
