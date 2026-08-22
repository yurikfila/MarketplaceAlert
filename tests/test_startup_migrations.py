"""Tests for `core/persistence/migrations.py` (automatic PostgreSQL
migrations at app startup - added because Render's Free plan has no
Pre-Deploy Command, see PROJECT_CONTEXT.md decision #20) and for the
startup ordering `main.py`'s `lifespan()` now guarantees around it.

Never connects to a real PostgreSQL server: a `postgresql`-dialected
`Engine` is built the same lazy way `tests/test_database_config.py`
already does (`create_engine()` doesn't actually connect until something
checks a connection out of the pool), and every actual database
interaction below goes through a small fake connection - proving this
module's own logic (dialect gating, lock acquire/release ordering,
fail-fast propagation, no credential leakage) without needing real
Postgres infrastructure in CI. The migrations themselves (the actual SQL
a revision applies) are already covered separately, against real
throwaway SQLite databases, by `tests/test_alembic_migrations.py`; these
tests are about whether *this* function invokes that mechanism safely,
not whether any individual migration is well-formed.
"""

import asyncio
import logging

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.persistence import migrations as migrations_module
from marketplace_alert.core.persistence.database import create_db_engine, resolve_database_url
from marketplace_alert.core.persistence.migrations import (
    _ALEMBIC_INI_PATH,
    _MIGRATION_ADVISORY_LOCK_KEY,
    run_pending_migrations,
)

_FAKE_POSTGRES_PASSWORD = "super-secret-db-password"
_FAKE_POSTGRES_URL = f"postgres://appuser:{_FAKE_POSTGRES_PASSWORD}@db.render.com:5432/marketplacealert_db"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    """Stands in for a real psycopg connection. `lock_state` (a plain
    dict, shared by reference) is how a test simulates the advisory lock
    already being held by "another process" before this connection ever
    tries to acquire it."""

    def __init__(self, lock_state: dict):
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0
        self.closed = False
        self._lock_state = lock_state

    def execute(self, statement, params=None):
        sql = str(statement)
        params = dict(params or {})
        self.executed.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            if self._lock_state.get("held"):
                return _FakeResult(False)
            self._lock_state["held"] = True
            return _FakeResult(True)
        if "pg_advisory_unlock" in sql:
            self._lock_state["held"] = False
            return _FakeResult(True)
        return _FakeResult(None)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _postgres_engine():
    return create_db_engine(resolve_database_url(_FAKE_POSTGRES_URL))


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same convention as tests/test_connector_retry.py and
    tests/test_telegram_provider.py - a lock-wait/timeout test should
    never actually sleep in the test suite."""
    monkeypatch.setattr(migrations_module.time, "sleep", lambda seconds: None)


# --- dialect gating ----------------------------------------------------


def test_sqlite_engine_is_skipped_entirely(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'skip_test.db'}")
    try:

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("alembic upgrade must never run for SQLite")

        monkeypatch.setattr(migrations_module.command, "upgrade", _fail_if_called)
        run_pending_migrations(engine)  # must not raise, must not call upgrade
    finally:
        engine.dispose()


# --- the PostgreSQL path -------------------------------------------------


def test_postgresql_acquires_lock_runs_upgrade_then_releases_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _postgres_engine()
    lock_state: dict = {}
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)

    upgrade_calls = []
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, target: upgrade_calls.append(target))

    try:
        run_pending_migrations(engine)
    finally:
        engine.dispose()

    assert upgrade_calls == ["head"]
    lock_calls = [p for sql, p in fake_connection.executed if "pg_try_advisory_lock" in sql]
    unlock_calls = [p for sql, p in fake_connection.executed if "pg_advisory_unlock" in sql]
    assert len(lock_calls) == 1
    assert len(unlock_calls) == 1
    assert lock_calls[0]["key"] == _MIGRATION_ADVISORY_LOCK_KEY
    assert unlock_calls[0]["key"] == _MIGRATION_ADVISORY_LOCK_KEY
    # released on the exact same connection that acquired it - a different
    # connection calling pg_advisory_unlock would silently do nothing.
    assert fake_connection.closed is True


def test_lock_is_acquired_before_upgrade_is_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _postgres_engine()
    lock_state: dict = {}
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)

    order: list[str] = []
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, target: order.append("upgrade"))
    original_execute = fake_connection.execute

    def tracking_execute(statement, params=None):
        if "pg_try_advisory_lock" in str(statement):
            order.append("lock")
        return original_execute(statement, params)

    fake_connection.execute = tracking_execute

    try:
        run_pending_migrations(engine)
    finally:
        engine.dispose()

    assert order == ["lock", "upgrade"]


def test_migration_failure_propagates_and_still_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _postgres_engine()
    lock_state: dict = {}
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)

    def _boom(cfg, target):
        raise RuntimeError("simulated bad migration")

    monkeypatch.setattr(migrations_module.command, "upgrade", _boom)

    try:
        with pytest.raises(RuntimeError, match="simulated bad migration"):
            run_pending_migrations(engine)
    finally:
        engine.dispose()

    unlock_calls = [p for sql, p in fake_connection.executed if "pg_advisory_unlock" in sql]
    assert len(unlock_calls) == 1  # released even though the migration failed
    assert fake_connection.closed is True  # connection still cleaned up, not leaked


def test_lock_already_held_by_another_process_times_out_without_migrating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates another process already holding the lock for the whole
    attempt - proves this fails fast (a bounded wait) instead of hanging
    forever or racing to run DDL concurrently."""
    engine = _postgres_engine()
    lock_state = {"held": True}  # already held by "someone else"
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)
    monkeypatch.setattr(settings, "migration_lock_timeout_seconds", 0.01)

    upgrade_calls = []
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, target: upgrade_calls.append(target))

    try:
        with pytest.raises(TimeoutError):
            run_pending_migrations(engine)
    finally:
        engine.dispose()

    assert upgrade_calls == []  # never ran migrations without holding the lock
    unlock_calls = [p for sql, p in fake_connection.executed if "pg_advisory_unlock" in sql]
    assert unlock_calls == []  # never acquired it, so never (incorrectly) unlocks either


