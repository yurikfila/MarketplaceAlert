"""`AuthService`: signup, login, refresh, logout, and access-token
validation - the one place this codebase's authentication business logic
lives. Built on `core/auth/security.py` (hashing/JWT/token primitives,
never touched directly by anything above this module) and `core/auth/
repository.py` (the only SQL/ORM access).

No FastAPI route, dependency, or ownership/multi-tenancy enforcement uses
this yet (see `core/auth/__init__.py`) - this module is fully usable and
fully tested standalone, ready for a thin route layer to wrap later
without needing to change.

**Per-account lockout, not per-IP, in this phase.** A shared IP (NAT,
office, mobile carrier) locking out every user behind it after one
account gets a few wrong guesses is a worse tradeoff than the brute-force
protection it would add here, given this app's current scale - see
PROJECT_CONTEXT.md's authentication design decision. Revisit if abuse at
real scale ever demonstrates otherwise.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.repository import RefreshTokenRepository, UserRepository
from marketplace_alert.core.auth.security import (
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password_or_dummy,
)


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; every stored value is UTC
    regardless - same helper, same reasoning, as
    `core/saved_searches/repository.py`'s `_as_aware_utc`. Every
    DB-loaded datetime compared against `datetime.now(timezone.utc)`
    below goes through this first, or the comparison itself raises
    `TypeError` on SQLite (confirmed directly - PostgreSQL's `timestamptz`
    doesn't have this problem, but this codebase's tests, and local dev,
    run on SQLite)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class EmailAlreadyRegisteredError(Exception):
    """Raised by `signup` when the (normalized) email is already in use."""


class InvalidCredentialsError(Exception):
    """Raised by `login` for every rejection reason that must be
    externally indistinguishable from every other one: no such account,
    wrong password, an inactive account, and a currently-locked account
    all raise exactly this, with exactly the same message, and (as far as
    practical) the same timing profile - see `login`'s docstring. There is
    deliberately no separate `AccountLockedError`/inactive-specific
    exception from `login` anymore: revealing *why* a login was rejected
    was judged, on review, to be its own account-enumeration channel (an
    attacker who suspects an email exists could confirm it by driving
    enough wrong guesses to observe a state change in the response) and
    was closed before any HTTP route gets built on top of this contract.
    `login`'s caller learns only "invalid credentials" - full stop.
    """


class InactiveAccountError(Exception):
    """Raised by `refresh` (only - `login` no longer distinguishes this,
    see `InvalidCredentialsError`) for a real, found account with
    `is_active=False`. `refresh` operates on an opaque, already-issued
    bearer token, not an attacker-guessable email - the account-
    enumeration concern that drove `login`'s uniform-failure redesign
    doesn't apply the same way here, so this remains its own exception
    type rather than being folded into `InvalidRefreshTokenError`."""


class InvalidRefreshTokenError(Exception):
    """Raised by `refresh`/`logout` for a token this database has no
    record of at all (never issued, or garbage)."""


class ExpiredRefreshTokenError(Exception):
    """Raised by `refresh` for a token that is real, not revoked, but
    past `expires_at`. Never itself triggers reuse detection - see
    `RefreshToken`'s model docstring for why expiry and revocation are
    kept as distinct states."""


class RefreshTokenReusedError(Exception):
    """Raised by `refresh` when the presented token was already revoked -
    a legitimate client never re-presents a token it already rotated away
    from or logged out with, so this is treated as a compromise signal:
    every refresh token this user has is revoked as a side effect of
    raising this."""


@dataclass
class TokenPair:
    """What every successful signup/login/refresh hands back."""

    access_token: str
    refresh_token: str


class AuthService:
    """Signup, login, refresh, logout, and access-token validation, all
    scoped to one request's `Session` (same lifecycle as
    `SavedSearchService`) - construct a fresh one per request; nothing
    here is safe to share across requests/threads.
    """

    _INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"

    def __init__(
        self,
        session: Session,
        *,
        secret_key: str,
        access_token_expire_minutes: float,
        refresh_token_expire_days: float,
        max_failed_login_attempts: int,
        account_lockout_minutes: float,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._secret_key = secret_key
        self._access_token_expire_minutes = access_token_expire_minutes
        self._refresh_token_expire_days = refresh_token_expire_days
        self._max_failed_login_attempts = max_failed_login_attempts
        self._account_lockout_minutes = account_lockout_minutes

    def signup(self, *, email: str, password: str) -> tuple[User, TokenPair]:
        """Create a new account and immediately issue a token pair
        (auto-login - no separate login round-trip needed after signup).

        Atomic: the new `User` row, and its first `RefreshToken` row,
        commit together in one transaction - either both exist or
        neither does. A duplicate (normalized) email raises
        `EmailAlreadyRegisteredError` - detected via the database's own
        `UNIQUE` index at flush time (`UserRepository.create`), not a
        separate check-then-insert (which would leave a race window under
        concurrent signups for the same email); the session is rolled
        back before raising, so it's immediately safe to reuse.
        """
        password_hash = hash_password(password)
        try:
            user = self._users.create(email=email, password_hash=password_hash)
        except IntegrityError:
            self._session.rollback()
            raise EmailAlreadyRegisteredError("This email is already registered") from None

        tokens = self._issue_token_pair(user.id)
        self._session.commit()
        return user, tokens

    def login(self, *, email: str, password: str) -> tuple[User, TokenPair]:
        """Verify credentials and issue a fresh token pair.

        **Every externally observable rejection reason collapses to the
        same `InvalidCredentialsError`, same message.** No such account,
        wrong password, an inactive account, and a currently-locked
        account are handled by four different code paths internally (the
        database still tracks exactly which one happened, and why - see
        `User.failed_login_attempts`/`locked_until`), but none of that
        distinction ever reaches this method's caller. `login` either
        returns a fresh `(User, TokenPair)` or raises this one exception -
        nothing in between, nothing more specific.

        **Timing, as far as practical, is kept consistent across the
        rejection paths too**: every path that doesn't already involve a
        real password verification performs one anyway
        (`verify_password_or_dummy`, which itself always does real bcrypt
        work even with no real hash to check against - see that
        function's docstring) before rejecting, specifically so "this
        account is locked/inactive" can't be inferred from an early,
        cheap return that skips hashing entirely.

        **A currently-locked account's lockout state is never touched by
        an attempt made while still locked** - not extended, not reset,
        regardless of whether the password supplied was actually correct.
        Only a *stale* lock (`locked_until` already in the past) is reset,
        lazily, in the same request that discovers it - no scheduled
        sweep job exists or is needed, matching the notification outbox's
        own claim-lease pattern. A wrong password against a *not*
        currently-locked account still increments the failed-attempt
        counter and can newly lock it, exactly as before.

        **An inactive account never receives tokens**, regardless of
        password correctness - checked before any token would be issued,
        not as an afterthought.
        """
        user = self._users.get_by_email(email)
        if user is None:
            verify_password_or_dummy(password, None)
            raise InvalidCredentialsError(self._INVALID_CREDENTIALS_MESSAGE)

        now = datetime.now(timezone.utc)
        currently_locked = user.locked_until is not None and _as_aware_utc(user.locked_until) > now

        if not user.is_active or currently_locked:
            # Real hash, real bcrypt work either way - the result is
            # deliberately discarded: neither an inactive account nor a
            # still-locked one may ever succeed here, and (per this
            # method's docstring) lockout state must never be mutated by
            # an attempt made while still locked, whether the password
            # given was right or wrong.
            verify_password_or_dummy(password, user.password_hash)
            raise InvalidCredentialsError(self._INVALID_CREDENTIALS_MESSAGE)

        if user.locked_until is not None:
            # Only reachable once the lock has already expired (the
            # combined check above already handled "still locked") -
            # lazy reset before proceeding to a normal password check.
            self._users.reset_failed_login_state(user)

        if not verify_password_or_dummy(password, user.password_hash):
            self._users.record_failed_login_attempt(
                user,
                max_attempts=self._max_failed_login_attempts,
                lockout_minutes=self._account_lockout_minutes,
            )
            self._session.commit()
            raise InvalidCredentialsError(self._INVALID_CREDENTIALS_MESSAGE)

        self._users.reset_failed_login_state(user)
        tokens = self._issue_token_pair(user.id)
        self._session.commit()
        return user, tokens

    def refresh(self, raw_refresh_token: str) -> TokenPair:
        """Rotate a refresh token: verify it, revoke it, issue a new pair.

        Order matters and is deliberate: the presented token is revoked
        (`RefreshTokenRepository.revoke`) *before* `_issue_token_pair`
        creates its replacement, and both happen in the same transaction,
        committed once - there is no window in which both the old and new
        token could be independently valid.

        A token this database has no record of raises
        `InvalidRefreshTokenError`. A token that's real but already
        revoked raises `RefreshTokenReusedError` - and, as a side effect,
        revokes every refresh token this user currently has (see that
        exception's docstring). A token that's real, unrevoked, but past
        `expires_at` raises `ExpiredRefreshTokenError` - ordinary
        staleness, never treated as reuse.
        """
        token_hash = hash_token(raw_refresh_token)
        stored = self._refresh_tokens.get_by_token_hash(token_hash)

        if stored is None:
            raise InvalidRefreshTokenError("Refresh token not recognized")

        if stored.revoked_at is not None:
            self._refresh_tokens.revoke_all_for_user(stored.user_id)
            self._session.commit()
            raise RefreshTokenReusedError("Refresh token has already been used")

        if _as_aware_utc(stored.expires_at) <= datetime.now(timezone.utc):
            raise ExpiredRefreshTokenError("Refresh token has expired")

        user = self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise InactiveAccountError("This account is inactive")

        self._refresh_tokens.revoke(stored)
        tokens = self._issue_token_pair(user.id)
        self._session.commit()
        return tokens

    def logout(self, raw_refresh_token: str) -> None:
        """Revoke one refresh token. Idempotent and never raises - an
        already-revoked or never-issued token is treated the same as a
        successful logout, since the end state (this token cannot be used
        again) is identical either way, and logout must never leak
        whether a token it was handed was ever valid."""
        token_hash = hash_token(raw_refresh_token)
        stored = self._refresh_tokens.get_by_token_hash(token_hash)
        if stored is not None:
            self._refresh_tokens.revoke(stored)
        self._session.commit()

    def get_current_user(self, access_token: str) -> User:
        """Decode an access token and return the account it names.

        Folds "token is cryptographically invalid" and "token is valid
        but the account it names is gone/inactive" into the same
        `InvalidAccessTokenError` - both mean the same thing to a caller
        (this bearer credential cannot be trusted right now), and this is
        exactly the one exception type a future FastAPI dependency needs
        to catch.
        """
        user_id = decode_access_token(access_token, secret_key=self._secret_key)
        user = self._users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise InvalidAccessTokenError("Token does not name an active account")
        return user

    def _issue_token_pair(self, user_id: int) -> TokenPair:
        access_token = create_access_token(
            user_id, secret_key=self._secret_key, expire_minutes=self._access_token_expire_minutes
        )
        raw_refresh_token = generate_token()
        expires_at = datetime.now(timezone.utc) + timedelta(days=self._refresh_token_expire_days)
        self._refresh_tokens.create(user_id=user_id, token_hash=hash_token(raw_refresh_token), expires_at=expires_at)
        return TokenPair(access_token=access_token, refresh_token=raw_refresh_token)
