"""Config loading tests."""

from __future__ import annotations

from app.config import REPO_ROOT, Settings


def test_default_database_url_is_repo_sqlite() -> None:
    settings = Settings()
    assert settings.database_url == f"sqlite:///{REPO_ROOT / 'data' / 'app.db'}"


def test_env_override_wins() -> None:
    settings = Settings(database_url="sqlite:///:memory:", llm_provider="mock")
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.llm_provider == "mock"


def test_relative_database_url_resolved_against_repo() -> None:
    settings = Settings(database_url="sqlite:///data/custom.db")
    assert settings.database_url == f"sqlite:///{REPO_ROOT / 'data' / 'custom.db'}"


def test_chargeback_keyword_override() -> None:
    settings = Settings(chargeback_keywords="chargeback, dispute, BBB")
    assert settings.chargeback_keyword_list == ["chargeback", "dispute", "BBB"]
