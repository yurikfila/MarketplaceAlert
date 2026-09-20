"""add device_tokens and pending_notifications push columns

Revision ID: 3f9c1a5b7d2e
Revises: 7b3f0a1d9c44
Create Date: 2026-09-19 00:00:00.000000

Phase 1 of native mobile push notifications (Expo Notifications) - see
`marketplace_alert/core/notifications/models.py:DeviceToken` and
`marketplace_alert/core/persistence/models.py:PendingNotification`'s
push-channel columns for the full design.

Two purely additive changes, neither touching any existing table's
existing columns/constraints/data:

1. A new `device_tokens` table - one row per registered Expo push token,
   `user_id` a plain indexed FK (not unique - a user may have more than
   one device), `expo_push_token` itself unique (re-registration is an
   upsert, never a duplicate row).
2. Six new nullable-or-defaulted columns on `pending_notifications`
   (`push_status`, `push_attempt_count`, `push_claimed_at`,
   `push_last_attempted_at`, `push_last_error`, `push_sent_at`) - a
   second, independent claim/deliver/complete lifecycle on the same row,
   mirroring the existing Telegram-oriented `status`/`attempt_count`/
   `claimed_at`/`last_attempted_at`/`last_error`/`sent_at` columns
   one-for-one, so a row's push and Telegram delivery state can never
   affect each other. `push_status` needs `server_default='pending'`
   and `push_attempt_count` needs `server_default='0'` for the same
   reason `d363f3f9d06c`'s `failed_login_attempts` did: adding a
   `NOT NULL` column with no default fails outright against any table
   that already has rows.

No existing row's existing data is touched by either change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f9c1a5b7d2e'
down_revision: Union[str, Sequence[str], None] = '7b3f0a1d9c44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'device_tokens',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('expo_push_token', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], name='fk_device_tokens_user_id', ondelete='CASCADE'
        ),
        sa.UniqueConstraint('expo_push_token', name='uq_device_tokens_expo_push_token'),
    )
    op.create_index('ix_device_tokens_user_id', 'device_tokens', ['user_id'])

    op.add_column(
        'pending_notifications',
        sa.Column('push_status', sa.String(), nullable=False, server_default='pending'),
    )
    op.add_column(
        'pending_notifications',
        sa.Column('push_attempt_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'pending_notifications', sa.Column('push_claimed_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'pending_notifications',
        sa.Column('push_last_attempted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('pending_notifications', sa.Column('push_last_error', sa.String(), nullable=True))
    op.add_column(
        'pending_notifications', sa.Column('push_sent_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        'ix_pending_notifications_push_status', 'pending_notifications', ['push_status']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_pending_notifications_push_status', table_name='pending_notifications')
    op.drop_column('pending_notifications', 'push_sent_at')
    op.drop_column('pending_notifications', 'push_last_error')
    op.drop_column('pending_notifications', 'push_last_attempted_at')
    op.drop_column('pending_notifications', 'push_claimed_at')
    op.drop_column('pending_notifications', 'push_attempt_count')
    op.drop_column('pending_notifications', 'push_status')

    op.drop_index('ix_device_tokens_user_id', table_name='device_tokens')
    op.drop_table('device_tokens')
