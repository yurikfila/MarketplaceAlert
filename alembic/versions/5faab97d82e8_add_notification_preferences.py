"""add notification preferences

Revision ID: 5faab97d82e8
Revises: d363f3f9d06c
Create Date: 2026-09-05 00:00:00.000000

Per-user notification routing (see `core/notifications/models.py`'s
`NotificationPreference` for the full design/reasoning). Adds one
brand-new table, `notification_preferences` - a strict 1:1 with `users`
(`UNIQUE(user_id)`), holding just `telegram_chat_id` for now.

Purely additive - no existing table dropped, no existing column altered,
no data touched by this migration itself. Every existing user simply has
no row here yet, which is exactly correct: "no preference configured" is
represented by the row's *absence*, not a nullable column on `users`.

A brand-new table with an inline foreign key is a single, portable
`CREATE TABLE` on every backend this project targets (SQLite included) -
no batch mode needed, matching `2732fd410d2f`'s (`add authentication
tables`) exact precedent for its own new tables.

Verified via the usual upgrade/downgrade/re-upgrade cycle against a
throwaway SQLite database before finalizing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5faab97d82e8'
down_revision: Union[str, Sequence[str], None] = 'd363f3f9d06c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `user_id` is both the uniqueness guarantee (one preference row per
    # user) and the lookup key `NotificationPreferenceRepository.
    # get_by_user_id()` needs - one UNIQUE index serves both, matching
    # exactly what `Mapped[int] = mapped_column(..., unique=True,
    # index=True)` on the model produces (verified against
    # `Base.metadata.create_all()`), not a separate UniqueConstraint plus
    # a second, redundant non-unique index.
    op.create_table('notification_preferences',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('telegram_chat_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_notification_preferences_user_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notification_preferences_user_id'), 'notification_preferences', ['user_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_notification_preferences_user_id'), table_name='notification_preferences')
    op.drop_table('notification_preferences')
