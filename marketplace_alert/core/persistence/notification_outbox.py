"""Raw persistence access for the notification outbox.

Mirrors `repository.py`'s own rule: this is the only module that writes
SQL/ORM queries against `PendingNotification` - everything above it (the
scan path enqueueing a row, the drain orchestration in
`core/notifications/outbox.py`) works with plain Python values. See
`core/persistence/models.py:PendingNotification` for the full schema
reasoning and `core/notifications/outbox.py` for the claim/deliver/
complete design this repository's `claim_batch` supports.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import (
    NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_SENT,
    DiscoveredListing,
    PendingNotification,
)


@dataclass
class ClaimedNotification:
    """One claimed outbox row, with everything needed to attempt delivery
    already read out into plain values - never a live ORM object, since by
    the time this is used the claiming session has already committed (see
    `NotificationOutboxRepository.claim_batch`'s docstring).

    `user_id` (Phase 2B of the multi-user notification outbox redesign) is
    the row's own stamped owner, straight from `PendingNotification.
    user_id` - `None` for historical rows enqueued before this column
    existed. `discovered_by_saved_search_id` is carried through *in
    addition to* `user_id`, not replaced by it - the drain loop
    (`core/notifications/outbox.py`) still needs it as the fallback
    resolution path for exactly those historical `user_id IS NULL` rows.
    `None` here means "no discovering search" (e.g. a legacy `/scan`-
    discovered listing) - a notification that can never be routed to
    anyone via the fallback, see that module's "SECURITY RULE".
    """

    notification_id: int
    listing: Listing
    user_id: int | None
    discovered_by_saved_search_id: int | None


class NotificationOutboxRepository:
    """Persistence operations for `PendingNotification` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, discovered_listing_id: int, *, user_id: int | None = None) -> PendingNotification:
        """Create a pending outbox row for a just-persisted listing.

        Called from `SavedSearchRunner`, in the *same* session/transaction
        as the listing's own insert - never commits itself (matches
        `ListingRepository.save_new()`'s convention: flush only, the
        caller decides when to commit). The `UNIQUE` constraint on
        `discovered_listing_id` (`PendingNotification`'s own definition)
        is the actual dedup guarantee - this method doesn't need to check
        for an existing row first, since a listing only ever reaches
        "just persisted" once.

        `user_id` (Phase 2B of the multi-user notification outbox
        redesign) is the saved search's owner *at enqueue time*, stamped
        directly onto the new row - see `PendingNotification.user_id`'s
        own docstring. Defaults to `None`, both for legacy/unowned
        searches (never guess an owner) and so every existing caller that
        predates this parameter is unaffected. **Never overwrites an
        existing row's `user_id`** - this method only ever inserts; if a
        row already exists for `discovered_listing_id`, the `UNIQUE`
        constraint rejects the second insert entirely (see
        `test_enqueue_twice_for_the_same_listing_violates_the_unique_
        constraint`), so there is no code path here that could ever
        transfer or change an already-stamped owner.
        """
        row = PendingNotification(discovered_listing_id=discovered_listing_id, user_id=user_id)
        self._session.add(row)
        self._session.flush()
        return row

    def claim_batch(
        self, *, limit: int, lease_seconds: float, no_destination_retry_seconds: float
    ) -> list[ClaimedNotification]:
        """Claims up to `limit` rows eligible for delivery - either
        genuinely `pending`, or `processing` with a `claimed_at` older
        than `lease_seconds` (an abandoned claim, e.g. the process that
        claimed it crashed before completing - see this module's and
        `core/notifications/outbox.py`'s docstrings).

        A `pending` row whose *previous* completion was Case A
        (`last_error == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG` -
        the owner is real but hasn't configured a Telegram destination
        yet) is additionally throttled: it's only eligible once
        `last_attempted_at` is at least `no_destination_retry_seconds`
        old. Without this, such a row would otherwise be reclaimed on
        every single drain cycle forever, since it's deliberately never
        allowed to reach `failed` (see `complete()` below) - see
        `core/notifications/outbox.py`'s "SECURITY RULE" section. A row
        that has never been attempted (`last_attempted_at IS NULL`) or
        whose last outcome was anything else (a genuine provider failure,
        or Case B's `NOTIFICATION_ERROR_OWNER_UNRESOLVED`) is unaffected
        by this predicate - only Case A is throttled; every other retry
        path keeps its existing immediate-reclaim behavior unchanged.

        `FOR UPDATE SKIP LOCKED`, scoped to `pending_notifications` only
        (`of=PendingNotification` - never locks the joined
        `discovered_listings` row, which the scan path may be writing to
        concurrently) - a second, concurrent claim attempt simply skips
        whatever this one has already locked, rather than blocking or
        double-claiming. Each claimed row is moved to `processing`,
        stamped with `claimed_at`, and has `attempt_count` incremented -
        but **this method does not commit**. The caller
        (`core/notifications/outbox.py:claim_due_notifications`) commits
        immediately after calling this, on purpose: releasing every lock
        *before* any Telegram network I/O is the entire point of this
        design - see that module's docstring.
        """
        now = datetime.now(timezone.utc)
        lease_cutoff = now - timedelta(seconds=lease_seconds)
        no_destination_cutoff = now - timedelta(seconds=no_destination_retry_seconds)

        stmt = (
            select(PendingNotification, DiscoveredListing)
            .join(DiscoveredListing, PendingNotification.discovered_listing_id == DiscoveredListing.id)
            .where(
                (
                    (PendingNotification.status == NOTIFICATION_STATUS_PENDING)
                    & (
                        (PendingNotification.last_error != NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG)
                        | (PendingNotification.last_attempted_at.is_(None))
                        | (PendingNotification.last_attempted_at <= no_destination_cutoff)
                    )
                )
                | (
                    (PendingNotification.status == NOTIFICATION_STATUS_PROCESSING)
                    & (PendingNotification.claimed_at < lease_cutoff)
                )
            )
            .order_by(PendingNotification.created_at.asc())
            .limit(limit)
            .with_for_update(of=PendingNotification, skip_locked=True)
        )
        rows = self._session.execute(stmt).all()

        claimed: list[ClaimedNotification] = []
        for notification, listing_row in rows:
            notification.status = NOTIFICATION_STATUS_PROCESSING
            notification.claimed_at = now
            notification.attempt_count += 1
            claimed.append(
                ClaimedNotification(
                    notification_id=notification.id,
                    listing=_to_listing(listing_row),
                    user_id=notification.user_id,
                    discovered_by_saved_search_id=listing_row.discovered_by_saved_search_id,
                )
            )
        self._session.flush()
        return claimed

    def complete(
        self,
        *,
        notification_id: int,
        success: bool,
        error: str | None,
        max_attempts: int,
    ) -> None:
        """Records one claimed row's delivery outcome - called with a
        *fresh* session, opened only for this single update, after
        delivery has already been attempted with no database session
        open at all (see `core/notifications/outbox.py`). Does not commit
        itself - matches every other repository method's convention; the
        caller commits right after, one row at a time.

        A row that no longer exists (e.g. its listing was deleted via
        `ondelete="CASCADE"` while a delivery attempt was in flight) is
        silently ignored rather than raising - there is nothing left to
        record an outcome against.

        Case A (`error == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG`)
        is handled distinctly from every other failure: `claim_batch()`
        already incremented `attempt_count` before this outcome was
        known, but waiting for the user to configure a destination is
        not a genuine delivery attempt, so that increment is undone here
        and the row is always sent back to `pending` regardless of
        `max_attempts` - it must never become terminally `failed` solely
        because Telegram isn't configured yet. See `core/notifications/
        outbox.py`'s "SECURITY RULE" section.
        """
        row = self._session.get(PendingNotification, notification_id)
        if row is None:
            return

        now = datetime.now(timezone.utc)
        row.last_attempted_at = now
        if success:
            row.status = NOTIFICATION_STATUS_SENT
            row.sent_at = now
            row.last_error = None
        elif error == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG:
            row.attempt_count = max(0, row.attempt_count - 1)
            row.last_error = error
            row.status = NOTIFICATION_STATUS_PENDING
        else:
            row.last_error = error
            row.status = NOTIFICATION_STATUS_FAILED if row.attempt_count >= max_attempts else NOTIFICATION_STATUS_PENDING
        self._session.flush()


def _to_listing(row: DiscoveredListing) -> Listing:
    """Reconstructs a `Listing` (the shape `NotificationProvider` expects)
    from an already-persisted `DiscoveredListing` row - every field
    `send_listing_alert` could plausibly use is a plain column on that
    row already; no second lookup, no guessing. `description` is not
    persisted on `DiscoveredListing` at all (see that model), so it's
    left `None` here too - an accurate gap, not a fabricated value."""
    return Listing(
        marketplace=row.marketplace,
        external_listing_id=row.external_listing_id,
        title=row.title,
        price=row.price,
        currency=row.currency,
        location=row.location,
        seller=row.seller,
        condition=row.condition,
        listing_url=row.listing_url,
        image_url=row.image_url,
        created_at=row.source_created_at,
        discovered_at=row.first_discovered_at,
    )
