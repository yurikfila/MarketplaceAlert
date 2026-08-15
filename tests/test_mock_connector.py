from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.core.models.listing import Listing


def test_connector_loads_correctly() -> None:
    connector = MockMarketplaceConnector()
    assert connector.marketplace_name == "mock"
    assert connector.health_check() is True


def test_arbitrary_keyword_search() -> None:
    connector = MockMarketplaceConnector()
    results = connector.search("Makita")
    assert len(results) == 1
    assert "Makita" in results[0].title


def test_search_is_case_insensitive() -> None:
    connector = MockMarketplaceConnector()
    lower = connector.search("rolex")
    upper = connector.search("ROLEX")
    mixed = connector.search("RoLeX")
    assert len(lower) == len(upper) == len(mixed) == 1
    assert (
        lower[0].external_listing_id
        == upper[0].external_listing_id
        == mixed[0].external_listing_id
    )


def test_search_with_no_matches_returns_empty_list() -> None:
    connector = MockMarketplaceConnector()
    results = connector.search("Nonexistent Item Zyxwvut")
    assert results == []


def test_search_returns_normalized_listing_objects() -> None:
    connector = MockMarketplaceConnector()
    results = connector.search("Pokemon")
    assert len(results) == 1

    listing = results[0]
    assert isinstance(listing, Listing)
    assert listing.marketplace == "mock"
    assert listing.external_listing_id
    assert listing.title
    assert str(listing.listing_url).startswith("https://mock-marketplace.example.com/listing/")


def test_search_only_returns_matching_listings() -> None:
    connector = MockMarketplaceConnector()
    results = connector.search("Adidas")
    assert all("adidas" in listing.title.lower() for listing in results)


def test_multiple_results_are_supported() -> None:
    connector = MockMarketplaceConnector()
    # "Tel Aviv" appears in both the Maccabi and Hapoel mock listings.
    results = connector.search("Tel Aviv")
    assert len(results) >= 2


def test_all_mock_listings_have_unique_external_ids() -> None:
    connector = MockMarketplaceConnector()
    # Empty query is a substring of every title, so this returns the whole catalog.
    results = connector.search("")
    ids = [listing.external_listing_id for listing in results]
    assert len(ids) >= 6
    assert len(ids) == len(set(ids))


def test_filters_are_applied_when_supported() -> None:
    connector = MockMarketplaceConnector()
    results = connector.search("Jacket", filters={"min_price": 1000})
    assert results == []
