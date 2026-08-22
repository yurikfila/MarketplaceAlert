"""The one shared entrypoint every scan path calls: `filter_relevant_listings()`.

Used by `core/saved_searches/runner.py` (scheduled scans and Run Now both
go through `SavedSearchRunner`) and `main.py`'s legacy `/scan` endpoint -
the only two places connector results are ever turned into persisted/
notified listings. Filtering happens here, before
`ListingDiscoveryService.process_listings()`, so a rejected listing is
never marked as discovered/new and never reaches the notification service.
"""

import logging

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.relevance.evaluator import evaluate_relevance
from marketplace_alert.core.relevance.models import RelevanceFilterResult

logger = logging.getLogger(__name__)


def filter_relevant_listings(
    *,
    query: str,
    listings: list[Listing],
    marketplace: str,
    saved_search_id: int | None = None,
) -> RelevanceFilterResult:
    relevant: list[Listing] = []
    rejected_count = 0
    for listing in listings:
        evaluation = evaluate_relevance(query, listing)
        if evaluation.is_relevant:
            relevant.append(listing)
            continue
        rejected_count += 1
        logger.info(
            "Relevance: rejected %s listing %s for saved search %s (query=%r): score=%d reason=%s",
            marketplace,
            listing.external_listing_id,
            saved_search_id if saved_search_id is not None else "-",
            query,
            evaluation.score,
            evaluation.rejected_reason,
        )
    return RelevanceFilterResult(
        relevant_listings=relevant,
        raw_count=len(listings),
        relevant_count=len(relevant),
        rejected_count=rejected_count,
    )
