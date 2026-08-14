"""Application settings loaded from environment variables / `.env`.

All credentials and tunables come from the environment (never hard-coded).
The authoritative config list is TECH.md section 11; fields used only by
later phases are still declared here so `.env.example` documents them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Repository root: backend/app/config.py -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Runtime settings. Env vars take priority over `.env` file values."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # App
    app_name: str = "shouhou-agent"
    app_env: str = "development"
    secret_key: str = ""  # Phase 2: JWT signing key
    encryption_key: str = ""  # Phase 4: Fernet key

    # Database (SQLite WAL only)
    database_url: str = ""

    # Mail (Hostinger Titan Email)
    imap_host: str = "imap.titan.email"
    imap_port: int = 993
    smtp_host: str = "smtp.titan.email"
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    email_username: str = ""
    email_password: str = ""
    mail_from_name: str = ""

    # LLM provider
    llm_provider: str = "deepseek"  # deepseek | openai | mock
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048
    llm_retries: int = 2

    # Alerting (reserved for Phase 4)
    alert_bark_webhook: str = ""
    alert_email_to: str = ""

    # Scheduling / business rules
    session_auto_close_days: int = 30
    conversation_subject_similarity_threshold: float = 0.85
    low_confidence_threshold: float = 0.6
    retention_max_attempts: int = 2
    compensation_max_usd: float = 10.0
    conversation_window_days: int = 7
    poll_interval_seconds: int = 90

    # Phase-1 service guard for pause/resume endpoints
    agent_service_token: str = ""

    # SMTP send policy
    smtp_rate_limit_per_hour: int = 0  # 0 = disabled
    imap_timeout: int = 30
    smtp_timeout: int = 30

    # Paths
    attachment_dir: str = "data/attachments"

    # Optional chargeback keyword override (comma separated)
    chargeback_keywords: str = ""

    @field_validator("database_url", mode="after")
    @classmethod
    def _resolve_database_url(cls, value: str) -> str:
        if not value:
            return f"sqlite:///{REPO_ROOT / 'data' / 'app.db'}"
        prefix = "sqlite:///"
        if value.startswith(prefix) and value != "sqlite:///:memory:":
            path = value[len(prefix):]
            if path and not Path(path).is_absolute():
                return f"{prefix}{REPO_ROOT / path}"
        return value

    @field_validator("attachment_dir", mode="after")
    @classmethod
    def _resolve_attachment_dir(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else REPO_ROOT / path)

    @property
    def chargeback_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.chargeback_keywords.split(",") if k.strip()]


@lru_cache(maxsize=1)
def get_settings(**overrides: Any) -> Settings:
    """Return a cached Settings instance; kwargs override env values (tests)."""
    return Settings(**overrides)
