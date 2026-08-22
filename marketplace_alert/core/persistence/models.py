"""SQLAlchemy models for locally persisted data.

Distinct from ``marketplace_alert.core.models.listing.Listing`` (the
Pydantic model every connector's ``search()`` returns): ``DiscoveredListing``
is what we remember about a listing once it's been seen, so a later scan can
tell new from already-seen.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, UniqueConstraint
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
