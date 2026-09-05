"""add password reset token failed attempts and composite uniqueness

Revision ID: 30fc5cd97dad
Revises: f3a7c9e2b4d1
Create Date: 2026-09-05 21:52:52.844381

Phase 1 (schema only) of the approved 6-digit email-verification-code
password-reset design (see the corresponding read-only design audit) -
no runtime behavior changes ship in this migration or the code around it.
No forgot-password/reset-password route, repository, service method, code
generator, or email provider exists yet - this migration only prepares
`password_reset_tokens` to be able to hold a 6-digit code's hash safely,
for a later, separate phase to actually start issuing them.

**Why this is needed at all.** `PasswordResetToken.token_hash` was
designed for a 256-bit opaque token (`core/auth/security.py:
generate_token()`/`hash_token()` - effectively zero collision
probability). The approved 6-digit-code design hashes a value with only
~1,000,000 possibilities instead, via the same (deterministic, unsalted)
`hash_token()` - two different users can legitimately be issued the exact
same 6-digit code and therefore produce the exact same hash. The original
`UNIQUE(token_hash)` constraint (declared unnamed, inline, in
`2732fd410d2f_add_authentication_tables.py`) does not tolerate that at
all - a second user's insert would fail outright with an `IntegrityError`
at exactly the wrong moment. This migration replaces it with a composite
`UNIQUE(user_id, token_hash)`
(`uq_password_reset_tokens_user_id_token_hash`): two different users
sharing a hash is fine (their `user_id` differs), while the same user
still cannot hold two rows with the same hash. Lookups at reset-password
time are always scoped by `user_id` (resolved server-side from the
client-supplied email) first, never by `token_hash` alone - this
constraint is defense-in-depth against an application bug, not the
primary lookup mechanism.

Also adds `failed_attempts` (`INTEGER NOT NULL DEFAULT 0`) - a 6-digit
code has far less entropy than the original opaque token, so a later
phase's verification logic needs a per-code attempt counter to bound
brute force; nothing anywhere writes to this column yet.

**No other column changes.** `id`, `user_id` (and its FK/`ON DELETE
CASCADE`/index), `token_hash`'s type, `created_at`, `expires_at`, and
`used_at` are completely unchanged - including their meaning: `used_at`
still means exactly what it always has at the schema level ("this row is
no longer a live candidate"); a later, separate phase's own service-layer
design additionally plans to set it when a newer code supersedes an
older, still-unused one (reusing the exact same field, the same way
`RefreshToken.revoked_at` already covers more than one real-world reason
in this codebase), but that is runtime behavior, not something this
migration does or needs to express.

**No row data is touched.** Nothing writes to `password_reset_tokens` in
production yet (confirmed in the design audit - no repository/service/
route uses this table at all), so there is no real historical data at
stake here; `failed_attempts` still gets a `server_default='0'` regardless,
for the same "a migration's correctness shouldn't depend on incidentally-
empty data" reasoning already applied in
`d363f3f9d06c_add_user_failed_login_lockout_tracking.py` for
`User.failed_login_attempts`.

**Discovering the old constraint's real name, not guessing it - and the
SQLite/PostgreSQL divergence - both handled with the exact same technique
already proven in `f3a7c9e2b4d1_replace_pending_notification_unique.py`
for `PendingNotification.discovered_listing_id`'s identical situation**
(an unnamed `UNIQUE` constraint declared inline at table-creation time).
On PostgreSQL, this migration reflects the live schema via
`Inspector.get_unique_constraints()` and looks for the one whose column
set is exactly `['token_hash']`, refusing to proceed (raising, rather than
guessing) if that isn't found exactly once, then drops it by its real,
reflected name. On SQLite, the same constraint reflects with `name: None`
(and is invisible to `get_indexes()` too - implemented as an internal
`sqlite_autoindex_*`) - there is no name to drop it by on that backend, so
`batch_alter_table`'s `copy_from` parameter is used instead, supplying the
table's shape exactly as it existed at revision `f3a7c9e2b4d1` (this
migration's own `down_revision`) but *without* the old unnamed
constraint - SQLite's table-recreation mechanism then simply never
carries it forward, functionally equivalent to "drop it" without needing
a name to reference. `copy_from` only affects SQLite's own recreate
mechanism; on PostgreSQL, `batch_alter_table` issues plain `ALTER TABLE`
statements directly (no recreation), so the reflected real name is used
to drop the constraint there instead - `copy_from` is simply unused on
that path. `copy_from`'s table shape below explicitly includes the
existing `ix_password_reset_tokens_user_id` index too - omitting an
existing index from `copy_from` is exactly the bug class the prior
migration's own docstring documents having been caught and fixed for
`ix_pending_notifications_status` (`copy_from` is the *entire* starting
point SQLite's recreation uses; anything not listed is silently dropped,
not preserved by default).

Verified via the usual upgrade/downgrade/re-upgrade cycle against a
throwaway SQLite database before finalizing, and cross-checked against
`Base.metadata.create_all()` to confirm the resulting constraint/column
shape matches the model exactly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '30fc5cd97dad'
down_revision: Union[str, Sequence[str], None] = 'f3a7c9e2b4d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_UNIQUE_CONSTRAINT_NAME = 'uq_password_reset_tokens_user_id_token_hash'
_RESTORED_SINGLE_COLUMN_UNIQUE_NAME = 'uq_password_reset_tokens_token_hash'


def _current_table_shape_for_sqlite_copy_from() -> sa.Table:
    """`PasswordResetToken` exactly as it exists at revision `f3a7c9e2b4d1`
    (this migration's own `down_revision`) - used only as `batch_alter_
    table`'s `copy_from` on SQLite, only when the old single-column unique
    constraint has no reflectable name (see this module's docstring).
    Deliberately NOT imported from `core/auth/models.py`: migrations must
    describe schema as it concretely was at a specific point in history,
    never drift silently if the live model changes later.

    Declared *without* the old `UNIQUE(token_hash)` - that's the entire
    point of using `copy_from` here instead of letting SQLAlchemy reflect
    the live table (which cannot see that constraint's name at all, but
    would otherwise still try to preserve *some* unique behavior on this
    column via its own reflection quirks). `failed_attempts` and the new
    composite constraint are added afterward via this migration's own
    `batch_op` calls - not baked in here, matching the identical division
    of responsibility already used in `f3a7c9e2b4d1`'s own
    `_current_table_shape_for_sqlite_copy_from()`.
    """
    table = sa.Table(
        'password_reset_tokens',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            name='fk_password_reset_tokens_user_id',
            ondelete='CASCADE',
        ),
    )
    sa.Index('ix_password_reset_tokens_user_id', table.c.user_id)
    return table


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())
    matching = [
        uc
        for uc in inspector.get_unique_constraints('password_reset_tokens')
        if uc['column_names'] == ['token_hash']
    ]
    if len(matching) != 1:
        raise RuntimeError(
            "Expected exactly one UNIQUE(token_hash) constraint on "
            f"password_reset_tokens; found {len(matching)}: {matching!r}. Refusing to "
            "guess which constraint to drop - see this migration's docstring."
        )
    old_constraint_name = matching[0]['name']

    # Only needed when the old constraint has no reflectable name at all
    # (SQLite) - see this module's docstring for exactly why.
    copy_from = None if old_constraint_name is not None else _current_table_shape_for_sqlite_copy_from()

    with op.batch_alter_table('password_reset_tokens', schema=None, copy_from=copy_from) as batch_op:
        if old_constraint_name is not None:
            batch_op.drop_constraint(old_constraint_name, type_='unique')
        batch_op.add_column(sa.Column('failed_attempts', sa.Integer(), nullable=False, server_default='0'))
        batch_op.create_unique_constraint(
            _NEW_UNIQUE_CONSTRAINT_NAME, ['user_id', 'token_hash']
        )


def downgrade() -> None:
    """Downgrade schema.

    **Only safe before any two different users have ever been issued the
    same 6-digit code while both codes were simultaneously live** (not
    yet possible at all - no code exists in this phase - but will become
    possible once a later phase actually starts issuing 6-digit codes).
    Once that happens, the `create_unique_constraint` call below will fail
    outright - PostgreSQL (and SQLite) both validate ALL existing rows
    against a new UNIQUE constraint at creation time, and two such rows
    are, by construction, a genuine violation of `UNIQUE(token_hash)`
    alone. This is expected and intentional: this downgrade must never
    silently delete, merge, or rewrite rows to force success. If this
    fails in that situation, the correct operational response is to roll
    back the *application code* that issues/verifies codes (not built in
    this phase) and leave the schema as this migration's `upgrade()` left
    it, rather than attempting to force this downgrade through - the
    exact same principle already documented in
    `f3a7c9e2b4d1_replace_pending_notification_unique.py`'s own downgrade
    for its analogous composite-constraint cutover.

    The restored single-column constraint is given a new explicit name
    (`uq_password_reset_tokens_token_hash`) rather than attempting to
    reproduce the original's auto-generated PostgreSQL name -
    `create_unique_constraint` requires an explicit name, and the
    original's auto-generated one was never itself part of any
    application-level contract.
    """
    with op.batch_alter_table('password_reset_tokens', schema=None) as batch_op:
        batch_op.drop_constraint(_NEW_UNIQUE_CONSTRAINT_NAME, type_='unique')
        batch_op.create_unique_constraint(
            _RESTORED_SINGLE_COLUMN_UNIQUE_NAME, ['token_hash']
        )
        batch_op.drop_column('failed_attempts')
