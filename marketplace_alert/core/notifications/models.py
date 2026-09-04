"""SQLAlchemy model for per-user notification delivery preferences.

Deliberately its own table, not columns on `core.auth.models.User` - that
model is documented as intentionally minimal (identity/auth only, "no
profile fields... deferred rather than pre-added speculatively" - see its
own docstring), and a delivery destination is exactly that kind of field,
not an identity concern. Same reasoning that already keeps `RefreshToken`/
`PasswordResetToken` in their own tables instead of columns on `User`.

Kept as narrow as the actual need today - one nullable `telegram_chat_id`
column, not a generic multi-channel table - since Telegram is the only
channel that exists. A future second channel (e.g. mobile push) can add
its own column here (still a strict 1:1-per-user shape) or, if the shape
genuinely stops fitting, prompt a proper redesign then - not speculated
on now.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
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
