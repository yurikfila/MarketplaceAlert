"""Tests for PostgreSQL/SQLite database selection
(`marketplace_alert.core.persistence.database`).

None of these connect to a real PostgreSQL server - `create_engine()`
builds an `Engine` object lazily (it only actually connects when a
connection is checked out of the pool), so asserting on the resolved URL/
dialect/driver is enough to prove "a PostgreSQL DATABASE_URL selects
PostgreSQL" without needing real Postgres infrastructure in CI.
"""

import logging

import pytest
from sqlalchemy.orm import sessionmaker

from marketplace_alert.core.persistence.database import (
    Base,
    _DEFAULT_SQLITE_URL,
    create_db_engine,
    get_db_session,
    init_db,
    normalize_database_url,
    resolve_database_url,
)

_FAKE_POSTGRES_PASSWORD = "super-secret-db-password"
_FAKE_POSTGRES_URL = f"postgres://appuser:{_FAKE_POSTGRES_PASSWORD}@db.render.com:5432/marketplacealert_db"


# --- DATABASE_URL absent -> SQLite -----------------------------------------


def test_resolve_database_url_falls_back_to_sqlite_when_none() -> None:
    assert resolve_database_url(None) == _DEFAULT_SQLITE_URL


def test_resolve_database_url_falls_back_to_sqlite_when_empty_string() -> None:
    assert resolve_database_url("") == _DEFAULT_SQLITE_URL


def test_default_sqlite_url_is_the_pre_existing_local_default() -> None:
    """Guards against accidentally changing the local dev default as part
    of adding PostgreSQL support - it must stay exactly what it was."""
    assert _DEFAULT_SQLITE_URL == "sqlite:///./marketplace_alert.db"


# --- DATABASE_URL present -> PostgreSQL, normalized ------------------------


def test_resolve_database_url_selects_postgres_for_postgres_scheme() -> None:
    resolved = resolve_database_url("postgres://user:pass@host:5432/dbname")
    assert resolved.startswith("postgresql+psycopg://")
    assert resolved == "postgresql+psycopg://user:pass@host:5432/dbname"


def test_resolve_database_url_selects_postgres_for_postgresql_scheme() -> None:
    resolved = resolve_database_url("postgresql://user:pass@host:5432/dbname")
    assert resolved == "postgresql+psycopg://user:pass@host:5432/dbname"


def test_normalize_database_url_is_idempotent_if_driver_already_specified() -> None:
    already_explicit = "postgresql+psycopg://user:pass@host:5432/dbname"
    assert normalize_database_url(already_explicit) == already_explicit


def test_normalize_database_url_leaves_sqlite_untouched() -> None:
    sqlite_url = "sqlite:///./marketplace_alert.db"
    assert normalize_database_url(sqlite_url) == sqlite_url


def test_normalize_database_url_leaves_urls_without_a_scheme_untouched() -> None:
    # Defensive: a malformed value should pass through rather than crash -
    # create_engine() downstream is what will raise a clear error.
    assert normalize_database_url("not-a-url") == "not-a-url"


# --- engine construction: correct dialect/driver, no real connection -------


def test_create_db_engine_selects_sqlite_dialect_for_sqlite_url() -> None:
    engine = create_db_engine(resolve_database_url(None))
    assert engine.dialect.name == "sqlite"
    engine.dispose()


def test_create_db_engine_selects_postgres_dialect_for_postgres_url() -> None:
    engine = create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))
    assert engine.dialect.name == "postgresql"
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()


def test_create_db_engine_sqlite_gets_check_same_thread_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    import sqlalchemy

    real_create_engine = sqlalchemy.create_engine

    def spy_create_engine(url, **kwargs):
        captured["connect_args"] = kwargs.get("connect_args")
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr("marketplace_alert.core.persistence.database.create_engine", spy_create_engine)

    engine = create_db_engine(resolve_database_url(None))
    assert captured["connect_args"] == {"check_same_thread": False}
    engine.dispose()


def test_create_db_engine_postgres_does_not_get_check_same_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    import sqlalchemy

    real_create_engine = sqlalchemy.create_engine

    def spy_create_engine(url, **kwargs):
        captured["connect_args"] = kwargs.get("connect_args")
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr("marketplace_alert.core.persistence.database.create_engine", spy_create_engine)

    engine = create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))
    assert captured["connect_args"] == {}
    engine.dispose()


