"""Tests for the `GET /listings` dashboard page (the rest of Phase 7's
stated scope - "...and viewing results" - alongside `GET /`'s existing
saved-search administration).

Uses the same `client`/`db_session` fixtures as every other dashboard/API
test (`tests/conftest.py`) - an isolated temp database, never the real one,
and `client` never triggers the real Telegram provider either (see that
fixture's FakeNotificationProvider).
"""

from datetime import datetime, timezone

import pytest

from marketplace_alert.core.persistence.models import DiscoveredListing


@pytest.fixture(autouse=True)
def _legacy_routes_enabled(with_legacy_routes_enabled) -> None:
    """The whole file is about the legacy `GET /listings` dashboard page,
    which is now opt-in-disabled by default - see config.py's
    `legacy_routes_enabled` docstring."""


def _insert_listing(db_session, *, marketplace="mock", external_id="a", title="Item", **overrides) -> DiscoveredListing:
    now = datetime.now(timezone.utc)
    row = DiscoveredListing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
        first_discovered_at=now,
        last_seen_at=now,
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _create_saved_search(client, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/saved-searches", json=body)


def test_listings_page_loads_with_no_listings(client) -> None:
    response = client.get("/listings")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "0 listings" in response.text
    assert "No listings match these filters yet." in response.text


def test_listings_page_shows_a_discovered_listing(client, db_session) -> None:
    _insert_listing(
        db_session,
        title="Vintage Camera",
        price=250.0,
        currency="USD",
        condition="Used",
        location="Austin, TX",
        seller="camera_shop_88",
        image_url="https://example.com/camera.jpg",
    )

    body = client.get("/listings").text
    assert "Vintage Camera" in body
    assert "USD 250" in body  # whole-dollar price, no invented ".00"
    assert "Used" in body
    assert "Seller: camera_shop_88" in body


def test_listings_page_omits_seller_line_when_absent(client, db_session) -> None:
    _insert_listing(db_session, title="No seller item", seller=None)

    body = client.get("/listings").text
    assert "Seller:" not in body


def test_listings_page_omits_condition_location_line_when_both_absent(client, db_session) -> None:
    _insert_listing(db_session, title="Bare item", condition=None, location=None, seller=None)

    body = client.get("/listings").text
    # Only the "Discovered ..." meta line should render - no stray empty
    # <p class="listing-meta"></p> for the missing condition/location row.
    assert body.count('class="listing-meta"') == 1


def test_listings_page_price_formatting_shows_cents_only_when_present(client, db_session) -> None:
    _insert_listing(db_session, external_id="a", title="Whole dollar item", price=399.0, currency="EUR")
    _insert_listing(db_session, external_id="b", title="Fractional item", price=79.99, currency="GBP")

    body = client.get("/listings").text
    assert "EUR 399" in body
    assert "EUR 399.00" not in body
    assert "GBP 79.99" in body


def test_listings_page_shows_placeholder_when_image_missing(client, db_session) -> None:
    _insert_listing(db_session, title="No image item", image_url=None)

    body = client.get("/listings").text
    assert "listing-image-placeholder" in body


def test_listings_page_omits_price_when_absent(client, db_session) -> None:
    _insert_listing(db_session, title="No price item", price=None)

    body = client.get("/listings").text
    assert "No price item" in body
    assert "listing-price" not in body


def test_listings_page_uses_brand_cased_marketplace_display_name(client, db_session) -> None:
    _insert_listing(db_session, marketplace="ebay", external_id="ebay-1", title="eBay item")

    body = client.get("/listings").text
    assert "eBay" in body
    assert "Ebay" not in body


def test_listings_page_filter_by_marketplace(client, db_session) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="m1", title="Mock item")
    _insert_listing(db_session, marketplace="etsy", external_id="e1", title="Etsy item")

    body = client.get("/listings", params={"marketplace": "etsy"}).text
    assert "Etsy item" in body
    assert "Mock item" not in body
    assert "1 listing" in body


