"""replace pending_notification unique constraint

Revision ID: f3a7c9e2b4d1
Revises: c4d8f1a6e0b2
Create Date: 2026-09-06 00:00:00.000000

Phase 2D-SCHEMA of the multi-user notification outbox redesign (see
`core/persistence/models.py:PendingNotification`'s own docstring, and the
Phase 2D read-only cutover audit, for the full design/reasoning). Schema
cutover ONLY - no runtime behavior changes ship in this migration or the
code around it. `SavedSearchRunner` still enqueues only one row per
globally-new listing (`discovery_result.new_listing_ids`), exactly as
before; this migration only makes the schema *permit* a second row for a
second user sharing the same listing, which a later, separate phase
(Phase 2D-BEHAVIOR) will actually start creating.

Replaces the original single-column `UNIQUE(discovered_listing_id)`
(declared unnamed, inline, in `163ae88ffc55_add_notification_outbox.py`)
with a composite `UNIQUE(user_id, discovered_listing_id)`, and adds a
dedicated, separate, non-unique index on `discovered_listing_id` alone
(needed because the new composite index leads with `user_id` and can't
efficiently serve a lookup keyed on `discovered_listing_id` alone - e.g.
this column's own `ON DELETE CASCADE` needing to find every notification
for a deleted listing - matching `ListingAttribution.discovered_
listing_id`'s identical existing precedent exactly).

**No row data is touched.** Every existing `user_id` value (whether
populated by Phase 2B's stamping, Phase 2C's backfill, or still `NULL`
for the 24 historical rows the backfill could not safely resolve) is
completely unaffected - this migration only changes which combinations
of `(user_id, discovered_listing_id)` the database will accept *going
forward*, never anything about a row that already exists. `user_id`
remains nullable - not just for those 24 rows, but structurally,
indefinitely: standard SQL uniqueness semantics never treat `NULL` as
equal to `NULL`, so any number of `user_id IS NULL` rows can freely
coexist under the new composite constraint (including multiple rows for
the very same `discovered_listing_id`, should that ever occur) with no
special-casing required anywhere - true identically on PostgreSQL and
SQLite.

**Discovering the old constraint's real name, not guessing it.** The
original migration declared `sa.UniqueConstraint('discovered_listing_id')`
with no explicit `name=`, so PostgreSQL auto-generated its actual
constraint name at creation time - this migration reflects the live
schema via `Inspector.get_unique_constraints()` and looks for the one
whose column set is exactly `['discovered_listing_id']`, refusing to
proceed (raising, rather than guessing) if that isn't found exactly once.
This also guarantees no *other*, unrelated unique constraint on this
table is ever touched.

**A genuine SQLite/PostgreSQL divergence, verified empirically before
finalizing this migration.** On PostgreSQL, an unnamed `UNIQUE` constraint
still gets a real, reflectable auto-generated name - dropped directly by
that name. On SQLite, the *exact same* unnamed constraint (declared
inline at table-creation time) reflects with `name: None` via `get_
unique_constraints()`, and is invisible to `get_indexes()` too (SQLite
implements it as an internal `sqlite_autoindex_*` that SQLAlchemy's own
dialect deliberately doesn't expose as a droppable index) - there is
simply no name to drop it by on this backend. `batch_alter_table`'s
`copy_from` parameter is the correct, documented answer for exactly this
situation: supplying the table's current shape explicitly (mirroring
`PendingNotification` exactly as it exists at revision `c4d8f1a6e0b2`)
lets SQLite's table-recreation mechanism simply never carry the old,
nameless constraint forward - functionally equivalent to "drop it"
without needing a name to reference. `copy_from` only affects SQLite's
own recreate mechanism; on PostgreSQL, `batch_alter_table` issues plain
`ALTER TABLE` statements directly (no recreation), so the reflected real
name is used to drop the constraint there instead - `copy_from` is simply
unused on that path.

Verified via the usual upgrade/downgrade/re-upgrade cycle against a
throwaway SQLite database before finalizing, and cross-checked against
`Base.metadata.create_all()` to confirm the resulting constraint/index
shape matches the model exactly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a7c9e2b4d1'
down_revision: Union[str, Sequence[str], None] = 'c4d8f1a6e0b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_UNIQUE_CONSTRAINT_NAME = 'uq_pending_notifications_user_id_discovered_listing_id'
_NEW_INDEX_NAME = 'ix_pending_notifications_discovered_listing_id'
_RESTORED_SINGLE_COLUMN_UNIQUE_NAME = 'uq_pending_notifications_discovered_listing_id'


def _current_table_shape_for_sqlite_copy_from() -> sa.Table:
    """`PendingNotification` exactly as it exists at revision
    `c4d8f1a6e0b2` (the revision immediately prior to this one) - used
    only as `batch_alter_table`'s `copy_from` on SQLite, only when the
    old single-column unique constraint has no reflectable name (see this
    module's docstring). Deliberately NOT imported from `core/persistence
    /models.py`: migrations must describe schema as it concretely was at
    a specific point in history, never drift silently if the live model
    changes later.

    **Must include every existing index too, not just columns/FKs** -
    `copy_from` is the *entire* starting point `batch_alter_table` uses
    to recreate the table on SQLite; anything not listed here (or added
    explicitly via this migration's own `batch_op` calls) is silently
    dropped during recreation, not preserved by default. Caught exactly
    this way: an earlier draft omitted `ix_pending_notifications_status`
    (added back in `163ae88ffc55_add_notification_outbox.py`) and the
    full upgrade/downgrade/re-upgrade round-trip test failed with `no
    such index: ix_pending_notifications_status` on a later downgrade,
    proving it had been silently lost.
    """
    table = sa.Table(
        'pending_notifications',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('discovered_listing_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_attempted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['discovered_listing_id'],
            ['discovered_listings.id'],
            name='fk_pending_notifications_discovered_listing_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            name='fk_pending_notifications_user_id',
            ondelete='SET NULL',
        ),
    )
    sa.Index('ix_pending_notifications_status', table.c.status)
    return table


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    matching = [
        uc
        for uc in inspector.get_unique_constraints('pending_notifications')
        if uc['column_names'] == ['discovered_listing_id']
    ]
    if len(matching) != 1:
        raise RuntimeError(
            "Expected exactly one UNIQUE(discovered_listing_id) constraint on "
            f"pending_notifications; found {len(matching)}: {matching!r}. Refusing to "
            "guess which constraint to drop - see this migration's docstring."
        )
    old_constraint_name = matching[0]['name']

    # Only needed when the old constraint has no reflectable name at all
    # (SQLite) - see this module's docstring for exactly why.
    copy_from = None if old_constraint_name is not None else _current_table_shape_for_sqlite_copy_from()

    with op.batch_alter_table('pending_notifications', schema=None, copy_from=copy_from) as batch_op:
        if old_constraint_name is not None:
            batch_op.drop_constraint(old_constraint_name, type_='unique')
        batch_op.create_unique_constraint(
            _NEW_UNIQUE_CONSTRAINT_NAME, ['user_id', 'discovered_listing_id']
        )
        batch_op.create_index(
            batch_op.f(_NEW_INDEX_NAME), ['discovered_listing_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema.

    **Only safe before Phase 2D-BEHAVIOR has ever run.** Once any
    canonical listing has two `PendingNotification` rows for two
    different non-NULL users (the entire point of this cutover), this
    `create_unique_constraint` call below will fail outright - PostgreSQL
    (and SQLite) both validate ALL existing rows against a new UNIQUE
    constraint at creation time, and two such rows are, by construction,
    a genuine violation of `UNIQUE(discovered_listing_id)` alone. This is
    expected and intentional: this downgrade must never silently delete
    or merge rows to force success. If this fails in that situation, the
    correct operational response is to roll back the *application code*
    to its Phase 2B behavior (already forward-compatible with the new
    composite constraint - see the Phase 2D audit) and leave the schema
    as this migration's `upgrade()` left it, rather than attempting to
    force this downgrade through.

    The restored single-column constraint is given a new explicit name
    (`uq_pending_notifications_discovered_listing_id`) rather than
    attempting to reproduce the original's auto-generated PostgreSQL name
    - `create_unique_constraint` requires an explicit name, and the
    original's auto-generated one was never itself part of any
    application-level contract.
    """
    with op.batch_alter_table('pending_notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f(_NEW_INDEX_NAME))
        batch_op.drop_constraint(_NEW_UNIQUE_CONSTRAINT_NAME, type_='unique')
        batch_op.create_unique_constraint(
            _RESTORED_SINGLE_COLUMN_UNIQUE_NAME, ['discovered_listing_id']
        )
