"""Shared FastAPI dependency providers and singletons.

Extracted out of `main.py` so both the legacy routes (`main.py`) and the
versioned `/api/v1` routers (`api/v1/`) depend on the exact same
singletons - one `NotificationService`, one `SavedSearchRunGuard` - never
two independently-constructed copies that could drift or let the same
saved search run through both an old and a new endpoint at once.
`main.py` re-imports everything here under its original (leading-
underscore) names, so existing behavior and existing test imports
(`from marketplace_alert.main import get_notification_service`, etc.) are
unaffected - this is a pure extraction, not a behavior change.
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from marketplace_alert.config import settings
from marketplace_alert.connectors.registry import get_connector, is_marketplace_supported
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.saved_searches.service import SavedSearchService
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.notifications.telegram.provider import TelegramNotificationProvider

# The concrete provider (Telegram) is chosen here, once, at startup - every
# caller (legacy routes, /api/v1 routes, the background scanner) only ever
# depends on the NotificationProvider interface. Disabled automatically if
# credentials are missing (see TelegramNotificationProvider). The provider
# retries its own transient failures (429/5xx/timeout, bounded, with
# backoff); the service paces sends between separate listings - see both
# modules' docstrings.
notification_service = NotificationService(
    TelegramNotificationProvider(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        max_retries=settings.telegram_max_retries,
        retry_base_seconds=settings.telegram_retry_base_seconds,
    ),
    send_delay_seconds=settings.telegram_send_delay_seconds,
)


def get_notification_service() -> NotificationService:
    """FastAPI dependency, overridden in tests with a fake provider.

    Never send real Telegram messages from automated tests - see
    tests/conftest.py.
    """
    return notification_service


def get_saved_search_service(session: Session = Depends(get_db_session)) -> SavedSearchService:
    """FastAPI dependency: validated saved-search CRUD, wired to the real connector registry."""
    return SavedSearchService(session, is_marketplace_supported=is_marketplace_supported)


# Bound to the real notification service, since the background scanner runs
# in a thread with no per-request Depends to pull a (possibly test-faked)
# one from. Resolves connectors only through get_connector - never imports
# a concrete connector class itself.
saved_search_runner = SavedSearchRunner(
    notification_service=notification_service,
    resolve_connector=get_connector,
)

# Shared between the scheduler, the legacy manual /run endpoint, and the
# /api/v1 manual run endpoint, so the same saved search can never be
# scanned by more than one of them at once.
saved_search_run_guard = SavedSearchRunGuard()