def test_listings_page_filter_by_price_range(client, db_session) -> None:
    _insert_listing(db_session, external_id="cheap", title="Cheap item", price=10.0)
    _insert_listing(db_session, external_id="pricey", title="Pricey item", price=999.0)

    body = client.get("/listings", params={"min_price": 5, "max_price": 100}).text
    assert "Cheap item" in body
    assert "Pricey item" not in body


def test_listings_page_filter_by_saved_search(client, db_session) -> None:
    created = _create_saved_search(client, query="Drill hunt").json()
    _insert_listing(db_session, external_id="a", title="Attributed", discovered_by_saved_search_id=created["id"])
    _insert_listing(db_session, external_id="b", title="Unattributed", discovered_by_saved_search_id=None)

    body = client.get("/listings", params={"saved_search_id": created["id"]}).text
    assert "Attributed" in body
    assert "Unattributed" not in body


def test_listings_page_sort_price_ascending(client, db_session) -> None:
    _insert_listing(db_session, external_id="mid", title="Mid item", price=50.0)
    _insert_listing(db_session, external_id="cheap", title="Cheap item", price=10.0)

    body = client.get("/listings", params={"sort": "price_asc"}).text
    assert body.index("Cheap item") < body.index("Mid item")


def test_listings_page_degrades_gracefully_on_invalid_marketplace(client, db_session) -> None:
    """Unlike the JSON API (which 422s), this is a browsable page - an
    invalid/hand-edited query string must never show an error page, just
    fall back to unfiltered results."""
    _insert_listing(db_session, external_id="a", title="Visible item")

    response = client.get("/listings", params={"marketplace": "not-a-real-marketplace"})
    assert response.status_code == 200
    assert "Visible item" in response.text


def test_listings_page_degrades_gracefully_on_invalid_sort(client, db_session) -> None:
    _insert_listing(db_session, external_id="a", title="Visible item")

    response = client.get("/listings", params={"sort": "not-a-real-sort"})
    assert response.status_code == 200
    assert "Visible item" in response.text


def test_listings_page_degrades_gracefully_when_min_price_exceeds_max_price(client, db_session) -> None:
    _insert_listing(db_session, external_id="a", title="Visible item", price=50.0)

    response = client.get("/listings", params={"min_price": 100, "max_price": 10})
    assert response.status_code == 200
    assert "Visible item" in response.text  # the nonsensical range is ignored, not applied


def test_listings_page_pagination_links_appear_when_there_are_more_pages(client, db_session) -> None:
    for i in range(30):
        _insert_listing(db_session, external_id=f"item-{i}", title=f"Item {i}")

    body = client.get("/listings").text
    assert "Older" in body
    assert "Newer" not in body  # first page - nothing to go back to

    body_page_2 = client.get("/listings", params={"offset": 24}).text
    assert "Newer" in body_page_2


def test_listings_page_escapes_title_to_prevent_xss(client, db_session) -> None:
    _insert_listing(db_session, title="<script>alert(1)</script>")

    body = client.get("/listings").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_listings_page_link_opens_original_listing_in_a_new_tab_safely(client, db_session) -> None:
    _insert_listing(db_session, title="Linked item")

    body = client.get("/listings").text
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_listings_page_never_exposes_credentials(client, monkeypatch) -> None:
    from marketplace_alert.config import settings

    monkeypatch.setattr(settings, "reverb_api_token", "super-secret-reverb-token")
    monkeypatch.setattr(settings, "bonanza_dev_name", "super-secret-bonanza-dev-name")

    body = client.get("/listings").text
    assert "super-secret-reverb-token" not in body
    assert "super-secret-bonanza-dev-name" not in body


def test_listings_nav_link_appears_on_both_pages(client) -> None:
    assert 'href="/listings"' in client.get("/").text
    assert 'href="/"' in client.get("/listings").text
