"""The one shared entrypoint every scan path calls: `filter_relevant_listings()`.

Used by `core/saved_searches/runner.py` (scheduled scans and Run Now both
go through `SavedSearchRunner`) and `main.py`'s legacy `/scan` endpoint -
the only two places connector results are ever turned into persisted/
notified listings. Filtering happens here, before
`ListingDiscoveryService.process_listings()`, so a rejected listing is
never marked as discovered/new and never reaches the notification service.
"""

import logging
from dataclasses import dataclass

from marketplace_alert.config import settings
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.relevance.engine_v2 import evaluate_relevance_v2
from marketplace_alert.core.relevance.evaluator import evaluate_relevance
from marketplace_alert.core.relevance.models import RelevanceEvaluation, RelevanceFilterResult

logger = logging.getLogger(__name__)

ENGINE_LEGACY = "legacy"
ENGINE_V2 = "v2"
ENGINE_SHADOW = "shadow"


def resolve_engine(engine: str | None = None) -> str:
    """`engine` if given, else `settings.relevance_engine`; anything
    unrecognized falls back to legacy (the safe default)."""
    chosen = (engine if engine is not None else settings.relevance_engine or ENGINE_LEGACY).strip().lower()
    return chosen if chosen in (ENGINE_LEGACY, ENGINE_V2, ENGINE_SHADOW) else ENGINE_LEGACY


@dataclass
class ShadowStats:
    """What v2 would have decided differently during one `filter_relevant_listings`
    call in `shadow` mode. Counters only - nothing here ever feeds back into a decision."""

    compared: int = 0
    v2_stricter: int = 0  # legacy accepts, v2 rejects
    v2_looser: int = 0  # legacy rejects, v2 accepts
    v2_errors: int = 0
    detail_lines_logged: int = 0

    @property
    def disagreements(self) -> int:
        return self.v2_stricter + self.v2_looser


def _evaluate(
    engine: str,
    query: str,
    listing: Listing,
    min_score: int,
    saved_search_id: int | None,
    shadow: ShadowStats,
    max_detail_logs: int,
) -> RelevanceEvaluation:
    if engine == ENGINE_V2:
        return evaluate_relevance_v2(query, listing, min_score=min_score)
    legacy = evaluate_relevance(query, listing)
    if engine == ENGINE_SHADOW:
        # Legacy is the only decision maker: `legacy` is returned unconditionally below.
        try:
            candidate = evaluate_relevance_v2(query, listing, min_score=min_score)
        except Exception:  # noqa: BLE001 - shadow evaluation must never affect a scan
            shadow.v2_errors += 1
        else:
            shadow.compared += 1
            if candidate.is_relevant != legacy.is_relevant:
                if legacy.is_relevant:
                    shadow.v2_stricter += 1
                else:
                    shadow.v2_looser += 1
                if shadow.detail_lines_logged < max_detail_logs:
                    shadow.detail_lines_logged += 1
                    # Ids and verdict codes only - never the query, title or description (user-entered / private).
                    logger.info(
                        "Relevance shadow detail: saved search %s listing %s legacy=%s(%s) v2=%s(%s)",
                        saved_search_id if saved_search_id is not None else "-",
                        listing.external_listing_id,
                        "accept" if legacy.is_relevant else "reject",
                        legacy.rejected_reason or "ok",
                        "accept" if candidate.is_relevant else "reject",
                        candidate.rejected_reason or "ok",
                    )
    return legacy


def _log_shadow_summary(stats: ShadowStats, marketplace: str, saved_search_id: int | None) -> None:
    """One bounded line per filter call (per saved search x marketplace per scan):
    the measurable record the shadow observation period is judged on."""
    logger.info(
        "Relevance shadow summary: saved search %s marketplace %s compared=%d disagreements=%d "
        "v2_stricter=%d v2_looser=%d v2_errors=%d",
        saved_search_id if saved_search_id is not None else "-",
        marketplace,
        stats.compared,
        stats.disagreements,
        stats.v2_stricter,
        stats.v2_looser,
        stats.v2_errors,
    )


def filter_relevant_listings(
    *,
    query: str,
    listings: list[Listing],
    marketplace: str,
    saved_search_id: int | None = None,
    engine: str | None = None,
    min_score: int | None = None,
) -> RelevanceFilterResult:
    chosen_engine = resolve_engine(engine)
    threshold = settings.relevance_min_score if min_score is None else min_score
    max_detail_logs = max(0, settings.relevance_shadow_max_detail_logs)
    shadow = ShadowStats()
    relevant: list[Listing] = []
    rejected_count = 0
    for listing in listings:
        evaluation = _evaluate(chosen_engine, query, listing, threshold, saved_search_id, shadow, max_detail_logs)
        if evaluation.is_relevant:
            relevant.append(listing)
            continue
        rejected_count += 1
        # The saved search is identified by id only - never log `query`: it is
        # user-entered text (see tests/test_log_privacy.py).
        logger.info(
            "Relevance: rejected %s listing %s for saved search %s: score=%d reason=%s",
            marketplace,
            listing.external_listing_id,
            saved_search_id if saved_search_id is not None else "-",
            evaluation.score,
            evaluation.rejected_reason,
        )
    if chosen_engine == ENGINE_SHADOW:
        _log_shadow_summary(shadow, marketplace, saved_search_id)
    return RelevanceFilterResult(
        relevant_listings=relevant,
        raw_count=len(listings),
        relevant_count=len(relevant),
        rejected_count=rejected_count,
    )
