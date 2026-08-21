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
    """One marketplace's outcome within a mobile-shaped run response."""

    new_count: int
    already_seen_count: int
    error: str | None = None


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

    `price`/`currency`/`location`/`condition`/`image_url` are always
    `null` today - `DiscoveredListing` (`core/persistence/models.py`) does
    not persist them yet (only `marketplace`, `external_listing_id`,
    `title`, `listing_url`, and the two timestamps are stored). Documented,
    not hidden - see `api/v1/listings.py`'s module docstring and
    PROJECT_CONTEXT.md/ARCHITECTURE.md "Mobile API" for the full
    explanation of this limitation.
    """

    id: int
    marketplace: str
    external_listing_id: str
    title: str
    price: float | None = None
    currency: str | None = None
    location: str | None = None
    condition: str | None = None
    listing_url: str
    image_url: str | None = None
    first_discovered_at: datetime
    last_seen_at: datetime

    @field_validator("first_discovered_at", "last_seen_at")
    @classmethod
    def ensure_utc(cls, value: datetime) -> datetime:
        # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
        # columns; every value written is UTC, so reattach it here rather
        # than returning an ambiguous naive datetime in API responses -
        # same rule as SavedSearchRead.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class ListingListResponse(BaseModel):
    """`GET /api/v1/listings` response: one page plus pagination metadata."""

    items: list[ListingOut]
    limit: int
    offset: int
    total_count: int
