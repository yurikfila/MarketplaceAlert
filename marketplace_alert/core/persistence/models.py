"""SQLAlchemy models for locally persisted data.

Distinct from ``marketplace_alert.core.models.listing.Listing`` (the
Pydantic model every connector's ``search()`` returns): ``DiscoveredListing``
is what we remember about a listing once it's been seen, so a later scan can
tell new from already-seen.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketplace_alert.core.persistence.database import Base

# `DiscoveredListing.metadata_backfill_status` values - see that column's
# docstring below for what each one means and why. Defined here (rather
# than in `core/persistence/backfill.py`) so both the repository's
# candidate query and the backfill service can import the same constants
# without a circular import (the service already imports the repository).
BACKFILL_STATUS_ENRICHED = "enriched"
BACKFILL_STATUS_NO_DATA = "no_data"
BACKFILL_STATUS_NOT_FOUND = "not_found"
BACKFILL_STATUS_UNSUPPORTED = "unsupported"
BACKFILL_STATUS_FAILED = "failed"

# Terminal: once persisted, a row in one of these states is never selected
# as a backfill candidate again. `BACKFILL_STATUS_FAILED` is deliberately
# excluded - it's retryable, not terminal.
BACKFILL_TERMINAL_STATUSES = (
    BACKFILL_STATUS_ENRICHED,
    BACKFILL_STATUS_NO_DATA,
    BACKFILL_STATUS_NOT_FOUND,
    BACKFILL_STATUS_UNSUPPORTED,
)


class DiscoveredListing(Base):
    """A listing previously discovered, keyed by (marketplace, external_listing_id)."""

    __tablename__ = "discovered_listings"
    __table_args__ = (
        UniqueConstraint(
            "marketplace", "external_listing_id", name="uq_discovered_listing_marketplace_external_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    marketplace: Mapped[str] = mapped_column(String, nullable=False, index=True)
    external_listing_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    listing_url: Mapped[str] = mapped_column(String, nullable=False)

    # Indexed - `ListingRepository.list_recent()` (backing `GET
    # /api/v1/listings`, the mobile app's Listings screen) orders by this
    # column on every page load; without an index, that's a full-table
    # sort that gets slower as this table grows. Found during a
    # production-hardening audit - see PROJECT_CONTEXT.md and
    # alembic/versions/ for the migration that adds it to an
    # already-deployed PostgreSQL database (this column already existed -
    # only the index is new).
    first_discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Product-experience fields (added in the Listings UX pass) ---
    #
    # All nullable, and all populated *once*, at first-discovery time only
    # (`ListingRepository.save_new()`) - a listing seen again later
    # (`touch_last_seen()`) does not refresh these, even if the source
    # listing's price/condition/etc. has since changed on the marketplace.
    # Tracking price history/changes over time is a deliberately separate,
    # not-yet-built feature (see PROJECT_CONTEXT.md) - these columns exist
    # to show what a listing looked like when discovered, not to stay
    # live-synced with the marketplace afterwards.
    #
    # Every connector already extracts these fields onto the transient
    # `Listing` Pydantic model (`core/models/listing.py`) - this was the
    # real gap: nothing persisted them, so `GET /api/v1/listings` could
    # only ever return `null` for all of them, regardless of what a
    # connector actually found. See ARCHITECTURE.md "Local persistence
    # and duplicate detection" and PROJECT_CONTEXT.md for the full
    # reasoning behind adding this now.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    seller: Mapped[str | None] = mapped_column(String, nullable=True)
    condition: Mapped[str | None] = mapped_column(String, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # The marketplace's own "listing created/published" timestamp
    # (`Listing.created_at`), distinct from `first_discovered_at` (when
    # *our* system first saw it). Display-only - "new"/sort/filter
    # semantics are deliberately anchored to `first_discovered_at` alone
    # (one consistent timestamp strategy - see `api/v1/listings.py`),
    # never this one, so a listing that's old on the marketplace but was
    # only just found by a new saved search still counts as newly
    # discovered.
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Which saved search's scan *first* discovered this row - set once, at
    # insert time, never updated afterwards. This is an honest "first
    # discovered by" attribution, not a "belongs to" relationship: the
    # same listing can independently match other saved searches too
    # (dedup identity is still global - `(marketplace, external_listing_id)`
    # - see ListingDiscoveryService), and those later matches are not
    # recorded here. NULL for a listing discovered before this column
    # existed, or discovered via the legacy `/scan` endpoint (which isn't
    # tied to any saved search). `ondelete="SET NULL"` - deleting a saved
    # search must never delete or orphan-cascade the listings it found;
    # it only forgets which search found them first. Indexed - this is
    # now a supported `GET /api/v1/listings?saved_search_id=` filter (see
    # `api/v1/listings.py`), and the Saved Search Detail screen's "latest
    # listings" section relies on it.
    discovered_by_saved_search_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "saved_searches.id", ondelete="SET NULL", name="fk_discovered_listings_discovered_by_saved_search_id"
        ),
        nullable=True,
        index=True,
    )

    # --- Historical metadata backfill state (added in the backfill
    # candidate-selection fix pass) ---
    #
    # `None` means "never attempted" (pending) - the default for every
    # row, including a brand-new one from a normal scan. A row that
    # already has every enrichable field populated at discovery time
    # simply never becomes a backfill candidate regardless of this
    # status (see `ListingRepository.list_missing_metadata`'s combined
    # status-and-missing-field filter), so defaulting every row to
    # "pending" costs nothing extra - it only matters for rows that
    # genuinely still need a lookup.
    #
    # Terminal (a row in one of these states is never selected as a
    # backfill candidate again, once persisted - see
    # `BACKFILL_TERMINAL_STATUSES` below): `enriched` (an authoritative
    # lookup filled in at least one field - this backfill generation is
    # done for this row, even if some fields remain `None` because the
    # marketplace itself doesn't provide them for this listing -
    # PROJECT_CONTEXT.md decision #23), `no_data` (the lookup succeeded
    # but had nothing new to add), `not_found` (the marketplace
    # confirmed the listing no longer exists), `unsupported` (this
    # marketplace has no connector lookup capability at all - a
    # structural fact, not a temporary condition).
    #
    # Retryable: `failed` (a transient failure - timeout/429/5xx/
    # malformed response) stays eligible for a future run, same as
    # `None`. A marketplace that's merely *unconfigured* right now
    # (missing credentials) is never persisted here at all - that's an
    # operational condition that can change without a data migration
    # (an operator adding the missing credential), so those rows are
    # simply left `None`/untouched and naturally retried whenever the
    # marketplace becomes configured.
    metadata_backfill_status: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_backfill_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# `PendingNotification.status` values - see `core/notifications/outbox.py`
# for the full claim/deliver/complete design these values support.
NOTIFICATION_STATUS_PENDING = "pending"
NOTIFICATION_STATUS_PROCESSING = "processing"
NOTIFICATION_STATUS_SENT = "sent"
NOTIFICATION_STATUS_FAILED = "failed"


class PendingNotification(Base):
    """The notification outbox: one row per newly-discovered listing that
    should generate a Telegram alert, created in the *same* transaction as
    the listing itself (see `ListingDiscoveryService.process_listings()`),
    so scanning/persistence never has to wait for - or even know about -
    actual delivery. A completely separate process (`core/notifications
    /outbox.py`'s `drain_pending_notifications()`) claims, delivers, and
    completes rows independently, on its own schedule.

    Delivery guarantee is explicitly **at-least-once, not exactly-once**:
    PostgreSQL can guarantee this row's bookkeeping is crash-safe, but it
    cannot make the same guarantee jointly with an external system
    (Telegram) - there is an unavoidable crash window between Telegram
    successfully receiving a message and this row being marked `sent`
    where a retry would send a genuine duplicate message. See
    `core/notifications/outbox.py`'s module docstring for the full
    reasoning and why this window is kept as small as practical rather
    than eliminated (it cannot be eliminated without a distributed
    transaction spanning Postgres and Telegram, which does not exist).

    Deduplication *before* sending - "never queue the same listing's
    notification twice" - is a hard guarantee: enforced by the `UNIQUE`
    constraint on `discovered_listing_id` below, not just by callers
    happening to only enqueue once.
    """

    __tablename__ = "pending_notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # The dedup guarantee - a listing can only ever have one outbox row,
    # enforced by the database, not just by `ListingDiscoveryService`
    # only ever enqueueing on first discovery. `ondelete="CASCADE"`: if
    # the listing itself is ever removed (e.g. by the historical
    # relevance cleanup script), an unsent notification about it is
    # meaningless and should go with it.
    discovered_listing_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_listings.id", ondelete="CASCADE", name="fk_pending_notifications_discovered_listing_id"),
        nullable=False,
        unique=True,
    )

    # Indexed - every drain run's claim query filters on this column
    # (`WHERE status = 'pending' OR (status = 'processing' AND ...)`),
    # a known, constant access pattern from day one - unlike
    # `metadata_backfill_status` (deliberately left unindexed, see that
    # column's docstring), this index is justified up front, not
    # deferred pending evidence.
    status: Mapped[str] = mapped_column(String, nullable=False, default=NOTIFICATION_STATUS_PENDING, index=True)

    # Incremented every time a row is claimed (first attempt or a
    # lease-timeout reclaim after a crash) - see `outbox.py`'s claim
    # phase. Gates `NOTIFICATION_STATUS_FAILED` once
    # `settings.notification_max_attempts` is reached, so a message that
    # reliably crashes the sender (a "poison pill") can't be retried
    # forever.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Set when a row moves to `processing` (claimed). A row still
    # `processing` with `claimed_at` older than
    # `settings.notification_lease_seconds` is treated as an
    # abandoned/crashed claim and becomes eligible for reclamation by a
    # later drain run - see `outbox.py`'s claim query. Doubles as the
    # lease timestamp; no separate `lease_expires_at` column - one fewer
    # field to keep the schema as small as possible.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
