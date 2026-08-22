"""SQLAlchemy models for locally persisted data.

Distinct from ``marketplace_alert.core.models.listing.Listing`` (the
Pydantic model every connector's ``search()`` returns): ``DiscoveredListing``
is what we remember about a listing once it's been seen, so a later scan can
tell new from already-seen.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from marketplace_alert.core.persistence.database import Base


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
