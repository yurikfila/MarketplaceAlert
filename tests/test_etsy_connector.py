from datetime import datetime, timezone

import httpx
import pytest

from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.models.listing import Listing


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep for retry backoff in tests - see
    tests/test_connector_retry.py for the exhaustive retry-policy tests;
    this just keeps this file fast and records what would have been
    waited for, for the one test here that checks it."""
    calls: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _raw_listing(**overrides) -> dict:
    base = {
        "listing_id": 123456789,
        "title": "Maccabi Tel Aviv Vintage Pennant",
        "description": "A vintage felt pennant.",
        "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
        "url": "https://www.etsy.com/listing/123456789/maccabi-vintage-pennant",
        "state": "active",
        "shop_id": 987654,
        "original_creation_timestamp": 1700000000,
        "images": [
            {
                "listing_image_id": 1,
                "url_75x75": "https://i.etsystatic.com/1.jpg",
                "url_170x135": "https://i.etsystatic.com/2.jpg",
                "url_570xN": "https://i.etsystatic.com/3.jpg",
                "url_fullxfull": "https://i.etsystatic.com/4.jpg",
            }
        ],
    }
    base.update(overrides)
    return base


def _connector(**overrides) -> EtsyMarketplaceConnector:
    kwargs = {"api_key": "test-keystring", "shared_secret": "test-shared-secret"}
    kwargs.update(overrides)
    return EtsyMarketplaceConnector(**kwargs)


# --- request construction --------------------------------------------------


def test_search_requests_correct_url_params_and_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return httpx.Response(200, json={"count": 0, "results": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = _connector(result_limit=10)
    connector.search("Maccabi")

    assert captured["url"] == "https://api.etsy.com/v3/application/listings/active"
    assert captured["params"]["keywords"] == "Maccabi"
    assert captured["params"]["limit"] == 10
    # findAllListingsActive doesn't support `includes` at all (confirmed
    # live, not just from docs - see this module's docstring); an
    # earlier version sent includes=Images here, which Etsy silently
    # ignored - removed rather than left in as misleading dead weight.
    assert "includes" not in captured["params"]
    assert captured["headers"]["x-api-key"] == "test-keystring:test-shared-secret"


def test_search_forwards_min_max_price_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["params"] = params
        return httpx.Response(200, json={"count": 0, "results": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector().search("Maccabi", filters={"min_price": 10, "max_price": 100})

    assert captured["params"]["min_price"] == 10
    assert captured["params"]["max_price"] == 100


# --- normalization -----------------------------------------------------


def test_normalize_listing_maps_all_available_fields() -> None:
    listing = _connector().normalize_listing(_raw_listing())

    assert isinstance(listing, Listing)
    assert listing.marketplace == "etsy"
    assert listing.external_listing_id == "123456789"
    assert listing.title == "Maccabi Tel Aviv Vintage Pennant"
    assert listing.description == "A vintage felt pennant."
    assert listing.price == 25.0
    assert listing.currency == "USD"
    assert str(listing.listing_url) == "https://www.etsy.com/listing/123456789/maccabi-vintage-pennant"
    assert str(listing.image_url) == "https://i.etsystatic.com/3.jpg"
    assert listing.created_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    # Not available / not verified from Etsy for this endpoint - must be
    # null rather than invented.
    assert listing.location is None
    assert listing.seller is None
    assert listing.condition is None


def test_normalize_listing_handles_missing_optional_fields() -> None:
    raw = _raw_listing()
    del raw["description"]
    del raw["price"]
    del raw["images"]
    del raw["original_creation_timestamp"]

    listing = _connector().normalize_listing(raw)

    assert listing.title == raw["title"]
    assert listing.description is None
    assert listing.price is None
    assert listing.currency is None
    assert listing.image_url is None
    assert listing.created_at is None


# --- credentials ---------------------------------------------------------


def test_missing_credentials_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"count": 0, "results": []})

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = EtsyMarketplaceConnector(api_key=None, shared_secret=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Maccabi")

    assert called is False


def test_partial_credentials_also_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = EtsyMarketplaceConnector(api_key="only-key", shared_secret=None)
    assert connector.is_configured is False
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Maccabi")


def test_health_check_reflects_configuration() -> None:
    assert _connector().health_check() is True
    assert EtsyMarketplaceConnector(api_key=None, shared_secret=None).health_check() is False


# --- result handling -------------------------------------------------------


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(200, json={"count": 0, "results": []})
    )
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_multiple_results_returns_multiple_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "count": 2,
        "results": [_raw_listing(listing_id=1), _raw_listing(listing_id=2)],
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Maccabi")

    assert len(results) == 2
    assert {r.external_listing_id for r in results} == {"1", "2"}


# --- malformed responses -----------------------------------------------


def test_response_missing_results_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"count": 0}))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Maccabi")


def test_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(200, content=b"not valid json")
    )
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Maccabi")


def test_one_malformed_listing_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(listing_id=1)
    bad = _raw_listing(listing_id=2)
    del bad["listing_id"]  # required field missing
    body = {"count": 2, "results": [good, bad]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Maccabi")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


# --- transport-level failures -----------------------------------------


def test_api_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Maccabi")


def test_rate_limit_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(429))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Maccabi")


def test_rate_limit_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """A transient 429 must not fail the search outright if a retry
    succeeds - full retry-policy behavior is exhaustively tested in
    tests/test_connector_retry.py; this just confirms Etsy's connector is
    actually wired up to it."""
    responses = [httpx.Response(429), httpx.Response(200, json={"count": 1, "results": [_raw_listing()]})]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    results = _connector().search("Maccabi")

    assert len(results) == 1
    assert len(_no_real_sleeps) == 1  # one backoff wait between the two attempts


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Maccabi")


# --- normalize_listing picks up shop name only when a `shop` key is present ---


def test_normalize_listing_populates_seller_when_shop_object_present() -> None:
    """Only ever present on a get_listing_by_id (includes=Shop) response -
    search responses never have this key, so this can't change what a
    search-discovered listing returns (see test above, still asserts
    seller is None for a plain search-shaped raw listing)."""
    raw = _raw_listing(shop={"shop_id": 987654, "shop_name": "VintageSportsIL"})
    listing = _connector().normalize_listing(raw)
    assert listing.seller == "VintageSportsIL"
    assert listing.location is None  # never populated, even with a shop object present
    assert listing.condition is None  # Etsy has no condition concept at all


def test_normalize_listing_shop_without_shop_name_leaves_seller_null() -> None:
    raw = _raw_listing(shop={"shop_id": 987654})
    listing = _connector().normalize_listing(raw)
    assert listing.seller is None


# --- get_listing_by_id (historical backfill) --------------------------------


def _mock_get_item(monkeypatch: pytest.MonkeyPatch, item_response: httpx.Response) -> dict:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return item_response

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


def test_get_listing_by_id_requests_the_correct_url_includes_and_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _mock_get_item(monkeypatch, httpx.Response(200, json=_raw_listing()))

    _connector().get_listing_by_id("123456789")

    assert captured["url"] == "https://api.etsy.com/v3/application/listings/123456789"
    assert captured["params"]["includes"] == "Images,Shop"
    assert captured["headers"]["x-api-key"] == "test-keystring:test-shared-secret"


def test_get_listing_by_id_returns_a_fully_normalized_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_listing(shop={"shop_id": 987654, "shop_name": "VintageSportsIL"})
    _mock_get_item(monkeypatch, httpx.Response(200, json=raw))

    listing = _connector().get_listing_by_id("123456789")

    assert listing is not None
    assert listing.price == 25.0
    assert listing.seller == "VintageSportsIL"
    assert str(listing.image_url) == "https://i.etsystatic.com/3.jpg"


def test_get_listing_by_id_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(404))
    assert _connector().get_listing_by_id("000000000") is None


def test_get_listing_by_id_missing_credentials_raises_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_raw_listing())

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = EtsyMarketplaceConnector(api_key=None, shared_secret=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.get_listing_by_id("123456789")

    assert called is False


def test_get_listing_by_id_api_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("123456789")


def test_get_listing_by_id_rate_limit_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]
) -> None:
    responses = [httpx.Response(429), httpx.Response(200, json=_raw_listing())]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    listing = _connector().get_listing_by_id("123456789")

    assert listing is not None
    assert len(_no_real_sleeps) == 1


def test_get_listing_by_id_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_listing()
    del raw["listing_id"]
    _mock_get_item(monkeypatch, httpx.Response(200, json=raw))
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("123456789")


def test_get_listing_by_id_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("123456789")


def test_get_listing_by_id_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("123456789")
