"""`GET /api/v1/listings` - browse, filter, and sort recently discovered listings.

Backed directly by `ListingRepository` (`core/persistence/`) - the exact
same persisted `DiscoveredListing` rows every scan (mock/Etsy/eBay/Reverb/
Bonanza, manual or scheduled) already writes via `ListingDiscoveryService`.
No new persistence, no duplicated duplicate-detection logic - this
endpoint only *reads*.

**Filters** (all optional, combined with AND):

- `marketplace` - one marketplace id (e.g. `"etsy"`); rejected (422) if not
  a currently-registered connector.
- `marketplaces` - zero or more marketplace ids (repeated query param,
  e.g. `?marketplaces=etsy&marketplaces=ebay`), for a mobile-style
  multi-select filter; a listing matches if its marketplace is any one of
  these. Independent of, and combinable with, the singular `marketplace`
  above (added later, purely additively - existing callers using
  `marketplace` alone are unaffected). Each value is validated the same
  way `marketplace` is (422 if any isn't a currently-registered connector).
- `saved_search_id` - the saved search whose scan *first* discovered a
  listing (see `ListingOut`'s docstring - a "first discovered by"
  attribution, not exclusive ownership). Must be a positive integer;
  an id that doesn't match any row simply returns zero results, same as
  any other filter that happens to match nothing - this endpoint never
  404s based on a filter value.
- `min_price`/`max_price` - inclusive bounds on `price`; a listing with no
  stored price never matches either bound (it's neither "at least X" nor
  "at most Y" - genuinely unknown, not zero). Rejected (422) if
  `min_price > max_price`.
- `currency` - exact match, case-insensitive (e.g. `"usd"` matches `"USD"`).
- `condition` - exact match, case-insensitive - marketplaces return a
  small, fairly fixed vocabulary here ("New", "Used", ...), unlike
  `location` below.
- `location` - case-insensitive substring match against the stored free
  text (never parsed/geocoded - see `ARCHITECTURE.md`).
- `discovered_after`/`discovered_before` - inclusive bounds on
  `first_discovered_at` (when *our* system found it, never the
  marketplace's own listing-creation time - see "One consistent
  timestamp strategy" below).
- `new_since` - convenience alias with the exact same meaning as
  `discovered_after` (both narrow the same column; if both are given,
  both apply - not a conflict, just two ways to express "how far back").
  Named separately so a client polling for "what's new since I last
  checked" can express that intent clearly without reaching for the more
  general `discovered_after`/`discovered_before` pair.

**Sort** (`sort`, default `"newest"`): `newest`, `oldest`, `price_asc`,
`price_desc`. A `null` price always sorts last in either price mode - see
`ListingRepository._sort_clause()`'s docstring for why.

**One consistent timestamp strategy**: every filter/sort here that means
"how recent" operates on `first_discovered_at` only - never
`source_created_at` (the marketplace's own listing-creation timestamp,
display-only, exposed on `ListingOut` but not filterable/sortable here).
This keeps "new" unambiguous: a listing that's old on the marketplace but
was only just surfaced by a brand-new saved search still counts as newly
discovered, which is what a user actually cares about.

**One remaining known limitation** (see PROJECT_CONTEXT.md/ARCHITECTURE.md
"Mobile API" for the full explanation):

- No `only_new` filter is offered - "new" is inherently relative to one
  specific scan run (see `ListingDiscoveryService`), not a persisted
  property of a row; there's no reliable stored flag to filter on without
  misrepresenting what's actually in the database. Use
  `new_since`/`discovered_after` with a client-tracked timestamp instead.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import ListingListResponse, ListingOut
from marketplace_alert.connectors.registry import is_marketplace_supported
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository, ListingSort

router = APIRouter(tags=["Mobile API - Listings"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_CURRENCY_LENGTH = 8  # ISO 4217 codes are 3 letters; generous headroom, never unbounded


def _to_listing_out(row: DiscoveredListing) -> ListingOut:
    """Explicit field-by-field mapping, not `from_attributes` auto-mapping -
    keeps every field's source column visible in one place, rather than
    relying on Pydantic to silently match them up by name."""
    return ListingOut(
        id=row.id,
        marketplace=row.marketplace,
        external_listing_id=row.external_listing_id,
        title=row.title,
        price=row.price,
        currency=row.currency,
        location=row.location,
        seller=row.seller,
        condition=row.condition,
        listing_url=row.listing_url,
        image_url=row.image_url,
        source_created_at=row.source_created_at,
        first_discovered_at=row.first_discovered_at,
        last_seen_at=row.last_seen_at,
        saved_search_id=row.discovered_by_saved_search_id,
    )


@router.get(
    "/listings",
    summary="Browse, filter, and sort recently discovered listings",
    description=(
        "See this endpoint's module docstring for the full filter/sort "
        "reference, the one-consistent-timestamp-strategy rule, and the "
        "one filter intentionally not offered (only_new) and why."
    ),
)
def list_listings(
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT, description="Max rows to return (1-100)."),
    offset: int = Query(0, ge=0, description="Rows to skip, for pagination."),
    marketplace: str | None = Query(None, description="Filter to one marketplace id, e.g. 'etsy'."),
    marketplaces: list[str] | None = Query(
        None, description="Filter to any of several marketplace ids (repeat the param)."
    ),
    saved_search_id: int | None = Query(
        None, gt=0, description="Filter to listings first discovered by this saved search."
    ),
    min_price: float | None = Query(None, ge=0, description="Inclusive lower price bound."),
    max_price: float | None = Query(None, ge=0, description="Inclusive upper price bound."),
    currency: str | None = Query(
        None, min_length=1, max_length=_MAX_CURRENCY_LENGTH, description="Exact currency match, case-insensitive."
    ),
    condition: str | None = Query(None, min_length=1, description="Exact condition match, case-insensitive."),
    location: str | None = Query(None, min_length=1, description="Case-insensitive substring match."),
    discovered_after: datetime | None = Query(None, description="Inclusive lower bound on first_discovered_at."),
    discovered_before: datetime | None = Query(None, description="Inclusive upper bound on first_discovered_at."),
    new_since: datetime | None = Query(
        None, description="Alias for discovered_after - listings discovered at or after this time."
    ),
    sort: ListingSort = Query("newest", description="newest | oldest | price_asc | price_desc"),
    session: Session = Depends(get_db_session),
) -> ListingListResponse:
    if marketplace is not None and not is_marketplace_supported(marketplace):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown marketplace {marketplace!r}",
        )
    if marketplaces is not None:
        unknown = [m for m in marketplaces if not is_marketplace_supported(m)]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown marketplace(s): {', '.join(repr(m) for m in unknown)}",
            )
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="min_price must not be greater than max_price",
        )

    filters = {
        "marketplace": marketplace,
        "marketplaces": marketplaces,
        "saved_search_id": saved_search_id,
        "min_price": min_price,
        "max_price": max_price,
        "currency": currency,
        "condition": condition,
        "location": location,
        "discovered_after": discovered_after,
        "discovered_before": discovered_before,
    }

    repository = ListingRepository(session)
    rows = repository.list_recent(limit=limit, offset=offset, sort=sort, new_since=new_since, **filters)
    total_count = repository.count(new_since=new_since, **filters)

    return ListingListResponse(
        items=[_to_listing_out(row) for row in rows],
        limit=limit,
        offset=offset,
        total_count=total_count,
    )
