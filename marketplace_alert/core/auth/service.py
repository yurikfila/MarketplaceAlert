"""`AuthService`: signup, login, refresh, logout, access-token validation,
and password-reset request/verification - the one place this codebase's
authentication business logic lives. Built on `core/auth/security.py`
(hashing/JWT/token primitives, never touched directly by anything above
this module) and `core/auth/repository.py` (the only SQL/ORM access).

No FastAPI route, dependency, or ownership/multi-tenancy enforcement uses
this yet (see `core/auth/__init__.py`) - this module is fully usable and
fully tested standalone, ready for a thin route layer to wrap later
without needing to change. This includes `request_password_reset`/
`reset_password` (Phase 2 of the approved 6-digit-code password-reset
design): no `/forgot-password`/`/reset-password` route and no email
provider exist yet - the raw 6-digit code `request_password_reset` hands
back is for a future email-delivery phase to use, never for any HTTP
response this codebase currently produces.

**Per-account lockout, not per-IP, in this phase.** A shared IP (NAT,
office, mobile carrier) locking out every user behind it after one
account gets a few wrong guesses is a worse tradeoff than the brute-force
protection it would add here, given this app's current scale - see
PROJECT_CONTEXT.md's authentication design decision. Revisit if abuse at
real scale ever demonstrates otherwise.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.repository import (
    PasswordResetTokenRepository,
    RefreshTokenRepository,
    UserRepository,
)
from marketplace_alert.core.auth.security import (
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_reset_code,
    generate_token,
    hash_password,
    hash_token,
    verify_password_or_dummy,
)

# Exactly 6 numeric digits - checked at the service boundary as defense in
# depth (there is no request schema/route in this phase to validate this
# shape first). A malformed code is never distinguished from a wrong one -
# both raise the identical `InvalidResetCodeError` below.
_RESET_CODE_PATTERN = re.compile(r"^[0-9]{6}$")


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


class InvalidResetCodeError(Exception):
    """Raised by `reset_password` for every rejection reason that must be
    externally indistinguishable from every other one: a malformed code,
    an unknown email, an inactive account, no live code at all (never
    issued / already used / superseded / expired), attempts already
    exhausted, a wrong code, or losing the atomic-claim race to a
    concurrent request - see `reset_password`'s own docstring for exactly
    which internal check each of these corresponds to. Same "one exception
    type, one message, revealing nothing about *why*" principle as
    `InvalidCredentialsError` above."""


class WeakNewPasswordError(Exception):
    """Raised by `reset_password` when `new_password` is shorter than
    `AuthService._MIN_NEW_PASSWORD_LENGTH` - deliberately NOT folded into
    `InvalidResetCodeError`: unlike every reason that exception covers,
    this reveals nothing about account/code state (the same information
    `SignupRequest`'s own validation already exposes today, non-generically -
    see `api/v1/schemas.py`), and raising it must never consume the code or
    count as a failed attempt."""


@dataclass
class TokenPair:
    """What every successful signup/login/refresh hands back."""

    access_token: str
    refresh_token: str


@dataclass
class PasswordResetRequestResult:
    """What `request_password_reset` hands back - internal only. There is
    no route in this phase at all, and this type must never be returned
    directly by a future one: FastAPI's `jsonable_encoder` serializes a
    plain dataclass automatically (verified directly - it would happily
    put `raw_code` straight into a JSON response body), so a future route
    handler must reshape this into its own explicit response schema, the
    exact same "never blind auto-mapping, always explicit field-by-field
    construction" discipline `api/v1/schemas.py` already documents and
    `api/v1/auth.py` already follows for `UserPublic`/`TokenPairOut` (see
    `_user_public`/`_token_pair_out` there) - never `return result` as-is.

    `should_deliver` is the primary signal any caller should branch on -
    `True` only when `raw_code` is a real, freshly-issued 6-digit code to
    actually email; `False` for every intentionally-suppressed case
    (unknown email, inactive account, resend cooldown, hourly cap already
    reached) with no further distinction between them anywhere in this
    type. A future API layer must return the identical generic "if that
    email is registered, a code has been sent" response regardless of
    `should_deliver`'s value - this field decides whether to actually
    send an email, never what any HTTP caller is told. A future email
    layer must send only when `should_deliver` is `True`.

    **Enforced invariant** (`__post_init__`, not just documented -
    "enforce it where a bug can't bypass it," the same discipline already
    applied to `ix_users_email_lower`/every other dedup guarantee in this
    codebase): `should_deliver=True` requires a real `raw_code`;
    `should_deliver=False` must never carry one. An inconsistent
    construction raises immediately rather than silently propagating a
    confusing or dangerous half-built result.

    `raw_code`, when present, is never logged, never persisted anywhere
    but its hash (`hash_token`), and never part of any exception message.
    """

    should_deliver: bool
    raw_code: str | None

    def __post_init__(self) -> None:
        if self.should_deliver and self.raw_code is None:
            raise ValueError("should_deliver=True requires a real raw_code")
        if not self.should_deliver and self.raw_code is not None:
            raise ValueError("should_deliver=False must never carry a raw_code")


class AuthService:
    """Signup, login, refresh, logout, access-token validation, and
    password-reset request/verification, all scoped to one request's
    `Session` (same lifecycle as `SavedSearchService`) - construct a
    fresh one per request; nothing here is safe to share across
    requests/threads.
    """

    _INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"
    _INVALID_RESET_CODE_MESSAGE = "Invalid or expired verification code"

    # Mirrors `SignupRequest.password`'s existing `Field(min_length=8)`
    # rule (`api/v1/schemas.py`) - not imported from there, since that
    # module depends on this package, not the other way around, and this
    # phase adds no route/schema layer of its own to import from yet. A
    # future route's own schema enforcing the same threshold is expected
    # and harmless redundancy (the schema is the primary, user-friendly
    # gate; this is the last-resort defensive floor for a method that, in
    # this phase, has no schema in front of it at all).
    _MIN_NEW_PASSWORD_LENGTH = 8

    def __init__(
        self,
        session: Session,
        *,
        secret_key: str,
        access_token_expire_minutes: float,
        refresh_token_expire_days: float,
        max_failed_login_attempts: int,
        account_lockout_minutes: float,
        password_reset_token_expire_minutes: float,
        password_reset_max_attempts: int,
        password_reset_resend_cooldown_seconds: float,
        password_reset_max_per_hour: int,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._password_reset_tokens = PasswordResetTokenRepository(session)
        self._secret_key = secret_key
        self._access_token_expire_minutes = access_token_expire_minutes
        self._refresh_token_expire_days = refresh_token_expire_days
        self._max_failed_login_attempts = max_failed_login_attempts
        self._account_lockout_minutes = account_lockout_minutes
        self._password_reset_token_expire_minutes = password_reset_token_expire_minutes
        self._password_reset_max_attempts = password_reset_max_attempts
        self._password_reset_resend_cooldown_seconds = password_reset_resend_cooldown_seconds
        self._password_reset_max_per_hour = password_reset_max_per_hour

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

    def request_password_reset(self, *, email: str) -> PasswordResetRequestResult:
        """Issue a fresh 6-digit verification code for `email`, if and
        only if doing so is currently allowed - never distinguishes any
        of the reasons it might not be (unknown email, inactive account,
        resend cooldown, hourly cap already reached) from one another in
        what it returns; see `PasswordResetRequestResult`'s own docstring
        for the `should_deliver`/`raw_code` contract a future caller must
        honor (and never return this object directly from a route).

        The raw code is returned ONLY for a future email-delivery phase
        to use - never logged, never persisted anywhere but its hash
        (`hash_token`), never part of any exception message. There is no
        API route in this phase; the only caller today is this module's
        own test suite.
        """
        now = datetime.now(timezone.utc)
        user = self._users.get_by_email(email)
        if user is None or not user.is_active:
            return PasswordResetRequestResult(should_deliver=False, raw_code=None)

        most_recent = self._password_reset_tokens.get_most_recent_for_user(user.id)
        if most_recent is not None:
            seconds_since_last_issue = (now - _as_aware_utc(most_recent.created_at)).total_seconds()
            if seconds_since_last_issue < self._password_reset_resend_cooldown_seconds:
                return PasswordResetRequestResult(should_deliver=False, raw_code=None)

        window_start = now - timedelta(hours=1)
        issued_in_window = self._password_reset_tokens.count_issued_since(user.id, since=window_start)
        if issued_in_window >= self._password_reset_max_per_hour:
            return PasswordResetRequestResult(should_deliver=False, raw_code=None)

        raw_code = generate_reset_code()
        expires_at = now + timedelta(minutes=self._password_reset_token_expire_minutes)

        # Supersede before creating - at most one live row per user must
        # exist the instant the new one is committed (see
        # `PasswordResetTokenRepository.supersede_unused_for_user`).
        self._password_reset_tokens.supersede_unused_for_user(user.id, now=now)
        self._password_reset_tokens.create(user_id=user.id, token_hash=hash_token(raw_code), expires_at=expires_at)
        self._session.commit()

        return PasswordResetRequestResult(should_deliver=True, raw_code=raw_code)

    def reset_password(self, *, email: str, code: str, new_password: str) -> None:
        """Redeem a 6-digit verification code, atomically: consumes the
        code, sets the new password, and revokes every refresh token this
        user has - all in one transaction, one commit.

        Raises `InvalidResetCodeError` for every reason a future API must
        treat identically - see that exception's own docstring for the
        full list. Never queries by `token_hash` alone - always resolves
        `email -> user` first (`UserRepository.get_by_email`), then reads
        and compares against *that user's own* live row only (see
        `PasswordResetTokenRepository`'s docstring for why a global
        `token_hash` lookup would be unsafe here).

        Raises `WeakNewPasswordError` - checked only *after* the code
        itself has already been verified correct, and *before* the atomic
        claim - if `new_password` is too short; see that exception's own
        docstring for why this is deliberately not folded into
        `InvalidResetCodeError`, and why it must never consume the code.

        Access tokens already issued before this call remain valid until
        their own natural expiry - an accepted MVP limitation (see the
        design audit) that this method does not attempt to close; only
        refresh tokens are revoked here.
        """
        if not _RESET_CODE_PATTERN.fullmatch(code):
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        now = datetime.now(timezone.utc)
        user = self._users.get_by_email(email)
        if user is None or not user.is_active:
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        live_token = self._password_reset_tokens.get_live_for_user(user.id, now=now)
        if live_token is None:
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        if live_token.failed_attempts >= self._password_reset_max_attempts:
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        if hash_token(code) != live_token.token_hash:
            self._password_reset_tokens.increment_failed_attempts(live_token.id)
            self._session.commit()
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        if len(new_password) < self._MIN_NEW_PASSWORD_LENGTH:
            raise WeakNewPasswordError(
                f"Password must be at least {self._MIN_NEW_PASSWORD_LENGTH} characters."
            )

        claimed = self._password_reset_tokens.claim_for_reset(
            live_token.id, now=now, max_attempts=self._password_reset_max_attempts
        )
        if not claimed:
            # Lost the race (or expired/hit its attempt ceiling in the
            # instant between being read and being claimed) - never
            # distinguished from any other rejection reason.
            raise InvalidResetCodeError(self._INVALID_RESET_CODE_MESSAGE)

        self._users.set_password_hash(user, hash_password(new_password))
        self._refresh_tokens.revoke_all_for_user(user.id)
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
