"""Tests for the legacy `saved_searches.marketplace` -> `saved_search_marketplaces`
migration (`marketplace_alert.core.saved_searches.migration`).

Builds a temp SQLite file with the *old* single-marketplace schema by hand
(mirroring exactly what real pre-existing user data looks like - see
CHANGELOG.md) rather than going through the current ORM models, since the
whole point is testing the transition away from that old shape.
"""

import sqlite3
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from marketplace_alert.core.persistence.database import Base, create_db_engine
from marketplace_alert.core.saved_searches.migration import migrate_legacy_marketplace_column
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _build_legacy_database(db_path) -> None:
    """Hand-build the OLD single-marketplace schema plus rows mirroring the
    real pre-migration data: Pokemon -> mock, Maccabi -> etsy, Makita -> etsy.

    Includes the index the original model had on `marketplace`
    (`index=True`) - omitting it here previously let this test pass while
    the real migration failed against the real database, since SQLite's
    DROP COLUMN doesn't clean up indexes on the dropped column by itself.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE saved_searches (
            id INTEGER NOT NULL,
            "query" VARCHAR NOT NULL,
            marketplace VARCHAR NOT NULL,
            is_active BOOLEAN NOT NULL,
            scan_interval_seconds INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            last_scanned_at DATETIME,
            PRIMARY KEY (id)
        )
        """
    )
    conn.execute("CREATE INDEX ix_saved_searches_marketplace ON saved_searches (marketplace)")
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        'INSERT INTO saved_searches '
        '(id, "query", marketplace, is_active, scan_interval_seconds, created_at, updated_at, last_scanned_at) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Pokemon", "mock", 1, 60, now, now, None),
            (2, "Maccabi", "etsy", 1, 60, now, now, None),
            (3, "Makita", "etsy", 1, 300, now, now, None),
        ],
    )
    conn.commit()
    conn.close()


def _list_all(engine) -> dict[str, dict]:
    """Plain dicts, not ORM objects - `.marketplaces` is a lazy-loaded
    relationship that can't be touched after the session closes."""
    session = sessionmaker(bind=engine)()
    try:
        return {
            s.query: {
                "id": s.id,
                "marketplaces": s.marketplaces,
                "scan_interval_seconds": s.scan_interval_seconds,
            }
            for s in SavedSearchRepository(session).list_all()
        }
    finally:
        session.close()


def test_migrates_legacy_marketplace_column_to_join_table(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    _build_legacy_database(db_path)

    engine = create_db_engine(f"sqlite:///{db_path}")
    # create_all() only adds missing tables (saved_search_marketplaces) - it
    # never touches the pre-existing saved_searches table.
    Base.metadata.create_all(bind=engine)
    migrate_legacy_marketplace_column(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("saved_searches")}
    assert "marketplace" not in columns

    all_searches = _list_all(engine)
    assert set(all_searches) == {"Pokemon", "Maccabi", "Makita"}
    assert all_searches["Pokemon"]["marketplaces"] == ["mock"]
    assert all_searches["Maccabi"]["marketplaces"] == ["etsy"]
    assert all_searches["Makita"]["marketplaces"] == ["etsy"]
    # Confirms real row data survived, not just the marketplace mapping.
    assert all_searches["Makita"]["scan_interval_seconds"] == 300
    assert all_searches["Pokemon"]["id"] == 1


def test_migration_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    _build_legacy_database(db_path)

    engine = create_db_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    migrate_legacy_marketplace_column(engine)
    migrate_legacy_marketplace_column(engine)  # must not raise or duplicate anything

    makita = _list_all(engine)["Makita"]
    assert makita["marketplaces"] == ["etsy"]  # not ["etsy", "etsy"]


def test_migration_is_a_no_op_on_a_fresh_database(tmp_path) -> None:
    db_path = tmp_path / "fresh.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)

    migrate_legacy_marketplace_column(engine)  # must not raise

    columns = {col["name"] for col in inspect(engine).get_columns("saved_searches")}
    assert "marketplace" not in columns  # the current model never had it


def test_migration_is_a_no_op_when_saved_searches_table_is_missing(tmp_path) -> None:
    db_path = tmp_path / "empty.db"
    engine = create_db_engine(f"sqlite:///{db_path}")

    migrate_legacy_marketplace_column(engine)  # must not raise on a table-less database
