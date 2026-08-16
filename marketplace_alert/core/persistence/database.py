"""SQLAlchemy engine, session, and schema setup.

**Database selection** (`resolve_database_url`): if `DATABASE_URL` is set
(e.g. in production, pointed at Render's managed PostgreSQL), it's used -
normalized for our driver, see `normalize_database_url`. If it's unset,
local development keeps using the existing SQLite file
(`marketplace_alert.db` in the project root) - unchanged from before this
module supported PostgreSQL. Nothing above this module needs to know which
backend is active; everything is built from the resolved URL string alone.

**Schema strategy** (`init_db`): SQLite (local dev/tests) keeps its
original zero-setup bootstrap - `Base.metadata.create_all()`, safe to call
on every startup since it only ever adds missing tables, never drops or
alters one. PostgreSQL (production) does NOT get this automatic
create/alter-on-startup treatment - its schema is managed by Alembic
migrations instead (see `alembic/`, and the "Database" section in
README.md/ARCHITECTURE.md), which is the safer strategy for a real,
persistent production database: schema changes are then reviewed,
versioned, and applied deliberately (e.g. via a Render Pre-Deploy Command),
never implicitly re-derived from whatever the current model code happens
to say at whatever moment the app happens to start.
"""

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from marketplace_alert.config import settings

logger = logging.getLogger(__name__)

# The pre-existing local development default - unchanged. Used whenever
# DATABASE_URL is not set, so `pytest`/local `uvicorn` runs keep working
# exactly as before PostgreSQL support was added.
_DEFAULT_SQLITE_URL = "sqlite:///./marketplace_alert.db"


class Base(DeclarativeBase):
    """Declarative base for all persistence (SQLAlchemy) models."""


def normalize_database_url(database_url: str) -> str:
    """Normalize a raw `DATABASE_URL` into one SQLAlchemy/our driver accepts.

    Render (like Heroku before it) issues PostgreSQL URLs starting with
    either `postgres://` or `postgresql://` - SQLAlchemy 2.x's `postgresql`
    dialect defaults to the `psycopg2` DBAPI unless a driver is named
    explicitly, so both forms are rewritten to `postgresql+psycopg://` to
    select the modern, actively-maintained psycopg (3) driver this project
    depends on (`pyproject.toml`). SQLite URLs (and anything else) pass
    through unchanged. This is the one, central place this normalization
    happens - nothing else should need to know Render's URL quirks.
    """
    scheme, separator, rest = database_url.partition("://")
    if not separator:
        return database_url
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme == "postgresql":
        scheme = "postgresql+psycopg"
    return f"{scheme}://{rest}"


def resolve_database_url(configured_url: str | None) -> str:
    """Decide which database URL to actually use - the one place this
    "DATABASE_URL set -> PostgreSQL, unset -> local SQLite" choice is made.

    Never logs `configured_url` - it may contain a real password.
    """
    if not configured_url:
        return _DEFAULT_SQLITE_URL
    return normalize_database_url(configured_url)


def create_db_engine(database_url: str) -> Engine:
    """Build a SQLAlchemy engine for the given (already-resolved) URL.

    SQLite needs ``check_same_thread=False`` to be used safely across
    FastAPI's request-handling threads; other backends don't need it and
    ignore it. ``pool_pre_ping=True`` is set for every backend - cheap for
    SQLite, and important for PostgreSQL specifically: a managed Postgres
    instance (Render's included) can close idle connections server-side,
    and pre-ping detects and transparently replaces a dead pooled
    connection instead of a request failing with it. Never logs
    ``database_url`` - it may contain a real password.
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine = create_db_engine(resolve_database_url(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    """Create any tables that don't already exist - SQLite only.

    Safe to call repeatedly (`create_all()` never drops or alters an
    existing table). For any other backend (PostgreSQL in production),
    this intentionally no-ops - schema there is managed by Alembic
    migrations instead, run explicitly and deliberately, never implicitly
    re-derived from model code at app startup. See this module's docstring.
    """
    target = bind or engine
    if target.dialect.name != "sqlite":
        logger.info(
            "Skipping automatic table creation for %s - schema is managed by Alembic migrations",
            target.dialect.name,
        )
        return
    Base.metadata.create_all(bind=target)


def get_db_session() -> Iterator[Session]:
    """FastAPI dependency: yields a request-scoped session, committing on success.

    A new `Session` is created per call (never one shared/reused globally
    across requests or threads - the `Engine`/`SessionLocal` factory above
    are what's safely shared, per SQLAlchemy's own thread-safety model) and
    always closed in `finally`, regardless of the backend. Overridden in
    tests (via ``app.dependency_overrides``) to point at an isolated
    temporary database instead of the real one.
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
