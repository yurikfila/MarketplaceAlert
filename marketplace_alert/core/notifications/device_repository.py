"""Raw persistence access for `DeviceToken` - the only module that queries
this table directly, same convention every other repository in this
codebase follows (`UserRepository`, `NotificationPreferenceRepository`).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.notifications.models import DeviceToken


class DeviceTokenRepository:
    """CRUD for `DeviceToken` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, *, user_id: int, expo_push_token: str, platform: str | None) -> DeviceToken:
        """Creates a new row for this token, or updates the existing one
        if the exact same token was already registered - looked up by
        `expo_push_token` alone (it's the unique column), never by
        `(user_id, expo_push_token)`.

        **Ownership transfer, not rejection**, if the token already
        exists under a *different* user - see `DeviceToken`'s own
        docstring for why: a physical device's token belongs to whoever
        is currently signed in and holding it. `user_id` always comes
        from the caller's own authenticated session (`api/v1/devices.py`),
        never accepted as a request parameter - so this method has no way
        to be tricked into reassigning a token to someone other than the
        actual caller.

        Flushes, does not commit - same convention as every other
        repository in this codebase; the caller (a route, via
        `get_db_session`'s auto-commit-on-success) decides the
        transaction boundary.
        """
        now = datetime.now(timezone.utc)
        existing = self._session.execute(
            select(DeviceToken).where(DeviceToken.expo_push_token == expo_push_token)
        ).scalar_one_or_none()
        if existing is None:
            row = DeviceToken(
                user_id=user_id,
                expo_push_token=expo_push_token,
                platform=platform,
                created_at=now,
                last_seen_at=now,
            )
            self._session.add(row)
        else:
            existing.user_id = user_id
            existing.platform = platform
            existing.last_seen_at = now
            row = existing
        self._session.flush()
        return row

    def list_tokens_for_user(self, user_id: int) -> list[str]:
        """Every currently-registered Expo push token for this user - may
        be empty (no device registered yet), one, or more than one (more
        than one device). Used by `core/notifications/outbox.py`'s push
        drain to fan a single notification out to every one of a user's
        active devices."""
        stmt = select(DeviceToken.expo_push_token).where(DeviceToken.user_id == user_id)
        return list(self._session.execute(stmt).scalars().all())

    def delete_for_user(self, *, user_id: int, expo_push_token: str) -> bool:
        """Removes one token, but only if it's actually owned by
        `user_id` - a user can never unregister someone else's device.
        Returns whether a row was actually deleted (idempotent either
        way - the caller, `api/v1/devices.py`'s logout-time best-effort
        unregister, never needs to distinguish "already gone" from "just
        removed").
        """
        row = self._session.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id, DeviceToken.expo_push_token == expo_push_token
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True
