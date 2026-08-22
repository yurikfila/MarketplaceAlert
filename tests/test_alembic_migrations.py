"""Tests for the Alembic baseline migration (`alembic/versions/*_baseline_schema.py`).

Runs entirely against a throwaway temp-file SQLite database - never a real
PostgreSQL server (not required for the normal test suite) and never the
developer's real local `marketplace_alert.db`. Proves the migration is
compatible with SQLite (as far as practical for local testing) and that it
produces exactly the same tables the app's own `Base.metadata` defines -
i.e. the baseline genuinely represents the current model schema.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.database import Base, create_db_engine

# Importing these registers every table on Base.metadata, same as alembic/env.py does.
import marketplace_alert.core.persistence.models  # noqa: F401
import marketplace_alert.core.saved_searches.models  # noqa: F401

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config_for(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    """alembic/env.py resolves its URL from `settings.database_url` (the
    same place the app does) rather than alembic.ini - so pointing a test
    at a temp database means monkeypatching settings, not the ini file."""
    monkeypatch.setattr(settings, "database_url", database_url)
    return Config(str(_PROJECT_ROOT / "alembic.ini"))


def test_upgrade_head_creates_every_expected_table(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alembic_upgrade_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for table_name in ("discovered_listings", "saved_searches", "saved_search_marketplaces"):
        assert table_name in tables
    assert "alembic_version" in tables


def test_upgrade_head_creates_the_first_discovered_at_index(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/v1/listings` orders by `first_discovered_at` on every
    page load (used by the mobile app's Listings screen - the web
    dashboard has no listings view) - found unindexed during a
    production-hardening audit, fixed by a dedicated migration. Confirms
    it's actually applied on a fresh database, not just present in the
    model."""
    db_path = tmp_path / "alembic_index_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        index_names = {idx["name"] for idx in inspect(engine).get_indexes("discovered_listings")}
    finally:
        engine.dispose()

    assert "ix_discovered_listings_first_discovered_at" in index_names


def test_upgrade_head_matches_base_metadata_tables(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The baseline migration must create exactly the tables the current
    SQLAlchemy models define - no more, no less (aside from Alembic's own
    bookkeeping table) - proving it truly represents "the current schema"."""
    db_path = tmp_path / "alembic_metadata_match_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables_from_migration = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert tables_from_migration == set(Base.metadata.tables.keys())


def test_saved_searches_table_has_no_legacy_marketplace_column(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline represents *today's* schema - post multi-marketplace -
    so `saved_searches` must not have the old single `marketplace` column;
    marketplace selection lives only in `saved_search_marketplaces`."""
    db_path = tmp_path / "alembic_no_legacy_column_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        columns = {col["name"] for col in inspect(engine).get_columns("saved_searches")}
    finally:
        engine.dispose()

    assert "marketplace" not in columns


def test_upgrade_head_is_idempotent_on_rerun(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running `upgrade head` again once already at head must be a clean
    no-op (Alembic itself guarantees this - a regression here would mean
    our env.py/config broke that guarantee)."""
    db_path = tmp_path / "alembic_idempotent_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise or duplicate anything

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert tables.count("discovered_listings") == 1


def test_upgrade_then_downgrade_then_upgrade_round_trips_cleanly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generated downgrade() must actually reverse upgrade() cleanly -
    confirms the migration is well-formed, even though downgrade is never
    invoked automatically by the app itself."""
    db_path = tmp_path / "alembic_round_trip_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables_after_downgrade = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert tables_after_downgrade == set()

    command.upgrade(cfg, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables_after_reupgrade = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert tables_after_reupgrade == set(Base.metadata.tables.keys())


def test_upgrading_a_database_that_already_has_the_tables_fails_without_data_loss(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that already has the current schema via `create_all()`
    (e.g. a pre-existing local SQLite dev database, never previously
    stamped) is NOT what `upgrade head` is for - it correctly refuses
    (table already exists) rather than silently doing something
    destructive. `alembic stamp head` is the documented tool for that case
    (see README.md "Database") - this test documents/guards the distinction."""
    db_path = tmp_path / "alembic_pre_existing_test.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()

    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)
    with pytest.raises(Exception, match="already exists"):
        command.upgrade(cfg, "head")

    # Nothing was dropped or corrupted by the failed attempt.
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"discovered_listings", "saved_searches", "saved_search_marketplaces"} <= tables


def test_stamp_head_on_a_pre_existing_database_succeeds_without_altering_tables(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correct way to adopt Alembic for a database that already has the
    current schema (e.g. the developer's real local SQLite file): stamp,
    not upgrade - records the revision as applied without executing DDL."""
    db_path = tmp_path / "alembic_stamp_test.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()

    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)
    command.stamp(cfg, "head")  # must not raise

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"discovered_listings", "saved_searches", "saved_search_marketplaces", "alembic_version"} <= tables
