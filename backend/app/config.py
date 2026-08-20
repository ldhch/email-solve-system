"""Application settings loaded from environment variables / `.env`.

All credentials and tunables come from the environment (never hard-coded).
The authoritative config list is TECH.md section 11; fields used only by
later phases are still declared here so `.env.example` documents them.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.security import SECRET_FIELDS, default_secrets_file, read_secrets_file

logger = logging.getLogger(__name__)


# Repository root: backend/app/config.py -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def prompts_dir() -> Path:
    """Return the `docs/prompts` directory, robust in repo and Docker layouts.

    The prompt files live next to the `backend/app` package (`backend/docs` or
    `/app/docs` inside the container). An optional `PROMPTS_DIR` env var
    overrides the location. Never relies on `parents[2]` path guessing.
    """

    override = os.environ.get("PROMPTS_DIR")
    if override:
        return Path(override)
    app_package = Path(__file__).resolve().parent  # .../app or /app/app
    return app_package.parent / "docs" / "prompts"


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
    secret_key: str = ""  # JWT signing key (required from Phase 2)
    encryption_key: str = ""  # Phase 4: Fernet key

    # Owner account (seeded by init-db / create-owner, Phase 2)
    owner_username: str = "boss"
    owner_password: str = ""
    jwt_expire_seconds: int = 86400  # 24h session, no refresh (TECH 6.1)

    # Database (SQLite WAL only)
    database_url: str = ""
    # Single data root: SQLite, attachments and secrets.bin all live here.
    # Relative values resolve against REPO_ROOT (local dev); Docker Compose
    # passes an absolute DATA_DIR=/app/data so everything stays in the volume.
    data_dir: str = "data"

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
    llm_base_url: str = "https://api.deepseek.com"
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "deepseek-v4-flash"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048
    llm_retries: int = 2

    # Alerting (reserved for Phase 4)
    alert_bark_webhook: str = ""
    alert_email_to: str = ""

    # Scheduling / business rules
    session_auto_close_days: int = 30
    conversation_subject_similarity_threshold: float = 0.75
    low_confidence_threshold: float = 0.6
    retention_max_attempts: int = 2
    compensation_max_usd: float = 20.0
    conversation_window_days: int = 7
    # How many past message rounds the LLM sees when drafting a reply. Bumped
    # from 6 to 12 so long threads keep their early promises/context.
    reply_history_max_turns: int = 12
    poll_interval_seconds: int = 90
    # Optional return-handling instructions appended to "release" replies.
    # Empty => the reply asks the customer for their order number first
    # (no fabricated addresses/instructions, PRD edge case 11).
    return_policy_text: str = ""

    # Phase-1 service guard for pause/resume endpoints
    agent_service_token: str = ""

    # SMTP send policy
    smtp_rate_limit_per_hour: int = 0  # 0 = disabled
    imap_timeout: int = 30
    smtp_timeout: int = 30
    # Mailbox folder where a copy of every outbound reply is stored via IMAP
    # APPEND (Titan's SMTP does not auto-save sent copies). Empty = disabled.
    imap_sent_folder: str = "Sent"

    # Paths (empty default => derived from DATA_DIR by _derive_paths_from_data_dir)
    attachment_dir: str = ""

    # Optional chargeback keyword override (comma separated)
    chargeback_keywords: str = ""

    @field_validator("database_url", mode="after")
    @classmethod
    def _resolve_database_url(cls, value: str) -> str:
        if not value:
            return ""
        prefix = "sqlite:///"
        if value.startswith(prefix) and value != "sqlite:///:memory:":
            path = value[len(prefix):]
            if path and not Path(path).is_absolute():
                return f"{prefix}{REPO_ROOT / path}"
        return value

    @field_validator("attachment_dir", mode="after")
    @classmethod
    def _resolve_attachment_dir(cls, value: str) -> str:
        if not value:
            return ""  # derived from DATA_DIR by the model validator below
        path = Path(value)
        return str(path if path.is_absolute() else REPO_ROOT / path)

    @model_validator(mode="after")
    def _derive_paths_from_data_dir(self) -> "Settings":
        """Fill database_url / attachment_dir defaults from the data root."""

        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir_path / 'app.db'}"
        if not self.attachment_dir:
            self.attachment_dir = str(self.data_dir_path / "attachments")
        return self

    @property
    def data_dir_path(self) -> Path:
        """Absolute data root (relative values resolve against REPO_ROOT)."""

        path = Path(self.data_dir)
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def chargeback_keyword_list(self) -> list[str]:
        return [k.strip() for k in self.chargeback_keywords.split(",") if k.strip()]


def apply_secret_overrides(
    settings: Settings,
    secrets_file: Path | None = None,
) -> Settings:
    """Decrypt ``data/secrets.bin`` (when present) over sensitive settings.

    A missing file, an empty ``ENCRYPTION_KEY`` or a decryption failure all
    fall back to the plaintext `.env` values, so local development and tests
    are unaffected (M-20).
    """

    path = secrets_file or default_secrets_file(settings)
    if not path.exists():
        return settings
    if not settings.encryption_key:
        logger.warning(
            "data/secrets.bin exists but ENCRYPTION_KEY is empty; using .env values"
        )
        return settings
    try:
        secrets = read_secrets_file(settings.encryption_key, path)
    except Exception as exc:  # noqa: BLE001 - never block startup on a bad key
        logger.warning("Failed to decrypt secrets.bin (%s); using .env values", exc)
        return settings
    for field in SECRET_FIELDS:
        value = secrets.get(field)
        if value and hasattr(settings, field):
            setattr(settings, field, value)
    return settings


@lru_cache(maxsize=1)
def get_settings(**overrides: Any) -> Settings:
    """Return a cached Settings instance; kwargs override env values (tests)."""

    return apply_secret_overrides(Settings(**overrides))
