import pytest

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.main import app, get_notification_service


@pytest.fixture(autouse=True)
def _legacy_routes_enabled(with_legacy_routes_enabled) -> None:
    """The whole file is about the legacy `/scan`/`/search` surface,
    which is now opt-in-disabled by default - see config.py's
    `legacy_routes_enabled` docstring."""


def test_scan_first_request_returns_new_listing(client) -> None:
    response = client.get("/scan", params={"q": "Maccabi"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "Maccabi"
    assert body["new_count"] == 1
    assert body["already_seen_count"] == 0
    assert len(body["new_listings"]) == 1
    assert "Maccabi" in body["new_listings"][0]["title"]


def test_scan_second_request_returns_zero_new_listings(client) -> None:
    first = client.get("/scan", params={"q": "Maccabi"})
    assert first.json()["new_count"] == 1

    second = client.get("/scan", params={"q": "Maccabi"})
    assert second.status_code == 200
    body = second.json()
    assert body["new_listings"] == []
    assert body["new_count"] == 0
    assert body["already_seen_count"] == 1


def test_scan_with_no_matches_returns_zero_counts(client) -> None:
    response = client.get("/scan", params={"q": "Nonexistent Item Zyxwvut"})
    assert response.status_code == 200
    body = response.json()
    assert body["new_listings"] == []
    assert body["new_count"] == 0
    assert body["already_seen_count"] == 0


def test_search_endpoint_stays_stateless_alongside_scan(client) -> None:
    scan_response = client.get("/scan", params={"q": "Maccabi"})
    assert scan_response.json()["new_count"] == 1

    # /search must keep returning the listing even after /scan has persisted
    # it - it never filters out "already seen" listings, unlike /scan.
    first_search = client.get("/search", params={"q": "Maccabi"}).json()
    second_search = client.get("/search", params={"q": "Maccabi"}).json()
    assert len(first_search) == len(second_search) == 1
    assert first_search[0]["external_listing_id"] == second_search[0]["external_listing_id"] == "mock-001"


def test_scan_sends_notification_for_first_discovery(client, fake_notification_provider) -> None:
    response = client.get("/scan", params={"q": "Maccabi"})
    assert response.status_code == 200
    assert len(fake_notification_provider.sent_listings) == 1
    assert fake_notification_provider.sent_listings[0].external_listing_id == "mock-001"


def test_scan_does_not_notify_for_already_seen_listing(client, fake_notification_provider) -> None:
    client.get("/scan", params={"q": "Maccabi"})
    fake_notification_provider.sent_listings.clear()

    response = client.get("/scan", params={"q": "Maccabi"})
    assert response.status_code == 200
    assert response.json()["new_count"] == 0
    assert fake_notification_provider.sent_listings == []


def test_scan_survives_notification_provider_failure(client) -> None:
    class FailingProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing) -> None:
            raise NotificationError("simulated failure")

    app.dependency_overrides[get_notification_service] = lambda: NotificationService(FailingProvider())

    response = client.get("/scan", params={"q": "Maccabi"})

    assert response.status_code == 200
    body = response.json()
    assert body["new_count"] == 1
    assert len(body["new_listings"]) == 1
