"""Pydantic request/response schemas for the saved-search API.

Validation split by concern: shape/type rules (non-empty query, minimum
interval, non-empty/unique marketplace list) live here, at the schema
level, since they need no external knowledge. Whether each marketplace name
actually has a registered connector is a business rule that needs the
connector registry - that's validated in `SavedSearchService`, not here
(see ARCHITECTURE.md).
"""

from datetime import datetime, timezone

from pydantic import BaseModel, field_validator

# Keeps the scanner from hammering a marketplace (or Telegram) too often.
# Tests are free to use shorter intervals by inserting rows directly via
# SavedSearchRepository, bypassing this schema - see tests/conftest.py.
MIN_SCAN_INTERVAL_SECONDS = 60


def _validate_marketplaces_shape(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("at least one marketplace must be selected")
    if len(value) != len(set(value)):
        raise ValueError("duplicate marketplace selections are not allowed")
    return value


class SavedSearchCreate(BaseModel):
    query: str
    marketplaces: list[str] = ["mock"]
    scan_interval_seconds: int = MIN_SCAN_INTERVAL_SECONDS
    is_active: bool = True

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query cannot be empty")
        return stripped

    @field_validator("marketplaces")
    @classmethod
    def marketplaces_must_be_valid(cls, value: list[str]) -> list[str]:
        return _validate_marketplaces_shape(value)

    @field_validator("scan_interval_seconds")
    @classmethod
    def interval_must_meet_minimum(cls, value: int) -> int:
        if value < MIN_SCAN_INTERVAL_SECONDS:
            raise ValueError(f"scan_interval_seconds must be at least {MIN_SCAN_INTERVAL_SECONDS}")
        return value


class SavedSearchUpdate(BaseModel):
    """PATCH body: every field optional, only what's provided gets changed.

    `marketplaces`, if provided, *replaces* the full set (add/remove a
    marketplace by sending the new complete list) - the same "set to this
    value if provided" semantics every other field on this schema already
    has, not an incremental add/remove.
    """

    query: str | None = None
    marketplaces: list[str] | None = None
    scan_interval_seconds: int | None = None
    is_active: bool | None = None

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("query cannot be empty")
        return stripped

    @field_validator("marketplaces")
    @classmethod
    def marketplaces_must_be_valid(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        return _validate_marketplaces_shape(value)

    @field_validator("scan_interval_seconds")
    @classmethod
    def interval_must_meet_minimum(cls, value: int | None) -> int | None:
        if value is not None and value < MIN_SCAN_INTERVAL_SECONDS:
            raise ValueError(f"scan_interval_seconds must be at least {MIN_SCAN_INTERVAL_SECONDS}")
        return value


class SavedSearchRead(BaseModel):
    id: int
    query: str
    marketplaces: list[str]
    is_active: bool
    scan_interval_seconds: int
    created_at: datetime
    updated_at: datetime
    last_scanned_at: datetime | None

    model_config = {"from_attributes": True}

    @field_validator("created_at", "updated_at", "last_scanned_at")
    @classmethod
    def ensure_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
        # columns; every value written is UTC, so reattach it here rather
        # than returning an ambiguous naive datetime in API responses.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class MarketplaceRunResult(BaseModel):
    """One marketplace's outcome from a single saved-search run."""

    marketplace: str
    new_count: int
    already_seen_count: int
    error: str | None = None


class SavedSearchRunResponse(BaseModel):
    saved_search_id: int
    results: list[MarketplaceRunResult]
    new_count: int
    already_seen_count: int
