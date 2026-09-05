"""Notification outbox drain: claim -> resolve destination -> deliver ->
complete, with no database lock (or transaction) held during delivery.

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
2. **Resolve + deliver** (`resolve_destination` + `_deliver`): resolves
   the owning user's Telegram destination first (a short, separate,
   lock-free read - see "SECURITY RULE" below), then calls
   `NotificationProvider.send_listing_alert` for one claimed row at a
   time. **No database session or transaction is open during either step
   at all.** This is the entire point of splitting claim from delivery -
   Telegram (or any provider) can be arbitrarily slow or flaky without
   ever holding a Postgres row lock, or blocking a concurrent scan or
   another drain run, hostage to it.
3. **Complete** (`complete_notification`): a short, separate transaction
   per row, recording the outcome - `sent` on success, back to `pending`
   (to retry next drain) or `failed` (if `attempt_count` has reached
   `notification_max_attempts`) on failure. Committed immediately.

**SECURITY RULE - per-user notification routing, never a global
fallback.** Each notification's destination is resolved independently, via
one of two paths (Phase 2B of the multi-user notification outbox
redesign - see `resolve_destination`'s own docstring for the full
reasoning):

1. **Stamped, preferred**: `PendingNotification.user_id` (set directly at
   enqueue time, see `NotificationOutboxRepository.enqueue`) -> straight
   to `NotificationPreference -> telegram_chat_id`. No dependency on
   `SavedSearch` still existing.
2. **Legacy fallback, for historical rows enqueued before `user_id`
   existed**: `PendingNotification -> DiscoveredListing -> discovered_by_
   saved_search_id -> SavedSearch -> user_id -> NotificationPreference ->
   telegram_chat_id` - the original ownership join already proven by the
   saved-searches/listings ownership-enforcement phase, unchanged.

If ownership cannot be resolved via either path (no stamped `user_id` and
no discovering search, an unowned search, no preference row, or an empty
`telegram_chat_id`), the notification is **never** delivered to the
legacy global `TELEGRAM_CHAT_ID`, or to any other user's destination -
`resolve_destination` returns a `ResolvedDestination` with `destination is
None`, and `_deliver` treats that as a distinct, explicit "no
destination" outcome (see below), never as "success" and never as "fall
back to something". The legacy global chat id is read only by the
one-time migration/backfill script
(`scripts/backfill_notification_preference.py`), never by this module -
that is the only place it is still allowed to matter at all.

**"No destination" is not silently successful, and splits into two
distinct cases, deliberately handled differently.** The smallest change
compatible with the existing four-status (`pending`/`processing`/`sent`/
`failed`) state model: a "no destination" outcome is passed to
`complete_notification` with `success=False` and one of two distinctive
`last_error` sentinels (see `core/persistence/models.py`):

- **Case A - `NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG`**: the
  owning user is fully resolved (a real saved search, a real owner), but
  hasn't configured - or has explicitly cleared - a Telegram destination
  yet. This is plausibly temporary (a brand new user hasn't gotten to the
  settings screen yet), so it deliberately does **not** follow the normal
  bounded-retry path: `NotificationOutboxRepository.complete()` undoes
  the `attempt_count` increment `claim_batch()` made and always leaves
  the row `pending`, never `failed` - waiting for configuration must
  never, by itself, exhaust `notification_max_attempts` or cause the
  notification to be permanently lost. To avoid that row then being
  reclaimed on every single drain cycle forever (a hot loop), `claim_
  batch()` additionally throttles it: it isn't eligible for reclaim again
  until `last_attempted_at` is at least `settings.notification_no_
  destination_retry_seconds` old (default 900s / 15 minutes).
- **Case B - `NOTIFICATION_ERROR_OWNER_UNRESOLVED`**: ownership/
  provenance itself cannot be established - no discovering saved search
  at all (e.g. a legacy `/scan`-originated row), a saved search that no
  longer exists, or one with no owner (`user_id IS NULL`, pre-cutover).
  Waiting can never fix this - there is no future event that resolves it.
  This keeps the *original*, unmodified bounded-retry behavior: back to
  `pending` for a later drain attempt, until `notification_max_attempts`
  is reached, then permanently `failed`, exactly like a genuine delivery
  failure.

Both are observable/testable as their own category - via `last_error` on
the row, and via `DrainResult.awaiting_destination_config_count` /
`DrainResult.unresolved_owner_count` at the per-run level - distinct from
`failed_count` (genuine provider-side failures) and never indistinguishable
from, or counted as, a successful send.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.models import NotificationPreference
from marketplace_alert.core.persistence.models import (
    NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG,
    NOTIFICATION_ERROR_OWNER_UNRESOLVED,
)
from marketplace_alert.core.persistence.notification_outbox import ClaimedNotification, NotificationOutboxRepository
from marketplace_alert.core.saved_searches.models import SavedSearch

logger = logging.getLogger(__name__)


@dataclass
class ResolvedDestination:
    """Outcome of resolving one claimed notification's Telegram
    destination - see this module's "SECURITY RULE" docstring section.

    `destination` is the chat id to deliver to. When it is `None`,
    `unresolved_reason` is always set to one of the two sentinels from
    `core/persistence/models.py`, distinguishing Case A
    (`NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG` - owner resolved,
    just not configured yet) from Case B
    (`NOTIFICATION_ERROR_OWNER_UNRESOLVED` - ownership itself could not be
    established) - the two cases this module deliberately treats
    differently (see the module docstring).
    """

    destination: str | None
    unresolved_reason: str | None = None


@dataclass
class DrainResult:
    """Outcome of one `drain_pending_notifications` call, for the caller
    (a one-shot script, a test) to log or assert against.

    `awaiting_destination_config_count` (Case A) and `unresolved_owner_
    count` (Case B) are, at the database-row level, still just two of the
    reasons a row lands back in `pending`/`failed` (see this module's
    docstring for how `complete_notification` handles each) - but broken
    out separately here so an operator watching drain output can
    immediately tell "a user hasn't configured notifications yet" apart
    from "this notification's ownership can never be resolved" apart from
    "a real delivery is failing" (`failed_count`), without having to go
    inspect `last_error` in the database.
    """

    claimed_count: int = 0
    sent_count: int = 0
    failed_count: int = 0
    awaiting_destination_config_count: int = 0
    unresolved_owner_count: int = 0


def claim_due_notifications(
    session_factory: Callable[[], Session],
    *,
    limit: int,
    lease_seconds: float,
    no_destination_retry_seconds: float,
) -> list[ClaimedNotification]:
    """Phase 1: claim up to `limit` due rows and commit immediately.

    Opens and closes its own session - by the time this returns, no lock
    from this claim is still held. See this module's docstring for why
    that matters. `no_destination_retry_seconds` is threaded straight
    through to `claim_batch()`'s Case-A throttle - see that method's
    docstring.
    """
    session = session_factory()
    try:
        claimed = NotificationOutboxRepository(session).claim_batch(
            limit=limit, lease_seconds=lease_seconds, no_destination_retry_seconds=no_destination_retry_seconds
        )
        session.commit()
        return claimed
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_destination(
    session_factory: Callable[[], Session],
    *,
    user_id: int | None,
    discovered_by_saved_search_id: int | None,
) -> ResolvedDestination:
    """Resolves the Telegram chat id belonging to the user this
    notification is for - see this module's "SECURITY RULE" docstring
    section. Never returns a global default.

    **Phase 2B: two resolution paths, stamped `user_id` preferred.**

    1. If `user_id` is already known (stamped directly onto the
       `PendingNotification` row at enqueue time - see `Notification
       OutboxRepository.enqueue`), it's used immediately, with no lookup
       through `SavedSearch` at all. This is what makes a stamped
       notification's destination resolution independent of whether its
       originating saved search still exists - if that search is deleted
       *after* enqueue, this path is completely unaffected by it.
    2. If `user_id` is `None` (a historical row enqueued before this
       column existed), falls back to the exact original chain: resolve
       the owner via `SavedSearch.user_id` for `discovered_by_saved_
       search_id`. This fails - Case B, `NOTIFICATION_ERROR_OWNER_
       UNRESOLVED` - if `discovered_by_saved_search_id` is `None` (e.g. a
       legacy `/scan`-discovered listing), the saved search no longer
       exists (deleted after this notification was claimed - a narrow,
       real race, safe to treat the same as "never existed"), or it
       exists but has no owner yet (`user_id IS NULL`, pre-cutover). None
       of these can ever resolve themselves by waiting. Both "no `user_id`
       and no `discovered_by_saved_search_id` at all" are checked before
       ever opening a session - there is nothing a database round-trip
       could resolve in that case.

    Once an owner is resolved (via either path), the same second step
    applies: look up their `NotificationPreference`. No row at all, or
    one with `telegram_chat_id IS NULL`/empty (never configured, or
    explicitly cleared) - Case A, `NOTIFICATION_ERROR_AWAITING_
    DESTINATION_CONFIG`. This is exactly the case that's plausibly
    temporary and retried indefinitely (throttled) rather than ever
    failing permanently - see the module docstring.

    Opens and closes its own short session - same "no lock held during
    delivery" discipline as `claim_due_notifications`/
    `complete_notification` (see this module's docstring).
    """
    if user_id is None and discovered_by_saved_search_id is None:
        return ResolvedDestination(None, NOTIFICATION_ERROR_OWNER_UNRESOLVED)

    session = session_factory()
    try:
        resolved_user_id = user_id
        if resolved_user_id is None:
            resolved_user_id = session.execute(
                select(SavedSearch.user_id).where(SavedSearch.id == discovered_by_saved_search_id)
            ).scalar_one_or_none()
            if resolved_user_id is None:
                return ResolvedDestination(None, NOTIFICATION_ERROR_OWNER_UNRESOLVED)

        telegram_chat_id = session.execute(
            select(NotificationPreference.telegram_chat_id).where(NotificationPreference.user_id == resolved_user_id)
        ).scalar_one_or_none()
        if not telegram_chat_id:
            return ResolvedDestination(None, NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG)

        return ResolvedDestination(telegram_chat_id)
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
    database session open at all. Case A vs Case B branching lives in
    `NotificationOutboxRepository.complete()` - see that method's
    docstring.
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


def _deliver(
    provider: NotificationProvider, claimed: ClaimedNotification, resolved: ResolvedDestination
) -> tuple[bool, str | None]:
    """Phase 2: attempt one delivery. No database session is open here.

    `resolved.destination is None` means routing could not be resolved
    (see `resolve_destination`) - reported as a failure (never as a false
    success - see this module's "SECURITY RULE" docstring section)
    *without* ever calling the provider, since there is nowhere to send
    it; `resolved.unresolved_reason` (Case A or Case B) is passed straight
    through as the outcome's error so `complete_notification` can apply
    the right retry behavior for each. Otherwise catches every exception,
    not just `NotificationError` - a delivery failure (expected or not)
    must never stop the rest of the batch from being attempted, matching
    `NotificationService.notify_new_listings`'s same guarantee for the
    legacy synchronous path.
    """
    if resolved.destination is None:
        logger.info(
            "No notification destination for notification %s (%s listing %s): %s - left for retry, never "
            "using a global fallback",
            claimed.notification_id,
            claimed.listing.marketplace,
            claimed.listing.external_listing_id,
            resolved.unresolved_reason,
        )
        return False, resolved.unresolved_reason

    try:
        provider.send_listing_alert(claimed.listing, resolved.destination)
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
    no_destination_retry_seconds: float,
) -> DrainResult:
    """One full drain pass: claim a batch, resolve each row's destination
    and deliver it (no lock held), complete each in its own short
    transaction.

    Returns immediately, claiming nothing, if `provider.is_enabled` is
    `False` - see this module's docstring.
    """
    if not provider.is_enabled:
        logger.info("Notification provider is disabled - skipping outbox drain")
        return DrainResult()

    claimed = claim_due_notifications(
        session_factory,
        limit=batch_size,
        lease_seconds=lease_seconds,
        no_destination_retry_seconds=no_destination_retry_seconds,
    )
    result = DrainResult(claimed_count=len(claimed))

    for notification in claimed:
        resolved = resolve_destination(
            session_factory,
            user_id=notification.user_id,
            discovered_by_saved_search_id=notification.discovered_by_saved_search_id,
        )
        success, error = _deliver(provider, notification, resolved)
        complete_notification(
            session_factory,
            notification_id=notification.notification_id,
            success=success,
            error=error,
            max_attempts=max_attempts,
        )
        if success:
            result.sent_count += 1
        elif error == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG:
            result.awaiting_destination_config_count += 1
        elif error == NOTIFICATION_ERROR_OWNER_UNRESOLVED:
            result.unresolved_owner_count += 1
        else:
            result.failed_count += 1

    return result
