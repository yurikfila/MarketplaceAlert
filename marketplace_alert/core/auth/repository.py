"""Raw persistence access for authentication: the only module that issues
SQL/ORM queries against `User`/`RefreshToken` - same rule every other
repository in this codebase follows (`ListingRepository`,
`SavedSearchRepository`, `NotificationOutboxRepository`). `core/auth/
service.py` orchestrates business logic on top of these; nothing above
that talks to SQLAlchemy directly.

Neither repository commits - flush only, same convention as
`NotificationOutboxRepository`: the caller (`AuthService`) decides
transaction boundaries, since a single service method (e.g. signup, or
refresh-token rotation) often needs more than one repository call to
succeed or fail together atomically.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from marketplace_alert.core.auth.models import RefreshToken, User


def normalize_email(email: str) -> str:
    """Strip + lowercase - the one normalization rule every email must
    pass through before it's ever inserted or looked up. Defined here
    (not duplicated in `AuthService`) so there is exactly one place this
    rule lives, even though `UserRepository` also defends the lookup side
    independently via `func.lower()` (see `get_by_email`) - belt and
    braces, matching the database's own case-insensitive index doing the
    same for inserts.
    """
    return email.strip().lower()


class UserRepository:
    """Persistence operations for `User` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, email: str, password_hash: str) -> User:
        """Insert a new user. `email` is normalized here - the one place
        every insert path (signup, and nothing else yet) passes through,
        so a caller can never forget to. Does not catch the `UNIQUE`
        index's `IntegrityError` on a duplicate - `AuthService.signup`
        decides how to turn that into a clean, user-facing error; this
        method's only job is the insert itself.
        """
        user = User(email=normalize_email(email), password_hash=password_hash)
        self._session.add(user)
        self._session.flush()
        return user

    def get_by_email(self, email: str) -> User | None:
        """Case-insensitive by construction - `func.lower()` on both
        sides, not just an assumption that every stored value is already
        normalized (defense in depth, matching the database index's own
        `lower(email)` expression - this is also the query shape that
        expression index actually accelerates)."""
        normalized = normalize_email(email)
        stmt = select(User).where(func.lower(User.email) == normalized)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_by_id(self, user_id: int) -> User | None:
        return self._session.get(User, user_id)

    def record_failed_login_attempt(self, user: User, *, max_attempts: int, lockout_minutes: float) -> None:
        """Increments the failed-attempt counter; locks the account (sets
        `locked_until`) once `max_attempts` is reached. Same "small
        threshold decision made right here" pattern already used by
        `NotificationOutboxRepository.complete()` for `attempt_count` vs
        `max_attempts`."""
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= max_attempts:
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)
        user.updated_at = datetime.now(timezone.utc)
        self._session.flush()

    def reset_failed_login_state(self, user: User) -> None:
        """Clears the failed-attempt counter and any lock - called both
        after a successful login, and lazily when a previously-set lock
        has already expired (see `AuthService.login`'s docstring for why
        there's no separate scheduled sweep)."""
        user.failed_login_attempts = 0
        user.locked_until = None
        user.updated_at = datetime.now(timezone.utc)
        self._session.flush()


class RefreshTokenRepository:
    """Persistence operations for `RefreshToken` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, user_id: int, token_hash: str, expires_at: datetime) -> RefreshToken:
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(token)
        self._session.flush()
        return token

    def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return self._session.execute(stmt).scalar_one_or_none()

    def revoke(self, token: RefreshToken) -> None:
        """Idempotent - revoking an already-revoked token leaves its
        original `revoked_at` untouched rather than overwriting it with a
        later timestamp, so it still accurately answers "when did this
        first become invalid" if that's ever inspected."""
        if token.revoked_at is None:
            token.revoked_at = datetime.now(timezone.utc)
            self._session.flush()

    def revoke_all_for_user(self, user_id: int) -> None:
        """The reuse-detection response: kill every still-valid refresh
        token this user has, in one statement - not a load-then-loop,
        since a compromised account could plausibly have many. Only
        touches rows not already revoked, for the same "don't overwrite
        the original revocation time" reason as `revoke()`."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        self._session.execute(stmt)
        self._session.flush()
