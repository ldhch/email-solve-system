"""Standard QA tests (M-14): matching + CRUD + soft delete."""

from __future__ import annotations

from app.models.qa_pair import QAPair
from app.services.qa import QAService, match_qa


def _pair(question: str, answer: str, category: str | None = None, enabled: bool = True) -> QAPair:
    from app.services.audit import utcnow

    return QAPair(
        question=question,
        answer=answer,
        category=category,
        enabled=enabled,
        updated_at=utcnow(),
    )


def test_match_qa_hit() -> None:
    pair = _pair("What is the shipping time to Germany?", "7-10 business days.")
    assert match_qa("Could you tell me the shipping time to Germany?", [pair]) is pair


def test_match_qa_miss() -> None:
    pair = _pair("Do you sell XL hoodies?", "Yes.")
    assert match_qa("Where is my order?", [pair]) is None


def test_match_qa_prefers_best_score() -> None:
    generic = _pair("What is the shipping time?", "Generic answer.")
    exact = _pair("What is the shipping time to Germany?", "Germany: 7-10 days.")
    assert match_qa("What is the shipping time to Germany?", [generic, exact]) is exact


def test_qa_service_crud(db) -> None:
    service = QAService(db)
    pair = service.create("What is your return policy?", "30 days.", "policy")
    assert pair.enabled is True

    assert service.update(pair.id, answer="30 days, no restocking fee.").answer == (
        "30 days, no restocking fee."
    )
    assert service.update(pair.id, enabled=False).enabled is False
    assert service.update(pair.id, category=None).category is None
    assert service.update(9999, answer="x") is None

    assert service.soft_delete(pair.id) is True
    assert service.soft_delete(pair.id) is False
    assert service.get(pair.id) is None


def test_list_active_excludes_disabled_and_deleted(db) -> None:
    service = QAService(db)
    enabled = service.create("Q1?", "A1.")
    disabled = service.create("Q2?", "A2.")
    service.update(disabled.id, enabled=False)
    deleted = service.create("Q3?", "A3.")
    service.soft_delete(deleted.id)

    active = service.list_active()
    assert [p.id for p in active] == [enabled.id]
    assert [p.id for p in service.list_all()] == [enabled.id, disabled.id]
