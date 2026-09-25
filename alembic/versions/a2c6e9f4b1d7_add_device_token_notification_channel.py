"""add device_tokens.notification_channel_id

Revision ID: a2c6e9f4b1d7
Revises: 3f9c1a5b7d2e
Create Date: 2026-09-25 00:00:00.000000

Phase 1 of notification sound selection - see
`marketplace_alert/core/notifications/models.py:DeviceToken` for the full
design. One purely additive, nullable column: which of a small fixed set of
versioned Android notification channel ids (e.g. "listing-alerts-radar-v1")
this device's user has explicitly chosen. `NULL` for every existing row -
deliberately not backfilled, since `NULL` ("never explicitly chosen yet") is
a real, distinct, safe state, not a placeholder needing correction - see
that column's own docstring for why it's kept distinct from the explicit
"listing-alerts-default-v1" System Default choice.

No existing row's existing data is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2c6e9f4b1d7'
down_revision: Union[str, Sequence[str], None] = '3f9c1a5b7d2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'device_tokens', sa.Column('notification_channel_id', sa.String(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('device_tokens', 'notification_channel_id')
