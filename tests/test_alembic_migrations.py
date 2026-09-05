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
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.database import Base, create_db_engine

# Importing these registers every table on Base.metadata, same as alembic/env.py does.
import marketplace_alert.core.auth.models  # noqa: F401
import marketplace_alert.core.notifications.models  # noqa: F401
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


def test_upgrade_head_adds_listing_product_fields_and_saved_search_attribution(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Part of the Listings product-experience pass: `discovered_listings`
    gained price/currency/location/seller/condition/image_url/
    source_created_at (all nullable) plus a nullable, indexed
    `discovered_by_saved_search_id` foreign key to `saved_searches.id`
    (`ON DELETE SET NULL`). Confirms every one of these is actually
    applied on a fresh database, not just present in the model - and that
    the foreign key's `ondelete` behavior is really `SET NULL`, not the
    database default (which would be more destructive)."""
    db_path = tmp_path / "alembic_listing_fields_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("discovered_listings")}
        for column_name in (
            "price",
            "currency",
            "location",
            "seller",
            "condition",
            "image_url",
            "source_created_at",
            "discovered_by_saved_search_id",
        ):
            assert column_name in columns

        index_names = {idx["name"] for idx in inspector.get_indexes("discovered_listings")}
        assert "ix_discovered_listings_discovered_by_saved_search_id" in index_names

        foreign_keys = inspector.get_foreign_keys("discovered_listings")
        assert len(foreign_keys) == 1
        fk = foreign_keys[0]
        assert fk["referred_table"] == "saved_searches"
        assert fk["constrained_columns"] == ["discovered_by_saved_search_id"]
        assert fk["options"].get("ondelete") == "SET NULL"
    finally:
        engine.dispose()


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


# --- Authentication tables (Phase 1 of the approved authentication design) -


def test_upgrade_head_creates_the_authentication_tables(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alembic_auth_tables_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    for table_name in ("users", "refresh_tokens", "password_reset_tokens"):
        assert table_name in tables


def test_users_table_columns_and_nullability(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alembic_users_columns_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        columns = {col["name"]: col for col in inspect(engine).get_columns("users")}
    finally:
        engine.dispose()

    for column_name in ("id", "email", "password_hash", "is_active", "created_at", "updated_at"):
        assert column_name in columns
    assert not columns["email"]["nullable"]
    assert not columns["password_hash"]["nullable"]


def test_users_table_has_no_redundant_plain_unique_constraint_on_email(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case-insensitive expression index (tested below) is the only
    uniqueness mechanism on this column - a plain, case-sensitive
    `UNIQUE(email)` table constraint would be fully redundant once it
    exists, and is deliberately not also present."""
    db_path = tmp_path / "alembic_users_no_plain_unique_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        unique_constraints = inspect(engine).get_unique_constraints("users")
    finally:
        engine.dispose()

    assert unique_constraints == []


def test_users_table_has_a_case_insensitive_unique_index_on_email(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ix_users_email_lower` (`UNIQUE` on `lower(email)`) is what actually
    enforces "no two accounts share an email, regardless of case."

    SQLAlchemy's SQLite dialect cannot reflect an expression-based index
    at all (`Inspector.get_indexes()` silently returns `[]` for it, with
    only a `SAWarning` as a clue - confirmed directly; this is a real
    SQLite-reflection limitation, not a bug in this migration) - so this
    is verified two ways instead: the index's own DDL, read directly from
    SQLite's `sqlite_master` (ground truth, independent of
    SQLAlchemy's reflection support), and its actual enforcement
    behavior via real inserts - the property that actually matters.
    """
    db_path = tmp_path / "alembic_users_email_lower_index_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ix_users_email_lower'"
            ).fetchone()
            assert row is not None
            assert "UNIQUE" in row[0].upper()
            assert "LOWER(EMAIL)" in row[0].upper()

        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            session.execute(
                sa.text(
                    "INSERT INTO users (email, password_hash, is_active, created_at, updated_at) "
                    "VALUES ('user@example.com', 'hash', 1, '2026-01-01', '2026-01-01')"
                )
            )
            session.commit()

            with pytest.raises(IntegrityError):
                session.execute(
                    sa.text(
                        "INSERT INTO users (email, password_hash, is_active, created_at, updated_at) "
                        "VALUES ('USER@example.com', 'hash', 1, '2026-01-01', '2026-01-01')"
                    )
                )
                session.commit()
            session.rollback()
        finally:
            session.close()
    finally:
        engine.dispose()


def test_refresh_tokens_table_has_expected_columns_fk_and_unique_token_hash(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "alembic_refresh_tokens_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("refresh_tokens")}
        unique_constraints = inspector.get_unique_constraints("refresh_tokens")
        index_names = {idx["name"] for idx in inspector.get_indexes("refresh_tokens")}
        foreign_keys = inspector.get_foreign_keys("refresh_tokens")
    finally:
        engine.dispose()

    for column_name in ("id", "user_id", "token_hash", "issued_at", "expires_at", "revoked_at"):
        assert column_name in columns
    assert any(uc["column_names"] == ["token_hash"] for uc in unique_constraints)
    assert "ix_refresh_tokens_user_id" in index_names

    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "users"
    assert fk["constrained_columns"] == ["user_id"]
    assert fk["options"].get("ondelete") == "CASCADE"


def test_password_reset_tokens_table_has_expected_columns_fk_and_unique_token_hash(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "alembic_password_reset_tokens_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("password_reset_tokens")}
        unique_constraints = inspector.get_unique_constraints("password_reset_tokens")
        index_names = {idx["name"] for idx in inspector.get_indexes("password_reset_tokens")}
        foreign_keys = inspector.get_foreign_keys("password_reset_tokens")
    finally:
        engine.dispose()

    for column_name in ("id", "user_id", "token_hash", "created_at", "expires_at", "used_at"):
        assert column_name in columns
    assert any(uc["column_names"] == ["token_hash"] for uc in unique_constraints)
    assert "ix_password_reset_tokens_user_id" in index_names

    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "users"
    assert fk["constrained_columns"] == ["user_id"]
    assert fk["options"].get("ondelete") == "CASCADE"


def test_upgrade_head_adds_nullable_indexed_user_id_to_saved_searches(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`saved_searches.user_id` must be nullable (Phase 1 - no user exists
    yet to attribute pre-existing rows to; a later cutover backfills it
    and only then is it made `NOT NULL`), indexed, and a foreign key to
    `users.id` with `ON DELETE CASCADE`."""
    db_path = tmp_path / "alembic_saved_search_user_id_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("saved_searches")}
        index_names = {idx["name"] for idx in inspector.get_indexes("saved_searches")}
        foreign_keys = inspector.get_foreign_keys("saved_searches")
    finally:
        engine.dispose()

    assert "user_id" in columns
    assert columns["user_id"]["nullable"] is True
    assert "ix_saved_searches_user_id" in index_names

    user_fks = [fk for fk in foreign_keys if fk["referred_table"] == "users"]
    assert len(user_fks) == 1
    assert user_fks[0]["constrained_columns"] == ["user_id"]
    assert user_fks[0]["options"].get("ondelete") == "CASCADE"


def test_downgrade_from_head_removes_the_authentication_tables_and_column(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "alembic_auth_downgrade_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "163ae88ffc55")  # one revision before the auth tables

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        saved_search_columns = {col["name"] for col in inspect(engine).get_columns("saved_searches")}
    finally:
        engine.dispose()

    for table_name in ("users", "refresh_tokens", "password_reset_tokens"):
        assert table_name not in tables
    assert "user_id" not in saved_search_columns


def test_upgrade_head_creates_notification_preferences_table(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alembic_notification_preferences_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("notification_preferences")}
        index_names_and_unique = {
            idx["name"]: idx["unique"] for idx in inspector.get_indexes("notification_preferences")
        }
        foreign_keys = inspector.get_foreign_keys("notification_preferences")
    finally:
        engine.dispose()

    for column_name in ("id", "user_id", "telegram_chat_id", "created_at", "updated_at"):
        assert column_name in columns

    # user_id is both the uniqueness guarantee and the lookup key - one
    # UNIQUE index serves both, matching the model exactly (see this
    # migration's own docstring for why there's no separate, redundant
    # non-unique index or UniqueConstraint alongside it).
    assert index_names_and_unique.get("ix_notification_preferences_user_id")  # 1 on SQLite, True on Postgres

    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "users"
    assert fk["constrained_columns"] == ["user_id"]
    assert fk["options"].get("ondelete") == "CASCADE"


def test_downgrade_one_revision_removes_only_notification_preferences(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downgrading exactly one revision (this migration's own) must remove
    `notification_preferences` and nothing else - `users`/`saved_searches`
    (and every other table) must be untouched."""
    db_path = tmp_path / "alembic_notification_preferences_downgrade_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "d363f3f9d06c")  # one revision before notification_preferences

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "notification_preferences" not in tables
    for table_name in ("users", "saved_searches", "discovered_listings", "refresh_tokens"):
        assert table_name in tables


def test_upgrade_head_creates_listing_attributions_table(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "alembic_listing_attributions_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"] for col in inspector.get_columns("listing_attributions")}
        index_names_and_unique = {
            idx["name"]: idx["unique"] for idx in inspector.get_indexes("listing_attributions")
        }
        unique_constraints = inspector.get_unique_constraints("listing_attributions")
        foreign_keys = {fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys("listing_attributions")}
    finally:
        engine.dispose()

    for column_name in ("id", "saved_search_id", "discovered_listing_id", "discovered_at"):
        assert column_name in columns

    # discovered_listing_id gets its own plain index (not covered by the
    # composite UNIQUE below, whose leftmost column is saved_search_id) -
    # matching the model's own docstring reasoning.
    assert index_names_and_unique.get("ix_listing_attributions_discovered_listing_id") == 0

    # The idempotency guarantee is a genuine multi-column UNIQUE table
    # constraint, not a separate unique index - matches the model's
    # `UniqueConstraint` (not `mapped_column(unique=True)`) exactly.
    assert len(unique_constraints) == 1
    assert unique_constraints[0]["column_names"] == ["saved_search_id", "discovered_listing_id"]

    assert foreign_keys["saved_search_id"]["referred_table"] == "saved_searches"
    assert foreign_keys["saved_search_id"]["options"].get("ondelete") == "CASCADE"
    assert foreign_keys["discovered_listing_id"]["referred_table"] == "discovered_listings"
    assert foreign_keys["discovered_listing_id"]["options"].get("ondelete") == "CASCADE"


def test_downgrade_one_revision_removes_only_listing_attributions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downgrading exactly one revision (this migration's own) must remove
    `listing_attributions` and nothing else - `discovered_listings.
    discovered_by_saved_search_id` (the historical column this migration
    deliberately leaves untouched) and every other table must survive."""
    db_path = tmp_path / "alembic_listing_attributions_downgrade_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "5faab97d82e8")  # one revision before listing_attributions

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        discovered_listing_columns = {col["name"] for col in inspector.get_columns("discovered_listings")}
    finally:
        engine.dispose()

    assert "listing_attributions" not in tables
    for table_name in ("users", "saved_searches", "discovered_listings", "notification_preferences"):
        assert table_name in tables
    assert "discovered_by_saved_search_id" in discovered_listing_columns


def test_upgrade_head_adds_pending_notification_user_id_column(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 2A of the multi-user notification outbox redesign: a
    nullable `user_id` foreign key column on `pending_notifications`,
    schema-only groundwork - confirms it's actually applied on a fresh
    database, the foreign key's `ondelete` behavior is really `CASCADE`,
    and the table's original `UNIQUE(discovered_listing_id)` constraint -
    unchanged since this table was first created - survives the SQLite
    batch-mode table recreation this migration requires."""
    db_path = tmp_path / "alembic_pending_notification_user_id_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {col["name"]: col for col in inspector.get_columns("pending_notifications")}
        foreign_keys = {fk["constrained_columns"][0]: fk for fk in inspector.get_foreign_keys("pending_notifications")}
        unique_constraints = inspector.get_unique_constraints("pending_notifications")
    finally:
        engine.dispose()

    assert "user_id" in columns
    assert columns["user_id"]["nullable"] is True

    assert "user_id" in foreign_keys
    assert foreign_keys["user_id"]["referred_table"] == "users"
    assert foreign_keys["user_id"]["options"].get("ondelete") == "CASCADE"

    # The pre-existing FK to discovered_listings must survive untouched.
    assert foreign_keys["discovered_listing_id"]["referred_table"] == "discovered_listings"
    assert foreign_keys["discovered_listing_id"]["options"].get("ondelete") == "CASCADE"

    # The sole identity this table has ever had - completely unchanged.
    assert len(unique_constraints) == 1
    assert unique_constraints[0]["column_names"] == ["discovered_listing_id"]


def test_migration_preserves_existing_pending_notification_rows(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of an additive migration: a row that existed before
    this migration ran must survive it completely unchanged, with
    `user_id` simply `NULL` (unknown, not invented) - never lost, never
    altered, never require a NOT NULL default that would fabricate an
    owner for a row nothing knows the owner of yet."""
    db_path = tmp_path / "alembic_pending_notification_migration_preserves_rows_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    # Upgrade to just before this migration, then insert a row exactly as
    # production data would already exist - a real discovered_listings
    # row plus its outbox row - before this migration has ever run.
    command.upgrade(cfg, "a1c2e5f9b3d7")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO discovered_listings "
                "(marketplace, external_listing_id, title, listing_url, first_discovered_at, last_seen_at) "
                "VALUES ('mock', 'pre-existing-1', 'Pre-existing listing', 'https://example.com/pre-existing-1', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
        )
        listing_id = conn.execute(sa.text("SELECT id FROM discovered_listings WHERE external_listing_id = 'pre-existing-1'")).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO pending_notifications "
                "(discovered_listing_id, status, attempt_count, created_at) "
                "VALUES (:listing_id, 'sent', 1, '2026-01-01T00:00:00+00:00')"
            ),
            {"listing_id": listing_id},
        )
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT discovered_listing_id, status, attempt_count, user_id FROM pending_notifications "
                    "WHERE discovered_listing_id = :listing_id"
                ),
                {"listing_id": listing_id},
            ).one()
    finally:
        engine.dispose()

    assert row.discovered_listing_id == listing_id
    assert row.status == "sent"
    assert row.attempt_count == 1
    assert row.user_id is None


def test_downgrade_one_revision_removes_only_pending_notification_user_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Downgrading exactly one revision (this migration's own) must remove
    only the `user_id` column/foreign key it added - the table itself,
    every other column, and `UNIQUE(discovered_listing_id)` must survive."""
    db_path = tmp_path / "alembic_pending_notification_user_id_downgrade_test.db"
    cfg = _alembic_config_for(f"sqlite:///{db_path}", monkeypatch)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "a1c2e5f9b3d7")

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        columns = {col["name"] for col in inspector.get_columns("pending_notifications")}
        unique_constraints = inspector.get_unique_constraints("pending_notifications")
    finally:
        engine.dispose()

    assert "pending_notifications" in tables
    assert "user_id" not in columns
    for column_name in ("id", "discovered_listing_id", "status", "attempt_count", "created_at"):
        assert column_name in columns
    assert len(unique_constraints) == 1
    assert unique_constraints[0]["column_names"] == ["discovered_listing_id"]
