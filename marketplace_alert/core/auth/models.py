"""SQLAlchemy models for authentication: user accounts, and the two kinds
of single-purpose, hashed, expiring tokens auth issues.

See this package's `__init__.py` for what's built so far. `core/auth/
repository.py` (added in Phase 2) is the only module that queries these
directly - see that module and `core/auth/service.py` for the actual
signup/login/refresh business logic built on top of this schema.

**Nothing is ever stored in cleartext that doesn't have to be.**
`User.password_hash` is a password hash (bcrypt, once Phase 2 adds the
hashing code) - never the password itself. `RefreshToken.token_hash` and
`PasswordResetToken.token_hash` are hashes of the actual bearer token -
never the raw token - for the same reason `DiscoveredListing`/config
secrets are never logged: a database leak alone must not hand out a
directly usable credential. Same discipline this codebase already applies
elsewhere (the Telegram bot token, database passwords), extended here to
the tokens this package owns.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from marketplace_alert.core.persistence.database import Base


class User(Base):
    """One account. Deliberately minimal in Phase 1 - just enough for a
    future login to identify who's who; no profile fields, no billing
    fields (see PROJECT_CONTEXT.md's authentication design decision for
    why those are deferred rather than pre-added speculatively).

    `email` must still be normalized (lowercased, stripped) by the caller
    before every insert and lookup - that's a later phase's
    `UserRepository`'s job, and nothing in this codebase writes a `User`
    row yet. But the database no longer just trusts that normalization
    happened: `ix_users_email_lower`, a `UNIQUE` index on `lower(email)`
    (`__table_args__` below), rejects `user@example.com` and
    `USER@example.com` coexisting even if a caller ever failed to
    normalize - the same "don't just trust the layer above, enforce it
    where a bug can't bypass it" reasoning already applied to every other
    dedup guarantee in this codebase (e.g.
    `PendingNotification.discovered_listing_id`). A plain `UNIQUE(email)`
    constraint would be strictly weaker (case-sensitive) and fully
    redundant once this exists - see this table's own reasoning; there is
    deliberately no separate `unique=True` on the column itself.
    """

    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # No column-level `unique=True` here - see the class docstring.
    # `ix_users_email_lower` is the real "no two accounts share an email,
    # regardless of case" guarantee, enforced by the database. It also
    # doubles as the lookup index a future `UserRepository.get_by_email()`
    # needs (`WHERE lower(email) = :normalized_email` matches this same
    # indexed expression) - one index serves both purposes.
    email: Mapped[str] = mapped_column(String, nullable=False)

    password_hash: Mapped[str] = mapped_column(String, nullable=False)

    # Reserved for a future admin-disable / email-verification-gating
    # mechanism - not enforced anywhere yet (no login code exists in this
    # phase), but every account needs a defined value from day one rather
    # than treating "active" as an implicit default no row can ever
    # contradict.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Brute-force protection (Phase 2 - see core/auth/service.py) ---
    #
    # Incremented on every wrong-password login attempt against this
    # account; reset to 0 on a successful login. `locked_until` is set
    # once `failed_login_attempts` reaches `settings.max_failed_login_
    # attempts` - while it's set and still in the future, login (and
    # refresh - see AuthService) is rejected outright, password
    # correctness never even checked. No background sweep clears an
    # expired lock: the *next* login attempt after `locked_until` has
    # passed lazily resets both fields and proceeds normally - the same
    # "reclaim on next read, no scheduled job" pattern already used for
    # the notification outbox's claim lease (core/notifications/outbox.py).
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    """One issued refresh token. Opaque and long-lived on the client side,
    but the source of truth for whether it's still valid lives entirely
    here - this is what makes revocation and rotation-with-reuse-detection
    possible at all (a bare JWT refresh token, verified only by signature,
    could not be revoked short of a blocklist).

    Rotation/reuse-detection/revocation logic lives in `core/auth/
    service.py`'s `AuthService.refresh()`/`logout()`. `revoked_at` set
    means "no longer valid, for any reason" (used up via rotation,
    explicitly logged out, or defensively revoked as part of reuse
    detection) - all the same state from a verification standpoint.
    **A token is never marked `revoked_at` just because `expires_at` has
    passed** - naturally-expired-but-never-revoked and
    explicitly-revoked are deliberately different states: presenting an
    expired token is rejected as "expired" and nothing more; presenting
    an already-*revoked* token is what triggers reuse detection
    (revoking every refresh token this user has) - a legitimate client
    never re-presents a token it already exchanged or logged out with,
    so only that second case is treated as a compromise signal.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Indexed - "look up/revoke every refresh token for this user" (logout
    # from all devices, reuse-detection revoking a whole token family, or
    # a future account deletion cascading here) is a known, foreseeable
    # access pattern from day one, same reasoning as
    # `PendingNotification.status` being indexed from its first migration.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_refresh_tokens_user_id"),
        nullable=False,
        index=True,
    )

    # A hash of the actual opaque bearer token, never the raw value - see
    # this module's docstring. `UNIQUE` because it's also the lookup key a
    # refresh/logout request is verified by; a collision would mean two
    # different tokens hash to the same value, which must never be treated
    # as "the same token."
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # No default - always set explicitly at issuance time, based on
    # `settings.refresh_token_expire_days` (a later phase's job).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # None = still valid. Set (to "now", by a later phase) on logout, on
    # rotation (the old token is revoked the moment it's exchanged for a
    # new one), or defensively on reuse-detection (revoking an entire
    # token family after a already-revoked token is presented again).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PasswordResetToken(Base):
    """One issued password-reset token - single-use, short-lived, and (per
    the approved authentication design) not wired to any email-delivery
    mechanism yet. This table exists now so the token/expiry/single-use
    model can be built and tested ahead of that, without a later schema
    change once email delivery is added.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_password_reset_tokens_user_id"),
        nullable=False,
        index=True,
    )

    # A hash of the actual token that would go in a reset link/email,
    # never the raw value - see this module's docstring. `UNIQUE` for the
    # same reason as `RefreshToken.token_hash`.
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # No default - always set explicitly at issuance time, based on
    # `settings.password_reset_token_expire_minutes`.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # None = not yet used. Set (to "now") the moment a reset is completed
    # with this token - single-use is enforced by checking this field, not
    # by deleting the row (keeping it is a harmless audit trail: "someone
    # did reset this account's password on this date").
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
