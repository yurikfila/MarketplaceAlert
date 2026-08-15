"""Saved-search management: validated CRUD over saved search definitions.

Routes call this, never `SavedSearchRepository` directly. Marketplace
validity is checked via an injected predicate (`is_marketplace_supported`)
rather than importing the connector registry directly - `core/` never
imports concrete connector code; `main.py` wires the real registry function
in. See ARCHITECTURE.md.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from marketplace_alert.core.saved_searches.models import SavedSearch
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.schemas import SavedSearchCreate, SavedSearchUpdate


class UnsupportedMarketplaceError(ValueError):
    """Raised when a saved search names one or more marketplaces with no registered connector."""


class SavedSearchService:
    """Validated CRUD over saved searches. Routes call this, not the repository."""

    def __init__(self, session: Session, is_marketplace_supported: Callable[[str], bool]) -> None:
        self._repository = SavedSearchRepository(session)
        self._is_marketplace_supported = is_marketplace_supported

    def create(self, data: SavedSearchCreate) -> SavedSearch:
        self._validate_marketplaces(data.marketplaces)
        return self._repository.create(
            query=data.query,
            marketplaces=data.marketplaces,
            scan_interval_seconds=data.scan_interval_seconds,
            is_active=data.is_active,
        )

    def get(self, saved_search_id: int) -> SavedSearch | None:
        return self._repository.get(saved_search_id)

    def list_all(self) -> list[SavedSearch]:
        return self._repository.list_all()

    def update(self, saved_search_id: int, data: SavedSearchUpdate) -> SavedSearch | None:
        saved_search = self._repository.get(saved_search_id)
        if saved_search is None:
            return None
        if data.marketplaces is not None:
            self._validate_marketplaces(data.marketplaces)
        return self._repository.update(
            saved_search,
            query=data.query,
            marketplaces=data.marketplaces,
            scan_interval_seconds=data.scan_interval_seconds,
            is_active=data.is_active,
        )

    def delete(self, saved_search_id: int) -> bool:
        return self._repository.delete(saved_search_id)

    def _validate_marketplaces(self, marketplaces: list[str]) -> None:
        unsupported = [m for m in marketplaces if not self._is_marketplace_supported(m)]
        if unsupported:
            raise UnsupportedMarketplaceError(
                f"Marketplace(s) {unsupported!r} have no registered connector"
            )
