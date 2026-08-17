"""Alerting channels + failure escalation (M-18, PRD F10).

Two channels are supported:
- SMTP email to ``ALERT_EMAIL_TO`` (reuses :class:`MailerService`).
- Bark push to ``ALERT_BARK_WEBHOOK``.

A channel whose target is not configured is skipped with a log line and never
raises; an alert send failure is also logged only and never affects the main
mail pipeline (TECH N-3).

Escalation rules (TECH 3.1 M-18):
- LLM: 5 consecutive failures within a 5-minute window -> Bark + email.
- IMAP: 3 consecutive failed poll cycles (about 4.5 min) -> Bark + email.

Counters are process-local (single-process deployment, TECH 2.2); a success
resets the consecutive-failure counters.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections import deque

from app.config import Settings
from app.services.audit import utcnow
from app.services.mailer import MailerService

logger = logging.getLogger(__name__)

LLM_FAILURE_WINDOW_SECONDS = 300  # 5 minutes
LLM_FAILURE_THRESHOLD = 5
IMAP_FAILURE_THRESHOLD = 3

_llm_failure_times: deque[float] = deque()
_imap_failure_count = 0


class AlertingService:
    """Dispatches alerts over every configured channel (SMTP + Bark)."""

    def __init__(
        self,
        settings: Settings,
        mailer: MailerService | None = None,
        urlopen=urllib.request.urlopen,
    ) -> None:
        self.settings = settings
        self._mailer = mailer
        self._urlopen = urlopen

    def send_alert(self, title: str, message: str) -> dict[str, bool]:
        """Send one alert through all configured channels.

        Returns ``{"bark": bool, "email": bool}`` delivered status. Missing
        channels and send failures are logged, never raised.
        """

        results = {"bark": False, "email": False}
        if self.settings.alert_bark_webhook:
            try:
                self._send_bark(title, message)
                results["bark"] = True
            except Exception as exc:  # noqa: BLE001 - alerting must never block
                logger.warning("Bark alert failed (title=%r): %s", title, exc)
        else:
            logger.info("Bark alert skipped: ALERT_BARK_WEBHOOK not configured")

        if self.settings.alert_email_to:
            try:
                self._send_email(title, message)
                results["email"] = True
            except Exception as exc:  # noqa: BLE001 - alerting must never block
                logger.warning("Email alert failed (title=%r): %s", title, exc)
        else:
            logger.info("Email alert skipped: ALERT_EMAIL_TO not configured")
        return results

    # ---------- channels ----------

    def _send_bark(self, title: str, message: str) -> None:
        """POST ``{title, body}`` JSON to the configured Bark webhook."""

        webhook = self.settings.alert_bark_webhook.strip()
        if "://" not in webhook:
            # Bare device key: use the official Bark push endpoint.
            webhook = f"https://api.day.app/{webhook}"
        payload = json.dumps({"title": title, "body": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._urlopen(req, timeout=10) as resp:
            resp.read()

    def _send_email(self, title: str, message: str) -> None:
        """Send the alert via SMTP, reusing :class:`MailerService`."""

        mailer = self._mailer or MailerService(None, self.settings)
        body = f"Time (UTC): {utcnow().isoformat(timespec='seconds')}\n{message}"
        mailer.send_text(
            to_email=self.settings.alert_email_to,
            subject=f"[shouhou-agent] {title}",
            body=body,
        )


def reset_failure_counters() -> None:
    """Clear process-local failure state (used by tests and restarts)."""

    global _imap_failure_count
    _llm_failure_times.clear()
    _imap_failure_count = 0


def record_llm_success() -> None:
    """A successful LLM call resets the consecutive-failure window."""

    _llm_failure_times.clear()


def record_llm_failure(settings: Settings, error: str | None = None) -> bool:
    """Record one LLM failure; returns True when an alert was dispatched."""

    now = time.time()
    while _llm_failure_times and now - _llm_failure_times[0] > LLM_FAILURE_WINDOW_SECONDS:
        _llm_failure_times.popleft()
    _llm_failure_times.append(now)
    if len(_llm_failure_times) < LLM_FAILURE_THRESHOLD:
        return False
    _llm_failure_times.clear()
    summary = error or "no error details"
    AlertingService(settings).send_alert(
        "LLM 连续失败告警",
        f"LLM 调用在 {LLM_FAILURE_WINDOW_SECONDS // 60} 分钟内连续失败 "
        f"{LLM_FAILURE_THRESHOLD} 次，请检查 API Key / 余额 / 网络。\n最近错误：{summary[:500]}",
    )
    return True


def record_imap_success() -> None:
    """A successful poll cycle resets the IMAP consecutive-failure counter."""

    global _imap_failure_count
    _imap_failure_count = 0


def record_imap_failure(settings: Settings, error: str | None = None) -> bool:
    """Record one failed poll cycle; returns True when an alert was dispatched."""

    global _imap_failure_count
    _imap_failure_count += 1
    if _imap_failure_count < IMAP_FAILURE_THRESHOLD:
        return False
    _imap_failure_count = 0
    summary = error or "no error details"
    AlertingService(settings).send_alert(
        "IMAP 拉取连续失败告警",
        f"IMAP 拉取已连续 {IMAP_FAILURE_THRESHOLD} 个轮询周期失败，"
        "邮件可能积压，请检查邮箱密码 / IMAP 服务可用性。\n最近错误："
        f"{summary[:500]}",
    )
    return True
