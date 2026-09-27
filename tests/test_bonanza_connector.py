import json
from datetime import datetime, timezone

import httpx
import pytest

from marketplace_alert.connectors.bonanza.connector import BonanzaMarketplaceConnector
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
    base = {
        "itemId": "998877",
        "title": "Fender Stratocaster Electric Guitar",
        "viewItemURL": "https://www.bonanza.com/listings/998877",
        "galleryURL": "https://img.bonanza.com/img/998877.jpg",
        "sellingStatus": {"currentPrice": {"__value__": "899.99", "@currencyId": "USD"}},
        "sellerInfo": {"sellerUserName": "guitarshop"},
        "listingInfo": {"startTime": "2026-08-20T10:00:00.000Z"},
        "location": "Austin, TX",
        "country": "US",
    }
    base.update(overrides)
    return base


def _connector(**overrides) -> BonanzaMarketplaceConnector:
    kwargs = {"dev_name": "test-dev-name"}
    kwargs.update(overrides)
    return BonanzaMarketplaceConnector(**kwargs)


def _response(items: list[dict] | None, ack: str = "Success") -> dict:
    """Builds a response body in the REAL, verified shape (confirmed via
    a live request, 2026-09-27): `ack` at the response's own top level,
    and `item` directly under `findItemsByKeywordsResponse` - NOT nested
    under a `searchResult` wrapper, which is what this connector
    originally (and incorrectly) assumed. See connector.py's module
    docstring "Verified against a real live response" section."""
    envelope: dict = {}
    if items is not None:
        envelope["item"] = items
    return {"ack": ack, "findItemsByKeywordsResponse": envelope}


def _real_shape_raw_listing(**overrides) -> dict:
    """Mirrors the exact field set/nesting of a real live Bonapitit
    `findItemsByKeywords` response item, captured 2026-09-27 (see
    PROJECT_CONTEXT.md and connector.py's module docstring) - seller/store
    identifiers and image/URL hostnames are sanitized placeholders, but
    every field name, nesting level, and value TYPE is the genuine
    verified shape: `sellingStatus.currentPrice` is a plain numeric
    string (never the eBay-Finding-API value/attribute object originally
    assumed), there is no currency field anywhere on the item, no
    `country` key alongside `location`, no `condition` key at all, and a
    real `descriptionBrief` field this connector did not previously map."""
    base = {
        "descriptionBrief": "A great guitar amplifier, barely used.",
        "galleryURL": "https://images-bucket.example.com/afu/images/example/s-l1600_thumb155_crop.jpg",
        "globalId": "BONANZLE",
        "itemId": 1342281715,
        "listingInfo": {
            "bestOfferEnabled": "false",
            "buyItNowPrice": "99.8",
            "convertedBuyItNowPrice": "99.8",
            "listingType": "FixedPrice",
            "price": "99.8",
            "startTime": "2022-09-20T09:24:13.000Z",
            "lastChangeTime": "2023-03-30T23:30:51.000Z",
        },
        "location": "Canada",
        "paymentMethod": ["PayPal"],
        "postalCode": "n3t1g7",
        "primaryCategory": {
            "categoryId": 180009,
            "categoryName": "Musical Instruments & Gear >> Guitars & Basses >> Parts & Accessories",
            "categoryIdHierarchy": [3858, 619],
        },
        "sellerInfo": {
            "feedbackRatingStar": "Green",
            "positiveFeedbackPercent": "97.8",
            "sellerUserName": "test-seller",
            "availableForChat": "false",
            "membershipLevel": None,
            "userPicture": "https://images-bucket.example.com/user_profile_image/example.jpg",
        },
        "sellingStatus": {
            "convertedCurrentPrice": "99.8",
            "currentPrice": "99.8",
            "sellingState": "Active",
        },
        "shippingInfo": {
            "shippingServiceCost": 0,
            "shippingType": "Free",
            "shipToLocations": ["United States", "Canada", "United Kingdom", "Germany"],
            "internationalShipping": [
                {"region": "United States", "shippingType": "Free", "shippingServiceCost": 0},
                {"region": "Canada", "shippingType": "Flat", "shippingServiceCost": "16.8"},
            ],
        },
        "sku": None,
        "storeInfo": {
            "storeDiscount": None,
            "storeItemCount": 2882,
            "storeName": "test-seller's booth",
            "storeURL": "https://www.bonanza.com/booths/test-seller",
        },
        "title": "Rowin WS-30 Wireless Guitar System Transmitters,Upgrades Portable Digital",
        "viewItemURL": "https://www.bonanza.com/booths/test-seller/items/1342281715",
    }
    base.update(overrides)
    return base


