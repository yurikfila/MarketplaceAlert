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
    #
    # Phase 2D-BEHAVIOR note: this field is intentionally left as-is -
    # "globally new canonical listing" - and is no longer what drives
    # notification enqueue (see `newly_attributed_listing_ids` below).
    # Kept exactly as it was for any other caller that only cares about
    # global newness (e.g. logging/counters in `SavedSearchRunner`).
    new_listing_ids: list[int] = field(default_factory=list)
    already_seen_count: int = 0
    # Phase 2D-BEHAVIOR: canonical listing ids for which *this* call's
    # `saved_search_id` just earned a brand-new `ListingAttribution` -
    # i.e. `ListingAttributionRepository.record_if_missing()` returned
    # `created=True` for that (saved_search_id, discovered_listing_id)
    # pair, during *this* scan. This is deliberately not the same set as
    # `new_listing_ids`: a listing that was already globally known (some
    # other search found it first) but that *this* search is matching
    # for the first time still belongs here - that's the entire point
    # (see `SavedSearchRunner`'s enqueue loop and `ListingAttribution`'s
    # own docstring "SECURITY RULE"/newly-created-attribution reasoning).
    # An attribution that already existed (this exact search already
    # matched this exact listing before, on an earlier scan) is never
    # included here, regardless of how "new" the listing looks from any
    # other angle - `created=False` means "nothing to notify about,"
    # full stop, which is what makes deploying this code safe against
    # every `ListingAttribution` row that already exists today (Phase 1's
    # backfill, or any prior scan) never generating a retroactive
    # notification just because this code started running.
    newly_attributed_listing_ids: list[int] = field(default_factory=list)


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
        new vs. already-seen); `record_if_missing`'s own `created` return
        value - whether *this* call is the one that just inserted the
        attribution, as opposed to one that already existed - is captured
        into `newly_attributed_listing_ids` (Phase 2D-BEHAVIOR), which is
        what `SavedSearchRunner` now enqueues notifications from instead
        of `new_listing_ids` - see that field's own docstring above for
        exactly why this distinction is the entire point.

        Deliberately does **not** touch the notification outbox itself -
        this service's job is duplicate-detection and attribution
        bookkeeping only, nothing about whether/how a caller wants to be
        told about a new listing. See `new_listing_ids`/`newly_attributed_
        listing_ids` above for why that's still possible without a caller
        re-querying.
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
                _, attribution_created = self._attribution_repository.record_if_missing(
                    saved_search_id=saved_search_id, discovered_listing_id=row.id
                )
                if attribution_created:
                    result.newly_attributed_listing_ids.append(row.id)
        return result
