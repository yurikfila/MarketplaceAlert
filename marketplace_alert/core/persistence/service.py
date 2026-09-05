"""Duplicate-detection service: turns normalized listings into new-vs-seen.

Works with ``Listing`` objects from any connector - mock or a future real
one - so it never needs to know or care where the listings came from. This
is the piece routes should call; they must never talk to the repository or
the database directly.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
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
        self._attribution_repository = ListingAttributionRepository(session)

    def process_listings(
        self, listings: list[Listing], *, saved_search_id: int | None = None
    ) -> ListingDiscoveryResult:
        """Classify each listing as new or already-seen, saving new ones as it goes.

        `saved_search_id` (the saved search whose scan produced `listings`,
        if any - `None` for the legacy `/scan` endpoint) is recorded on
        newly-discovered rows exactly as before (see `ListingRepository
        .save_new()` and `DiscoveredListing.discovered_by_saved_search_id`'s
        docstring - that historical "first discovered by" fact is
        untouched by Phase 1 of multi-user listing attribution).

        **What's new**: regardless of whether the canonical listing itself
        was globally new or already existed (discovered earlier by some
        other search entirely), a `ListingAttribution` row is recorded for
        *this* `saved_search_id` too, if one is given - see
        `ListingAttributionRepository.record_if_missing`. This is the
        actual fix for the global-dedup limitation: every search that
        genuinely matches a listing gets its own attribution, independent
        of who found it first. `new_listings`/`new_listing_ids`/
        `already_seen_count` keep their exact original meaning (globally
        new vs. already-seen) - callers that only enqueue notifications
        for `new_listing_ids` (see `SavedSearchRunner`) are completely
        unaffected; per-search notification delivery for a listing that
        was already-globally-seen is explicitly out of scope for this
        phase (see `core/persistence/models.py:ListingAttribution`'s
        docstring).

        Deliberately does **not** touch the notification outbox itself -
        this service's job is duplicate-detection and attribution
        bookkeeping only, nothing about whether/how a caller wants to be
        told about a new listing. See `new_listing_ids` above for why
        that's still possible without a caller re-querying.
        """
        result = ListingDiscoveryResult()
        for listing in listings:
            row, created = self._repository.get_or_create(listing, saved_search_id=saved_search_id)
            if created:
                result.new_listings.append(listing)
                result.new_listing_ids.append(row.id)
            else:
                self._repository.touch_last_seen(row)
                result.already_seen_count += 1

            if saved_search_id is not None:
                self._attribution_repository.record_if_missing(
                    saved_search_id=saved_search_id, discovered_listing_id=row.id
                )
        return result
