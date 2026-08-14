"""Domain exceptions for the Phase-1 pipeline."""

from __future__ import annotations


class ShouhouError(Exception):
    """Base class for all application errors."""


class ConfigurationError(ShouhouError):
    """Missing/invalid configuration (e.g. empty required credentials)."""


class IMAPError(ShouhouError):
    """IMAP fetch/login failure."""


class SMTPError(ShouhouError):
    """SMTP send failure."""


class SMTPRateLimitError(SMTPError):
    """Outbound rate limit reached."""


class LLMError(ShouhouError):
    """LLM call failed after retries, or returned unparseable output."""


class AIPausedError(ShouhouError):
    """System is in emergency-pause state."""
