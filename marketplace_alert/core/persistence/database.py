"""SQLAlchemy engine, session, and schema setup.

Local development uses SQLite via ``DATABASE_URL`` (see ``config.py`` and
``.env.example``). Everything here is built from that URL string alone, so
switching to PostgreSQL later means changing ``DATABASE_URL`` - not this
module or anything above it.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from marketplace_alert.config import settings


class Base(DeclarativeBase):
    """Declarative base for all persistence (SQLAlchemy) models."""


def create_db_engine(database_url: str) -> Engine:
    """Build a SQLAlchemy engine for the given URL.

    SQLite needs ``check_same_thread=False`` to be used safely across
    FastAPI's request-handling threads; other backends don't need it and
    ignore it.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    """Create any tables that don't already exist. Safe to call repeatedly."""
    Base.metadata.create_all(bind=bind or engine)


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency: yields a request-scoped session, committing on success.

    Overridden in tests (via ``app.dependency_overrides``) to point at an
    isolated temporary database instead of the real one.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
