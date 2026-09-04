"""Tests for the GET / management dashboard.

Uses the `client` fixture (isolated temp DB, fake notification provider -
see conftest.py) throughout, same as the saved-search API tests. The
dashboard has no business logic of its own to test in isolation - it reads
through SavedSearchService exactly like the JSON API does, so these tests
exercise the rendered page, not duplicate CRUD behavior already covered by
tests/test_saved_searches_api.py.
"""

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.main import app, get_notification_service


@pytest.fixture(autouse=True)
def _legacy_routes_enabled(with_legacy_routes_enabled) -> None:
    """The whole file is about the legacy dashboard surface, which is now
    opt-in-disabled by default (see config.py's `legacy_routes_enabled`
    docstring) - enable it for every test here rather than the default
    itself being weakened."""


class DisabledNotificationProvider(NotificationProvider):
    """A fake provider that is always disabled, for testing the dashboard's
    "Telegram: Not configured" state (the standard `client` fixture's
    FakeNotificationProvider is always enabled)."""

    @property
    def is_enabled(self) -> bool:
        return False

    def send_listing_alert(self, listing: Listing) -> None:
        raise AssertionError("must never be called while disabled")


def _create_saved_search(client, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/saved-searches", json=body)


def test_dashboard_loads_with_all_three_sections(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Create a saved search" in body
    assert "Saved searches" in body
    assert "System status" in body


def test_dashboard_shows_empty_state_when_no_saved_searches(client) -> None:
    response = client.get("/")
    assert "No saved searches yet" in response.text


def test_dashboard_lists_existing_saved_searches(client) -> None:
    _create_saved_search(client, query="Maccabi vintage shirt", marketplaces=["mock"])
    _create_saved_search(client, query="Rolex Submariner", marketplaces=["etsy"])

    body = client.get("/").text

    assert "Maccabi vintage shirt" in body
    assert "Rolex Submariner" in body
    assert "No saved searches yet" not in body


def test_dashboard_marketplace_checkboxes_match_registry(client) -> None:
    from marketplace_alert.connectors.registry import list_supported_marketplaces

    body = client.get("/").text
    for marketplace in list_supported_marketplaces():
        assert f'name="marketplaces" value="{marketplace}"' in body


def test_dashboard_has_a_select_all_marketplaces_checkbox(client) -> None:
    body = client.get("/").text
    assert 'id="marketplaces-select-all"' in body


def test_dashboard_marketplace_checkbox_labels_use_brand_casing_not_title_case(client) -> None:
    """Regression: the checkbox label used to be `{{ marketplace.title() }}`
    - correct for "etsy"/"mock", but Python's `.title()` on "ebay" gives
    "Ebay", not the correct "eBay". Now driven by the shared
    `display_name_for()` (same source `GET /api/v1/marketplaces` uses),
    so this must never regress back to raw title-casing."""
    body = client.get("/").text
    assert "eBay" in body
    assert "Ebay" not in body


def test_dashboard_saved_search_table_uses_brand_casing_for_marketplaces(client) -> None:
    """Same regression, in the saved-searches table's marketplaces_display
    column rather than the create-form checkboxes."""
    _create_saved_search(client, query="Drill search", marketplaces=["ebay"])

    body = client.get("/").text

    assert "eBay" in body
    assert "Ebay" not in body


def test_dashboard_saved_search_table_shows_all_selected_marketplaces(client) -> None:
    _create_saved_search(client, query="Makita", marketplaces=["etsy", "mock"])

    body = client.get("/").text

    assert "Etsy" in body
    assert "Mock" in body


def test_dashboard_saved_search_rows_expose_action_buttons(client) -> None:
    created = _create_saved_search(client).json()
    saved_search_id = created["id"]

    body = client.get("/").text

    assert f'data-action="run" data-id="{saved_search_id}"' in body
    assert f'data-action="toggle" data-id="{saved_search_id}"' in body
    assert f'data-action="delete" data-id="{saved_search_id}"' in body
    assert "Disable" in body  # created active, so the toggle button reads "Disable"


def test_dashboard_shows_never_for_unscanned_search(client) -> None:
    _create_saved_search(client)
    assert "Never" in client.get("/").text


def test_dashboard_shows_last_scanned_time_after_a_run(client) -> None:
    created = _create_saved_search(client, query="Maccabi").json()
    client.post(f"/saved-searches/{created['id']}/run")

    body = client.get("/").text

    assert "Never" not in body or "UTC" in body


def test_dashboard_active_saved_search_count(client) -> None:
    _create_saved_search(client, query="Active one", is_active=True)
    _create_saved_search(client, query="Inactive one", is_active=False)

    body = client.get("/").text

    assert '<span class="badge badge-neutral">1</span>' in body


def test_dashboard_marketplace_count_matches_registry(client) -> None:
    from marketplace_alert.connectors.registry import list_supported_marketplaces

    body = client.get("/").text
    count = len(list_supported_marketplaces())

    assert f'<span class="badge badge-neutral">{count}</span>' in body


def _status_section(body: str) -> str:
    """The system-status section only - "Etsy"/"Telegram" also appear
    earlier on the page (the marketplace checkboxes), so scope to after the
    "System status" heading before looking for either name."""
    return body.split("System status", 1)[1]


def test_dashboard_shows_telegram_configured_when_provider_enabled(client) -> None:
    # The `client` fixture's FakeNotificationProvider is always enabled.
    status_section = _status_section(client.get("/").text)
    assert "Telegram" in status_section
    assert "Not configured" not in status_section.split("Telegram", 1)[1][:200]


def test_dashboard_shows_telegram_not_configured_when_provider_disabled(client) -> None:
    app.dependency_overrides[get_notification_service] = lambda: NotificationService(
        DisabledNotificationProvider()
    )
    try:
        status_section = _status_section(client.get("/").text)
    finally:
        # Restore the fixture's normal (enabled) override for any later use.
        app.dependency_overrides.pop(get_notification_service, None)

    telegram_section = status_section.split("Telegram", 1)[1][:200]
    assert "Not configured" in telegram_section


def test_dashboard_shows_etsy_configured_when_credentials_present(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "etsy_api_key", "test-key")
    monkeypatch.setattr(settings, "etsy_shared_secret", "test-secret")

    status_section = _status_section(client.get("/").text)
    etsy_section = status_section.split("Etsy", 1)[1][:200]
    assert "Configured" in etsy_section
    assert "Not configured" not in etsy_section


def test_dashboard_shows_etsy_not_configured_when_credentials_missing(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "etsy_api_key", None)
    monkeypatch.setattr(settings, "etsy_shared_secret", None)

    status_section = _status_section(client.get("/").text)
    etsy_section = status_section.split("Etsy", 1)[1][:200]
    assert "Not configured" in etsy_section


def test_dashboard_shows_ebay_configured_when_credentials_present(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "ebay_app_id", "test-app-id")
    monkeypatch.setattr(settings, "ebay_cert_id", "test-cert-id")

    status_section = _status_section(client.get("/").text)
    ebay_section = status_section.split("eBay", 1)[1][:200]
    assert "Configured" in ebay_section
    assert "Not configured" not in ebay_section


def test_dashboard_shows_ebay_not_configured_when_credentials_missing(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "ebay_app_id", None)
    monkeypatch.setattr(settings, "ebay_cert_id", None)

    status_section = _status_section(client.get("/").text)
    ebay_section = status_section.split("eBay", 1)[1][:200]
    assert "Not configured" in ebay_section


def test_dashboard_status_panel_includes_reverb(client) -> None:
    """Requirement: Reverb must appear in the dashboard's system-status
    panel automatically through the registry-driven marketplace list -
    never a separately hard-coded entry."""
    status_section = _status_section(client.get("/").text)
    assert "Reverb" in status_section


def test_dashboard_shows_reverb_configured_when_token_present(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reverb_api_token", "test-reverb-token")

    status_section = _status_section(client.get("/").text)
    reverb_section = status_section.split("Reverb", 1)[1][:200]
    assert "Configured" in reverb_section
    assert "Not configured" not in reverb_section


def test_dashboard_shows_reverb_not_configured_when_token_missing(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reverb_api_token", None)

    status_section = _status_section(client.get("/").text)
    reverb_section = status_section.split("Reverb", 1)[1][:200]
    assert "Not configured" in reverb_section


def test_dashboard_status_panel_includes_bonanza(client) -> None:
    """Requirement: Bonanza must appear in the dashboard's system-status
    panel automatically through the registry-driven marketplace list -
    never a separately hard-coded entry."""
    status_section = _status_section(client.get("/").text)
    assert "Bonanza" in status_section


def test_dashboard_shows_bonanza_configured_when_dev_name_present(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bonanza_dev_name", "test-bonanza-dev-name")

    status_section = _status_section(client.get("/").text)
    bonanza_section = status_section.split("Bonanza", 1)[1][:200]
    assert "Configured" in bonanza_section
    assert "Not configured" not in bonanza_section


def test_dashboard_shows_bonanza_not_configured_when_dev_name_missing(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "bonanza_dev_name", None)

    status_section = _status_section(client.get("/").text)
    bonanza_section = status_section.split("Bonanza", 1)[1][:200]
    assert "Not configured" in bonanza_section


def test_dashboard_escapes_query_to_prevent_xss(client) -> None:
    _create_saved_search(client, query="<script>alert(1)</script>")

    body = client.get("/").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_dashboard_never_exposes_credential_values(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "reverb_api_token", "super-secret-reverb-token")
    monkeypatch.setattr(settings, "bonanza_dev_name", "super-secret-bonanza-dev-name")

    response = client.get("/")
    body = response.text

    leaked = any(
        secret and secret in body
        for secret in [
            settings.etsy_api_key,
            settings.etsy_shared_secret,
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            settings.ebay_app_id,
            settings.ebay_cert_id,
            settings.reverb_api_token,
            settings.bonanza_dev_name,
        ]
    )
    assert leaked is False


def test_dashboard_never_mentions_credential_field_names(client) -> None:
    body = client.get("/").text
    for forbidden in [
        "ETSY_API_KEY",
        "ETSY_SHARED_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
        "EBAY_DEV_ID",
        "REVERB_API_TOKEN",
        "BONANZA_DEV_NAME",
    ]:
        assert forbidden not in body


def test_static_css_is_served(client) -> None:
    response = client.get("/static/dashboard.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
    assert len(response.text) > 0


def test_static_js_is_served(client) -> None:
    response = client.get("/static/dashboard.js")
    assert response.status_code == 200
    assert len(response.text) > 0


def test_existing_saved_searches_api_still_works_alongside_dashboard(client) -> None:
    """Requirement: existing endpoints keep working unchanged alongside the
    new dashboard route - a light smoke check; full coverage lives in
    tests/test_saved_searches_api.py."""
    created = _create_saved_search(client).json()
    assert client.get("/saved-searches").status_code == 200
    assert client.get(f"/saved-searches/{created['id']}").status_code == 200
    assert client.patch(f"/saved-searches/{created['id']}", json={"is_active": False}).status_code == 200
    assert client.delete(f"/saved-searches/{created['id']}").status_code == 204
