"""SQLAlchemy models for per-user notification delivery: `NotificationPreference`
(Telegram) and `DeviceToken` (Expo push - Phase 1 of native mobile push).

Both deliberately their own tables, not columns on `core.auth.models.User` -
that model is documented as intentionally minimal (identity/auth only, "no
profile fields... deferred rather than pre-added speculatively" - see its
own docstring), and a delivery destination is exactly that kind of field,
not an identity concern. Same reasoning that already keeps `RefreshToken`/
`PasswordResetToken` in their own tables instead of columns on `User`.

`NotificationPreference` is kept as narrow as the actual need - one
nullable `telegram_chat_id` column, a strict 1:1-per-user shape - since
Telegram is a single, at-most-one-destination channel. Push turned out
**not** to fit that same shape (a user can have more than one device), so
it deliberately did not become a second column here - see `DeviceToken`'s
own docstring below for why it's a separate, one-row-per-token table
instead.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketplace_alert.core.persistence.database import Base


class NotificationPreference(Base):
    """One user's notification delivery preference. Absence of a row (not
    a nullable column on `User`) means "not configured yet" - the natural
    default, never assumed to mean "use some global default instead" (see
    `core/notifications/outbox.py`'s module docstring "SECURITY RULE")."""

    __tablename__ = "notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # UNIQUE - strictly one preference row per user (never a nullable
    # column on User - see this module's docstring). Indexed for the same
    # reason `SavedSearch.user_id` is: "look up this user's own row" is a
    # known, foreseeable access pattern from day one.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_notification_preferences_user_id"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Nullable - a user can have a preference row (e.g. created by the
    # one-time production backfill, or by their own PUT) with no chat id
    # set (yet), or can explicitly clear it. Never defaulted to a global
    # value anywhere this column is read - see outbox.py's security rule.
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Bumped explicitly by the repository's upsert - same "no DB-level
    # auto-update trigger" convention as SavedSearch.updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class DeviceToken(Base):
    """One registered Expo push token - native mobile push, Phase 1.

    One row **per token**, not per user - `user_id` is a plain indexed
    FK, deliberately not unique, so a user can have more than one
    registered device (a second phone, a reinstall that got a fresh
    token before the old one expired) and receive push on all of them.
    `expo_push_token` itself is the unique column: re-registering the
    same token (e.g. on every app launch) is an idempotent upsert, never
    a duplicate row - see `core/notifications/device_repository.py:
    DeviceTokenRepository.upsert`.

    **Ownership transfer, not rejection, on re-registration under a
    different user.** If the same physical token is later registered by
    a *different* authenticated user (a shared/re-logged-in device),
    `upsert` re-points `user_id` to the new caller rather than leaving it
    pointed at the previous owner - a device token belongs to whoever is
    currently signed in and holding it, never to whoever registered it
    first. Never accepted as an admin-suppliable value; always derived
    from the caller's own authenticated session (`get_current_user`) -
    see `api/v1/devices.py`.
    """

    __tablename__ = "device_tokens"
    __table_args__ = (UniqueConstraint("expo_push_token", name="uq_device_tokens_expo_push_token"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_device_tokens_user_id"),
        nullable=False,
        index=True,
    )

    expo_push_token: Mapped[str] = mapped_column(String, nullable=False)

    # Informational only ("ios"/"android") - nothing in this codebase
    # branches on it yet; kept for future filtering/debugging.
    platform: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Bumped on every re-registration (app launch, re-login) - lets a
    # future cleanup job identify tokens not seen in a long time, without
    # needing a push delivery failure to detect staleness. Nothing reads
    # this yet in Phase 1.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
