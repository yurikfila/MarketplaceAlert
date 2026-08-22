"""Duplicate-detection service: turns normalized listings into new-vs-seen.

Works with ``Listing`` objects from any connector - mock or a future real
one - so it never needs to know or care where the listings came from. This
is the piece routes should call; they must never talk to the repository or
the database directly.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.repository import ListingRepository


@dataclass
class ListingDiscoveryResult:
    new_listings: list[Listing] = field(default_factory=list)
    already_seen_count: int = 0


class ListingDiscoveryService:
    """Determines which listings are new vs. already seen, persisting new ones."""

    def __init__(self, session: Session) -> None:
        self._repository = ListingRepository(session)

    def process_listings(
        self, listings: list[Listing], *, saved_search_id: int | None = None
    ) -> ListingDiscoveryResult:
        """Classify each listing as new or already-seen, saving new ones as it goes.

        `saved_search_id` (the saved search whose scan produced `listings`,
        if any - `None` for the legacy `/scan` endpoint) is recorded only
        on newly-discovered rows - see `ListingRepository.save_new()` and
        `DiscoveredListing.discovered_by_saved_search_id`'s docstring.
        """
        result = ListingDiscoveryResult()
        for listing in listings:
            existing = self._repository.get(listing.marketplace, listing.external_listing_id)
            if existing is None:
                self._repository.save_new(listing, saved_search_id=saved_search_id)
                result.new_listings.append(listing)
            else:
                self._repository.touch_last_seen(existing)
                result.already_seen_count += 1
        return result
