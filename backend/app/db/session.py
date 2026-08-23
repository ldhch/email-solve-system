"""Engine / session factory, schema creation and seed data.

No migration framework (red line): `create_all` on startup + a seed script
that guarantees the `system_state` singleton row exists.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.models.system_state import SystemState
from app.models.user import User
from app.services.audit import utcnow


def make_engine(database_url: str) -> Engine:
    """Create a SQLite engine with WAL + busy_timeout (file DBs only)."""

    connect_args = {"check_same_thread": False}
    if not database_url.startswith("sqlite"):
        raise ValueError(f"Only SQLite is supported in this project, got: {database_url}")

    engine = create_engine(
        database_url,
        connect_args={**connect_args, "timeout": 5},
    )

    if database_url != "sqlite:///:memory:":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, created lazily from settings."""

    global _engine, _session_factory
    if _engine is None:
        settings = settings or get_settings()
        _engine = make_engine(settings.database_url)
        _session_factory = make_session_factory(_engine)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _session_factory is not None
    return _session_factory


def ensure_data_dirs(settings: Settings | None = None) -> None:
    """Create data/attachments and data/exports directories."""

    settings = settings or get_settings()
    for path in (Path(settings.attachment_dir), Path(settings.attachment_dir).parent / "exports"):
        path.mkdir(parents=True, exist_ok=True)


def seed(settings: Settings | None = None) -> None:
    """Insert the `system_state` singleton + owner user if missing.

    The owner is created only when it does not exist yet; the stored hash is
    never overwritten on restart so a password changed via `create-owner`
    survives later `init-db` calls.
    """

    factory = get_session_factory(settings)
    with factory() as db:
        if db.get(SystemState, 1) is None:
            db.add(SystemState(id=1, ai_paused=False))
        if settings.owner_password:
            from sqlalchemy import select

            existing = db.execute(
                select(User).where(User.username == settings.owner_username)
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    User(
                        username=settings.owner_username,
                        password_hash=hash_password(settings.owner_password),
                        role="owner",
                        created_at=utcnow(),
                    )
                )
        # Quick reply templates: seed the defaults once, never overwrite edits.
        from app.models.reply_template import (
            DEFAULT_REPLY_TEMPLATES,
            ReplyTemplate,
        )
        from sqlalchemy import func, select as _select

        if db.scalar(_select(func.count(ReplyTemplate.id))) == 0:
            for order, (name, content) in enumerate(DEFAULT_REPLY_TEMPLATES):
                db.add(
                    ReplyTemplate(
                        name=name,
                        content=content,
                        sort_order=order,
                        created_at=utcnow(),
                    )
                )
        db.commit()


def _ensure_email_is_read_column(engine: Engine) -> None:
    """Add ``emails.is_read`` to DBs created before the column existed.

    ``create_all`` only creates missing tables, never missing columns; a
    guarded ALTER TABLE backfills the new column on existing installs.
    """

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))}
        if "is_read" not in cols:
            conn.execute(
                text("ALTER TABLE emails ADD COLUMN is_read BOOLEAN NOT NULL DEFAULT 0")
            )


def _ensure_email_pending_after_pause_column(engine: Engine) -> None:
    """Add ``emails.pending_after_pause`` to DBs created before the column existed."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))}
        if "pending_after_pause" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE emails "
                    "ADD COLUMN pending_after_pause BOOLEAN NOT NULL DEFAULT 0"
                )
            )


def _ensure_reply_source_column(engine: Engine) -> None:
    """Add ``replies.source`` to DBs created before the column existed."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(replies)"))}
        if "source" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE replies "
                    "ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'system'"
                )
            )


def _ensure_email_content_cn_column(engine: Engine) -> None:
    """Add ``emails.content_cn`` to DBs created before the column existed."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))}
        if "content_cn" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN content_cn TEXT"))


def _ensure_email_is_ad_column(engine: Engine) -> None:
    """Add ``emails.is_ad`` to DBs created before the column existed."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))}
        if "is_ad" not in cols:
            conn.execute(
                text("ALTER TABLE emails ADD COLUMN is_ad BOOLEAN NOT NULL DEFAULT 0")
            )


def _ensure_reply_low_confidence_column(engine: Engine) -> None:
    """Add ``replies.low_confidence`` to DBs created before the column existed."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(replies)"))}
        if "low_confidence" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE replies "
                    "ADD COLUMN low_confidence BOOLEAN NOT NULL DEFAULT 0"
                )
            )


def _ensure_email_imap_uid_columns(engine: Engine) -> None:
    """Add ``emails.imap_uid`` / ``emails.imap_uidvalidity`` to existing DBs."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(emails)"))}
        if "imap_uid" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN imap_uid VARCHAR(64)"))
        if "imap_uidvalidity" not in cols:
            conn.execute(text("ALTER TABLE emails ADD COLUMN imap_uidvalidity VARCHAR(64)"))


def _ensure_system_state_test_columns(engine: Engine) -> None:
    """Add ``system_state.test_mode`` / ``test_whitelist`` to existing DBs."""

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(system_state)"))}
        if "test_mode" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE system_state "
                    "ADD COLUMN test_mode BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        if "test_whitelist" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE system_state "
                    "ADD COLUMN test_whitelist VARCHAR(2048) NOT NULL DEFAULT ''"
                )
            )


def init_db(settings: Settings | None = None) -> None:
    """Create all tables (create_all) and seed defaults. No migrations."""

    settings = settings or get_settings()
    ensure_data_dirs(settings)
    # Importing the models package registers every model on Base.metadata.
    from app import models  # noqa: F401

    engine = get_engine(settings)
    Base.metadata.create_all(bind=engine)
    _ensure_email_is_read_column(engine)
    _ensure_email_pending_after_pause_column(engine)
    _ensure_reply_source_column(engine)
    _ensure_email_content_cn_column(engine)
    _ensure_email_is_ad_column(engine)
    _ensure_email_imap_uid_columns(engine)
    _ensure_reply_low_confidence_column(engine)
    _ensure_system_state_test_columns(engine)
    seed(settings)


async def get_db():
    """FastAPI dependency: yield a session and always close it.

    Async generator on purpose: sync dependencies run in anyio's thread pool,
    which is unavailable in some sandboxed environments.
    """

    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()
