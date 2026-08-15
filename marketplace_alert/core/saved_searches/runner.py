"""Runs one saved search end-to-end: search -> dedup -> notify -> bookkeeping.

The single place this logic lives - used by both the manual
`POST /saved-searches/{id}/run` endpoint and the background scanner, so the
two paths can never drift apart (see ARCHITECTURE.md). Depends on
`MarketplaceConnector` only through an injected resolver function (never a
concrete connector class), plus `ListingDiscoveryService` and
`NotificationService` - the same dependencies /scan already uses.

A saved search now targets one or more marketplaces
(`SavedSearch.marketplaces`). Each is searched independently: **resilience
across marketplaces is this module's job** - one marketplace failing (a
missing connector, a network error, a malformed response) is caught,
logged, and recorded as that marketplace's result, and never stops the
other marketplaces in the same run. This is a deliberate change from the
single-marketplace version, where errors propagated out of `run`/
`run_by_id` for the caller to handle - now there's nothing left for a
per-marketplace failure to usefully propagate *to*, since the run always
covers multiple independent marketplaces. Resilience *across saved
searches* is still the scheduler's job (see `core/scheduler/scanner.py`) -
unchanged.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.service import ListingDiscoveryService
from marketplace_alert.core.saved_searches.models import SavedSearch
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository

logger = logging.getLogger(__name__)


@dataclass
class MarketplaceRunResult:
    """One marketplace's outcome from a single saved-search run."""

    marketplace: str
    new_count: int = 0
    already_seen_count: int = 0
    error: str | None = None


@dataclass
class SavedSearchRunResult:
    saved_search_id: int
    results: list[MarketplaceRunResult] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return sum(r.new_count for r in self.results)

    @property
    def already_seen_count(self) -> int:
        return sum(r.already_seen_count for r in self.results)


class SavedSearchRunner:
    """Runs one saved search across all its marketplaces: search -> dedup ->
    notify -> bookkeeping, tolerating a failure in any single marketplace."""

    def __init__(
        self,
        notification_service: NotificationService,
        resolve_connector: Callable[[str], MarketplaceConnector],
    ) -> None:
        self._notification_service = notification_service
        self._resolve_connector = resolve_connector

    def run(self, session: Session, saved_search: SavedSearch) -> SavedSearchRunResult:
        marketplaces = saved_search.marketplaces
        logger.info("Saved search %s started (marketplaces=%s)", saved_search.id, marketplaces)

        results = [
            self._run_one_marketplace(session, saved_search, marketplace) for marketplace in marketplaces
        ]

        SavedSearchRepository(session).mark_scanned(saved_search)

        total_new = sum(r.new_count for r in results)
        total_seen = sum(r.already_seen_count for r in results)
        logger.info(
            "Saved search %s completed: %d new, %d already seen across %d marketplace(s)",
            saved_search.id,
            total_new,
            total_seen,
            len(results),
        )
        return SavedSearchRunResult(saved_search_id=saved_search.id, results=results)

    def _run_one_marketplace(
        self, session: Session, saved_search: SavedSearch, marketplace: str
    ) -> MarketplaceRunResult:
        try:
            connector = self._resolve_connector(marketplace)
            listings = connector.search(saved_search.query)
        except MarketplaceConnectorError as exc:
            logger.error(
                "Saved search %s / %s failed: %s", saved_search.id, marketplace, exc
            )
            return MarketplaceRunResult(marketplace=marketplace, error=str(exc))
        except Exception:
            logger.exception(
                "Saved search %s / %s failed unexpectedly", saved_search.id, marketplace
            )
            return MarketplaceRunResult(marketplace=marketplace, error="An unexpected error occurred")

        discovery_result = ListingDiscoveryService(session).process_listings(listings)
        self._notification_service.notify_new_listings(discovery_result.new_listings)

        logger.info(
            "Saved search %s / %s: %d new, %d already seen",
            saved_search.id,
            marketplace,
            len(discovery_result.new_listings),
            discovery_result.already_seen_count,
        )
        return MarketplaceRunResult(
            marketplace=marketplace,
            new_count=len(discovery_result.new_listings),
            already_seen_count=discovery_result.already_seen_count,
        )

    def run_by_id(self, session: Session, saved_search_id: int) -> SavedSearchRunResult | None:
        """Load the saved search fresh in this session and run it.

        Returns None if it no longer exists (e.g. deleted between being
        picked up as due and actually running).
        """
        saved_search = SavedSearchRepository(session).get(saved_search_id)
        if saved_search is None:
            logger.warning("Saved search %s not found - skipping", saved_search_id)
            return None
        return self.run(session, saved_search)
