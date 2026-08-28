"""Notification outbox drain: claim -> deliver -> complete, with no
database lock (or transaction) held during delivery.

**Why this exists**: `SavedSearchRunner` only ever enqueues a
`PendingNotification` row (see its module docstring) - it never calls a
`NotificationProvider` itself. Something has to actually deliver those
rows. That "something" is this module, run periodically and completely
independently of scanning (see `scripts/drain_notification_outbox.py` and
the Render Cron Job that calls it) - so a slow or failing notification
provider can never block, delay, or crash a scan, and a slow or failing
scan can never delay a notification.

**The three-phase claim/deliver/complete pattern** (this is the load-
bearing design decision here, and the reason this isn't just "select
pending rows, send, mark sent" in one transaction):

1. **Claim** (`claim_due_notifications`): opens a session, calls
   `NotificationOutboxRepository.claim_batch()` (which uses
   `SELECT ... FOR UPDATE SKIP LOCKED` to atomically move eligible rows
   from `pending` - or an abandoned `processing` past its lease - to
   `processing`), and **commits immediately**, before returning. By the
   time this function returns, every row lock has already been released -
   nothing below this point ever touches the database while a lock is
   held.
2. **Deliver** (`_deliver`): calls `NotificationProvider.send_listing_alert`
   for one claimed row at a time. **No database session or transaction is
   open during this call at all.** This is the entire point of splitting
   claim from delivery - Telegram (or any provider) can be arbitrarily
   slow or flaky without ever holding a Postgres row lock, or blocking a
   concurrent scan or another drain run, hostage to it.
3. **Complete** (`complete_notification`): a short, separate transaction
   per row, recording the outcome - `sent` on success, back to `pending`
   (to retry next drain) or `failed` (if `attempt_count` has reached
   `notification_max_attempts`) on failure. Committed immediately.

**Crash recovery**: if the process dies between phase 1 and phase 3 (e.g.
mid-delivery), the row is left in `processing` with a `claimed_at`
timestamp. `claim_batch` treats any `processing` row whose `claimed_at` is
older than `lease_seconds` as eligible again, so the *next* drain run
reclaims and retries it automatically - no separate cleanup job needed.

**Delivery semantics: at-least-once, not exactly-once.** Dedup is enforced
once, before a notification is ever created, by the `UNIQUE` constraint on
`PendingNotification.discovered_listing_id` - the same listing can never
get two outbox rows. But *sending* is not transactional with Postgres:
there is an unavoidable window, between a provider successfully accepting
a message and `complete_notification`'s commit recording that fact, where
a crash would leave the row in `processing` - the *next* drain would then
re-send it once its lease expires. This window is real and is deliberately
kept small (a single provider call, then an immediate single-row commit),
but it is not eliminated, and this module does not pretend otherwise: a
user can, in a narrow crash window, receive the same alert twice. It can
never silently lose one.

**A disabled provider is a configuration state, not a delivery failure.**
If `NotificationProvider.is_enabled` is `False` (e.g. no Telegram bot
token configured - the normal case for local dev/tests), `drain_pending_
notifications` does not claim anything at all - claiming and then failing
every row would inflate `attempt_count` and eventually mark rows
permanently `failed` for a reason that has nothing to do with the
notification itself. Rows are left exactly as they were, ready to be
delivered whenever a real provider is configured and a drain runs again.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.persistence.notification_outbox import ClaimedNotification, NotificationOutboxRepository

logger = logging.getLogger(__name__)


@dataclass
class DrainResult:
    """Outcome of one `drain_pending_notifications` call, for the caller
    (a one-shot script, a test) to log or assert against."""

    claimed_count: int = 0
    sent_count: int = 0
    failed_count: int = 0


def claim_due_notifications(
    session_factory: Callable[[], Session],
    *,
    limit: int,
    lease_seconds: float,
) -> list[ClaimedNotification]:
    """Phase 1: claim up to `limit` due rows and commit immediately.

    Opens and closes its own session - by the time this returns, no lock
    from this claim is still held. See this module's docstring for why
    that matters.
    """
    session = session_factory()
    try:
        claimed = NotificationOutboxRepository(session).claim_batch(limit=limit, lease_seconds=lease_seconds)
        session.commit()
        return claimed
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def complete_notification(
    session_factory: Callable[[], Session],
    *,
    notification_id: int,
    success: bool,
    error: str | None,
    max_attempts: int,
) -> None:
    """Phase 3: record one row's delivery outcome in its own short transaction.

    Opens and closes its own session, same as `claim_due_notifications` -
    called only *after* delivery has already been attempted with no
    database session open at all.
    """
    session = session_factory()
    try:
        NotificationOutboxRepository(session).complete(
            notification_id=notification_id, success=success, error=error, max_attempts=max_attempts
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _deliver(provider: NotificationProvider, claimed: ClaimedNotification) -> tuple[bool, str | None]:
    """Phase 2: attempt one delivery. No database session is open here.

    Catches every exception, not just `NotificationError` - a delivery
    failure (expected or not) must never stop the rest of the batch from
    being attempted, matching `NotificationService.notify_new_listings`'s
    same guarantee for the old synchronous path.
    """
    try:
        provider.send_listing_alert(claimed.listing)
        return True, None
    except NotificationError as exc:
        logger.exception(
            "Failed to deliver notification %s for %s listing %s",
            claimed.notification_id,
            claimed.listing.marketplace,
            claimed.listing.external_listing_id,
        )
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - defensive: one bad row can't block the rest of the drain
        logger.exception(
            "Unexpected error delivering notification %s for %s listing %s",
            claimed.notification_id,
            claimed.listing.marketplace,
            claimed.listing.external_listing_id,
        )
        return False, str(exc)


def drain_pending_notifications(
    session_factory: Callable[[], Session],
    provider: NotificationProvider,
    *,
    batch_size: int,
    lease_seconds: float,
    max_attempts: int,
) -> DrainResult:
    """One full drain pass: claim a batch, deliver each one at a time
    (no lock held), complete each in its own short transaction.

    Returns immediately, claiming nothing, if `provider.is_enabled` is
    `False` - see this module's docstring.
    """
    if not provider.is_enabled:
        logger.info("Notification provider is disabled - skipping outbox drain")
        return DrainResult()

    claimed = claim_due_notifications(session_factory, limit=batch_size, lease_seconds=lease_seconds)
    result = DrainResult(claimed_count=len(claimed))

    for notification in claimed:
        success, error = _deliver(provider, notification)
        complete_notification(
            session_factory,
            notification_id=notification.notification_id,
            success=success,
            error=error,
            max_attempts=max_attempts,
        )
        if success:
            result.sent_count += 1
        else:
            result.failed_count += 1

    return result
