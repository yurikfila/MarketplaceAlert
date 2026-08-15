from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from marketplace_alert.core.models.listing import Listing


def test_listing_minimal_valid() -> None:
    listing = Listing(
        marketplace="ebay",
        external_listing_id="12345",
        title="Rolex Submariner",
        listing_url="https://example.com/listing/12345",
    )
    assert listing.marketplace == "ebay"
    assert listing.title == "Rolex Submariner"
    assert listing.description is None
    assert listing.price is None
    # discovered_at should default to "now" without being supplied.
    assert isinstance(listing.discovered_at, datetime)


def test_listing_full_fields() -> None:
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    listing = Listing(
        marketplace="ebay",
        external_listing_id="12345",
        title="Rolex Submariner",
        description="Mint condition",
        price=8500.0,
        currency="USD",
        location="New York, NY",
        seller="watchdealer99",
        condition="used",
        listing_url="https://example.com/listing/12345",
        image_url="https://example.com/images/12345.jpg",
        created_at=created,
    )
    assert listing.price == 8500.0
    assert listing.created_at == created


def test_listing_requires_url() -> None:
    with pytest.raises(ValidationError):
        Listing(
            marketplace="ebay",
            external_listing_id="12345",
            title="Rolex Submariner",
            listing_url="not-a-url",
        )


def test_listing_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        Listing(marketplace="ebay")  # type: ignore[call-arg]
