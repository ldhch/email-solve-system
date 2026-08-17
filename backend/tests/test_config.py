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


def test_compensation_cap_default_is_20() -> None:
    assert Settings().compensation_max_usd == 20.0


def test_data_dir_absolute_derives_db_and_attachments() -> None:
    settings = Settings(data_dir="/app/data", database_url="", attachment_dir="")
    assert settings.database_url == "sqlite:////app/data/app.db"
    assert settings.attachment_dir == "/app/data/attachments"


def test_data_dir_relative_resolves_against_repo() -> None:
    settings = Settings(data_dir="var/data", database_url="", attachment_dir="")
    assert settings.database_url == f"sqlite:///{REPO_ROOT / 'var' / 'data' / 'app.db'}"
    assert settings.attachment_dir == str(REPO_ROOT / "var" / "data" / "attachments")


def test_explicit_paths_win_over_data_dir() -> None:
    settings = Settings(
        data_dir="/app/data",
        database_url="sqlite:///:memory:",
        attachment_dir="/tmp/attachments",
    )
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.attachment_dir == "/tmp/attachments"