def test_create_db_engine_uses_pool_pre_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    import sqlalchemy

    real_create_engine = sqlalchemy.create_engine

    def spy_create_engine(url, **kwargs):
        captured["pool_pre_ping"] = kwargs.get("pool_pre_ping")
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr("marketplace_alert.core.persistence.database.create_engine", spy_create_engine)

    engine = create_db_engine(resolve_database_url(None))
    assert captured["pool_pre_ping"] is True
    engine.dispose()


# --- secrets are never logged ----------------------------------------------


def test_resolving_and_creating_engine_never_logs_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        resolved = resolve_database_url(_FAKE_POSTGRES_URL)
        engine = create_db_engine(resolved)
    engine.dispose()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert _FAKE_POSTGRES_PASSWORD not in log_text


def test_engine_repr_and_str_never_expose_the_raw_password() -> None:
    """SQLAlchemy's own Engine/URL __repr__ masks the password by default -
    confirm we haven't done anything (e.g. custom logging) that bypasses
    that masking anywhere in our own code path."""
    engine = create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))
    try:
        assert _FAKE_POSTGRES_PASSWORD not in str(engine.url)
        assert _FAKE_POSTGRES_PASSWORD not in repr(engine.url)
    finally:
        engine.dispose()


# --- init_db(): SQLite bootstraps itself, PostgreSQL relies on Alembic -----


def test_init_db_creates_tables_for_sqlite(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'init_test.db'}")
    try:
        init_db(bind=engine)
        from sqlalchemy import inspect

        assert "discovered_listings" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_init_db_skips_create_all_for_non_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgreSQL schema is managed by Alembic, not an implicit create_all()
    at every startup - init_db() must no-op rather than attempt it."""
    engine = create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))
    try:
        called = False

        def fake_create_all(bind):
            nonlocal called
            called = True

        monkeypatch.setattr(Base.metadata, "create_all", fake_create_all)
        init_db(bind=engine)

        assert called is False
    finally:
        engine.dispose()


def test_init_db_skip_message_does_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))
    try:
        with caplog.at_level(logging.INFO):
            init_db(bind=engine)
        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert _FAKE_POSTGRES_PASSWORD not in log_text
    finally:
        engine.dispose()


# --- session lifecycle -------------------------------------------------


def test_get_db_session_yields_a_fresh_session_each_call(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never one Session reused globally across calls/threads - each call to
    the dependency must produce its own instance."""
    import marketplace_alert.core.persistence.database as database_module

    engine = create_db_engine(f"sqlite:///{tmp_path / 'session_test.db'}")
    Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", test_session_local)

    gen_a = database_module.get_db_session()
    session_a = next(gen_a)
    gen_b = database_module.get_db_session()
    session_b = next(gen_b)

    try:
        assert session_a is not session_b
    finally:
        for gen in (gen_a, gen_b):
            try:
                next(gen)
            except StopIteration:
                pass
        engine.dispose()


def test_get_db_session_closes_the_session_after_use(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import marketplace_alert.core.persistence.database as database_module

    engine = create_db_engine(f"sqlite:///{tmp_path / 'session_close_test.db'}")
    Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", test_session_local)

    closed_sessions = []
    gen = database_module.get_db_session()
    session = next(gen)
    original_close = session.close

    def tracking_close():
        closed_sessions.append(session)
        original_close()

    monkeypatch.setattr(session, "close", tracking_close)

    try:
        next(gen)
    except StopIteration:
        pass

    assert session in closed_sessions
    engine.dispose()


def test_get_db_session_rolls_back_on_exception(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import marketplace_alert.core.persistence.database as database_module

    engine = create_db_engine(f"sqlite:///{tmp_path / 'session_rollback_test.db'}")
    Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "SessionLocal", test_session_local)

    gen = database_module.get_db_session()
    session = next(gen)
    rolled_back = []
    monkeypatch.setattr(session, "rollback", lambda: rolled_back.append(True))
    monkeypatch.setattr(session, "close", lambda: None)

    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("simulated failure mid-request"))

    assert rolled_back == [True]
    engine.dispose()
