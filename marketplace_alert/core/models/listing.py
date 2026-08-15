"""The normalized listing model.

Every marketplace connector's ``normalize_listing`` must return a
``Listing``. This is the single shape the rest of the system (dedup engine,
alerting, storage, API) is written against, so it never needs to know which
marketplace a listing came from.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, HttpUrl


class Listing(BaseModel):
    """A single marketplace listing, normalized to a marketplace-agnostic shape."""

    marketplace: str
    """Identifier of the marketplace this listing came from (e.g. "ebay")."""

    external_listing_id: str
    """The listing's unique ID on the source marketplace."""

    title: str
    description: str | None = None

    price: float | None = None
    currency: str | None = None

    location: str | None = None
    seller: str | None = None
    condition: str | None = None

    listing_url: HttpUrl
    image_url: HttpUrl | None = None

    created_at: datetime | None = None
    """When the listing was originally created on the marketplace, if known."""

    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    """When our system first saw this listing."""
