"""One-shot entrypoint: drain one batch of due notification-outbox rows on
EVERY channel, then exit.

Intended to be invoked by the Render Cron Job "drain" schedule (see
ARCHITECTURE.md and `marketplace_alert/core/notifications/outbox.py`'s
module docstring for the full claim/deliver/complete design) - a
completely separate Cron Job from the scan one
(`scripts/run_due_scans.py`), so a slow or failing notification provider
can never block, delay, or crash a scan, and vice versa.

**Two independent channels, one script, one Cron Job - deliberately not
a second Cron Job for push.** Telegram and native mobile push (Expo
Notifications, Phase 1) are drained as two separate, sequential passes
in the same run - `drain_pending_notifications()` (Telegram, entirely
unchanged by the push addition) and `drain_pending_push_notifications()`
(push, new). Each channel already claims/delivers/completes its own
`push_*`-or-Telegram-columns independently (see `core/notifications/
outbox.py`'s "Push channel" section) - a slow or failing channel in one
pass cannot block or corrupt the other pass, so there is no reliability
reason to split them into separate Cron Jobs, only extra Render
configuration for no benefit.

Uses `dependencies.notification_provider`/`dependencies.expo_push_provider`
directly (the concrete providers, not `NotificationService`) - a drain
run delivers claimed outbox rows one at a time, itself, via
`core/notifications/outbox.py`'s own pacing-free delivery loops; it has
no use for `NotificationService.notify_new_listings`'s batch-pacing
wrapper, which was designed for a single scan's in-memory result list,
not a durable queue that already spreads deliveries out naturally across
whatever cron interval "drain" runs on.

If a given provider is disabled (no Telegram bot token configured, or
`EXPO_PUSH_ENABLED=false`), that channel's pass exits having claimed
nothing - see `drain_pending_notifications`/`drain_pending_push_
notifications`'s own docstrings. The two channels are evaluated
independently either way - a disabled/unconfigured Telegram never
prevents the push pass from running, and vice versa.

Usage (from the project root, with the project's virtualenv active):

    python scripts/drain_notification_outbox.py

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app itself
uses.
"""

import logging
import sys

from marketplace_alert.config import settings
from marketplace_alert.core.logging_config import configure_logging
from marketplace_alert.core.notifications.outbox import drain_pending_notifications, drain_pending_push_notifications
from marketplace_alert.core.persistence.database import SessionLocal
from marketplace_alert.dependencies import expo_push_provider, notification_provider

logger = logging.getLogger(__name__)


def main() -> int:
    configure_logging(settings.log_level)

    try:
        result = drain_pending_notifications(
            SessionLocal,
            notification_provider,
            batch_size=settings.notification_outbox_batch_size,
            lease_seconds=settings.notification_lease_seconds,
            max_attempts=settings.notification_max_attempts,
            no_destination_retry_seconds=settings.notification_no_destination_retry_seconds,
        )
    except Exception:
        logger.exception("drain_notification_outbox: Telegram drain pass failed")
        return 1

    logger.info(
        "drain_notification_outbox: telegram claimed=%d sent=%d failed=%d awaiting_config=%d unresolved_owner=%d",
        result.claimed_count,
        result.sent_count,
        result.failed_count,
        result.awaiting_destination_config_count,
        result.unresolved_owner_count,
    )

    try:
        push_result = drain_pending_push_notifications(
            SessionLocal,
            expo_push_provider,
            batch_size=settings.notification_outbox_batch_size,
            lease_seconds=settings.notification_lease_seconds,
            max_attempts=settings.push_notification_max_attempts,
            no_destination_retry_seconds=settings.notification_no_destination_retry_seconds,
        )
    except Exception:
        logger.exception("drain_notification_outbox: push drain pass failed")
        return 1

    logger.info(
        "drain_notification_outbox: push claimed=%d sent=%d failed=%d awaiting_device=%d unresolved_owner=%d",
        push_result.claimed_count,
        push_result.sent_count,
        push_result.failed_count,
        push_result.awaiting_device_count,
        push_result.unresolved_owner_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
