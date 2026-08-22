from datetime import datetime, timezone

import httpx
import pytest

from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.models.listing import Listing

_TOKEN_BODY = {"access_token": "fake-access-token", "expires_in": 7200}


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep for retry backoff in tests - see
    tests/test_connector_retry.py for the exhaustive retry-policy tests."""
    calls: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _raw_listing(**overrides) -> dict:
    base = {
        "itemId": "v1|123456789|0",
        "title": "Makita Cordless Drill",
        "shortDescription": "A lightly used cordless drill.",
        "price": {"value": "89.99", "currency": "USD"},
        "itemWebUrl": "https://www.ebay.com/itm/123456789",
        "itemCreationDate": "2024-01-15T10:30:00.000Z",
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l500.jpg"},
        "itemLocation": {"city": "Austin", "stateOrProvince": "TX", "country": "US"},
        "seller": {"username": "power_tools_seller"},
        "condition": "Used",
    }
    base.update(overrides)
    return base


def _connector(**overrides) -> EbayMarketplaceConnector:
    kwargs = {"app_id": "test-app-id", "cert_id": "test-cert-id"}
    kwargs.update(overrides)
    return EbayMarketplaceConnector(**kwargs)


def _mock_token_and_search(monkeypatch: pytest.MonkeyPatch, search_response: httpx.Response) -> dict:
    """Mocks httpx.post (OAuth token) to always succeed, and httpx.get
    (search) to return the given response. Returns a dict that records the
    captured search call's url/params/headers."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_TOKEN_BODY))

    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return search_response

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


# --- request construction ------------------------------------------------


def test_search_requests_correct_url_params_and_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _mock_token_and_search(monkeypatch, httpx.Response(200, json={"itemSummaries": []}))

    connector = _connector(result_limit=10)
    connector.search("Makita")

    assert captured["url"] == "https://api.ebay.com/buy/browse/v1/item_summary/search"
    assert captured["params"]["q"] == "Makita"
    assert captured["params"]["limit"] == 10
    assert captured["params"]["offset"] == 0
    assert captured["headers"]["Authorization"] == "Bearer fake-access-token"
    assert captured["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_search_supports_pagination_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _mock_token_and_search(monkeypatch, httpx.Response(200, json={"itemSummaries": []}))

    connector = _connector()
    connector._fetch_results_page("Makita", offset=50)

    assert captured["params"]["offset"] == 50


# --- token reuse across searches ------------------------------------------


def test_token_manager_is_reused_not_refetched_per_search(monkeypatch: pytest.MonkeyPatch) -> None:
    token_call_count = 0

    def fake_post(*args, **kwargs):
        nonlocal token_call_count
        token_call_count += 1
        return httpx.Response(200, json=_TOKEN_BODY)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"itemSummaries": []}))

    connector = _connector()
    connector.search("Makita")
    connector.search("Makita")

    assert token_call_count == 1


# --- normalization ---------------------------------------------------------


def test_normalize_listing_maps_all_available_fields() -> None:
    listing = _connector().normalize_listing(_raw_listing())

    assert isinstance(listing, Listing)
    assert listing.marketplace == "ebay"
    assert listing.external_listing_id == "v1|123456789|0"
    assert listing.title == "Makita Cordless Drill"
    assert listing.description == "A lightly used cordless drill."
    assert listing.price == 89.99
    assert listing.currency == "USD"
    assert str(listing.listing_url) == "https://www.ebay.com/itm/123456789"
    assert str(listing.image_url) == "https://i.ebayimg.com/images/g/abc/s-l500.jpg"
    assert listing.location == "Austin, TX, US"
    assert listing.seller == "power_tools_seller"
    assert listing.condition == "Used"
    assert listing.created_at == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_normalize_listing_handles_missing_optional_fields() -> None:
    raw = _raw_listing()
    del raw["shortDescription"]
    del raw["price"]
    del raw["image"]
    del raw["itemLocation"]
    del raw["seller"]
    del raw["condition"]
    del raw["itemCreationDate"]

    listing = _connector().normalize_listing(raw)

    assert listing.title == raw["title"]
    assert listing.description is None
    assert listing.price is None
    assert listing.currency is None
    assert listing.image_url is None
    assert listing.location is None
    assert listing.seller is None
    assert listing.condition is None
    assert listing.created_at is None


# --- credentials -----------------------------------------------------------


def test_missing_credentials_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_TOKEN_BODY)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_post)

    connector = EbayMarketplaceConnector(app_id=None, cert_id=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Makita")

    assert called is False


def test_partial_credentials_also_raise() -> None:
    connector = EbayMarketplaceConnector(app_id="only-app-id", cert_id=None)
    assert connector.is_configured is False
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Makita")


def test_health_check_reflects_configuration() -> None:
    assert _connector().health_check() is True
    assert EbayMarketplaceConnector(app_id=None, cert_id=None).health_check() is False


# --- result handling ---------------------------------------------------------


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(200, json={}))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_multiple_results_returns_multiple_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "itemSummaries": [
            _raw_listing(itemId="item-1"),
            _raw_listing(itemId="item-2"),
        ]
    }
    _mock_token_and_search(monkeypatch, httpx.Response(200, json=body))

    results = _connector().search("Makita")

    assert len(results) == 2
    assert {r.external_listing_id for r in results} == {"item-1", "item-2"}


# --- malformed responses -----------------------------------------------


def test_response_with_non_list_item_summaries_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(200, json={"itemSummaries": "not-a-list"}))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_one_malformed_listing_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(itemId="item-1")
    bad = _raw_listing(itemId="item-2")
    del bad["title"]  # required field missing
    body = {"itemSummaries": [good, bad]}
    _mock_token_and_search(monkeypatch, httpx.Response(200, json=body))

    results = _connector().search("Makita")

    assert len(results) == 1
    assert results[0].external_listing_id == "item-1"


# --- transport-level and auth failures ------------------------------------


def test_api_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_unauthorized_status_raises_and_invalidates_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(401))
    connector = _connector()
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Makita")
    assert connector._token_manager._access_token is None


def test_forbidden_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(403))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_rate_limit_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_token_and_search(monkeypatch, httpx.Response(429))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_rate_limit_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """A transient 429 must not fail the search outright if a retry
    succeeds - full retry-policy behavior is exhaustively tested in
    tests/test_connector_retry.py; this just confirms eBay's connector is
    actually wired up to it."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_TOKEN_BODY))
    responses = [httpx.Response(429), httpx.Response(200, json={"itemSummaries": [_raw_listing()]})]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    results = _connector().search("Makita")

    assert len(results) == 1
    assert len(_no_real_sleeps) == 1


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_TOKEN_BODY))

    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


def test_oauth_failure_propagates_as_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Makita")


# --- registry -----------------------------------------------------------


def test_marketplace_name_is_ebay() -> None:
    assert _connector().marketplace_name == "ebay"
