"""Standard QA service (M-14): CRUD + full injection (<=100 rows)."""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.qa_pair import QAPair
from app.services.audit import utcnow

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9']+")

# Generic English function words are ignored when matching a customer email
# against stored questions (keyword matching only, no vector retrieval).
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "for", "with", "without", "from",
        "into", "onto", "about", "what", "how", "why", "when", "where", "which",
        "who", "is", "are", "was", "were", "do", "does", "did", "can", "could",
        "would", "should", "will", "shall", "may", "might", "i", "you", "your",
        "my", "me", "we", "us", "our", "it", "its", "this", "that", "these",
        "those", "to", "of", "in", "on", "at", "please", "have", "has", "had",
        "been", "be", "not", "no", "yes", "if", "as", "so", "than", "then",
    }
)


def match_qa(
    text: str,
    pairs: list[QAPair],
    threshold: float = 0.6,
    min_words: int = 2,
) -> QAPair | None:
    """Pick the best keyword match between a customer email and stored QAs.

    Deterministic pre-hit so a matched standard answer is returned verbatim
    without a free-form LLM generation (M-07). All active QAs are still
    injected into the prompt as a semantic fallback.
    """

    body_words = set(_WORD_RE.findall((text or "").lower()))
    best: QAPair | None = None
    best_score = 0.0
    best_words = 0
    for pair in pairs:
        q_words = [
            w
            for w in _WORD_RE.findall(pair.question.lower())
            if len(w) >= 3 and w not in _STOPWORDS
        ]
        if len(q_words) < min_words:
            continue
        present = sum(1 for w in q_words if w in body_words)
        score = present / len(q_words)
        # Equal scores: prefer the more specific question (more keywords).
        if score >= threshold and (score > best_score or (score == best_score and len(q_words) > best_words)):
            best, best_score, best_words = pair, score, len(q_words)
    return best


class QAService:
    """CRUD for standard Q&A pairs (soft delete, enable/disable)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_active(self, limit: int = 100) -> list[QAPair]:
        """Enabled, non-deleted pairs for full prompt injection (<=100)."""

        return self.db.execute(
            select(QAPair)
            .where(QAPair.is_deleted.is_(False), QAPair.enabled.is_(True))
            .order_by(QAPair.id.asc())
            .limit(limit)
        ).scalars().all()

    def list_all(self) -> list[QAPair]:
        return self.db.execute(
            select(QAPair)
            .where(QAPair.is_deleted.is_(False))
            .order_by(QAPair.id.asc())
        ).scalars().all()

    def get(self, pair_id: int) -> QAPair | None:
        pair = self.db.get(QAPair, pair_id)
        if pair is None or pair.is_deleted:
            return None
        return pair

    def create(
        self, question: str, answer: str, category: str | None = None
    ) -> QAPair:
        pair = QAPair(
            question=question.strip(),
            answer=answer.strip(),
            category=(category or "").strip() or None,
            enabled=True,
            updated_at=utcnow(),
        )
        self.db.add(pair)
        self.db.flush()
        return pair

    def update(self, pair_id: int, **fields) -> QAPair | None:
        pair = self.get(pair_id)
        if pair is None:
            return None
        for key, value in fields.items():
            if key == "category":
                if value is None:
                    pair.category = None
                else:
                    pair.category = (value or "").strip() or None
            elif value is not None:
                setattr(pair, key, value)
        pair.updated_at = utcnow()
        self.db.flush()
        return pair

    def soft_delete(self, pair_id: int) -> bool:
        pair = self.get(pair_id)
        if pair is None:
            return False
        pair.is_deleted = True
        pair.updated_at = utcnow()
        self.db.flush()
        return True
