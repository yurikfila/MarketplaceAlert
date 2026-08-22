"""Historical relevance cleanup: re-evaluates previously-discovered listings
against the CURRENT relevance engine (`core/relevance/`) and removes the
ones it would now reject.

**Why this exists.** Relevance filtering (see CHANGELOG.md's "Relevance
filtering layer" entry) only applies going forward - existing
`discovered_listings` rows persisted before it existed were never
filtered, so a listing like "Makita Battery Holder Wall Mount" (rejected
by today's engine for a "Makita drill" search) can still be sitting in
the table and showing up in `GET /api/v1/listings` even though a fresh
scan would never have kept it. This module is the deliberate, explicit,
one-off cleanup for that pre-existing data - never wired into a request
path, the scheduler, or app startup (see `scripts/cleanup_historical_listings.py`
for how it's actually invoked).

**The schema limitation, and how this works around it honestly.**
`DiscoveredListing` has no relationship to `SavedSearch` at all (see
ARCHITECTURE.md "Local persistence and duplicate detection" and
PROJECT_CONTEXT.md's Mobile API decision) - its dedup identity is only
`(marketplace, external_listing_id)`, deliberately global across every
saved search that happens to match a listing, not scoped to whichever one
discovered it first. There is no stored query, and no foreign key, so
there is no way to reliably ask "was this specific row relevant to the
specific saved search that found it" - that fact was never recorded.

Rather than guess or invent that relationship, this re-evaluates each row
against **every saved search currently targeting that row's marketplace**
(active or paused - pausing a search doesn't mean its owner stopped
caring about the results it already found; only deleting one does, and
deleted saved searches are excluded automatically since they no longer
exist to query). A listing is kept if it's relevant to **at least one**
of them - the closest honest proxy available for "does any current
interest still want this listing" - and removed only if it fails every
one of them. A listing whose marketplace has **no** saved search left to
evaluate it against (e.g. every saved search that ever targeted that
marketplace was deleted) is left untouched: there's nothing to compare it
against, and deleting it would be guessing, not concluding. See
`HistoricalCleanupResult.skipped_no_saved_search_count`.

**A second, smaller data limitation**: `DiscoveredListing` never stored a
listing's description (only `title`, alongside `marketplace`,
`external_listing_id`, `listing_url`, and the two discovery timestamps -
see `core/persistence/models.py`), so historical re-evaluation runs on
title text only. This can only ever make a historical evaluation more
conservative than the original live one would have been (a description
could only have added matching signal, never removed it) - never a
reason to keep something that shouldn't be, so it doesn't compromise the
"never remove something that's actually relevant" guarantee.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.relevance import evaluate_relevance
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


@dataclass(frozen=True)
class RemovedListing:
    """One row `run_historical_cleanup` removed (or `preview_historical_cleanup` would)."""

    id: int
    marketplace: str
    external_listing_id: str
    title: str


@dataclass
class HistoricalCleanupResult:
    """A full accounting of one cleanup pass - nothing here is a guess;
    every count is derived directly from `total_rows`."""

    total_rows: int
    evaluated_count: int
    skipped_no_saved_search_count: int
    kept_count: int
    removed_count: int
    removed: list[RemovedListing] = field(default_factory=list)


def preview_historical_cleanup(session: Session) -> HistoricalCleanupResult:
    """Dry run: computes exactly what `run_historical_cleanup` would remove,
    without deleting or changing anything. Always safe to call."""
    return _evaluate(session)


def run_historical_cleanup(session: Session) -> HistoricalCleanupResult:
    """Deletes every row `preview_historical_cleanup` would flag as no
    longer relevant to any current saved search targeting its marketplace.

    Never touches `SavedSearch`/`SavedSearchMarketplace` rows - read-only
    against those. The caller owns the session's transaction (commit or
    roll back); this only stages deletes and flushes.
    """
    result = _evaluate(session)
    if result.removed:
        listing_repository = ListingRepository(session)
        rows_by_id = {row.id: row for row in listing_repository.list_all()}
        for removed in result.removed:
            row = rows_by_id.get(removed.id)
            if row is not None:
                listing_repository.delete(row)
        session.flush()
    return result


def _evaluate(session: Session) -> HistoricalCleanupResult:
    all_rows = ListingRepository(session).list_all()
    queries_by_marketplace = _queries_by_marketplace(session)

    evaluated_count = 0
    skipped_count = 0
    kept_count = 0
    removed: list[RemovedListing] = []

    for row in all_rows:
        candidate_queries = queries_by_marketplace.get(row.marketplace)
        if not candidate_queries:
            skipped_count += 1
            continue

        evaluated_count += 1
        listing = _to_listing(row)
        if any(evaluate_relevance(query, listing).is_relevant for query in candidate_queries):
            kept_count += 1
        else:
            removed.append(
                RemovedListing(
                    id=row.id,
                    marketplace=row.marketplace,
                    external_listing_id=row.external_listing_id,
                    title=row.title,
                )
            )

    return HistoricalCleanupResult(
        total_rows=len(all_rows),
        evaluated_count=evaluated_count,
        skipped_no_saved_search_count=skipped_count,
        kept_count=kept_count,
        removed_count=len(removed),
        removed=removed,
    )


def _queries_by_marketplace(session: Session) -> dict[str, list[str]]:
    queries: dict[str, list[str]] = {}
    for saved_search in SavedSearchRepository(session).list_all():
        for marketplace in saved_search.marketplaces:
            queries.setdefault(marketplace, []).append(saved_search.query)
    return queries


def _to_listing(row: DiscoveredListing) -> Listing:
    return Listing(
        marketplace=row.marketplace,
        external_listing_id=row.external_listing_id,
        title=row.title,
        listing_url=row.listing_url,
    )
