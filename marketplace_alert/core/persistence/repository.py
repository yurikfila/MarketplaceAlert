"""Raw persistence access for discovered listings.

Only this module (plus ``database.py`` and ``models.py``) knows any SQL/ORM
details. Everything above it - the service layer, routes - works with
``Listing`` and plain Python types only. See ``ARCHITECTURE.md``.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing


class ListingRepository:
    """Persistence operations for ``DiscoveredListing`` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, marketplace: str, external_listing_id: str) -> DiscoveredListing | None:
        """Return the stored row for this listing, or None if never seen before."""
        stmt = select(DiscoveredListing).where(
            DiscoveredListing.marketplace == marketplace,
            DiscoveredListing.external_listing_id == external_listing_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def save_new(self, listing: Listing) -> DiscoveredListing:
        """Persist a listing seen for the first time."""
        now = datetime.now(timezone.utc)
        row = DiscoveredListing(
            marketplace=listing.marketplace,
            external_listing_id=listing.external_listing_id,
            title=listing.title,
            listing_url=str(listing.listing_url),
            first_discovered_at=now,
            last_seen_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def touch_last_seen(self, row: DiscoveredListing) -> None:
        """Update last_seen_at for a listing that showed up again."""
        row.last_seen_at = datetime.now(timezone.utc)
        self._session.add(row)
