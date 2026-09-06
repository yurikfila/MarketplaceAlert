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

from marketplace_alert.core.auth.models import PasswordResetToken, RefreshToken, User


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

    def set_password_hash(self, user: User, password_hash: str) -> None:
        """Overwrites the stored password hash - the one reusable "set/
        change password" operation this repository was missing (used by
        `AuthService.reset_password`). Bumps `updated_at`, same as every
        other User-mutating method here; never touches
        `failed_login_attempts`/`locked_until` - a password reset and a
        login-lockout reset are independent concerns, not something this
        method should conflate."""
        user.password_hash = password_hash
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


class PasswordResetTokenRepository:
    """Persistence operations for `PasswordResetToken` rows, scoped to one
    session - same "raw SQL/ORM access only, caller (`AuthService`)
    decides transaction boundaries" convention as `UserRepository`/
    `RefreshTokenRepository` above.

    **Every lookup here is scoped by `user_id`, never by `token_hash`
    alone** - see `PasswordResetToken`'s own class docstring for why a
    global `token_hash` lookup is unsafe for a 6-digit code (two different
    users can legitimately share one). `AuthService` always resolves
    `email -> user` first (via `UserRepository.get_by_email`), then calls
    into this repository with that user's `id` - nothing here accepts a
    raw code or a bare `token_hash` to search by, on purpose.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, *, user_id: int, token_hash: str, expires_at: datetime) -> PasswordResetToken:
        token = PasswordResetToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(token)
        self._session.flush()
        return token

    def get_live_for_user(self, user_id: int, *, now: datetime) -> PasswordResetToken | None:
        """The one row (if any) that's still a usable candidate for this
        user right now - unused (`used_at IS NULL`) and unexpired.
        Supersession-on-issue (`supersede_unused_for_user`, always called
        before `create()` - see `AuthService.request_password_reset`) is
        what keeps this to at most one row in the ordinary case; `.first()`
        over `created_at` descending is used rather than a strict
        "exactly one or none" query so a theoretical concurrent-issuance
        race (two `request_password_reset` calls for the same user landing
        at the exact same instant - a narrow window this phase doesn't add
        explicit locking for) degrades to "the newest one wins," never a
        hard crash over an edge case this rare.
        """
        stmt = (
            select(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .order_by(PasswordResetToken.created_at.desc())
        )
        return self._session.execute(stmt).scalars().first()

    def get_most_recent_for_user(self, user_id: int) -> PasswordResetToken | None:
        """The most recently issued row for this user, regardless of
        whether it's still live - what the resend cooldown
        (`settings.password_reset_resend_cooldown_seconds`) is measured
        against. A superseded or already-used code's issuance still
        counts as "the last time a code was requested" - `get_live_for_
        user` above would miss it entirely once it's no longer live,
        which is exactly why this is a separate query, not a reuse of
        that one.
        """
        stmt = (
            select(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id)
            .order_by(PasswordResetToken.created_at.desc())
        )
        return self._session.execute(stmt).scalars().first()

    def count_issued_since(self, user_id: int, *, since: datetime) -> int:
        """How many codes have been issued to this user since `since` -
        every issuance counts toward `settings.password_reset_max_per_
        hour`, live or not (the hourly cap bounds request/email volume,
        not how many codes are still usable right now)."""
        stmt = select(func.count(PasswordResetToken.id)).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.created_at >= since,
        )
        return self._session.execute(stmt).scalar_one()

    def supersede_unused_for_user(self, user_id: int, *, now: datetime) -> None:
        """Invalidates every still-unused row for this user by setting
        `used_at` - the exact same "no longer valid, for any reason"
        pattern already used by `RefreshTokenRepository.revoke_all_for_
        user` above, applied here so at most one live row ever exists per
        user immediately after a new one is issued. Never deletes a row -
        kept as a harmless audit trail, same reasoning as
        `PasswordResetToken.used_at`'s own docstring."""
        stmt = (
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
            .values(used_at=now)
        )
        self._session.execute(stmt)
        self._session.flush()

    def increment_failed_attempts(self, token_id: int) -> None:
        """A single atomic `SET failed_attempts = failed_attempts + 1` -
        safe under concurrent wrong-code submissions with no lost-update
        risk, since the increment is evaluated by the database against
        the current committed value at write time, not a value read
        earlier in Python. A benign race could let this overshoot the
        configured maximum by one or two under heavy concurrent guessing -
        harmless, since only "is it >= the maximum" is ever checked, never
        the exact value."""
        stmt = (
            update(PasswordResetToken)
            .where(PasswordResetToken.id == token_id)
            .values(failed_attempts=PasswordResetToken.failed_attempts + 1)
        )
        self._session.execute(stmt)
        self._session.flush()

    def claim_for_reset(self, token_id: int, *, now: datetime, max_attempts: int) -> bool:
        """Atomically marks this row used - returns `True` only if THIS
        call is the one that actually claimed it. A single conditional
        `UPDATE`, not a `SELECT ... FOR UPDATE` first: the database
        serializes concurrent claims against the same row, and only one
        can ever see its own `WHERE` clause still match (`used_at IS
        NULL`) once the winner's write has landed - the same "optimistic
        claim, check rowcount" idiom this codebase's notification outbox
        already uses for its own conditional updates, just for a single
        contested row here rather than a shared pool. Re-checks `expires_
        at`/`failed_attempts` in the same statement so a code that expired
        or hit its attempt ceiling in the instant between being read and
        being claimed can never still succeed.

        `synchronize_session=False`: this statement's `WHERE` clause
        compares `expires_at` (a real column) against `now` (an aware
        Python `datetime`) - SQLAlchemy's default "evaluate" synchronize
        strategy tries to re-check that comparison in Python against any
        already-loaded `PasswordResetToken` instance in this session's
        identity map (e.g. the very row `AuthService.reset_password` just
        read via `get_live_for_user`), and SQLite drops tzinfo on
        round-trip - comparing that naive, already-loaded value against
        the aware `now` raises `TypeError: can't compare offset-naive and
        offset-aware datetimes` (confirmed directly). The `UPDATE` itself
        is unaffected either way - only in-memory object synchronization
        is skipped - and nothing here relies on the loaded object
        reflecting the new `used_at` without an explicit reload.
        """
        stmt = (
            update(PasswordResetToken)
            .where(
                PasswordResetToken.id == token_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
                PasswordResetToken.failed_attempts < max_attempts,
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(stmt)
        self._session.flush()
        return result.rowcount == 1
