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


class ListingAttribution(Base):
    """Records that one specific `SavedSearch` legitimately matched one
    specific `DiscoveredListing` - the fix for the global-dedup limitation
    `DiscoveredListing.discovered_by_saved_search_id` has by itself (see
    that column's docstring): a canonical listing's dedup identity stays
    global (`UNIQUE(marketplace, external_listing_id)` on `discovered_
    listings`, unchanged), but *attribution* - who is entitled to see this
    listing, and (in a later phase) be notified about it - is no longer
    limited to whichever search happened to discover it first. Every
    search that genuinely matches a listing gets its own row here,
    independent of whether the canonical listing itself was brand new.

    Deliberately narrow and immutable: three facts (which search, which
    listing, when), never updated after insert - there is nothing about
    an attribution that later changes, unlike `DiscoveredListing.last_seen_at`.

    `UNIQUE(saved_search_id, discovered_listing_id)` is the idempotency
    guarantee - the same search can never attribute the same listing
    twice, enforced by the database, not just by callers checking first
    (see `ListingAttributionRepository.record_if_missing`).

    Both foreign keys `ON DELETE CASCADE`, deliberately different from
    `discovered_by_saved_search_id`'s `SET NULL`: that column's `SET NULL`
    exists because it used to be the *only* record of discovery, so
    losing it entirely on search-deletion felt too destructive. That
    reasoning doesn't apply here - this row's entire reason for existing
    is "this search found this listing"; if the search is deleted, that
    specific claim should go with it. The canonical `discovered_listings`
    row is never touched by that cascade (nothing here ever cascades
    *into* `discovered_listings`), and every other search's/user's own
    attribution rows are completely unaffected. Cascading on `discovered_
    listing_id` too, matching `PendingNotification`'s identical reasoning:
    if the listing itself is ever removed (e.g. historical relevance
    cleanup), an attribution to it is meaningless and should go with it.

    Phase 1 note: this table is populated by `ListingDiscoveryService.
    process_listings()` going forward, and by the one-time `scripts/
    backfill_listing_attributions.py` for pre-existing history - but
    nothing yet *reads* it for notification delivery (`PendingNotification`
    is explicitly out of scope this phase - see that model's docstring).
    `ListingRepository.list_recent_owned`/`count_owned` are the only
    current readers, for listing *visibility*.
    """

    __tablename__ = "listing_attributions"
    __table_args__ = (
        UniqueConstraint(
            "saved_search_id", "discovered_listing_id", name="uq_listing_attribution_saved_search_listing"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    saved_search_id: Mapped[int] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="CASCADE", name="fk_listing_attributions_saved_search_id"),
        nullable=False,
    )
    # Indexed on its own (unlike saved_search_id, which is already the
    # leading column of the UNIQUE constraint above and so already has an
    # index covering saved-search-first lookups for free) - ownership
    # queries also need to go the other way ("does any attribution exist
    # for this listing"), which the composite index alone can't serve.
    discovered_listing_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_listings.id", ondelete="CASCADE", name="fk_listing_attributions_discovered_listing_id"),
        nullable=False,
        index=True,
    )

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


# `PendingNotification.status` values - see `core/notifications/outbox.py`
# for the full claim/deliver/complete design these values support.
NOTIFICATION_STATUS_PENDING = "pending"
NOTIFICATION_STATUS_PROCESSING = "processing"
NOTIFICATION_STATUS_SENT = "sent"
NOTIFICATION_STATUS_FAILED = "failed"

