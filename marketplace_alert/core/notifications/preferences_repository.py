"""Raw persistence access for `NotificationPreference` - the only module
that queries this table directly, same convention every other repository
in this codebase follows.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.notifications.models import NotificationPreference


class NotificationPreferenceRepository:
    """CRUD for one user's own `NotificationPreference` row, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_user_id(self, user_id: int) -> NotificationPreference | None:
        stmt = select(NotificationPreference).where(NotificationPreference.user_id == user_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_telegram_chat_id(self, user_id: int, telegram_chat_id: str | None) -> NotificationPreference:
        """Creates the row if this user has none yet, otherwise updates the
        existing one - never a second row (the `UNIQUE(user_id)` constraint
        would reject that anyway; this method just avoids ever attempting
        it). Flushes, does not commit - the caller (a route, via
        `get_db_session`'s auto-commit-on-success, or a script) decides the
        transaction boundary, same convention as every other repository in
        this codebase."""
        now = datetime.now(timezone.utc)
        existing = self.get_by_user_id(user_id)
        if existing is None:
            row = NotificationPreference(
                user_id=user_id, telegram_chat_id=telegram_chat_id, created_at=now, updated_at=now
            )
            self._session.add(row)
        else:
            existing.telegram_chat_id = telegram_chat_id
            existing.updated_at = now
            row = existing
        self._session.flush()
        return row
