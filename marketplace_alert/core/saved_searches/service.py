"""Saved-search management: validated CRUD over saved search definitions.

Routes call this, never `SavedSearchRepository` directly. Marketplace
validity is checked via an injected predicate (`is_marketplace_supported`)
rather than importing the connector registry directly - `core/` never
imports concrete connector code; `main.py` wires the real registry function
in. See ARCHITECTURE.md.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from marketplace_alert.core.connectors.base import MarketplaceConnector
from marketplace_alert.core.saved_searches.models import SavedSearch
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunResult, SavedSearchRunner
from marketplace_alert.core.saved_searches.schemas import SavedSearchCreate, SavedSearchUpdate


class UnsupportedMarketplaceError(ValueError):
    """Raised when a saved search names one or more marketplaces with no registered connector."""


class SavedSearchService:
    """Validated CRUD over saved searches. Routes call this, not the repository."""

    def __init__(
        self,
        session: Session,
        is_marketplace_supported: Callable[[str], bool],
        resolve_connector: Callable[[str], MarketplaceConnector] | None = None,
    ) -> None:
        self._session = session
        self._repository = SavedSearchRepository(session)
        self._is_marketplace_supported = is_marketplace_supported
        # Optional, and unused by every current caller (dependencies.py's
        # get_saved_search_service doesn't pass one - nothing calls
        # run_owned yet). Only needed to actually run_owned() - see that
        # method's docstring.
        self._runner = SavedSearchRunner(resolve_connector=resolve_connector) if resolve_connector is not None else None

    def create(self, data: SavedSearchCreate, *, user_id: int | None = None) -> SavedSearch:
        self._validate_marketplaces(data.marketplaces)
        return self._repository.create(
            query=data.query,
            marketplaces=data.marketplaces,
            scan_interval_seconds=data.scan_interval_seconds,
            is_active=data.is_active,
            user_id=user_id,
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

    # --- Ownership-scoped variants (groundwork for the route-protection
    # phase - see PROJECT_CONTEXT.md's authentication design decision).
    # Not yet called by any route - create/get/list_all/update/delete
    # above remain exactly what every current route uses, unscoped,
    # until a later cutover phase switches them over.

    def get_owned(self, saved_search_id: int, *, user_id: int) -> SavedSearch | None:
        return self._repository.get_owned(saved_search_id, user_id=user_id)

    def list_owned(self, *, user_id: int) -> list[SavedSearch]:
        return self._repository.list_owned(user_id=user_id)

    def update_owned(self, saved_search_id: int, *, user_id: int, data: SavedSearchUpdate) -> SavedSearch | None:
        saved_search = self._repository.get_owned(saved_search_id, user_id=user_id)
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

    def delete_owned(self, saved_search_id: int, *, user_id: int) -> bool:
        return self._repository.delete_owned(saved_search_id, user_id=user_id)

    def run_owned(self, saved_search_id: int, *, user_id: int) -> SavedSearchRunResult | None:
        """The ownership-scoped equivalent of the manual "Run Now" flow
        (`api/v1/saved_searches.py`'s `run_saved_search_now` today
        composes `get`-then-`SavedSearchRunner.run_by_id` unscoped,
        itself, in the route) - this method exists so a *future* route
        never has to compose that itself and risk getting the ordering
        wrong: ownership is verified via `get_owned` first, and
        `SavedSearchRunner` is invoked only if that lookup succeeds. A
        nonexistent id and one owned by a different user are
        indistinguishable - both return `None` without the runner ever
        being called, matching every other `*_owned` method's
        not-found-not-forbidden contract (and directly testable - a test
        can assert the runner was never invoked, not just that nothing
        changed).

        Requires this service to have been constructed with
        `resolve_connector` (see `__init__`) - no current caller
        (`dependencies.py`'s `get_saved_search_service`) provides one,
        since nothing calls this method yet. Raises `RuntimeError` if
        called without one, rather than silently doing nothing - a
        future route wiring this up incompletely should fail loudly, not
        pretend to have run something it didn't.
        """
        saved_search = self._repository.get_owned(saved_search_id, user_id=user_id)
        if saved_search is None:
            return None
        if self._runner is None:
            raise RuntimeError(
                "SavedSearchService.run_owned requires resolve_connector to be provided at construction"
            )
        return self._runner.run_by_id(self._session, saved_search_id)

    def _validate_marketplaces(self, marketplaces: list[str]) -> None:
        unsupported = [m for m in marketplaces if not self._is_marketplace_supported(m)]
        if unsupported:
            raise UnsupportedMarketplaceError(
                f"Marketplace(s) {unsupported!r} have no registered connector"
            )
