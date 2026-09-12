"""Read-only aggregate queries for the admin user-management API
(`api/v1/admin.py`). Deliberately its own module, not folded into
`core/auth/repository.py` (scoped to `User`/`RefreshToken`/
`PasswordResetToken` persistence only) or `core/saved_searches/
repository.py` (scoped to `SavedSearch` persistence only) - this
module's whole purpose is to join across both domains for one reporting
view, which neither of those modules should take on as a new
responsibility.

**Every field `AdminUserRow` exposes is already safe to show the
application owner.** This module never selects `password_hash`,
`token_hash`, `failed_login_attempts`, `locked_until`, or any row from
`refresh_tokens`/`password_reset_tokens` - it has no reason to, and
`api/v1/schemas.py:AdminUserOut` is built field-by-field from
`AdminUserRow` only (never `from_attributes` on a raw `User`), so a new,
more sensitive `User` column added later cannot silently start being
serialized through this API either.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.saved_searches.models import SavedSearch


@dataclass
class AdminUserRow:
    """One row of `GET /api/v1/admin/users` - deliberately just these six
    fields, the same discipline `UserPublic` already applies to `/me`.
    Never add a field here without also checking that it's safe to show
    the application owner and updating `api/v1/admin.py`'s own
    serialization tests."""

    id: int
    email: str
    created_at: datetime
    is_admin: bool
    is_active: bool
    saved_search_count: int


class AdminRepository:
    """Read-only, scoped to one session - same convention as every other
    repository in this codebase (`UserRepository`, `SavedSearchRepository`).
    Never flushes or commits - there is nothing here to write."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_users(self) -> list[AdminUserRow]:
        """One row per registered user, oldest first (matches
        `SavedSearchRepository.list_all`'s own `ORDER BY id` convention),
        each carrying how many saved searches that user currently owns.
        A single grouped `LEFT JOIN` - not one query per user - so this
        stays cheap regardless of how many accounts exist. A user with
        zero saved searches still gets a row here (`LEFT JOIN`, not an
        inner join) with `saved_search_count=0`, via `func.count` over the
        joined `SavedSearch.id` (counts actual matched rows, not `User.id`,
        so an unmatched left-join row correctly counts as zero rather than
        one)."""
        stmt = (
            select(User, func.count(SavedSearch.id))
            .outerjoin(SavedSearch, SavedSearch.user_id == User.id)
            .group_by(User.id)
            .order_by(User.id)
        )
        rows = self._session.execute(stmt).all()
        return [
            AdminUserRow(
                id=user.id,
                email=user.email,
                created_at=user.created_at,
                is_admin=user.is_admin,
                is_active=user.is_active,
                saved_search_count=saved_search_count,
            )
            for user, saved_search_count in rows
        ]

    def count_users(self) -> int:
        return self._session.execute(select(func.count(User.id))).scalar_one()

    def count_saved_searches(self) -> int:
        """Every saved search, owned or not (unowned rows predating
        multi-tenancy - see `SavedSearch.user_id`'s own docstring - still
        count; this is a total across the whole table, not a sum of the
        per-user counts above, which would silently exclude them)."""
        return self._session.execute(select(func.count(SavedSearch.id))).scalar_one()
