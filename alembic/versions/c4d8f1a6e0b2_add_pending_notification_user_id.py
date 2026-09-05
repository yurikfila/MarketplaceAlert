"""add pending_notification user_id

Revision ID: c4d8f1a6e0b2
Revises: a1c2e5f9b3d7
Create Date: 2026-09-05 00:00:00.000000

Phase 2A of the multi-user notification outbox redesign (see
`core/persistence/models.py:PendingNotification`'s own docstring for the
full design/reasoning). Adds one nullable `user_id` foreign key column to
the existing `pending_notifications` table - schema-only groundwork,
nothing more.

Purely additive - no existing column altered or dropped, no existing row
touched, no data migrated. `user_id` is nullable, and nothing in this
codebase writes or reads it yet; every existing row simply has `NULL`
here, which is exactly correct (unknown/unset, not invented). The
existing `UNIQUE(discovered_listing_id)` constraint - the sole identity
this table has had since it was first created (see
`163ae88ffc55_add_notification_outbox.py`) - is completely unchanged:
still the only uniqueness guarantee, still enforced, still the reason a
listing can never get two outbox rows. Deliberately no index on `user_id`
yet either - nothing queries by it, since nothing writes it yet (see the
model's own docstring for the precedent this follows).

`ON DELETE SET NULL`, not `CASCADE` - corrected before this (still
unreleased) migration was ever deployed. `pending_notifications` can
carry real delivery/retry history (`sent`/`failed`, `attempt_count`,
timestamps); deleting a user must not silently destroy that history, only
forget whose notification it specifically was - see the model's own
docstring for the full reasoning (the same principle `discovered_by_
saved_search_id` above already applies for the identical situation).

**Uses `op.batch_alter_table()`, hand-added after autogenerate** - same
reason `280fbde82447` (`add listing product fields and saved search
attribution`) needed it: plain `op.add_column()`/`op.create_foreign_key()`
fails on SQLite with `NotImplementedError: No support for ALTER of
constraints in SQLite dialect` - SQLite has no `ALTER TABLE ... ADD
CONSTRAINT` at all. Batch mode transparently recreates the table on
SQLite (copy-and-move, preserving every existing row and every other
existing constraint, including `UNIQUE(discovered_listing_id)`); on every
other backend (PostgreSQL in production) it compiles to the exact same
plain `ALTER TABLE ADD COLUMN` / `ADD CONSTRAINT` statements autogenerate
would have produced directly.

Verified via the usual upgrade/downgrade/re-upgrade cycle against a
throwaway SQLite database before finalizing, and cross-checked against
`Base.metadata.create_all()` to confirm the resulting columns/FK/unique
constraint match the model exactly - including that `UNIQUE(discovered_
listing_id)` survives the SQLite batch-mode table recreation unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8f1a6e0b2'
down_revision: Union[str, Sequence[str], None] = 'a1c2e5f9b3d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('pending_notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_pending_notifications_user_id',
            'users',
            ['user_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('pending_notifications', schema=None) as batch_op:
        batch_op.drop_constraint('fk_pending_notifications_user_id', type_='foreignkey')
        batch_op.drop_column('user_id')
