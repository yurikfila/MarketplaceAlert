"""`GET /api/v1/listings` - browse recently discovered listings.

Backed directly by `ListingRepository` (`core/persistence/`) - the exact
same persisted `DiscoveredListing` rows every scan (mock/Etsy/eBay, manual
or scheduled) already writes via `ListingDiscoveryService`. No new
persistence, no duplicated duplicate-detection logic - this endpoint only
*reads*.

**Known limitations** (see PROJECT_CONTEXT.md/ARCHITECTURE.md "Mobile API"
for the full explanation - documented, never faked):

- `price`/`currency`/`location`/`condition`/`image_url` are always `null`
  below. `DiscoveredListing` does not persist them - only `marketplace`,
  `external_listing_id`, `title`, `listing_url`, and the two discovery
  timestamps are stored today. Extending persistence to store the rest of
  `Listing`'s fields is a separate, larger change, not part of this one.
- No `saved_search_id` filter is offered. `DiscoveredListing` has no
  relationship to `SavedSearch` at all - its dedup identity is
  `(marketplace, external_listing_id)` only, global across every saved
  search that happens to match it. Adding a filter here would mean
  inventing a relationship that isn't actually stored, which this task's
  own instructions explicitly rule out.
- No `only_new` filter is offered either - "new" is inherently relative to
  one specific scan run (see `ListingDiscoveryService`), not a persisted
  property of a row; there's no reliable stored flag to filter on without
  misrepresenting what's actually in the database.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import ListingListResponse, ListingOut
from marketplace_alert.connectors.registry import is_marketplace_supported
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository

router = APIRouter(tags=["Mobile API - Listings"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _to_listing_out(row: DiscoveredListing) -> ListingOut:
    """Explicit field-by-field mapping, not `from_attributes` auto-mapping -
    several `ListingOut` fields don't exist on `DiscoveredListing` at all
    yet (see this module's docstring); being explicit here makes that
    impossible to miss, rather than relying on Pydantic to silently
    default them."""
    return ListingOut(
        id=row.id,
        marketplace=row.marketplace,
        external_listing_id=row.external_listing_id,
        title=row.title,
        price=None,
        currency=None,
        location=None,
        condition=None,
        listing_url=row.listing_url,
        image_url=None,
        first_discovered_at=row.first_discovered_at,
        last_seen_at=row.last_seen_at,
    )


@router.get(
    "/listings",
    summary="Browse recently discovered listings",
    description=(
        "Newest-discovered first. See this endpoint's module docstring "
        "for fields/filters intentionally not offered yet "
        "(price/currency/location/condition/image_url are always null; "
        "no saved_search_id or only_new filter) and why."
    ),
)
def list_listings(
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Max rows to return (1-100)."),
    offset: int = Query(0, ge=0, description="Rows to skip, for pagination."),
    marketplace: str | None = Query(None, description="Filter to one marketplace id, e.g. 'etsy'."),
    session: Session = Depends(get_db_session),
) -> ListingListResponse:
    if marketplace is not None and not is_marketplace_supported(marketplace):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown marketplace {marketplace!r}",
        )

    repository = ListingRepository(session)
    rows = repository.list_recent(limit=limit, offset=offset, marketplace=marketplace)
    total_count = repository.count(marketplace=marketplace)

    return ListingListResponse(
        items=[_to_listing_out(row) for row in rows],
        limit=limit,
        offset=offset,
        total_count=total_count,
    )