# --- request construction --------------------------------------------------


def test_search_posts_to_the_correct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, data, headers, timeout):
        captured["url"] = url
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "post", fake_post)

    _connector().search("Fender Stratocaster")

    assert captured["url"] == "https://api.bonanza.com/api_requests/standard_request"


def test_search_sends_dev_name_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, data, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "post", fake_post)

    _connector(dev_name="my-real-dev-name").search("Fender")

    assert captured["headers"]["X-BONANZLE-API-DEV-NAME"] == "my-real-dev-name"


def test_search_encodes_keywords_and_pagination_in_the_form_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, data, headers, timeout):
        captured["data"] = data
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "post", fake_post)

    _connector(result_limit=10).search("Fender Stratocaster")

    payload = json.loads(captured["data"]["findItemsByKeywords"])
    assert payload["keywords"] == "Fender Stratocaster"
    assert payload["paginationInput"]["pageNumber"] == 1
    assert payload["paginationInput"]["entriesPerPage"] == 10


def test_entries_per_page_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, data, headers, timeout: (captured.update(data=data), httpx.Response(200, json=_response([])))[1],
    )

    _connector(result_limit=500).search("Fender")

    payload = json.loads(captured["data"]["findItemsByKeywords"])
    assert payload["paginationInput"]["entriesPerPage"] <= 100


# --- pagination --------------------------------------------------------


def test_pagination_advances_page_number_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post(url, data, headers, timeout):
        payload = json.loads(data["findItemsByKeywords"])
        page = payload["paginationInput"]["pageNumber"]
        calls.append(page)
        if page == 1:
            items = [_raw_listing(itemId=str(i)) for i in range(1, 4)]  # full page (entriesPerPage=3)
        else:
            items = [_raw_listing(itemId="4")]  # short page - signals no more results
        return httpx.Response(200, json=_response(items))

    monkeypatch.setattr(httpx, "post", fake_post)

    results = _connector(result_limit=3).search("Fender")
    # entriesPerPage matches result_limit (3), so page 1 alone satisfies it
    assert len(results) == 3
    assert calls == [1]


def test_pagination_stops_once_result_limit_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append(1)
        items = [_raw_listing(itemId=str(i)) for i in range(1, 6)]
        return httpx.Response(200, json=_response(items))

    monkeypatch.setattr(httpx, "post", fake_post)

    results = _connector(result_limit=2).search("Fender")

    assert len(results) == 2
    assert len(calls) == 1


def test_pagination_respects_a_hard_max_pages_safety_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = {"n": 0}

    def fake_post(url, data, headers, timeout):
        call_count["n"] += 1
        payload = json.loads(data["findItemsByKeywords"])
        entries_per_page = payload["paginationInput"]["entriesPerPage"]
        # Always return a full page, so pagination would never naturally stop.
        items = [_raw_listing(itemId=str(call_count["n"] * 1000 + i)) for i in range(entries_per_page)]
        return httpx.Response(200, json=_response(items))

    monkeypatch.setattr(httpx, "post", fake_post)

    results = _connector(result_limit=1000).search("Fender")

    assert call_count["n"] <= 10
    assert len(results) <= 1000


# --- normalization -----------------------------------------------------


def test_normalize_listing_maps_all_available_fields() -> None:
    listing = _connector().normalize_listing(_raw_listing())

    assert isinstance(listing, Listing)
    assert listing.marketplace == "bonanza"
    assert listing.external_listing_id == "998877"
    assert listing.title == "Fender Stratocaster Electric Guitar"
    assert listing.price == 899.99
    assert listing.currency == "USD"
    assert listing.seller == "guitarshop"
    assert listing.location == "Austin, TX, US"
    assert str(listing.listing_url) == "https://www.bonanza.com/listings/998877"
    assert str(listing.image_url) == "https://img.bonanza.com/img/998877.jpg"
    assert listing.created_at == datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)


