"""A fake marketplace connector with a fixed catalog of listings.

We are waiting on eBay Developer API approval, so there is no real
connector yet. This connector exists to let the rest of the system (search,
filters, and eventually dedup/alerting) be built and exercised end-to-end
without any external API credentials.

It implements ``MarketplaceConnector`` like any real connector would, so it
is a faithful stand-in for testing - but it is intentionally isolated in its
own subpackage under ``marketplace_alert/connectors/`` so it can later be
deleted or ignored without touching real connectors, and a real connector
must never be built by editing this file in place.
"""

from typing import Any

from marketplace_alert.core.connectors.base import MarketplaceConnector
from marketplace_alert.core.models.listing import Listing

_MOCK_LISTINGS: list[dict[str, Any]] = [
    {
        "id": "mock-001",
        "title": "Maccabi Tel Aviv Vintage Football Shirt",
        "description": "Vintage 1990s Maccabi Tel Aviv home shirt, size L.",
        "price": 45.0,
        "currency": "USD",
        "location": "Tel Aviv, Israel",
        "seller": "vintage_kits_il",
        "condition": "used",
        "image_url": "https://example.com/images/maccabi-shirt.jpg",
    },
    {
        "id": "mock-002",
        "title": "Hapoel Tel Aviv Fan Scarf",
        "description": "Official Hapoel Tel Aviv fan scarf, one size.",
        "price": 15.0,
        "currency": "USD",
        "location": "Tel Aviv, Israel",
        "seller": "fan_gear_shop",
        "condition": "new",
        "image_url": "https://example.com/images/hapoel-scarf.jpg",
    },
    {
        "id": "mock-003",
        "title": "Rolex Submariner Date 116610LN",
        "description": "Rolex Submariner, box and papers included.",
        "price": 12500.0,
        "currency": "USD",
        "location": "New York, NY",
        "seller": "watchdealer99",
        "condition": "used",
        "image_url": "https://example.com/images/rolex-submariner.jpg",
    },
    {
        "id": "mock-004",
        "title": "Makita 18V Cordless Drill Driver",
        "description": "Makita DHP482 cordless combi drill, 2 batteries included.",
        "price": 120.0,
        "currency": "USD",
        "location": "Chicago, IL",
        "seller": "tool_outlet",
        "condition": "new",
        "image_url": "https://example.com/images/makita-drill.jpg",
    },
    {
        "id": "mock-005",
        "title": "Pokemon Charizard Holo Card 1999",
        "description": "1999 base set Charizard holographic card.",
        "price": 350.0,
        "currency": "USD",
        "location": "Los Angeles, CA",
        "seller": "card_collector_88",
        "condition": "used",
        "image_url": "https://example.com/images/pokemon-charizard.jpg",
    },
    {
        "id": "mock-006",
        "title": "Adidas Originals Track Jacket",
        "description": "Classic Adidas Originals track jacket, size M.",
        "price": 60.0,
        "currency": "USD",
        "location": "Berlin, Germany",
        "seller": "streetwear_hub",
        "condition": "new",
        "image_url": "https://example.com/images/adidas-jacket.jpg",
    },
]


class MockMarketplaceConnector(MarketplaceConnector):
    """Searches a fixed, in-memory catalog of fake listings.

    Supports the same shape of call as a real connector - free-text query
    plus optional filters - so code written against this connector keeps
    working unchanged once a real one exists.
    """

    @property
    def marketplace_name(self) -> str:
        return "mock"

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        needle = query.strip().lower()
        filters = filters or {}
        min_price = filters.get("min_price")
        max_price = filters.get("max_price")
        condition = filters.get("condition")

        matches = []
        for item in _MOCK_LISTINGS:
            if needle not in item["title"].lower():
                continue
            if min_price is not None and item["price"] < min_price:
                continue
            if max_price is not None and item["price"] > max_price:
                continue
            if condition is not None and item["condition"].lower() != str(condition).lower():
                continue
            matches.append(item)

        return [self.normalize_listing(item) for item in matches]

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=raw_listing["id"],
            title=raw_listing["title"],
            description=raw_listing.get("description"),
            price=raw_listing.get("price"),
            currency=raw_listing.get("currency"),
            location=raw_listing.get("location"),
            seller=raw_listing.get("seller"),
            condition=raw_listing.get("condition"),
            listing_url=f"https://mock-marketplace.example.com/listing/{raw_listing['id']}",
            image_url=raw_listing.get("image_url"),
        )

    def get_listing_by_id(self, external_listing_id: str) -> Listing | None:
        """Looks up a fixed catalog entry by id - a genuine, if trivial,
        implementation (not a stub), so tests that exercise the
        historical backfill service (`core/persistence/backfill.py`)
        against a real connector interface don't need real marketplace
        credentials. Returns `None` for an id not in the catalog, the
        same "no longer exists" signal a real connector's 404 produces."""
        for item in _MOCK_LISTINGS:
            if item["id"] == external_listing_id:
                return self.normalize_listing(item)
        return None

    def health_check(self) -> bool:
        return True
