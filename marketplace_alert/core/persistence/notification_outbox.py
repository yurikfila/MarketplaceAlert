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
    `NotificationOutboxRepository.claim_batch`'s docstring)."""

    notification_id: int
    listing: Listing


class NotificationOutboxRepository:
    """Persistence operations for `PendingNotification` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, discovered_listing_id: int) -> PendingNotification:
        """Create a pending outbox row for a just-persisted listing.

        Called from `ListingDiscoveryService.process_listings()`, in the
        *same* session/transaction as the listing's own insert - never
        commits itself (matches `ListingRepository.save_new()`'s
        convention: flush only, the caller decides when to commit). The
        `UNIQUE` constraint on `discovered_listing_id`
        (`PendingNotification`'s own definition) is the actual dedup
        guarantee - this method doesn't need to check for an existing row
        first, since a listing only ever reaches "just persisted" once.
        """
        row = PendingNotification(discovered_listing_id=discovered_listing_id)
        self._session.add(row)
        self._session.flush()
        return row

    def claim_batch(self, *, limit: int, lease_seconds: float) -> list[ClaimedNotification]:
        """Claims up to `limit` rows eligible for delivery - either
        genuinely `pending`, or `processing` with a `claimed_at` older
        than `lease_seconds` (an abandoned claim, e.g. the process that
        claimed it crashed before completing - see this module's and
        `core/notifications/outbox.py`'s docstrings).

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

        stmt = (
            select(PendingNotification, DiscoveredListing)
            .join(DiscoveredListing, PendingNotification.discovered_listing_id == DiscoveredListing.id)
            .where(
                (PendingNotification.status == NOTIFICATION_STATUS_PENDING)
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
                ClaimedNotification(notification_id=notification.id, listing=_to_listing(listing_row))
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