# Distinct, matchable `last_error` sentinels for the two reasons a
# notification can go undelivered with no Telegram call ever made - see
# `core/notifications/outbox.py`'s "SECURITY RULE" docstring section for
# the full reasoning. Defined here, next to `NOTIFICATION_STATUS_*`,
# because `NotificationOutboxRepository.claim_batch()`'s throttle
# predicate (this module) and the drain loop
# (`core/notifications/outbox.py`) both need to match against the exact
# same string - one shared constant avoids duplicating it, or having this
# persistence module import from the higher-level outbox module.
#
# - AWAITING_DESTINATION_CONFIG ("Case A"): the owning user is fully
#   resolved (a real saved search, a real owner) but hasn't configured -
#   or has cleared - a Telegram destination yet. Plausibly temporary:
#   never counted against `notification_max_attempts` (see `claim_batch`
#   and `complete` below) and retried indefinitely, throttled by
#   `settings.notification_no_destination_retry_seconds` rather than
#   reclaimed on every drain cycle.
# - OWNER_UNRESOLVED ("Case B"): ownership/provenance itself cannot be
#   established (no discovering saved search, a deleted saved search, or
#   a saved search with no owner) - not something waiting will ever fix.
#   Keeps the existing bounded-retry-then-`failed` behavior.
NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG = "Owning user has not configured a Telegram destination yet"
NOTIFICATION_ERROR_OWNER_UNRESOLVED = "Notification owner could not be resolved"


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

    Deduplication is a hard database guarantee, not just callers happening
    to enqueue once - see `UniqueConstraint` below for exactly what's
    unique and why (Phase 2D-SCHEMA of the multi-user notification outbox
    redesign changed this from a single-column to a composite constraint;
    see that constraint's own comment for the full reasoning).

    **Phase 2D-SCHEMA note (multi-user notification outbox redesign,
    schema-only half)**: `user_id` is still schema-only groundwork as far
    as *runtime behavior* goes - `SavedSearchRunner` still enqueues only
    one row per globally-new listing (`discovery_result.new_listing_ids`),
    exactly as before; nothing yet enqueues a second row for a second
    user sharing the same listing. What changed in this phase is only
    that the schema now *permits* that second row, once a later,
    separate phase (Phase 2D-BEHAVIOR) actually starts creating it - see
    the audit that preceded this phase for why schema-first, behavior-
    later is the safe order. Ownership resolution is unchanged: `user_id`
    preferred when stamped (Phase 2B), falling back to `discovered_
    listing_id -> DiscoveredListing.discovered_by_saved_search_id ->
    SavedSearch.user_id` for historical `NULL` rows (see `core/
    notifications/outbox.py`'s `resolve_destination`).
    """

    __tablename__ = "pending_notifications"
    __table_args__ = (
        # Phase 2D-SCHEMA: replaces the original single-column
        # `UNIQUE(discovered_listing_id)` (see `163ae88ffc55_add_
        # notification_outbox.py` - unnamed there, so PostgreSQL auto-
        # generated its actual constraint name; the migration that
        # replaces it discovers that name via reflection rather than
        # guessing it - see that migration's own docstring). A listing
        # can now have at most one outbox row *per user* rather than one
        # ever, globally - the schema half of enabling User A and User B
        # to each get their own notification for the same canonical
        # listing. `user_id IS NULL` never collides with itself under
        # standard SQL uniqueness semantics (NULL is never treated as
        # equal to NULL) - both PostgreSQL and SQLite agree on this, so
        # the 24 (and growing) historical rows enqueued before `user_id`
        # existed, and any future genuinely-unresolvable-owner row, can
        # freely coexist without ever needing special-casing here.
        UniqueConstraint(
            "user_id", "discovered_listing_id", name="uq_pending_notifications_user_id_discovered_listing_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # `ondelete="CASCADE"`: if the listing itself is ever removed (e.g.
    # by the historical relevance cleanup script), an unsent notification
    # about it is meaningless and should go with it.
    #
    # No longer `unique=True` on its own (Phase 2D-SCHEMA - see this
    # table's `UniqueConstraint` above for the replacement). `index=True`
    # here instead: the composite unique index above leads with `user_id`,
    # which doesn't efficiently serve a lookup keyed on `discovered_
    # listing_id` alone (e.g. this FK's own `ON DELETE CASCADE` needing to
    # find every notification for a deleted listing) - a dedicated,
    # non-unique index on this column alone closes that gap, matching
    # `ListingAttribution.discovered_listing_id`'s identical precedent and
    # reasoning exactly.
    discovered_listing_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_listings.id", ondelete="CASCADE", name="fk_pending_notifications_discovered_listing_id"),
        nullable=False,
        index=True,
    )

    # Phase 2A schema-only groundwork for multi-user notification
    # delivery (see this model's own docstring) - nullable, and nothing
    # in this codebase writes or reads it yet. `ON DELETE SET NULL`,
    # deliberately not `CASCADE`: unlike `NotificationPreference.user_id`
    # (a *current* setting, meaningless once the user is gone) or
    # `ListingAttribution`'s foreign keys (a *current* visibility grant),
    # this row can carry real delivery/retry history (`sent`/`failed`,
    # `attempt_count`, timestamps) - a past event, not a live
    # relationship. Deleting a user must not silently destroy that
    # history; it only forgets whose notification it specifically was -
    # the row survives with `user_id` reset to `NULL`, exactly the same
    # reasoning `discovered_by_saved_search_id` above already uses for
    # the identical situation. A still-`pending` row left with `user_id
    # IS NULL` this way simply falls back to the existing Case B
    # (`NOTIFICATION_ERROR_OWNER_UNRESOLVED`) path and expires via the
    # already-proven bounded-retry-then-`failed` mechanism - no special
    # handling needed. Deliberately left unindexed for now, matching
    # `DiscoveredListing.metadata_backfill_status`'s own precedent of
    # deferring an index until there's an actual query that needs it -
    # nothing queries by this column yet, since nothing writes it yet.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_pending_notifications_user_id"),
        nullable=True,
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
    # forever. A claim later found to be Case A
    # (`NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG`) has this
    # increment undone in `NotificationOutboxRepository.complete()` -
    # waiting for the user to configure a destination is not a genuine
    # delivery attempt, so it must never count towards that limit.
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
