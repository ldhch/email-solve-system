"""User-editable quick reply template model.

The boss manages a small set of canned Chinese replies (退货/物流/补偿/通用 by
default); the conversation reply box renders them as one-click fill buttons.
Editing here propagates to the editor immediately.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# First-run defaults for the quick-reply row (退货/物流/补偿/通用), matching the
# replies that were previously hardcoded in the frontend editor. Seeded only
# when the table is empty, so the boss's edits are never overwritten.
DEFAULT_REPLY_TEMPLATES = [
    (
        "退货",
        "非常抱歉给您带来不便。您可以退回商品，我们会为您全额退款。"
        "请回复您的订单号，我们会通过邮件发送退货标签和详细指引。",
    ),
    (
        "物流",
        "感谢您的耐心等待。我已为您查询物流，包裹正在途中，预计很快送达。"
        "若仍无更新，我们会继续为您跟进。",
    ),
    (
        "补偿",
        "非常抱歉给您带来的不便。为表歉意，我们将为您申请补偿。"
        "请回复确认，我们会尽快为您处理。",
    ),
    (
        "通用",
        "感谢您联系我们。我们会尽快处理您的问题，并在一到两个工作日内给您回复。",
    ),
]


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Display order in the quick-template row (lowest first).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