# --- no credential leakage ----------------------------------------------


def test_never_logs_the_database_password_on_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = _postgres_engine()
    lock_state: dict = {}
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, target: None)

    try:
        with caplog.at_level(logging.DEBUG):
            run_pending_migrations(engine)
    finally:
        engine.dispose()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert _FAKE_POSTGRES_PASSWORD not in log_text


def test_never_logs_the_database_password_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = _postgres_engine()
    lock_state: dict = {}
    fake_connection = _FakeConnection(lock_state)
    monkeypatch.setattr(engine, "connect", lambda: fake_connection)
    monkeypatch.setattr(
        migrations_module.command,
        "upgrade",
        lambda cfg, target: (_ for _ in ()).throw(RuntimeError("simulated migration failure")),
    )

    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(RuntimeError):
                run_pending_migrations(engine)
    finally:
        engine.dispose()

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert _FAKE_POSTGRES_PASSWORD not in log_text


def test_alembic_config_points_at_the_projects_alembic_ini() -> None:
    assert _ALEMBIC_INI_PATH.name == "alembic.ini"
    assert _ALEMBIC_INI_PATH.is_file()


# --- lifespan ordering ----------------------------------------------------
#
# tests/test_lifespan_isolation.py proves TestClient never triggers the
# real lifespan() at all. These two tests deliberately invoke the real
# lifespan() directly (bypassing TestClient/conftest.py's autouse no-op
# fixture on purpose) to prove its *internal* ordering - every other call
# inside it is monkeypatched to a no-op recorder, so nothing here ever
# touches a real database, starts a real thread, or makes a real network
# call.


def test_lifespan_runs_migrations_before_init_db_before_legacy_migration_before_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import marketplace_alert.main as main_module

    order: list[str] = []
    monkeypatch.setattr(main_module, "run_pending_migrations", lambda *a, **k: order.append("migrations"))
    monkeypatch.setattr(main_module, "init_db", lambda *a, **k: order.append("init_db"))
    monkeypatch.setattr(
        main_module, "migrate_legacy_marketplace_column", lambda *a, **k: order.append("legacy_migration")
    )
    monkeypatch.setattr(main_module._background_scanner, "start", lambda *a, **k: order.append("scanner_start"))
    monkeypatch.setattr(main_module._background_scanner, "stop", lambda *a, **k: order.append("scanner_stop"))

    async def _run() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(_run())

    assert order == ["migrations", "init_db", "legacy_migration", "scanner_start", "scanner_stop"]


def test_lifespan_migration_failure_prevents_the_rest_of_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-fast, proven at the lifespan level: if run_pending_migrations
    raises, init_db/the legacy migration/the scanner must never run, and
    the exception must propagate out of lifespan() - which is what makes
    FastAPI/uvicorn treat this as a failed startup (so Render keeps
    serving the previous successful deploy instead of going live with a
    schema/code mismatch)."""
    import marketplace_alert.main as main_module

    order: list[str] = []

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(main_module, "run_pending_migrations", _boom)
    monkeypatch.setattr(main_module, "init_db", lambda *a, **k: order.append("init_db"))
    monkeypatch.setattr(
        main_module, "migrate_legacy_marketplace_column", lambda *a, **k: order.append("legacy_migration")
    )
    monkeypatch.setattr(main_module._background_scanner, "start", lambda *a, **k: order.append("scanner_start"))
    monkeypatch.setattr(main_module._background_scanner, "stop", lambda *a, **k: order.append("scanner_stop"))

    async def _run() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        asyncio.run(_run())

    assert order == []
