"""add user is_admin

Revision ID: 7b3f0a1d9c44
Revises: 30fc5cd97dad
Create Date: 2026-09-12 00:00:00.000000

Adds `is_admin` (`BOOLEAN NOT NULL DEFAULT FALSE`) to `users` - the
authorization flag `core/auth/dependencies.py:require_admin` checks for
the read-only admin user-management API (`api/v1/admin.py`). See
`core/auth/models.py`'s `User.is_admin` docstring for the full design:
defaults to `False` for every existing and new row, and nothing in the
application ever sets it to `True` at runtime - only a server operator
running `scripts/set_admin.py` directly against the database does that.

A plain `add_column`, same shape as `d363f3f9d06c_add_user_failed_login_
lockout_tracking.py`'s `failed_login_attempts` - no existing constraint
or index on `users` is touched, so no `batch_alter_table`/`copy_from`
dance is needed here (unlike `30fc5cd97dad`, which had to replace an
existing unnamed constraint). `server_default=sa.false()` is required
for the same reason `failed_login_attempts` needed `server_default='0'`:
adding a `NOT NULL` column with no default fails outright against any
`users` table that already has rows (confirmed by that migration's own
docstring) - `sa.false()` renders the correct dialect-appropriate
literal on both SQLite (`0`) and PostgreSQL (`FALSE`), so every existing
account becomes a non-admin by default, exactly as required.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b3f0a1d9c44'
down_revision: Union[str, Sequence[str], None] = '30fc5cd97dad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'is_admin')