def test_normalize_listing_price_handles_plain_number_fallback() -> None:
    raw = _raw_listing(sellingStatus={"currentPrice": 42.5, "currencyId": "USD"})
    listing = _connector().normalize_listing(raw)
    assert listing.price == 42.5
    assert listing.currency == "USD"


def test_normalize_listing_condition_as_object_or_plain_string() -> None:
    obj_listing = _connector().normalize_listing(_raw_listing(condition={"conditionDisplayName": "Used"}))
    assert obj_listing.condition == "Used"

    str_listing = _connector().normalize_listing(_raw_listing(condition="New"))
    assert str_listing.condition == "New"


def test_normalize_listing_handles_missing_optional_fields() -> None:
    raw = _raw_listing()
    del raw["sellingStatus"]
    del raw["sellerInfo"]
    del raw["galleryURL"]
    del raw["listingInfo"]
    del raw["location"]
    del raw["country"]

    listing = _connector().normalize_listing(raw)

    assert listing.title == raw["title"]
    assert listing.price is None
    assert listing.currency is None
    assert listing.seller is None
    assert listing.image_url is None
    assert listing.created_at is None
    assert listing.location is None
    assert listing.condition is None
    # This fixture has no `descriptionBrief` key - never invented.
    assert listing.description is None


def test_normalize_listing_maps_the_verified_live_response_shape() -> None:
    """Regression test for the real root cause found 2026-09-27 (see
    connector.py's module docstring): this exact field set/nesting/typing
    is what a real Bonapitit response actually looks like."""
    listing = _connector().normalize_listing(_real_shape_raw_listing())

    assert listing.external_listing_id == "1342281715"
    assert listing.title == "Rowin WS-30 Wireless Guitar System Transmitters,Upgrades Portable Digital"
    assert str(listing.listing_url) == "https://www.bonanza.com/booths/test-seller/items/1342281715"
    assert listing.price == 99.8
    assert listing.currency is None  # genuinely not present anywhere in the real response
    assert listing.seller == "test-seller"
    assert listing.location == "Canada"
    assert listing.condition is None  # genuinely not present in the real response
    assert str(listing.image_url) == "https://images-bucket.example.com/afu/images/example/s-l1600_thumb155_crop.jpg"
    assert listing.created_at == datetime(2022, 9, 20, 9, 24, 13, tzinfo=timezone.utc)
    assert listing.description == "A great guitar amplifier, barely used."


def test_normalize_listing_preserves_unicode_content() -> None:
    raw = _raw_listing(title="Gibson Les Paul \U0001f3b8 – café au lait finish")
    listing = _connector().normalize_listing(raw)
    assert listing.title == "Gibson Les Paul \U0001f3b8 – café au lait finish"


# --- credentials ---------------------------------------------------------


def test_missing_dev_name_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_response([]))

    monkeypatch.setattr(httpx, "post", fake_post)

    connector = BonanzaMarketplaceConnector(dev_name=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Fender")

    assert called is False


def test_health_check_reflects_configuration() -> None:
    assert _connector().health_check() is True
    assert BonanzaMarketplaceConnector(dev_name=None).health_check() is False


def test_is_configured_false_without_dev_name() -> None:
    assert BonanzaMarketplaceConnector(dev_name=None).is_configured is False
    assert BonanzaMarketplaceConnector(dev_name="x").is_configured is True


# --- result handling -------------------------------------------------------


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_response([])))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_missing_item_key_treated_as_zero_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like eBay's Finding API (which Bonanza's API mirrors), the `item`
    key may be omitted entirely for a zero-match search rather than an
    empty array - must not be treated as malformed."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_response(None)))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_multiple_results_returns_multiple_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _response([_raw_listing(itemId="1"), _raw_listing(itemId="2")])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 2
    assert {r.external_listing_id for r in results} == {"1", "2"}


def test_one_malformed_listing_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(itemId="1")
    bad = _raw_listing(itemId="2")
    del bad["itemId"]
    body = _response([good, bad])
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


# --- malformed responses / ack handling ---------------------------------


