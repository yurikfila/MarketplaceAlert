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
    # Parallel to `new_listings` (same index -> same listing) - the
    # persisted row's id, deliberately *not* folded into `new_listings`
    # itself, since that list is serialized directly as an API response
    # elsewhere (`main.py`'s legacy `/scan`) and must stay a plain
    # `list[Listing]`. Exists so a caller that wants to enqueue a
    # notification-outbox row (see `core/persistence/notification_outbox
    # .py`) can do so without a second database lookup - `/scan` simply
    # doesn't use this field, which is exactly why it's additive rather
    # than a replacement.
    new_listing_ids: list[int] = field(default_factory=list)
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

        Deliberately does **not** touch the notification outbox itself -
        this service's job is duplicate-detection bookkeeping only,
        nothing about whether/how a caller wants to be told about a new
        listing. See `new_listing_ids` above for why that's still possible
        without a caller re-querying.
        """
        result = ListingDiscoveryResult()
        for listing in listings:
            existing = self._repository.get(listing.marketplace, listing.external_listing_id)
            if existing is None:
                row = self._repository.save_new(listing, saved_search_id=saved_search_id)
                result.new_listings.append(listing)
                result.new_listing_ids.append(row.id)
            else:
                self._repository.touch_last_seen(existing)
                result.already_seen_count += 1
        return result
