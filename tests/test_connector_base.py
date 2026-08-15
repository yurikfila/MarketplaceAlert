from typing import Any

import pytest

from marketplace_alert.core.connectors.base import MarketplaceConnector
from marketplace_alert.core.models.listing import Listing


def test_cannot_instantiate_abstract_connector() -> None:
    with pytest.raises(TypeError):
        MarketplaceConnector()  # type: ignore[abstract]


class FakeConnector(MarketplaceConnector):
    """Minimal concrete connector used only to exercise the interface in tests."""

    @property
    def marketplace_name(self) -> str:
        return "fake"

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        return [self.normalize_listing({"id": "1", "title": query})]

    def normalize_listing(self, raw_listing: Any) -> Listing:
        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=raw_listing["id"],
            title=raw_listing["title"],
            listing_url=f"https://fake.example.com/listing/{raw_listing['id']}",
        )

    def health_check(self) -> bool:
        return True


def test_concrete_connector_implements_interface() -> None:
    connector = FakeConnector()
    assert connector.marketplace_name == "fake"
    assert connector.health_check() is True

    results = connector.search("Maccabi")
    assert len(results) == 1
    assert isinstance(results[0], Listing)
    assert results[0].title == "Maccabi"
    assert results[0].marketplace == "fake"


def test_incomplete_connector_cannot_be_instantiated() -> None:
    class IncompleteConnector(MarketplaceConnector):
        @property
        def marketplace_name(self) -> str:
            return "incomplete"

        # Missing search / normalize_listing / health_check on purpose.

    with pytest.raises(TypeError):
        IncompleteConnector()  # type: ignore[abstract]
