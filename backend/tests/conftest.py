"""Shared pytest fixtures: in-memory SQLite + mock LLM + fake SMTP/IMAP."""

from __future__ import annotations

import re
from datetime import date, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.models.system_state import SystemState

_MID_RE = re.compile(rb"Message-ID:\s*<([^>\r\n]+)>", re.IGNORECASE)


def _mid_from_raw(raw: bytes) -> str:
    """Bare Message-ID from a raw email (FakeIMAP header-FETCH helper)."""

    match = _MID_RE.search(raw)
    return match.group(1).decode("utf-8") if match else "unknown@local"


_IMAP_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _recent_internaldate(days_ago: int = 2) -> str:
    """An INTERNALDATE string safely inside the poll's fetch window."""
    d = date.today() - timedelta(days=days_ago)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year} 10:00:00 +0000"


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_provider="mock",
        email_username="bot@example.com",
        email_password="test-password",
        deepseek_api_key="",
        openai_api_key="",
        encryption_key="",
        secret_key="test-secret-key-for-jwt-0123456789abcdef0123456789",
        owner_username="boss",
        owner_password="test-owner-password",
        agent_service_token="test-token",
        smtp_rate_limit_per_hour=0,
        compensation_max_usd=20.0,
        attachment_dir="/tmp/shouhou-agent-test-attachments",
        return_policy_text="Return address: 123 Test Street, return within 30 days.",
        # Disable IMAP sent-copy in unit tests by default (no real mailbox);
        # the append path is covered by a dedicated test with a fake IMAP.
        imap_sent_folder="",
    )


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """The auth rate limiter is module-global; isolate it per test."""

    import app.api.auth as auth_module

    auth_module._ip_attempts.clear()
    auth_module._account_failures.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_alert_failure_counters():
    """Clear the process-local LLM/IMAP failure counters before each test."""

    from app.services import alerting
    from app.services import scheduler as scheduler_module

    alerting.reset_failure_counters()
    scheduler_module._alerted_sla_ticket_ids.clear()
    scheduler_module._alerted_retention_reply_ids.clear()
    yield


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app import models  # noqa: F401 - register all models

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(SystemState.__table__.insert().values(id=1, ai_paused=False))
    return engine


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


class FakeSMTP:
    """Records outbound messages; can simulate N consecutive failures."""

    instances: list["FakeSMTP"] = []
    fail_remaining = 0

    @classmethod
    def reset(cls, fail_remaining: int = 0) -> None:
        cls.instances = []
        cls.fail_remaining = fail_remaining

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def login(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def send_message(self, msg: EmailMessage) -> None:
        if FakeSMTP.fail_remaining > 0:
            FakeSMTP.fail_remaining -= 1
            raise ConnectionError("simulated SMTP failure")
        self.sent.append(msg)

    def quit(self) -> None:
        pass


class FakeIMAP:
    """Minimal in-memory IMAP: SEARCH (ALL/UNSEEN) / FETCH / STORE +FLAGS.

    ``fetched`` records which UIDs were body-fetched so tests can assert the
    poll skips already-processed mail without relying on server \\Seen flags.
    """

    UIDVALIDITY = "12345"

    def __init__(self, items: list[tuple[str, bytes]]) -> None:
        self.items = dict(items)
        self.seen: list[str] = []
        self.fetched: list[str] = []
        self.internaldate = _recent_internaldate()

    def status(self, mailbox: str, names: str) -> tuple[str, list]:
        return ("OK", [f"{mailbox} (UIDVALIDITY {self.UIDVALIDITY})".encode()])

    def uid(self, *args) -> tuple[str, list]:
        command = args[0]
        if command == "SEARCH":
            criteria = " ".join(str(a) for a in args[1:] if a is not None).upper()
            uids = list(self.items)
            if "UNSEEN" in criteria:
                uids = [u for u in uids if u not in self.seen]
            return ("OK", [b" ".join(u.encode() for u in uids)])
        if command == "FETCH":
            # uid-set / 1:* fetch; marker carries UID so the batched parser can
            # pair each response back to its message. Header-only FETCH (used by
            # the UID backfill) returns just the Message-ID literal, not a body.
            uid_spec = str(args[1])
            item_spec = " ".join(str(a) for a in args[2:] if a is not None)
            if uid_spec == "1:*":
                fetch_uids = list(self.items)
            elif ":" in uid_spec and uid_spec.replace(":", "").isdigit():
                low, high = uid_spec.split(":", 1)
                fetch_uids = [
                    u for u in self.items if int(low) <= int(u) <= int(high)
                ]
            else:
                fetch_uids = uid_spec.split(",")
            header_only = "HEADER.FIELDS" in item_spec
            responses = []
            for uid in fetch_uids:
                raw = self.items[uid]
                if header_only:
                    literal = f"Message-ID: <{_mid_from_raw(raw)}>\r\n".encode()
                else:
                    self.fetched.append(uid)
                    literal = raw
                marker = (
                    f'{uid} (INTERNALDATE "{self.internaldate}" '
                    f"UID {uid} BODY[] {{{len(literal)}}}"
                )
                responses.append((marker.encode(), literal))
            return ("OK", responses)
        if command == "STORE":
            self.seen.append(args[1])
            return ("OK", [None])
        raise AssertionError(f"unexpected IMAP command: {args}")


@pytest.fixture()
def fake_smtp_class():
    FakeSMTP.reset()
    return FakeSMTP


@pytest.fixture()
def fake_imap():
    def _make(items: list[tuple[str, bytes]]) -> FakeIMAP:
        return FakeIMAP(items)

    return _make


class FakeIMAPAppend:
    """In-memory IMAP that records APPENDs made by MailerService._append_sent_copy."""

    instances: list["FakeIMAPAppend"] = []
    fail_append = False

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.fail_append = False

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.append_calls: list[tuple[str, bytes]] = []
        FakeIMAPAppend.instances.append(self)

    def login(self, username: str, password: str) -> None:
        self.username = username

    def append(self, folder: str, flags: str, date, raw: bytes) -> tuple[str, list]:
        if FakeIMAPAppend.fail_append:
            raise ConnectionError("simulated IMAP APPEND failure")
        self.append_calls.append((folder, raw))
        return ("OK", [None])

    def logout(self) -> None:
        pass


def make_raw_email(
    subject: str = "Where is my order?",
    body: str = "Hello, I ordered last week but have not received anything.",
    message_id: str = "<mail-1@example.com>",
    from_email: str = "customer@example.com",
    from_name: str = "John Smith",
    in_reply_to: str | None = None,
    references: str | None = None,
    date: str = "Tue, 12 Aug 2026 10:00:00 +0000",
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = "bot@example.com"
    msg["Message-ID"] = message_id
    msg["Date"] = date
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return bytes(msg)
