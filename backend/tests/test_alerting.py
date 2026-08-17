"""M-18 alerting channel + failure escalation tests."""

from __future__ import annotations

import json
import time

import pytest

from app.config import Settings
from app.core.exceptions import LLMError, SMTPError
from app.llm.client import MockLLMClient
from app.services import alerting
from app.services.alerting import AlertingService
from app.services.mailer import MailerService

from conftest import FakeSMTP


def _settings(**kwargs) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        llm_provider="mock",
        email_username="bot@example.com",
        email_password="pw",
        secret_key="test-key",
        alert_bark_webhook="",
        alert_email_to="",
    )
    base.update(kwargs)
    return Settings(**base)


class _UrlOpenSpy:
    """Records Bark POST payloads; can simulate failures."""

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[tuple[str, bytes]] = []
        self.fail = fail

    def __call__(self, request, timeout=None):
        if self.fail:
            raise OSError("simulated bark network failure")
        self.requests.append((request.full_url, request.data))
        return _FakeResponse()


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return b"ok"


def test_no_channels_configured_is_skipped_gracefully() -> None:
    service = AlertingService(_settings())
    result = service.send_alert("title", "message")
    assert result == {"bark": False, "email": False}


def test_bark_channel_posts_json() -> None:
    spy = _UrlOpenSpy()
    service = AlertingService(
        _settings(alert_bark_webhook="https://bark.example/push"), urlopen=spy
    )
    result = service.send_alert("告警标题", "告警内容")
    assert result["bark"] is True
    assert spy.requests
    url, data = spy.requests[0]
    assert url == "https://bark.example/push"
    payload = json.loads(data.decode("utf-8"))
    assert payload == {"title": "告警标题", "body": "告警内容"}


def test_bark_bare_device_key_uses_official_endpoint() -> None:
    spy = _UrlOpenSpy()
    service = AlertingService(_settings(alert_bark_webhook="ABCD1234"), urlopen=spy)
    service.send_alert("t", "m")
    assert spy.requests[0][0] == "https://api.day.app/ABCD1234"


def test_bark_failure_only_logs(caplog) -> None:
    service = AlertingService(
        _settings(alert_bark_webhook="https://bark.example/push"),
        urlopen=_UrlOpenSpy(fail=True),
    )
    result = service.send_alert("t", "m")
    assert result == {"bark": False, "email": False}


def test_email_channel_reuses_mailer(fake_smtp_class) -> None:
    settings = _settings(alert_email_to="boss@example.com")
    mailer = MailerService(None, settings, smtp_class=FakeSMTP)
    service = AlertingService(settings, mailer=mailer)
    result = service.send_alert("告警标题", "告警内容")
    assert result["email"] is True
    sent = FakeSMTP.instances[0].sent
    assert len(sent) == 1
    assert sent[0]["To"] == "boss@example.com"
    assert "告警标题" in sent[0]["Subject"]
    assert "告警内容" in sent[0].get_content()
    assert "Time (UTC)" in sent[0].get_content()


def test_email_channel_failure_only_logs(fake_smtp_class) -> None:
    FakeSMTP.reset(fail_remaining=10)
    settings = _settings(alert_email_to="boss@example.com")
    mailer = MailerService(None, settings, smtp_class=FakeSMTP)
    service = AlertingService(settings, mailer=mailer)
    result = service.send_alert("t", "m")
    assert result["email"] is False


def test_llm_failure_alerts_after_five_consecutive(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    settings = _settings()
    assert alerting.record_llm_failure(settings, "boom") is False
    assert alerting.record_llm_failure(settings, "boom") is False
    assert alerting.record_llm_failure(settings, "boom") is False
    assert alerting.record_llm_failure(settings, "boom") is False
    assert alerting.record_llm_failure(settings, "boom") is True
    assert len(alerts) == 1
    assert "LLM" in alerts[0][0]
    assert "boom" in alerts[0][1]


def test_llm_success_resets_counter(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    settings = _settings()
    for _ in range(4):
        alerting.record_llm_failure(settings)
    alerting.record_llm_success()
    for _ in range(4):
        assert alerting.record_llm_failure(settings) is False
    assert len(alerts) == 0


def test_llm_failures_outside_window_do_not_alert(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(alerting.time, "time", lambda: clock["now"])
    settings = _settings()
    for i in range(4):
        clock["now"] = i * 400  # each failure 400s apart (> 5min window)
        alerting.record_llm_failure(settings)
    assert len(alerts) == 0
    # 5 failures inside one 5-minute span triggers the alert.
    alerting.reset_failure_counters()
    for i in range(5):
        clock["now"] = i * 60
        alerting.record_llm_failure(settings)
    assert len(alerts) == 1


def test_imap_failure_alerts_after_three_cycles(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    settings = _settings()
    assert alerting.record_imap_failure(settings) is False
    assert alerting.record_imap_failure(settings) is False
    assert alerting.record_imap_failure(settings) is True
    assert len(alerts) == 1
    assert "IMAP" in alerts[0][0]


def test_imap_success_resets_counter(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    settings = _settings()
    alerting.record_imap_failure(settings)
    alerting.record_imap_failure(settings)
    alerting.record_imap_success()
    alerting.record_imap_failure(settings)
    alerting.record_imap_failure(settings)
    assert alerting.record_imap_failure(settings) is True
    assert len(alerts) == 1


def test_chat_with_retry_wires_llm_counter(monkeypatch) -> None:
    alerts = []
    monkeypatch.setattr(
        AlertingService,
        "send_alert",
        lambda self, title, message: alerts.append((title, message)),
    )
    settings = _settings(llm_retries=0)
    client = MockLLMClient(settings)
    # One success clears everything; four failures must not alert.
    client.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    for _ in range(4):
        alerting.record_llm_failure(settings)
    assert len(alerts) == 0

    class BrokenLLM(MockLLMClient):
        def chat(self, *a, **k):
            raise LLMError("provider down")

    broken = BrokenLLM(settings)
    with pytest.raises(LLMError):
        broken.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    assert len(alerts) == 1
    assert "LLM" in alerts[0][0]


def test_send_text_retries_then_succeeds(fake_smtp_class) -> None:
    FakeSMTP.reset(fail_remaining=2)  # two failures, third attempt succeeds
    mailer = MailerService(None, _settings(), smtp_class=FakeSMTP)
    mailer.send_text("boss@example.com", "subject", "body")
    assert len(FakeSMTP.instances) == 3  # one SMTP connection per attempt
    assert len(FakeSMTP.instances[-1].sent) == 1


def test_send_text_raises_after_three_failures(fake_smtp_class) -> None:
    FakeSMTP.reset(fail_remaining=10)
    mailer = MailerService(None, _settings(), smtp_class=FakeSMTP)
    with pytest.raises(SMTPError):
        mailer.send_text("boss@example.com", "subject", "body")
