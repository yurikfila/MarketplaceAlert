import pytest
from sqlalchemy import select

from marketplace_alert.core.persistence.models import PendingNotification


@pytest.fixture(autouse=True)
def _legacy_routes_enabled(with_legacy_routes_enabled) -> None:
    """The whole file is about the legacy `/saved-searches*` CRUD/run
    surface, which is now opt-in-disabled by default - see config.py's
    `legacy_routes_enabled` docstring."""


def _create(client, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/saved-searches", json=body)


def test_create_saved_search_with_one_marketplace(client) -> None:
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["query"] == "Pokemon"
    assert body["marketplaces"] == ["mock"]
    assert body["scan_interval_seconds"] == 60
    assert body["is_active"] is True
    assert body["id"] is not None
    assert body["last_scanned_at"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None


def test_create_saved_search_with_multiple_marketplaces(client) -> None:
    response = _create(client, marketplaces=["etsy", "mock"])
    assert response.status_code == 201
    assert sorted(response.json()["marketplaces"]) == ["etsy", "mock"]


def test_create_saved_search_with_etsy_and_ebay(client) -> None:
    response = _create(client, marketplaces=["etsy", "ebay"])
    assert response.status_code == 201
    assert sorted(response.json()["marketplaces"]) == ["ebay", "etsy"]


def test_create_saved_search_defaults_marketplaces_to_mock(client) -> None:
    response = client.post("/saved-searches", json={"query": "Rolex", "scan_interval_seconds": 60})
    assert response.status_code == 201
    assert response.json()["marketplaces"] == ["mock"]


def test_list_saved_searches(client) -> None:
    _create(client, query="Pokemon")
    _create(client, query="Rolex")

    response = client.get("/saved-searches")
    assert response.status_code == 200
    queries = {item["query"] for item in response.json()}
    assert queries == {"Pokemon", "Rolex"}


def test_get_saved_search_returns_marketplace_list(client) -> None:
    created = _create(client, marketplaces=["etsy", "mock"]).json()

    response = client.get(f"/saved-searches/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert sorted(body["marketplaces"]) == ["etsy", "mock"]


def test_get_saved_search_not_found(client) -> None:
    response = client.get("/saved-searches/999999")
    assert response.status_code == 404


def test_edit_saved_search_query(client) -> None:
    created = _create(client).json()

    response = client.patch(f"/saved-searches/{created['id']}", json={"query": "Charizard"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "Charizard"
    assert body["marketplaces"] == ["mock"]  # untouched field stays as it was


def test_edit_saved_search_replaces_marketplace_selection(client) -> None:
    created = _create(client, marketplaces=["mock"]).json()

    response = client.patch(f"/saved-searches/{created['id']}", json={"marketplaces": ["etsy", "mock"]})
    assert response.status_code == 200
    assert sorted(response.json()["marketplaces"]) == ["etsy", "mock"]


def test_edit_saved_search_can_remove_a_marketplace(client) -> None:
    created = _create(client, marketplaces=["etsy", "mock"]).json()

    response = client.patch(f"/saved-searches/{created['id']}", json={"marketplaces": ["mock"]})
    assert response.status_code == 200
    assert response.json()["marketplaces"] == ["mock"]


def test_disable_saved_search(client) -> None:
    created = _create(client).json()

    response = client.patch(f"/saved-searches/{created['id']}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_edit_saved_search_not_found(client) -> None:
    response = client.patch("/saved-searches/999999", json={"query": "Charizard"})
    assert response.status_code == 404


def test_delete_saved_search(client) -> None:
    created = _create(client).json()

    delete_response = client.delete(f"/saved-searches/{created['id']}")
    assert delete_response.status_code == 204

    get_response = client.get(f"/saved-searches/{created['id']}")
    assert get_response.status_code == 404


def test_delete_saved_search_not_found(client) -> None:
    response = client.delete("/saved-searches/999999")
    assert response.status_code == 404


def test_create_saved_search_rejects_empty_query(client) -> None:
    response = _create(client, query="   ")
    assert response.status_code == 422


def test_create_saved_search_rejects_interval_below_minimum(client) -> None:
    response = _create(client, scan_interval_seconds=5)
    assert response.status_code == 422


def test_create_saved_search_rejects_unsupported_marketplace(client) -> None:
    response = _create(client, marketplaces=["vinted"])
    assert response.status_code == 422


def test_create_saved_search_rejects_unsupported_marketplace_mixed_with_valid_one(client) -> None:
    response = _create(client, marketplaces=["mock", "vinted"])
    assert response.status_code == 422


def test_create_saved_search_rejects_zero_marketplaces(client) -> None:
    response = _create(client, marketplaces=[])
    assert response.status_code == 422


def test_create_saved_search_rejects_duplicate_marketplace_values(client) -> None:
    response = _create(client, marketplaces=["mock", "mock"])
    assert response.status_code == 422


def test_edit_saved_search_rejects_empty_query(client) -> None:
    created = _create(client).json()
    response = client.patch(f"/saved-searches/{created['id']}", json={"query": ""})
    assert response.status_code == 422


def test_edit_saved_search_rejects_interval_below_minimum(client) -> None:
    created = _create(client).json()
    response = client.patch(f"/saved-searches/{created['id']}", json={"scan_interval_seconds": 1})
    assert response.status_code == 422


def test_edit_saved_search_rejects_zero_marketplaces(client) -> None:
    created = _create(client).json()
    response = client.patch(f"/saved-searches/{created['id']}", json={"marketplaces": []})
    assert response.status_code == 422


def test_edit_saved_search_rejects_duplicate_marketplace_values(client) -> None:
    created = _create(client).json()
    response = client.patch(f"/saved-searches/{created['id']}", json={"marketplaces": ["etsy", "etsy"]})
    assert response.status_code == 422


def test_edit_saved_search_rejects_unsupported_marketplace(client) -> None:
    created = _create(client).json()
    response = client.patch(f"/saved-searches/{created['id']}", json={"marketplaces": ["vinted"]})
    assert response.status_code == 422


def test_manual_run_sends_notification_for_new_listing(client, db_session) -> None:
    """"Run Now" no longer sends synchronously - a new listing gets a
    notification-outbox row instead (see SavedSearchRunner's module
    docstring). Delivery, and surviving a Telegram-side failure, is
    core/notifications/outbox.py's concern - structurally, this endpoint
    can no longer be affected by a notification provider failure at all,
    since it never calls one."""
    created = _create(client, query="Maccabi").json()

    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["saved_search_id"] == created["id"]
    assert body["new_count"] == 1
    assert body["already_seen_count"] == 0
    assert len(db_session.execute(select(PendingNotification)).scalars().all()) == 1

    updated = client.get(f"/saved-searches/{created['id']}").json()
    assert updated["last_scanned_at"] is not None


def test_manual_run_returns_per_marketplace_breakdown(client) -> None:
    created = _create(client, query="Maccabi", marketplaces=["mock"]).json()

    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["marketplace"] == "mock"
    assert body["results"][0]["new_count"] == 1
    assert body["results"][0]["already_seen_count"] == 0
    assert body["results"][0]["error"] is None


def test_manual_run_executes_every_selected_marketplace(client) -> None:
    created = _create(client, query="a", marketplaces=["mock"]).json()
    # "a" matches several mock listings, proving the mock connector ran;
    # this test's real point is the *shape* of a multi-marketplace run -
    # see test_saved_search_scheduler.py for a genuinely multi-connector run.
    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    marketplaces_run = {r["marketplace"] for r in response.json()["results"]}
    assert marketplaces_run == {"mock"}


def test_manual_run_does_not_renotify_already_seen_listing(client, fake_notification_provider) -> None:
    created = _create(client, query="Maccabi").json()

    client.post(f"/saved-searches/{created['id']}/run")
    fake_notification_provider.sent_listings.clear()

    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 200
    body = response.json()
    assert body["new_count"] == 0
    assert body["already_seen_count"] == 1
    assert fake_notification_provider.sent_listings == []


def test_manual_run_not_found(client) -> None:
    response = client.post("/saved-searches/999999/run")
    assert response.status_code == 404


def test_manual_run_rejects_inactive_saved_search(client) -> None:
    created = _create(client, is_active=False).json()
    response = client.post(f"/saved-searches/{created['id']}/run")
    assert response.status_code == 409
