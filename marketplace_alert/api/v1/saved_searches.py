"""`/api/v1/saved-searches*` - versioned (mobile) saved-search endpoints.

Every endpoint here is a thin adapter over the exact same
`SavedSearchService` / `SavedSearchRunner` / `SavedSearchRunGuard` the
legacy `/saved-searches*` routes (`main.py`) use, wired from
`marketplace_alert.dependencies` - no saved-search business logic is
duplicated for the mobile API, only the response *shape* differs for the
manual-run endpoint (see `SavedSearchRunResult` in `api/v1/schemas.py`).
Kept as its own module/contract so it can stay stable for a mobile client
even if the legacy routes' shapes evolve independently later.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import (
    MarketplaceRunOutcome,
    SavedSearchCreate,
    SavedSearchRead,
    SavedSearchRunResult,
    SavedSearchUpdate,
)
from marketplace_alert.connectors.registry import get_connector
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.saved_searches.service import SavedSearchService, UnsupportedMarketplaceError
from marketplace_alert.dependencies import get_saved_search_service, saved_search_run_guard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/saved-searches", tags=["Mobile API - Saved Searches"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a saved search",
    description="Identical validation and behavior to the legacy `POST /saved-searches`.",
)
def create_saved_search(
    data: SavedSearchCreate, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    try:
        saved_search = service.create(data)
    except UnsupportedMarketplaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return SavedSearchRead.model_validate(saved_search)


@router.get("", summary="List saved searches")
def list_saved_searches(
    service: SavedSearchService = Depends(get_saved_search_service),
) -> list[SavedSearchRead]:
    return [SavedSearchRead.model_validate(s) for s in service.list_all()]


@router.get("/{saved_search_id}", summary="Get one saved search")
def get_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    saved_search = service.get(saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return SavedSearchRead.model_validate(saved_search)


@router.patch(
    "/{saved_search_id}",
    summary="Update a saved search",
    description=(
        "Only provided fields are changed. `marketplaces`, if provided, "
        "replaces the full set (not an incremental add/remove)."
    ),
)
def update_saved_search(
    saved_search_id: int,
    data: SavedSearchUpdate,
    service: SavedSearchService = Depends(get_saved_search_service),
) -> SavedSearchRead:
    try:
        saved_search = service.update(saved_search_id, data)
    except UnsupportedMarketplaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return SavedSearchRead.model_validate(saved_search)


@router.delete(
    "/{saved_search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved search",
)
def delete_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> None:
    if not service.delete(saved_search_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")


@router.post(
    "/{saved_search_id}/run",
    summary="Run a saved search now",
    description=(
        "Immediately runs one saved search through the exact same "
        "SavedSearchRunner and run guard as the legacy "
        "`POST /saved-searches/{id}/run` (they share the guard, so the "
        "same saved search can never run through both paths, or the "
        "background scheduler, at once) - only the response shape differs, "
        "grouped by marketplace name rather than a list, for easier mobile "
        "consumption. 404 if the saved search doesn't exist, 409 if it's "
        "inactive or already running."
    ),
)
def run_saved_search_now(
    saved_search_id: int,
    session: Session = Depends(get_db_session),
) -> SavedSearchRunResult:
    saved_search = SavedSearchRepository(session).get(saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    if not saved_search.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Saved search is inactive")

    if not saved_search_run_guard.try_acquire(saved_search_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Saved search is already running"
        )
    try:
        # A fresh runner per request - same pattern the legacy endpoint
        # uses, deliberately not the shared dependencies.py singleton
        # runner. No notification dependency to inject at all anymore
        # (see SavedSearchRunner's module docstring) - a manual run only
        # ever enqueues outbox rows, the same as a scheduled one.
        runner = SavedSearchRunner(resolve_connector=get_connector)
        result = runner.run(session, saved_search)
    finally:
        saved_search_run_guard.release(saved_search_id)

    marketplaces = {
        r.marketplace: MarketplaceRunOutcome(
            new_count=r.new_count,
            already_seen_count=r.already_seen_count,
            error=r.error,
            raw_count=r.raw_count,
            rejected_count=r.rejected_count,
        )
        for r in result.results
    }
    return SavedSearchRunResult(
        saved_search_id=result.saved_search_id,
        query=saved_search.query,
        marketplaces=marketplaces,
        total_new_count=result.new_count,
        total_already_seen_count=result.already_seen_count,
    )
