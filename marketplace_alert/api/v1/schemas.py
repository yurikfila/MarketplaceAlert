"""Pydantic request/response schemas for the versioned mobile API (`/api/v1`).

Deliberately its own module, separate from
`marketplace_alert.core.saved_searches.schemas`, even for the saved-search
shapes that happen to be identical to what the legacy `/saved-searches*`
routes already use (re-exported below, not re-declared, so validation
logic can't drift into two copies) - `api/v1/schemas.py` is meant to stay
the *one* place every mobile API contract is defined, independent of
whatever the legacy routes' schemas evolve into later. See
PROJECT_CONTEXT.md/ARCHITECTURE.md "Mobile API".

Nothing here is ever built from a raw SQLAlchemy model - every response
schema is populated by explicit field-by-field construction in the route
modules (never blind `from_attributes` auto-mapping), which also makes it
impossible to accidentally forget that several `ListingOut` fields aren't
persisted yet (see `api/v1/listings.py`).
"""

from datetime import datetime, timezone

from pydantic import BaseModel, field_validator

# Reused as-is - the mobile saved-search contract this task asks for
# (id/query/marketplaces/is_active/scan_interval_seconds/created_at/
# updated_at/last_scanned_at, plus the same create/update validation rules)
# is exactly what these already provide. Re-declaring the same fields here
# would just be duplicated validation logic that could quietly drift from
# the legacy schemas' rules over time.
from marketplace_alert.core.saved_searches.schemas import (
    SavedSearchCreate,
    SavedSearchRead,
    SavedSearchUpdate,
)

__all__ = [
    "SavedSearchCreate",
    "SavedSearchRead",
    "SavedSearchUpdate",
    "MobileStatus",
    "MarketplaceInfo",
    "MarketplaceRunOutcome",
    "SavedSearchRunResult",
    "ListingOut",
    "ListingListResponse",
]


class MobileStatus(BaseModel):
    """`GET /api/v1/status` response - booleans and plain identifiers only,
    never a credential value or the raw DATABASE_URL."""

    status: str = "ok"
    backend: bool = True
    database: bool
    telegram_configured: bool
    supported_marketplaces: list[str]


class MarketplaceInfo(BaseModel):
    """One entry in `GET /api/v1/marketplaces`."""

    id: str
    name: str
    configured: bool
    available: bool


class MarketplaceRunOutcome(BaseModel):
    """One marketplace's outcome within a mobile-shaped run response.

    `new_count`/`already_seen_count` are post relevance-filtering (see
    `core/relevance/`); `raw_count`/`rejected_count` optionally expose how
    many results the connector returned before filtering and how many of
    those were dropped as not relevant.
    """

    new_count: int
    already_seen_count: int
    error: str | None = None
    raw_count: int = 0
    rejected_count: int = 0


class SavedSearchRunResult(BaseModel):
    """`POST /api/v1/saved-searches/{id}/run` response.

    A different shape than the legacy `SavedSearchRunResponse`
    (`marketplaces` keyed by name here, a list there) - genuinely more
    convenient for a mobile client to consume (no need to scan a list to
    find one marketplace's outcome), not a duplicate of the legacy schema.
    Built from the exact same `SavedSearchRunResult`
    (`core/saved_searches/runner.py`) the legacy endpoint uses - only the
    reshaping differs, never the run itself.
    """

    saved_search_id: int
    query: str
    marketplaces: dict[str, MarketplaceRunOutcome]
    total_new_count: int
    total_already_seen_count: int


class ListingOut(BaseModel):
    """One row from `GET /api/v1/listings`.

    `price`/`currency`/`location`/`seller`/`condition`/`image_url`/
    `source_created_at` reflect whatever the connector that discovered
    this listing actually returned at discovery time - genuinely `null`
    when a marketplace/connector didn't provide that field for this
    listing (never invented), and never refreshed afterwards even if the
    source listing changes (see `DiscoveredListing`'s docstring in
    `core/persistence/models.py`). `saved_search_id` is the saved search
    whose scan *first* discovered this row - `null` for a listing
    discovered via the legacy `/scan` endpoint, or before this column
    existed - and is a "first discovered by" attribution, not an
    exclusive-ownership relationship (see
    `DiscoveredListing.discovered_by_saved_search_id`'s docstring).
    """

    id: int
    marketplace: str
    external_listing_id: str
    title: str
    price: float | None = None
    currency: str | None = None
    location: str | None = None
    seller: str | None = None
    condition: str | None = None
    listing_url: str
    image_url: str | None = None
    source_created_at: datetime | None = None
    first_discovered_at: datetime
    last_seen_at: datetime
    saved_search_id: int | None = None

    @field_validator("source_created_at", "first_discovered_at", "last_seen_at")
    @classmethod
    def ensure_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
        # columns; every value written is UTC, so reattach it here rather
        # than returning an ambiguous naive datetime in API responses -
        # same rule as SavedSearchRead.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class ListingListResponse(BaseModel):
    """`GET /api/v1/listings` response: one page plus pagination metadata."""

    items: list[ListingOut]
    limit: int
    offset: int
    total_count: int
