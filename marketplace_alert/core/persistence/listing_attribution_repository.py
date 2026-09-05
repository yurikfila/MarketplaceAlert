"""Raw persistence access for `ListingAttribution`.

Mirrors `notification_outbox.py`'s own rule: a table-specific concern gets
its own module even within `core/persistence/`, rather than being folded
into `repository.py` (which owns `DiscoveredListing` alone). See
`core/persistence/models.py:ListingAttribution` for the full schema
reasoning.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketplace_alert.core.persistence.models import ListingAttribution
from marketplace_alert.core.saved_searches.models import SavedSearch


class ListingAttributionRepository:
    """CRUD for `ListingAttribution` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, saved_search_id: int, discovered_listing_id: int) -> ListingAttribution | None:
        stmt = select(ListingAttribution).where(
            ListingAttribution.saved_search_id == saved_search_id,
            ListingAttribution.discovered_listing_id == discovered_listing_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def record_if_missing(self, *, saved_search_id: int, discovered_listing_id: int) -> tuple[ListingAttribution, bool]:
        """Idempotent: returns the existing row (created=False) if this
        exact (saved_search, listing) pair is already attributed - a
        repeated scan of the same saved search must never create a
        second row (`UNIQUE(saved_search_id, discovered_listing_id)`).

        Race-safe the same way `ListingRepository.get_or_create` is (see
        that method's docstring): the check-then-insert here isn't
        atomic, so a genuinely concurrent caller attributing the exact
        same pair at the same instant could still hit the UNIQUE
        constraint - wrapped in a SAVEPOINT so that failure rolls back
        only this one insert, never the caller's whole transaction, and
        is recovered by re-reading rather than raising. In practice this
        specific pair can only be attempted twice concurrently if the
        same saved search's scan overlaps itself, which `SavedSearchRunGuard`
        already prevents - this is defense in depth (also protects a
        future direct caller, like the backfill script, that doesn't go
        through the guard), not a scenario this codebase can currently
        trigger via the scanner alone.
        """
        existing = self.get(saved_search_id=saved_search_id, discovered_listing_id=discovered_listing_id)
        if existing is not None:
            return existing, False

        try:
            with self._session.begin_nested():
                row = ListingAttribution(
                    saved_search_id=saved_search_id,
                    discovered_listing_id=discovered_listing_id,
                    discovered_at=datetime.now(timezone.utc),
                )
                self._session.add(row)
                self._session.flush()
            return row, True
        except IntegrityError:
            existing = self.get(saved_search_id=saved_search_id, discovered_listing_id=discovered_listing_id)
            if existing is None:
                raise
            return existing, False

    def get_earliest_attribution_search_ids(
        self, *, user_id: int, discovered_listing_ids: list[int]
    ) -> dict[int, int]:
        """For each of `discovered_listing_ids`, the id of `user_id`'s own
        earliest-attributed saved search - i.e. what `ListingOut.
        saved_search_id` should report for that row when a user has more
        than one saved search that independently matched the same
        listing (`ListingOut.saved_search_id` stays a single scalar value
        by product decision - see `api/v1/listings.py` - never a list).

        A listing missing from the returned dict means this user has no
        attribution for it at all - callers should treat that as `None`,
        not as an error (shouldn't happen for a listing `list_recent_owned`
        itself just returned to this same user, but this method makes no
        assumption about that).

        Only joins through `saved_searches.user_id` (this user's own
        searches) - another user's earlier attribution of the same
        listing is invisible here, exactly as it must be.
        """
        if not discovered_listing_ids:
            return {}

        stmt = (
            select(
                ListingAttribution.discovered_listing_id,
                ListingAttribution.saved_search_id,
            )
            .join(SavedSearch, ListingAttribution.saved_search_id == SavedSearch.id)
            .where(
                SavedSearch.user_id == user_id,
                ListingAttribution.discovered_listing_id.in_(discovered_listing_ids),
            )
            .order_by(
                ListingAttribution.discovered_listing_id.asc(),
                ListingAttribution.discovered_at.asc(),
                ListingAttribution.id.asc(),
            )
        )
        earliest_by_listing: dict[int, int] = {}
        for discovered_listing_id, saved_search_id in self._session.execute(stmt).all():
            # Ordered earliest-first per listing - the first row seen for
            # a given listing id is its earliest attribution; later rows
            # for the same listing (a second matching search) are skipped.
            earliest_by_listing.setdefault(discovered_listing_id, saved_search_id)
        return earliest_by_listing
