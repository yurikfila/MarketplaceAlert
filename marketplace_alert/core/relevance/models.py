"""Result types returned by the relevance module. Plain dataclasses, not
Pydantic - these are internal evaluation objects, matching this codebase's
existing convention of using dataclasses for internal result shapes (e.g.
`MarketplaceRunResult`, `ListingDiscoveryResult`) and reserving Pydantic
for the normalized `Listing` model and API schemas.
"""

from dataclasses import dataclass

from marketplace_alert.core.models.listing import Listing


@dataclass(frozen=True)
class RelevanceEvaluation:
    """The transparent, deterministic verdict for one (query, listing) pair."""

    is_relevant: bool
    score: int
    matched_terms: list[str]
    rejected_reason: str | None = None


@dataclass
class RelevanceFilterResult:
    """The outcome of filtering one marketplace's raw search results down
    to the relevant ones. `raw_count`/`relevant_count`/`rejected_count`
    let callers report post-filter counts without recomputing them."""

    relevant_listings: list[Listing]
    raw_count: int
    relevant_count: int
    rejected_count: int
