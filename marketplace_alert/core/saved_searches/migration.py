"""One-time, idempotent migration: the old single `saved_searches.marketplace`
column -> the `saved_search_marketplaces` join table.

This project has no migration framework (Alembic) - `init_db()` only ever
adds tables that don't exist yet (`Base.metadata.create_all()`), it never
alters an existing one. That's normally fine, but multi-marketplace saved
searches changed the shape of `saved_searches` itself, and real saved
searches created before this change must not be silently destroyed (see
PROJECT_CONTEXT.md). This module is the one-time, hand-written fix: safe to
run on every startup, a no-op once already applied.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def migrate_legacy_marketplace_column(engine: Engine) -> None:
    """If `saved_searches` still has the old single `marketplace` column,
    copy each row's value into `saved_search_marketplaces` (skipping any
    that already have that association, so this is safe to run repeatedly)
    and drop the legacy column. No-op if the column is already gone, or if
    `saved_searches` doesn't exist yet (a brand-new database).

    Must run after `init_db()` - it needs `saved_search_marketplaces` to
    already exist to insert into.
    """
    inspector = inspect(engine)
    if "saved_searches" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("saved_searches")}
    if "marketplace" not in columns:
        return

    logger.info("Migrating legacy saved_searches.marketplace column to saved_search_marketplaces")

    # SQLite's DROP COLUMN doesn't clean up indexes that reference the
    # dropped column - left in place, the ALTER TABLE below fails with
    # "error in index ... after drop column: no such column". The original
    # single-marketplace model had `index=True` on this column, so drop
    # whatever index SQLAlchemy named it, rather than assuming the name.
    indexes_on_marketplace = [
        index["name"]
        for index in inspector.get_indexes("saved_searches")
        if "marketplace" in index["column_names"]
    ]

    migrated = 0
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT id, marketplace FROM saved_searches")).fetchall()
        for saved_search_id, marketplace in rows:
            if not marketplace:
                continue
            already_linked = connection.execute(
                text(
                    "SELECT 1 FROM saved_search_marketplaces "
                    "WHERE saved_search_id = :sid AND marketplace_name = :name"
                ),
                {"sid": saved_search_id, "name": marketplace},
            ).first()
            if already_linked is not None:
                continue
            connection.execute(
                text(
                    "INSERT INTO saved_search_marketplaces (saved_search_id, marketplace_name) "
                    "VALUES (:sid, :name)"
                ),
                {"sid": saved_search_id, "name": marketplace},
            )
            migrated += 1

        for index_name in indexes_on_marketplace:
            connection.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
        connection.execute(text("ALTER TABLE saved_searches DROP COLUMN marketplace"))

    logger.info("Migrated %d saved search(es) to the new marketplace join table", migrated)
