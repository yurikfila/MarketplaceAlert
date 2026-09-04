"""`/api/v1/notification-preferences/me` - the authenticated user's own
Telegram notification destination.

Always "me" - there is no `user_id` in either route's path or request
body, on purpose. Ownership is derived exclusively from `get_current_user`
(the bearer token), so there is no way to read or write anyone else's
preference through this API - see `core/notifications/outbox.py`'s
module docstring "SECURITY RULE": this endpoint is the only way a user's
notification destination is ever set at runtime (the one-time production
migration script is the only other writer, and only for one specific,
explicitly-named existing account - see `scripts/backfill_notification_
preference.py`).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import NotificationPreferenceRead, NotificationPreferenceUpdate
from marketplace_alert.core.auth.dependencies import get_current_user
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.notifications.preferences_repository import NotificationPreferenceRepository
from marketplace_alert.core.persistence.database import get_db_session

router = APIRouter(prefix="/notification-preferences", tags=["Mobile API - Notification Preferences"])


@router.get(
    "/me",
    summary="Get my notification preference",
    description="Always the authenticated caller's own preference - never accepts or exposes a user id.",
)
def get_my_notification_preference(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> NotificationPreferenceRead:
    preference = NotificationPreferenceRepository(session).get_by_user_id(current_user.id)
    return NotificationPreferenceRead(telegram_chat_id=preference.telegram_chat_id if preference else None)


@router.put(
    "/me",
    summary="Set my notification preference",
    description=(
        "Creates the preference row if this is the first time, otherwise updates it in place. "
        "`telegram_chat_id: null` (or blank) clears it - notifications stop being delivered, "
        "never fall back to any default."
    ),
)
def update_my_notification_preference(
    data: NotificationPreferenceUpdate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> NotificationPreferenceRead:
    preference = NotificationPreferenceRepository(session).upsert_telegram_chat_id(
        current_user.id, data.telegram_chat_id
    )
    return NotificationPreferenceRead(telegram_chat_id=preference.telegram_chat_id)
