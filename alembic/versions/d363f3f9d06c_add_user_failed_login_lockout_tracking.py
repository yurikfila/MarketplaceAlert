"""add user failed login lockout tracking

Revision ID: d363f3f9d06c
Revises: 2732fd410d2f
Create Date: 2026-08-29 13:52:00.607215

Phase 2 of the approved authentication design (see PROJECT_CONTEXT.md's
authentication decision): brute-force login protection needs somewhere
to persist a per-account failed-attempt count and lock expiry - see
`core/auth/models.py`'s `User.failed_login_attempts`/`locked_until` and
`core/auth/service.py`'s `AuthService.login()` for the full design.

`failed_login_attempts` needs `server_default='0'`, hand-added after
autogenerate - autogenerate produced a plain `NOT NULL` column with no
default, which fails outright against any `users` table that already has
rows (`sqlite3.OperationalError: Cannot add a NOT NULL column with
default value NULL`, confirmed directly; PostgreSQL rejects the
equivalent for the same reason). No `users` row exists in production yet
(Phase 1's cutover hasn't happened), so this migration would have
happened to work today regardless - but a migration's correctness
shouldn't depend on incidentally-empty data, so it's written to be
correct either way. `locked_until` needs no default - it's nullable, and
`NULL` (never locked) is exactly the correct value for every existing
row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd363f3f9d06c'
down_revision: Union[str, Sequence[str], None] = '2732fd410d2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('failed_login_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_login_attempts')
