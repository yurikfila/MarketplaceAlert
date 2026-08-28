"""add notification outbox

Revision ID: 163ae88ffc55
Revises: 3e288e0d0a15
Create Date: 2026-08-28 22:32:29.399672

Adds `pending_notifications`, the notification outbox that decouples
scanning/listing persistence from Telegram delivery entirely - see
PROJECT_CONTEXT.md's Render-reliability decision and
`core/persistence/models.py:PendingNotification` / `core/notifications
/outbox.py` for the full design (a diagnostic investigation found that
scanning and Telegram delivery sharing one synchronous, no-timeout code
path was a real contributor to production scan gaps on Render's Free
tier).

Purely additive - a brand-new table, no existing column/table touched.
No batch mode needed (unlike some earlier migrations): creating a new
table with an inline foreign key is a single, portable `CREATE TABLE`
statement on every backend, including SQLite - batch mode is only
required when altering an *existing* table's constraints, which SQLite
can't do directly.

The `status` index is included from this table's very first migration,
deliberately, unlike `discovered_listings.metadata_backfill_status`
(left unindexed pending evidence of need) - here the access pattern is
known and constant before a single row is ever written: every drain run
filters `WHERE status = 'pending' OR (status = 'processing' AND ...)`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '163ae88ffc55'
down_revision: Union[str, Sequence[str], None] = '3e288e0d0a15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pending_notifications',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('discovered_listing_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['discovered_listing_id'], ['discovered_listings.id'], name='fk_pending_notifications_discovered_listing_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('discovered_listing_id')
    )
    op.create_index(op.f('ix_pending_notifications_status'), 'pending_notifications', ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_pending_notifications_status'), table_name='pending_notifications')
    op.drop_table('pending_notifications')
