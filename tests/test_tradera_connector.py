import httpx
import pytest

from marketplace_alert.connectors.tradera.connector import TraderaMarketplaceConnector
from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.models.listing import Listing


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep for retry backoff in tests - see
    tests/test_connector_retry.py for the exhaustive retry-policy tests."""
    calls: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _raw_listing(**overrides) -> dict:
    """Mirrors the exact field set of a real live Tradera `/v4/search`
    response item (verified 2026-09-28 against a real Bosch search) -
    sanitized values, but the field names and structure are the genuine
    verified shape: no `title` field at all (`shortDescription` serves
    that role), no `condition`/`location`/currency field anywhere, and
    `endDate` (never used for `source_created_at`)."""
    base = {
        "id": 1234567890,
        "shortDescription": "Bosch Professional Borrhammare GBH 2-26",
        "longDescription": "Kraftfull borrhammare i mycket gott skick. Fungerar perfekt.",
        "sellerAlias": "test-seller",
        "thumbnailLink": "https://img.tradera.net/images/example/thumb.jpg",
        "imageLinks": [{"url": "https://img.tradera.net/images/example/full.jpg", "format": "jpg"}],
        "itemUrl": "https://www.tradera.com/item/123456/1234567890/bosch-professional",
        "itemType": "Auction",
        "maxBid": 450.0,
        "nextBid": 470.0,
        "bidCount": 3,
        "hasBids": True,
        "endDate": "2026-10-01T18:00:00.000Z",
    }
    base.update(overrides)
    return base


def _connector(**overrides) -> TraderaMarketplaceConnector:
    kwargs = {"app_id": "test-app-id", "app_key": "test-app-key"}
    kwargs.update(overrides)
    return TraderaMarketplaceConnector(**kwargs)


def _response(items: list[dict] | None, errors: list[dict] | None = None) -> dict:
    body: dict = {"totalNumberOfItems": len(items) if items is not None else 0}
    if items is not None:
        body["items"] = items
    if errors is not None:
        body["errors"] = errors
    return body


# --- request construction --------------------------------------------------


def test_search_gets_the_correct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector().search("Bosch")

    assert captured["url"] == "https://api.tradera.com/v4/search"


def test_search_sends_app_id_and_app_key_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector(app_id="my-app-id", app_key="my-app-key").search("Bosch")

    assert captured["headers"]["X-App-Id"] == "my-app-id"
    assert captured["headers"]["X-App-Key"] == "my-app-key"


def test_search_sends_the_query_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["params"] = params
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector().search("Bosch")

    assert captured["params"]["query"] == "Bosch"


# --- normalization: price (Buy-It-Now vs. auction bidding state) -----------


def test_normalize_listing_uses_buy_it_now_price_when_present() -> None:
    raw = _raw_listing(buyItNowPrice=999.0, hasBids=False, maxBid=None, nextBid=None)
    listing = _connector().normalize_listing(raw)
    assert listing.price == 999.0


def test_normalize_listing_uses_max_bid_for_an_auction_with_bids() -> None:
    raw = _raw_listing(hasBids=True, maxBid=450.0, nextBid=470.0)
    assert "buyItNowPrice" not in raw  # the live-verified shape: absent, not null, for a plain auction

    listing = _connector().normalize_listing(raw)

    assert listing.price == 450.0


def test_normalize_listing_uses_next_bid_for_an_auction_with_no_bids_yet() -> None:
    raw = _raw_listing(hasBids=False, maxBid=None, nextBid=120.0)

    listing = _connector().normalize_listing(raw)

    assert listing.price == 120.0


def test_normalize_listing_price_is_none_when_no_price_signal_is_present() -> None:
    raw = _raw_listing(hasBids=False, maxBid=None, nextBid=None)

    listing = _connector().normalize_listing(raw)

    assert listing.price is None


def test_normalize_listing_ignores_max_bid_when_has_bids_is_false() -> None:
    """`hasBids: false` with a stray `maxBid` value must not be used - see
    connector.py's `_parse_price` docstring: maxBid is only meaningful
    once hasBids is true."""
    raw = _raw_listing(hasBids=False, maxBid=999.0, nextBid=50.0)

    listing = _connector().normalize_listing(raw)

    assert listing.price == 50.0


# --- normalization: everything else -----------------------------------


def test_normalize_listing_maps_all_available_fields() -> None:
    listing = _connector().normalize_listing(_raw_listing())

    assert isinstance(listing, Listing)
    assert listing.marketplace == "tradera"
    assert listing.external_listing_id == "1234567890"
    assert listing.title == "Bosch Professional Borrhammare GBH 2-26"
    assert listing.description == "Kraftfull borrhammare i mycket gott skick. Fungerar perfekt."
    assert listing.seller == "test-seller"
    assert listing.currency == "SEK"
    assert str(listing.listing_url) == "https://www.tradera.com/item/123456/1234567890/bosch-professional"
    assert str(listing.image_url) == "https://img.tradera.net/images/example/thumb.jpg"
    # Never available from search results - see connector.py's module docstring.
    assert listing.condition is None
    assert listing.location is None
    assert listing.created_at is None


def test_normalize_listing_currency_is_always_sek() -> None:
    """Tradera never returns a currency field on any search result - see
    connector.py's module docstring "Currency is never present" section.
    SEK is a fixed marketplace-level default, not read from the payload."""
    listing = _connector().normalize_listing(_raw_listing())
    assert listing.currency == "SEK"


def test_normalize_listing_never_maps_end_date_to_source_created_at() -> None:
    raw = _raw_listing(endDate="2026-10-01T18:00:00.000Z")
    listing = _connector().normalize_listing(raw)
    assert listing.created_at is None


def test_normalize_listing_image_prefers_thumbnail_link() -> None:
    raw = _raw_listing(
        thumbnailLink="https://img.tradera.net/thumb.jpg",
        imageLinks=[{"url": "https://img.tradera.net/full.jpg", "format": "jpg"}],
    )
    listing = _connector().normalize_listing(raw)
    assert str(listing.image_url) == "https://img.tradera.net/thumb.jpg"


def test_normalize_listing_image_falls_back_to_first_image_link_when_no_thumbnail() -> None:
    raw = _raw_listing(thumbnailLink=None, imageLinks=[{"url": "https://img.tradera.net/full.jpg", "format": "jpg"}])
    listing = _connector().normalize_listing(raw)
    assert str(listing.image_url) == "https://img.tradera.net/full.jpg"


def test_normalize_listing_image_skips_an_image_link_with_no_url() -> None:
    raw = _raw_listing(
        thumbnailLink=None,
        imageLinks=[{"url": None, "format": "jpg"}, {"url": "https://img.tradera.net/second.jpg", "format": "jpg"}],
    )
    listing = _connector().normalize_listing(raw)
    assert str(listing.image_url) == "https://img.tradera.net/second.jpg"


def test_normalize_listing_image_is_none_when_neither_field_has_a_usable_value() -> None:
    raw = _raw_listing(thumbnailLink=None, imageLinks=None)
    listing = _connector().normalize_listing(raw)
    assert listing.image_url is None


def test_normalize_listing_handles_missing_optional_fields() -> None:
    raw = _raw_listing()
    del raw["longDescription"]
    del raw["sellerAlias"]
    del raw["thumbnailLink"]
    del raw["imageLinks"]

    listing = _connector().normalize_listing(raw)

    assert listing.title == raw["shortDescription"]
    assert listing.description is None
    assert listing.seller is None
    assert listing.image_url is None


def test_normalize_listing_preserves_unicode_content() -> None:
    raw = _raw_listing(shortDescription="Gitarr \U0001f3b8 – blå färg, mycket fint skick")
    listing = _connector().normalize_listing(raw)
    assert listing.title == "Gitarr \U0001f3b8 – blå färg, mycket fint skick"


# --- normalization: missing required fields (title / URL) ------------------


def test_normalize_listing_raises_for_missing_title(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_listing()
    del raw["shortDescription"]
    with pytest.raises(ValueError):
        _connector().normalize_listing(raw)


def test_normalize_listing_raises_for_blank_title() -> None:
    raw = _raw_listing(shortDescription="   ")
    with pytest.raises(ValueError):
        _connector().normalize_listing(raw)


def test_normalize_listing_raises_for_missing_url() -> None:
    raw = _raw_listing()
    del raw["itemUrl"]
    with pytest.raises(ValueError):
        _connector().normalize_listing(raw)


def test_normalize_listing_raises_for_blank_url() -> None:
    raw = _raw_listing(itemUrl="")
    with pytest.raises(ValueError):
        _connector().normalize_listing(raw)


def test_search_skips_a_listing_with_no_title_but_keeps_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(id=1)
    bad = _raw_listing(id=2)
    del bad["shortDescription"]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_response([good, bad])))

    results = _connector().search("Bosch")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


def test_search_skips_a_listing_with_no_url_but_keeps_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(id=1)
    bad = _raw_listing(id=2)
    del bad["itemUrl"]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_response([good, bad])))

    results = _connector().search("Bosch")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


# --- credentials ---------------------------------------------------------


def test_missing_app_id_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = TraderaMarketplaceConnector(app_id=None, app_key="test-app-key")
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Bosch")

    assert called is False


def test_missing_app_key_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = TraderaMarketplaceConnector(app_id="test-app-id", app_key=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Bosch")

    assert called is False


def test_health_check_reflects_configuration() -> None:
    assert _connector().health_check() is True
    assert TraderaMarketplaceConnector(app_id=None, app_key=None).health_check() is False


def test_is_configured_false_without_both_credentials() -> None:
    assert TraderaMarketplaceConnector(app_id=None, app_key=None).is_configured is False
    assert TraderaMarketplaceConnector(app_id="x", app_key=None).is_configured is False
    assert TraderaMarketplaceConnector(app_id=None, app_key="x").is_configured is False
    assert TraderaMarketplaceConnector(app_id="x", app_key="x").is_configured is True


# --- result handling -------------------------------------------------------


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_response([])))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_missing_items_key_treated_as_zero_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"totalNumberOfItems": 0, "errors": []}))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_multiple_results_returns_multiple_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _response([_raw_listing(id=1), _raw_listing(id=2)])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Bosch")

    assert len(results) == 2
    assert {r.external_listing_id for r in results} == {"1", "2"}


def test_one_malformed_listing_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(id=1)
    bad = _raw_listing(id=2)
    del bad["id"]
    body = _response([good, bad])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Bosch")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


def test_result_limit_caps_the_number_of_listings_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _response([_raw_listing(id=i) for i in range(1, 6)])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector(result_limit=2).search("Bosch")

    assert len(results) == 2


# --- top-level API errors (errors array, even on HTTP 200) -----------------


def test_non_empty_errors_array_raises_even_on_http_200(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _response([], errors=[{"code": "InvalidQuery", "message": "Something went wrong"}])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


def test_empty_errors_array_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _response([_raw_listing()], errors=[])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Bosch")

    assert len(results) == 1


# --- malformed responses -------------------------------------------------


def test_items_not_a_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"items": "not-a-list", "errors": []}))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


def test_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


# --- transport-level failures -----------------------------------------


def test_401_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    with pytest.raises(MarketplaceConnectorError, match="401"):
        _connector().search("Bosch")


def test_403_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(403))
    with pytest.raises(MarketplaceConnectorError, match="403"):
        _connector().search("Bosch")


def test_429_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(429))
    with pytest.raises(MarketplaceConnectorError, match="429"):
        _connector().search("Bosch")


def test_429_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """A transient 429 must not fail the search outright if a retry
    succeeds - full retry-policy behavior is exhaustively tested in
    tests/test_connector_retry.py; this just confirms Tradera's connector
    is actually wired up to it."""
    responses = [httpx.Response(429), httpx.Response(200, json=_response([_raw_listing()]))]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    results = _connector().search("Bosch")

    assert len(results) == 1
    assert len(_no_real_sleeps) == 1


def test_500_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


def test_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError("simulated connection error")

    monkeypatch.setattr(httpx, "get", raise_connect_error)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Bosch")


# --- safety: never logs the app key -------------------------------------


def test_error_message_never_contains_the_app_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    connector = _connector(app_key="super-secret-app-key")
    try:
        connector.search("Bosch")
    except MarketplaceConnectorError as exc:
        assert "super-secret-app-key" not in str(exc)
    else:
        pytest.fail("expected MarketplaceConnectorError")


def test_log_output_never_contains_the_app_key(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_response([_raw_listing()])))
    with caplog.at_level("DEBUG"):
        _connector(app_key="super-secret-app-key").search("Bosch")
    assert "super-secret-app-key" not in caplog.text


# --- get_listing_by_id: deliberately not implemented in this phase ---------


def test_get_listing_by_id_raises_not_supported() -> None:
    """Not implemented in this phase (see connector.py's module docstring
    "No get_listing_by_id() override in this phase") - must raise the
    base class's ListingLookupNotSupportedError, which the historical
    backfill service treats as "skip this marketplace, log why," never a
    connector failure to retry."""
    from marketplace_alert.core.connectors.base import ListingLookupNotSupportedError

    with pytest.raises(ListingLookupNotSupportedError):
        _connector().get_listing_by_id("1234567890")
