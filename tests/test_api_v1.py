"""Tests for the versioned mobile API (`/api/v1`).

Uses the same `client`/`db_session`/`fake_notification_provider` fixtures
as every other API test (`tests/conftest.py`) - an isolated temp database
and a fake notification provider, never the developer's real database or
real Telegram. `client` never triggers `lifespan()` (it's constructed
without the `with TestClient(app) as ...:` form - see conftest.py), so the
real background scanner never starts during these tests either.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution, PendingNotification


@pytest.fixture(autouse=True)
def _authenticated(client) -> dict:
    """Every `/api/v1` saved-search and listings route now requires
    authentication and ownership - sign up one real user per test and
    attach its access token to every request the `client` fixture makes
    for the rest of this test, rather than adding an `Authorization`
    header to each individual call site. Harmless for the handful of
    tests in this file that don't need auth (status/marketplaces) -
    those routes simply ignore the header. Returns the signed-up user
    body, for tests that need the id.
    """
    signup = client.post(
        "/api/v1/auth/signup", json={"email": "api-v1-tests@example.com", "password": "a-strong-password"}
    )
    body = signup.json()
    client.headers["Authorization"] = f"Bearer {body['tokens']['access_token']}"
    return body["user"]


def _create(client, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/api/v1/saved-searches", json=body)


@pytest.fixture()
def owned_saved_search_id(client, _authenticated) -> int:
    """One saved search owned by `_authenticated`'s user - for listings
    tests that just need some real, owned saved search to attribute a
    manually-inserted listing to (via `_insert_listing`'s
    `saved_search_id`), without caring about its query/marketplaces."""
    return _create(client).json()["id"]


# --- GET /api/v1/status -----------------------------------------------


def test_status_returns_expected_shape(client) -> None:
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["backend"] is True
    assert body["database"] is True
    assert isinstance(body["telegram_configured"], bool)
    assert isinstance(body["supported_marketplaces"], list)


def test_status_reflects_telegram_configuration(client, fake_notification_provider) -> None:
    # The `client` fixture's FakeNotificationProvider is always enabled.
    body = client.get("/api/v1/status").json()
    assert body["telegram_configured"] is True


def test_status_supported_marketplaces_matches_registry(client) -> None:
    from marketplace_alert.connectors.registry import list_supported_marketplaces

    body = client.get("/api/v1/status").json()
    assert sorted(body["supported_marketplaces"]) == sorted(list_supported_marketplaces())


def test_status_never_exposes_secrets(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "super-secret-bot-token")
    monkeypatch.setattr(settings, "etsy_api_key", "super-secret-etsy-key")
    monkeypatch.setattr(settings, "database_url", "postgresql://user:super-secret-db-pw@host/db")

    response = client.get("/api/v1/status")
    body_text = response.text

    assert "super-secret-bot-token" not in body_text
    assert "super-secret-etsy-key" not in body_text
    assert "super-secret-db-pw" not in body_text


# --- GET /api/v1/marketplaces -------------------------------------------


def test_marketplaces_matches_registry(client) -> None:
    from marketplace_alert.connectors.registry import list_supported_marketplaces

    response = client.get("/api/v1/marketplaces")
    assert response.status_code == 200
    body = response.json()
    assert sorted(item["id"] for item in body) == sorted(list_supported_marketplaces())


def test_marketplaces_have_expected_fields(client) -> None:
    body = client.get("/api/v1/marketplaces").json()
    for item in body:
        assert set(item.keys()) == {"id", "name", "configured", "available"}
        assert isinstance(item["configured"], bool)
        assert isinstance(item["available"], bool)


def test_marketplaces_display_names_use_brand_casing(client) -> None:
    body = {item["id"]: item["name"] for item in client.get("/api/v1/marketplaces").json()}
    assert body["ebay"] == "eBay"
    assert body["etsy"] == "Etsy"
    assert body["mock"] == "Mock"
    assert body["reverb"] == "Reverb"
    assert body["bonanza"] == "Bonanza"


def test_marketplaces_includes_reverb_and_bonanza(client) -> None:
    """Requirement: every new connector must appear here automatically
    through the connector registry - never a separately hard-coded entry."""
    body = client.get("/api/v1/marketplaces").json()
    ids = {item["id"] for item in body}
    assert "reverb" in ids
    assert "bonanza" in ids


def test_marketplaces_reflect_credential_configuration(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "etsy_api_key", "test-key")
    monkeypatch.setattr(settings, "etsy_shared_secret", "test-secret")
    monkeypatch.setattr(settings, "ebay_app_id", None)
    monkeypatch.setattr(settings, "ebay_cert_id", None)
    monkeypatch.setattr(settings, "reverb_api_token", None)
    monkeypatch.setattr(settings, "bonanza_dev_name", None)

    body = {item["id"]: item for item in client.get("/api/v1/marketplaces").json()}

    assert body["etsy"]["configured"] is True
    assert body["etsy"]["available"] is True
    assert body["ebay"]["configured"] is False
    assert body["ebay"]["available"] is False
    assert body["reverb"]["configured"] is False
    assert body["reverb"]["available"] is False
    assert body["bonanza"]["configured"] is False
    assert body["bonanza"]["available"] is False
    # mock has nothing to configure - always both true.
    assert body["mock"]["configured"] is True
    assert body["mock"]["available"] is True


def test_marketplaces_reflect_reverb_token_present(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reverb_api_token", "test-reverb-token")

    body = {item["id"]: item for item in client.get("/api/v1/marketplaces").json()}

    assert body["reverb"]["configured"] is True
    assert body["reverb"]["available"] is True


def test_marketplaces_reflect_bonanza_dev_name_present(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bonanza_dev_name", "test-bonanza-dev-name")

    body = {item["id"]: item for item in client.get("/api/v1/marketplaces").json()}

    assert body["bonanza"]["configured"] is True
    assert body["bonanza"]["available"] is True


def test_marketplaces_never_exposes_secrets(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "etsy_api_key", "super-secret-etsy-key")
    monkeypatch.setattr(settings, "ebay_app_id", "super-secret-ebay-app-id")
    monkeypatch.setattr(settings, "reverb_api_token", "super-secret-reverb-token")
    monkeypatch.setattr(settings, "bonanza_dev_name", "super-secret-bonanza-dev-name")

    response = client.get("/api/v1/marketplaces")
    assert "super-secret-etsy-key" not in response.text
    assert "super-secret-ebay-app-id" not in response.text
    assert "super-secret-reverb-token" not in response.text
    assert "super-secret-bonanza-dev-name" not in response.text


# --- saved searches: create / list / get / update / delete -----------------


def test_create_saved_search_returns_expected_fields(client) -> None:
    response = _create(client, query="Makita", marketplaces=["mock"])
    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) >= {
        "id",
        "query",
        "marketplaces",
        "is_active",
        "scan_interval_seconds",
        "created_at",
        "updated_at",
        "last_scanned_at",
    }
    assert body["query"] == "Makita"
    assert body["marketplaces"] == ["mock"]
    assert body["is_active"] is True
    assert body["last_scanned_at"] is None


def test_list_saved_searches(client) -> None:
    _create(client, query="Pokemon")
    _create(client, query="Rolex")

    response = client.get("/api/v1/saved-searches")
    assert response.status_code == 200
    queries = {item["query"] for item in response.json()}
    assert queries == {"Pokemon", "Rolex"}


def test_get_saved_search_detail(client) -> None:
    created = _create(client, query="Adidas").json()

    response = client.get(f"/api/v1/saved-searches/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["query"] == "Adidas"


def test_get_saved_search_not_found(client) -> None:
    response = client.get("/api/v1/saved-searches/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Saved search not found"


def test_update_saved_search(client) -> None:
    created = _create(client).json()

    response = client.patch(f"/api/v1/saved-searches/{created['id']}", json={"query": "Charizard"})
    assert response.status_code == 200
    assert response.json()["query"] == "Charizard"


def test_update_saved_search_not_found(client) -> None:
    response = client.patch("/api/v1/saved-searches/999999", json={"query": "Charizard"})
    assert response.status_code == 404


def test_delete_saved_search(client) -> None:
    created = _create(client).json()

    delete_response = client.delete(f"/api/v1/saved-searches/{created['id']}")
    assert delete_response.status_code == 204

    get_response = client.get(f"/api/v1/saved-searches/{created['id']}")
    assert get_response.status_code == 404


def test_delete_saved_search_not_found(client) -> None:
    response = client.delete("/api/v1/saved-searches/999999")
    assert response.status_code == 404


# --- validation --------------------------------------------------------


def test_create_saved_search_rejects_invalid_marketplace(client) -> None:
    response = _create(client, marketplaces=["not-a-real-marketplace"])
    assert response.status_code == 422


def test_create_saved_search_rejects_interval_below_minimum(client) -> None:
    response = _create(client, scan_interval_seconds=5)
    assert response.status_code == 422


def test_create_saved_search_rejects_empty_query(client) -> None:
    response = _create(client, query="   ")
    assert response.status_code == 422


def test_update_saved_search_rejects_invalid_marketplace(client) -> None:
    created = _create(client).json()
    response = client.patch(
        f"/api/v1/saved-searches/{created['id']}", json={"marketplaces": ["not-a-real-marketplace"]}
    )
    assert response.status_code == 422


# --- manual run ----------------------------------------------------------


def test_manual_run_returns_structured_mobile_response(client, db_session) -> None:
    created = _create(client, query="Maccabi", marketplaces=["mock"]).json()

    response = client.post(f"/api/v1/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()

    assert body["saved_search_id"] == created["id"]
    assert body["query"] == "Maccabi"
    assert "mock" in body["marketplaces"]
    assert body["marketplaces"]["mock"]["new_count"] == 1
    assert body["marketplaces"]["mock"]["already_seen_count"] == 0
    assert body["marketplaces"]["mock"]["error"] is None
    assert body["total_new_count"] == 1
    assert body["total_already_seen_count"] == 0
    # The manual run no longer sends synchronously - it enqueues a
    # notification-outbox row instead (see SavedSearchRunner's module
    # docstring); delivery is core/notifications/outbox.py's concern.
    assert len(db_session.execute(select(PendingNotification)).scalars().all()) == 1


def test_manual_run_not_found(client) -> None:
    response = client.post("/api/v1/saved-searches/999999/run")
    assert response.status_code == 404


def test_manual_run_inactive_returns_409(client) -> None:
    created = _create(client, is_active=False).json()
    response = client.post(f"/api/v1/saved-searches/{created['id']}/run")
    assert response.status_code == 409


def test_manual_run_shares_guard_with_legacy_endpoint(client) -> None:
    """The /api/v1 run endpoint and the legacy /saved-searches run endpoint
    must share the same SavedSearchRunGuard - acquiring via one path must
    be visible to the other, so the same saved search can never run
    through both at once."""
    from marketplace_alert.dependencies import saved_search_run_guard

    created = _create(client).json()
    acquired = saved_search_run_guard.try_acquire(created["id"])
    assert acquired is True
    try:
        response = client.post(f"/api/v1/saved-searches/{created['id']}/run")
        assert response.status_code == 409
        assert response.json()["detail"] == "Saved search is already running"
    finally:
        saved_search_run_guard.release(created["id"])


# --- listings: pagination, filtering, ordering ------------------------


def _insert_listing(
    db_session,
    *,
    marketplace: str,
    external_id: str,
    title: str,
    first_discovered_at=None,
    price=None,
    currency=None,
    location=None,
    seller=None,
    condition=None,
    image_url=None,
    source_created_at=None,
    saved_search_id=None,
) -> DiscoveredListing:
    """`saved_search_id`, when given, also creates the corresponding
    `ListingAttribution` row - that's what actually drives `GET
    /api/v1/listings`' ownership scoping now (Phase 1 of multi-user
    listing attribution: `list_recent_owned` queries `ListingAttribution`,
    not `discovered_by_saved_search_id` alone). Kept as the same
    parameter shape every existing test in this file already uses."""
    now = first_discovered_at or datetime.now(timezone.utc)
    row = DiscoveredListing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
        price=price,
        currency=currency,
        location=location,
        seller=seller,
        condition=condition,
        image_url=image_url,
        source_created_at=source_created_at,
        discovered_by_saved_search_id=saved_search_id,
        first_discovered_at=now,
        last_seen_at=now,
    )
    db_session.add(row)
    db_session.commit()
    if saved_search_id is not None:
        db_session.add(
            ListingAttribution(saved_search_id=saved_search_id, discovered_listing_id=row.id, discovered_at=now)
        )
        db_session.commit()
    return row


def test_listings_empty_when_nothing_discovered(client) -> None:
    response = client.get("/api/v1/listings")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total_count"] == 0


def test_listings_via_legacy_scan_are_invisible_since_they_have_no_owning_search(
    client, with_legacy_routes_enabled
) -> None:
    """`/scan` (mock-only, legacy - see PART 3) never attributes a
    discovered listing to any saved search. Once `/api/v1/listings` is
    ownership-scoped (an `INNER JOIN` through `discovered_by_saved_
    search_id -> saved_searches.user_id`), a listing with no owning
    search can never appear for anyone, regardless of who's asking - the
    intended behavior (see PART 6), not a regression."""
    scan_response = client.get("/scan", params={"q": "Maccabi"})
    assert scan_response.json()["new_count"] == 1

    response = client.get("/api/v1/listings")
    assert response.status_code == 200
    assert response.json()["total_count"] == 0
    assert response.json()["items"] == []


def test_listings_persists_and_returns_product_fields_from_the_connector(client) -> None:
    """price/currency/location/seller/condition/image_url are captured
    from whatever the connector actually returned at discovery time - the
    mock connector's "Maccabi" fixture has real values for every one of
    these, so they must round-trip through persistence and the API,
    never come back null just because the field CAN be null in general.
    Discovered via an owned saved-search run (not `/scan`, which never
    attributes ownership and so is invisible via this now-scoped route -
    see the test above)."""
    created = _create(client, query="Maccabi", marketplaces=["mock"]).json()
    client.post(f"/api/v1/saved-searches/{created['id']}/run")

    item = client.get("/api/v1/listings").json()["items"][0]
    assert item["price"] == 45.0
    assert item["currency"] == "USD"
    assert item["location"] == "Tel Aviv, Israel"
    assert item["seller"] == "vintage_kits_il"
    assert item["condition"] == "used"
    assert item["image_url"] == "https://example.com/images/maccabi-shirt.jpg"
    # The mock connector never sets Listing.created_at - genuinely absent,
    # not a bug - must stay null rather than falling back to something else.
    assert item["source_created_at"] is None
    assert item["saved_search_id"] == created["id"]


def test_listings_optional_fields_stay_null_when_a_connector_did_not_provide_them(
    client, db_session, owned_saved_search_id
) -> None:
    """A field genuinely absent from a connector's response must stay
    `null` end to end - never defaulted to 0/""/a guessed value.
    Attributed to an owned saved search so the row is visible at all
    under ownership scoping - unrelated to what this test actually
    checks (which optional fields stay null)."""
    _insert_listing(
        db_session, marketplace="mock", external_id="bare", title="Bare listing", saved_search_id=owned_saved_search_id
    )

    item = client.get("/api/v1/listings").json()["items"][0]
    assert item["price"] is None
    assert item["currency"] is None
    assert item["location"] is None
    assert item["seller"] is None
    assert item["condition"] is None
    assert item["image_url"] is None
    assert item["source_created_at"] is None
    assert item["saved_search_id"] == owned_saved_search_id


def test_listings_records_which_saved_search_first_discovered_a_row(client) -> None:
    created = _create(client, query="Maccabi", marketplaces=["mock"]).json()
    run_response = client.post(f"/api/v1/saved-searches/{created['id']}/run")
    assert run_response.json()["total_new_count"] == 1

    item = client.get("/api/v1/listings").json()["items"][0]
    assert item["saved_search_id"] == created["id"]


# --- listings: filters (marketplace, saved_search_id, price, currency, condition, location, time) ---


def test_listings_filter_by_saved_search_id(client, db_session) -> None:
    search_a = _create(client, query="Search A").json()["id"]
    search_b = _create(client, query="Search B").json()["id"]
    _insert_listing(db_session, marketplace="mock", external_id="a", title="A", saved_search_id=search_a)
    _insert_listing(db_session, marketplace="mock", external_id="b", title="B", saved_search_id=search_b)
    _insert_listing(db_session, marketplace="mock", external_id="c", title="C", saved_search_id=None)

    body = client.get("/api/v1/listings", params={"saved_search_id": search_a}).json()
    assert body["total_count"] == 1
    assert body["items"][0]["external_listing_id"] == "a"


def test_listings_saved_search_id_matching_nothing_returns_empty_not_404(
    client, db_session, owned_saved_search_id
) -> None:
    """A filter value that matches no rows is a normal empty result, not
    an error - this endpoint never validates that the id refers to an
    existing saved search, only that it's structurally a positive int."""
    _insert_listing(db_session, marketplace="mock", external_id="a", title="A", saved_search_id=owned_saved_search_id)

    response = client.get("/api/v1/listings", params={"saved_search_id": 999999})
    assert response.status_code == 200
    assert response.json()["total_count"] == 0


def test_listings_rejects_malformed_saved_search_id(client) -> None:
    assert client.get("/api/v1/listings", params={"saved_search_id": 0}).status_code == 422
    assert client.get("/api/v1/listings", params={"saved_search_id": -1}).status_code == 422
    assert client.get("/api/v1/listings", params={"saved_search_id": "not-a-number"}).status_code == 422


def test_listings_filter_by_price_range(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="cheap", title="Cheap", price=10.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="mid", title="Mid", price=50.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="pricey", title="Pricey", price=500.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="unknown", title="No price", price=None, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"min_price": 20, "max_price": 100}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"mid"}

    # A null price is genuinely unknown - neither ">= min" nor "<= max" -
    # so it must never satisfy a price range filter.
    body = client.get("/api/v1/listings", params={"min_price": 0}).json()
    assert "unknown" not in {item["external_listing_id"] for item in body["items"]}


def test_listings_rejects_min_price_greater_than_max_price(client) -> None:
    response = client.get("/api/v1/listings", params={"min_price": 100, "max_price": 10})
    assert response.status_code == 422


def test_listings_rejects_negative_price_bounds(client) -> None:
    assert client.get("/api/v1/listings", params={"min_price": -5}).status_code == 422
    assert client.get("/api/v1/listings", params={"max_price": -5}).status_code == 422


def test_listings_filter_by_currency_is_case_insensitive(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="usd", title="USD item", currency="USD", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="eur", title="EUR item", currency="EUR", saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"currency": "usd"}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"usd"}


def test_listings_filter_by_condition_is_exact_case_insensitive_match(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="new1", title="New item", condition="New", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="used1", title="Used item", condition="Used", saved_search_id=owned_saved_search_id)
    _insert_listing(
        db_session,
        marketplace="mock",
        external_id="renewed1",
        title="Renewed item",
        condition="Certified Renewed",
        saved_search_id=owned_saved_search_id,
    )

    body = client.get("/api/v1/listings", params={"condition": "new"}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"new1"}


def test_listings_filter_by_location_is_case_insensitive_substring(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="tlv", title="Tel Aviv item", location="Tel Aviv, Israel", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="nyc", title="NYC item", location="New York, NY", saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"location": "israel"}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"tlv"}


def test_listings_filter_by_discovered_time_window(client, db_session, owned_saved_search_id) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=3)
    mid = datetime.now(timezone.utc) - timedelta(days=1)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=old, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="mid", title="Mid", first_discovered_at=mid, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="recent", title="Recent", first_discovered_at=recent, saved_search_id=owned_saved_search_id)

    after = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    body = client.get("/api/v1/listings", params={"discovered_after": after}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"mid", "recent"}

    before = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    body = client.get("/api/v1/listings", params={"discovered_before": before}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"old"}


def test_listings_new_since_is_equivalent_to_discovered_after(client, db_session, owned_saved_search_id) -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    recent = datetime.now(timezone.utc) - timedelta(minutes=1)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=old, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="recent", title="Recent", first_discovered_at=recent, saved_search_id=owned_saved_search_id)

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    body = client.get("/api/v1/listings", params={"new_since": cutoff}).json()
    assert {item["external_listing_id"] for item in body["items"]} == {"recent"}


# --- listings: sort ------------------------------------------------------


def test_listings_sort_oldest_first(client, db_session, owned_saved_search_id) -> None:
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    newer = datetime.now(timezone.utc) - timedelta(minutes=1)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=older, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="new", title="New", first_discovered_at=newer, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"sort": "oldest"}).json()
    assert [item["external_listing_id"] for item in body["items"]] == ["old", "new"]


def test_listings_sort_price_asc_puts_null_price_last(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="mid", title="Mid", price=50.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="cheap", title="Cheap", price=10.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="unknown", title="Unknown", price=None, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"sort": "price_asc"}).json()
    assert [item["external_listing_id"] for item in body["items"]] == ["cheap", "mid", "unknown"]


def test_listings_sort_price_desc_puts_null_price_last(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="mid", title="Mid", price=50.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="pricey", title="Pricey", price=500.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="unknown", title="Unknown", price=None, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"sort": "price_desc"}).json()
    assert [item["external_listing_id"] for item in body["items"]] == ["pricey", "mid", "unknown"]


def test_listings_rejects_invalid_sort(client) -> None:
    response = client.get("/api/v1/listings", params={"sort": "cheapest_or_whatever"})
    assert response.status_code == 422


def test_listings_default_sort_is_newest(client, db_session, owned_saved_search_id) -> None:
    older = datetime.now(timezone.utc) - timedelta(hours=1)
    newer = datetime.now(timezone.utc)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=older, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="new", title="New", first_discovered_at=newer, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings").json()
    assert [item["external_listing_id"] for item in body["items"]] == ["new", "old"]


# --- listings: pagination metadata reflects the filtered set, not the whole table ---


def test_listings_total_count_reflects_active_filters(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="a", title="A", price=10.0, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="b", title="B", price=999.0, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings", params={"max_price": 100}).json()
    assert body["total_count"] == 1


def test_listings_pagination_limit_and_offset(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="a", title="Item A", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="b", title="Item B", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="c", title="Item C", saved_search_id=owned_saved_search_id)

    first_page = client.get("/api/v1/listings", params={"limit": 2, "offset": 0}).json()
    assert len(first_page["items"]) == 2
    assert first_page["total_count"] == 3
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0

    second_page = client.get("/api/v1/listings", params={"limit": 2, "offset": 2}).json()
    assert len(second_page["items"]) == 1
    assert second_page["total_count"] == 3

    first_ids = {item["external_listing_id"] for item in first_page["items"]}
    second_ids = {item["external_listing_id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {"a", "b", "c"}


def test_listings_sorted_newest_first(client, db_session, owned_saved_search_id) -> None:
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    newer = datetime.now(timezone.utc) - timedelta(minutes=1)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=older, saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="mock", external_id="new", title="New", first_discovered_at=newer, saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings").json()
    assert [item["external_listing_id"] for item in body["items"]] == ["new", "old"]


def test_listings_filter_by_marketplace(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="m1", title="Mock item", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="etsy", external_id="e1", title="Etsy item", saved_search_id=owned_saved_search_id)

    response = client.get("/api/v1/listings", params={"marketplace": "etsy"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["items"][0]["marketplace"] == "etsy"
    assert body["items"][0]["external_listing_id"] == "e1"


def test_listings_rejects_invalid_marketplace_filter(client) -> None:
    response = client.get("/api/v1/listings", params={"marketplace": "not-a-real-marketplace"})
    assert response.status_code == 422


def test_listings_filter_by_multiple_marketplaces(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="m1", title="Mock item", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="etsy", external_id="e1", title="Etsy item", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="ebay", external_id="eb1", title="eBay item", saved_search_id=owned_saved_search_id)

    response = client.get("/api/v1/listings", params=[("marketplaces", "mock"), ("marketplaces", "etsy")])
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 2
    assert {item["marketplace"] for item in body["items"]} == {"mock", "etsy"}


def test_listings_rejects_invalid_marketplace_in_plural_filter(client) -> None:
    response = client.get(
        "/api/v1/listings", params=[("marketplaces", "mock"), ("marketplaces", "not-a-real-marketplace")]
    )
    assert response.status_code == 422


def test_listings_plural_marketplaces_absent_does_not_restrict_results(client, db_session, owned_saved_search_id) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="m1", title="Mock item", saved_search_id=owned_saved_search_id)
    _insert_listing(db_session, marketplace="etsy", external_id="e1", title="Etsy item", saved_search_id=owned_saved_search_id)

    body = client.get("/api/v1/listings").json()
    assert body["total_count"] == 2


def test_listings_rejects_limit_above_maximum(client) -> None:
    response = client.get("/api/v1/listings", params={"limit": 1000})
    assert response.status_code == 422


def test_listings_rejects_negative_offset(client) -> None:
    response = client.get("/api/v1/listings", params={"offset": -1})
    assert response.status_code == 422


# --- consistency / safety -----------------------------------------------


def test_error_responses_never_include_stack_traces(client) -> None:
    response = client.get("/api/v1/saved-searches/999999")
    assert response.status_code == 404
    body_text = response.text
    assert "Traceback" not in body_text
    assert "site-packages" not in body_text


def test_existing_legacy_routes_still_work_alongside_v1(client, with_legacy_routes_enabled) -> None:
    """Adding /api/v1 must not remove or break the existing surface."""
    created = _create(client, query="Legacy check").json()
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/saved-searches").status_code == 200
    assert client.get(f"/saved-searches/{created['id']}").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
