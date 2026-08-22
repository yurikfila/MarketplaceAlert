"""Tests for the versioned mobile API (`/api/v1`).

Uses the same `client`/`db_session`/`fake_notification_provider` fixtures
as every other API test (`tests/conftest.py`) - an isolated temp database
and a fake notification provider, never the developer's real database or
real Telegram. `client` never triggers `lifespan()` (it's constructed
without the `with TestClient(app) as ...:` form - see conftest.py), so the
real background scanner never starts during these tests either.
"""

from datetime import datetime, timedelta, timezone

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.models import DiscoveredListing


def _create(client, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/api/v1/saved-searches", json=body)


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


def test_manual_run_returns_structured_mobile_response(client, fake_notification_provider) -> None:
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
    # Proves the /api/v1 run endpoint shares the same NotificationService
    # override as the legacy endpoint (dependencies.py singleton sharing) -
    # if it didn't, this would have attempted a real Telegram call instead.
    assert len(fake_notification_provider.sent_listings) == 1


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
    db_session, *, marketplace: str, external_id: str, title: str, first_discovered_at=None
) -> DiscoveredListing:
    now = first_discovered_at or datetime.now(timezone.utc)
    row = DiscoveredListing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
        first_discovered_at=now,
        last_seen_at=now,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_listings_empty_when_nothing_discovered(client) -> None:
    response = client.get("/api/v1/listings")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total_count"] == 0


def test_listings_returns_discovered_rows_via_scan(client) -> None:
    scan_response = client.get("/scan", params={"q": "Maccabi"})
    assert scan_response.json()["new_count"] == 1

    response = client.get("/api/v1/listings")
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    item = body["items"][0]
    assert item["marketplace"] == "mock"
    assert item["external_listing_id"] == "mock-001"
    assert item["title"] == "Maccabi Tel Aviv Vintage Football Shirt"
    assert item["listing_url"]
    assert item["first_discovered_at"]
    assert item["last_seen_at"]


def test_listings_unpersisted_fields_are_null(client) -> None:
    """price/currency/location/condition/image_url are not persisted on
    DiscoveredListing yet - must be null, never invented."""
    client.get("/scan", params={"q": "Maccabi"})

    item = client.get("/api/v1/listings").json()["items"][0]
    assert item["price"] is None
    assert item["currency"] is None
    assert item["location"] is None
    assert item["condition"] is None
    assert item["image_url"] is None


def test_listings_pagination_limit_and_offset(client, db_session) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="a", title="Item A")
    _insert_listing(db_session, marketplace="mock", external_id="b", title="Item B")
    _insert_listing(db_session, marketplace="mock", external_id="c", title="Item C")

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


def test_listings_sorted_newest_first(client, db_session) -> None:
    older = datetime.now(timezone.utc) - timedelta(hours=2)
    newer = datetime.now(timezone.utc) - timedelta(minutes=1)
    _insert_listing(db_session, marketplace="mock", external_id="old", title="Old", first_discovered_at=older)
    _insert_listing(db_session, marketplace="mock", external_id="new", title="New", first_discovered_at=newer)

    body = client.get("/api/v1/listings").json()
    assert [item["external_listing_id"] for item in body["items"]] == ["new", "old"]


def test_listings_filter_by_marketplace(client, db_session) -> None:
    _insert_listing(db_session, marketplace="mock", external_id="m1", title="Mock item")
    _insert_listing(db_session, marketplace="etsy", external_id="e1", title="Etsy item")

    response = client.get("/api/v1/listings", params={"marketplace": "etsy"})
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["items"][0]["marketplace"] == "etsy"
    assert body["items"][0]["external_listing_id"] == "e1"


def test_listings_rejects_invalid_marketplace_filter(client) -> None:
    response = client.get("/api/v1/listings", params={"marketplace": "not-a-real-marketplace"})
    assert response.status_code == 422


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


def test_existing_legacy_routes_still_work_alongside_v1(client) -> None:
    """Adding /api/v1 must not remove or break the existing surface."""
    created = _create(client, query="Legacy check").json()
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/saved-searches").status_code == 200
    assert client.get(f"/saved-searches/{created['id']}").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