def test_ack_failure_on_first_page_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_response([], ack="Failure")))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_response_missing_envelope_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={}))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_search_correctly_parses_the_real_response_envelope_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual root-cause regression test. A real live response nests
    `ack` at the response's top level and `item` directly under
    `findItemsByKeywordsResponse` - NOT under a `searchResult` wrapper,
    which is what this connector incorrectly assumed until this fix
    (sourced from Bonanza's own auto-converted docs page, never
    byte-exact-confirmed before now - see module docstring). Before this
    fix, `search()` silently returned `[]` for this exact real shape,
    even though the real API had 6941 total matching entries."""
    body = {
        "ack": "Success",
        "version": "1.1.2",
        "timestamp": "2026-09-27T09:51:04.000Z",
        "findItemsByKeywordsResponse": {
            "paginationOutput": {"entriesPerPage": 1, "pageNumber": 1, "totalEntries": 6941},
            "item": [_real_shape_raw_listing()],
        },
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector(result_limit=1).search("guitar")

    assert len(results) == 1
    assert results[0].external_listing_id == "1342281715"


def test_search_still_accepts_the_originally_assumed_searchresult_nested_shape_as_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive fallback only, never tried first anymore - see
    `_extract_raw_listings`. Kept in case a differently-configured
    account or a future response variant nests results this way."""
    body = {"ack": "Success", "findItemsByKeywordsResponse": {"searchResult": {"item": [_raw_listing()]}}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert results[0].external_listing_id == "998877"


def test_missing_item_key_with_unrecognized_ack_raises_not_silently_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `item` key is only a legitimate zero-result search when
    `ack` is a confirmed `"Success"` (see
    test_missing_item_key_treated_as_zero_results). Any other/unrecognized
    `ack` value combined with no `item` key at all is an unexpected
    response shape and must raise - never be silently treated as a
    zero-result search. This is the actual protection added for the real
    bug found 2026-09-27, where a real, non-empty response was
    misread as zero results with nothing logged to explain why."""
    body = {"ack": "Warning", "findItemsByKeywordsResponse": {}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_missing_item_key_with_no_ack_at_all_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"findItemsByKeywordsResponse": {}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


# --- transport-level failures -----------------------------------------


def test_401_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    with pytest.raises(MarketplaceConnectorError, match="401"):
        _connector().search("Fender")


def test_403_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(403))
    with pytest.raises(MarketplaceConnectorError, match="403"):
        _connector().search("Fender")


def test_429_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(429))
    with pytest.raises(MarketplaceConnectorError, match="429"):
        _connector().search("Fender")


def test_429_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """A transient 429 must not fail the search outright if a retry
    succeeds - full retry-policy behavior is exhaustively tested in
    tests/test_connector_retry.py; this just confirms Bonanza's connector
    is actually wired up to it."""
    responses = [httpx.Response(429), httpx.Response(200, json=_response([_raw_listing()]))]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: responses.pop(0))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert len(_no_real_sleeps) == 1


def test_500_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "post", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError("simulated connection error")

    monkeypatch.setattr(httpx, "post", raise_connect_error)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


# --- safety: never logs the dev name ------------------------------------


def test_error_message_never_contains_the_dev_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    connector = _connector(dev_name="super-secret-dev-name")
    try:
        connector.search("Fender")
    except MarketplaceConnectorError as exc:
        assert "super-secret-dev-name" not in str(exc)
    else:
        pytest.fail("expected MarketplaceConnectorError")


def test_log_output_never_contains_the_dev_name(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_response([_raw_listing()])))
    with caplog.at_level("DEBUG"):
        _connector(dev_name="super-secret-dev-name").search("Fender")
    assert "super-secret-dev-name" not in caplog.text


# --- get_listing_by_id: deliberately unsupported (historical backfill) ------


def test_get_listing_by_id_raises_not_supported() -> None:
    """Bonanza has no confirmed single-item lookup endpoint (see the
    connector's module docstring) - it must raise the base class's
    ListingLookupNotSupportedError, which the historical backfill
    service treats as "skip this marketplace, log why," never a
    connector failure to retry."""
    from marketplace_alert.core.connectors.base import ListingLookupNotSupportedError

    with pytest.raises(ListingLookupNotSupportedError):
        _connector().get_listing_by_id("123456789")
