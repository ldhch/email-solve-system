"""Engine / session factory, schema creation and seed data.

No migration framework (red line): `create_all` on startup + a seed script
that guarantees the `system_state` singleton row exists.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.base import Base
from app.models.system_state import SystemState


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
    """Insert the single `system_state` row if missing (id must be 1)."""

    factory = get_session_factory(settings)
    with factory() as db:
        if db.get(SystemState, 1) is None:
            db.add(SystemState(id=1, ai_paused=False))
            db.commit()


def init_db(settings: Settings | None = None) -> None:
    """Create all tables (create_all) and seed defaults. No migrations."""

    settings = settings or get_settings()
    ensure_data_dirs(settings)
    # Importing the models package registers every model on Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine(settings))
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
