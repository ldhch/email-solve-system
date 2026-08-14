"""Shared pytest fixtures: in-memory SQLite + mock LLM + fake SMTP/IMAP."""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.base import Base
from app.models.system_state import SystemState


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        llm_provider="mock",
        email_username="bot@example.com",
        email_password="test-password",
        agent_service_token="test-token",
        smtp_rate_limit_per_hour=0,
        attachment_dir="/tmp/shouhou-agent-test-attachments",
    )


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
    """Minimal in-memory IMAP: SEARCH UNSEEN / FETCH / STORE +FLAGS."""

    def __init__(self, items: list[tuple[str, bytes]]) -> None:
        self.items = dict(items)
        self.seen: list[str] = []

    def uid(self, *args) -> tuple[str, list]:
        command = args[0]
        if command == "SEARCH":
            uids = [u for u in self.items if u not in self.seen]
            return ("OK", [b" ".join(u.encode() for u in uids)])
        if command == "FETCH":
            uid = args[1]
            raw = self.items[uid]
            return ("OK", [(f"1 (RFC822 {{{len(raw)}}}".encode(), raw)])
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
