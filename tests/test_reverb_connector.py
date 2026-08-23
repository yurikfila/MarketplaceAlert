from datetime import datetime, timedelta, timezone

import httpx
import pytest

from marketplace_alert.connectors.reverb.connector import ReverbMarketplaceConnector
from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.base import MarketplaceAuthError, MarketplaceConnectorError
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
        "id": 84838674,
        "title": "Fender Stratocaster - Sunburst",
        "description": "Great condition, plays beautifully.",
        "price": {"amount": "1419.30", "currency": "USD"},
        "condition": {"uuid": "abc-uuid", "display_name": "Very Good"},
        "shop": {"name": "Pedal Haus", "id": 5231040},
        "photos": [
            {
                "_links": {
                    "full": {"href": "https://img.reverb.com/full.jpg"},
                    "large": {"href": "https://img.reverb.com/large.jpg"},
                }
            }
        ],
        "_links": {
            "web": {"href": "https://reverb.com/item/84838674"},
            "self": {"href": "https://api.reverb.com/api/listings/84838674"},
        },
        "published_at": "2026-08-20T10:00:00-05:00",
    }
    base.update(overrides)
    return base


def _connector(**overrides) -> ReverbMarketplaceConnector:
    kwargs = {"api_token": "test-token"}
    kwargs.update(overrides)
    return ReverbMarketplaceConnector(**kwargs)


def _body(listings: list[dict], links: dict | None = None) -> dict:
    return {"listings": listings, "_links": links or {}}


# --- request construction --------------------------------------------------


def test_search_requests_correct_url_and_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        return httpx.Response(200, json=_body([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector(result_limit=10).search("Fender Stratocaster")

    assert captured["url"] == "https://api.reverb.com/api/listings"
    assert captured["params"]["query"] == "Fender Stratocaster"
    assert captured["params"]["per_page"] == 10


def test_search_sends_bearer_token_and_required_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json=_body([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector(api_token="my-real-token").search("Fender")

    assert captured["headers"]["Authorization"] == "Bearer my-real-token"
    assert captured["headers"]["Accept"] == "application/hal+json"


def test_search_sends_accept_version_3_0(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_get(url, params, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json=_body([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    _connector().search("Fender")

    assert captured["headers"]["Accept-Version"] == "3.0"


def test_per_page_is_capped_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, params, headers, timeout: (captured.update(params=params), httpx.Response(200, json=_body([])))[1],
    )

    _connector(result_limit=5).search("Fender")
    assert captured["params"]["per_page"] == 5

    _connector(result_limit=500).search("Fender")
    assert captured["params"]["per_page"] <= 50  # conservative safety cap, see connector module docstring


# --- pagination --------------------------------------------------------


def test_pagination_follows_links_next_href_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append({"url": url, "params": params})
        if url == "https://api.reverb.com/api/listings":
            return httpx.Response(
                200,
                json=_body(
                    [_raw_listing(id=1)],
                    links={"next": {"href": "https://api.reverb.com/api/listings?page=2&query=Fender"}},
                ),
            )
        assert url == "https://api.reverb.com/api/listings?page=2&query=Fender"
        return httpx.Response(200, json=_body([_raw_listing(id=2)]))

    monkeypatch.setattr(httpx, "get", fake_get)

    results = _connector(result_limit=10).search("Fender")

    assert {r.external_listing_id for r in results} == {"1", "2"}
    # The second request followed the href directly, with no extra params
    # reconstructed on top of it - HAL convention, see module docstring.
    assert calls[1]["params"] is None


def test_pagination_stops_once_result_limit_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(url)
        return httpx.Response(
            200,
            json=_body(
                [_raw_listing(id=1), _raw_listing(id=2)],
                links={"next": {"href": "https://api.reverb.com/api/listings?page=2"}},
            ),
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    results = _connector(result_limit=2).search("Fender")

    assert len(results) == 2
    assert len(calls) == 1  # never fetched page 2 - already had enough


def test_pagination_stops_when_no_next_link_present(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, params, headers, timeout: (calls.append(url), httpx.Response(200, json=_body([_raw_listing()])))[1],
    )

    results = _connector(result_limit=50).search("Fender")

    assert len(results) == 1
    assert len(calls) == 1


def test_pagination_respects_a_hard_max_pages_safety_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if every page reports a `next` link and returns only one
    result, pagination must not run away - a safety cap bounds the total
    number of requests for a single search() call."""
    call_count = {"n": 0}

    def fake_get(url, params, headers, timeout):
        call_count["n"] += 1
        return httpx.Response(
            200,
            json=_body(
                [_raw_listing(id=call_count["n"])],
                links={"next": {"href": f"https://api.reverb.com/api/listings?page={call_count['n'] + 1}"}},
            ),
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    results = _connector(result_limit=1000).search("Fender")

    assert call_count["n"] <= 10  # a small, bounded number of requests, not hundreds
    assert len(results) == call_count["n"]


# --- normalization -----------------------------------------------------


def test_normalize_listing_maps_all_available_fields() -> None:
    listing = _connector().normalize_listing(_raw_listing())

    assert isinstance(listing, Listing)
    assert listing.marketplace == "reverb"
    assert listing.external_listing_id == "84838674"
    assert listing.title == "Fender Stratocaster - Sunburst"
    assert listing.description == "Great condition, plays beautifully."
    assert listing.price == 1419.30
    assert listing.currency == "USD"
    assert listing.condition == "Very Good"
    assert listing.seller == "Pedal Haus"
    assert str(listing.listing_url) == "https://reverb.com/item/84838674"
    assert str(listing.image_url) == "https://img.reverb.com/full.jpg"
    assert listing.created_at == datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone(-timedelta(hours=5)))


def test_normalize_listing_title_and_description_are_exact() -> None:
    listing = _connector().normalize_listing(_raw_listing(title="Exact Title", description="Exact description."))
    assert listing.title == "Exact Title"
    assert listing.description == "Exact description."


def test_normalize_listing_price_falls_back_to_price_cents(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_listing()
    del raw["price"]
    raw["price_cents"] = 141930
    raw["currency"] = "USD"

    listing = _connector().normalize_listing(raw)

    assert listing.price == 1419.30
    assert listing.currency == "USD"


def test_normalize_listing_never_parses_a_display_price_string() -> None:
    """No structured price field at all (no `price.amount`, no
    `price_cents`) must leave price null - never guessed from a display
    string like "$1,419.30", per this connector's own requirements."""
    raw = _raw_listing()
    del raw["price"]
    raw["price_display"] = "$1,419.30"

    listing = _connector().normalize_listing(raw)

    assert listing.price is None


def test_normalize_listing_condition_as_plain_string() -> None:
    listing = _connector().normalize_listing(_raw_listing(condition="Mint"))
    assert listing.condition == "Mint"


def test_normalize_listing_seller_falls_back_to_shop_name_field() -> None:
    raw = _raw_listing()
    del raw["shop"]
    raw["shop_name"] = "Fallback Shop"

    listing = _connector().normalize_listing(raw)

    assert listing.seller == "Fallback Shop"


def test_normalize_listing_image_falls_back_to_plain_url_string() -> None:
    listing = _connector().normalize_listing(_raw_listing(photos=["https://img.reverb.com/plain.jpg"]))
    assert str(listing.image_url) == "https://img.reverb.com/plain.jpg"


def test_normalize_listing_image_falls_back_to_large_when_full_is_missing() -> None:
    raw = _raw_listing(photos=[{"_links": {"large": {"href": "https://img.reverb.com/large-only.jpg"}}}])
    listing = _connector().normalize_listing(raw)
    assert str(listing.image_url) == "https://img.reverb.com/large-only.jpg"


def test_normalize_listing_published_at_parsed_as_datetime() -> None:
    listing = _connector().normalize_listing(_raw_listing(published_at="2026-08-20T10:00:00Z"))
    assert listing.created_at == datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)


def test_normalize_listing_published_at_falls_back_to_created_at() -> None:
    raw = _raw_listing()
    del raw["published_at"]
    raw["created_at"] = "2026-08-19T09:00:00Z"

    listing = _connector().normalize_listing(raw)

    assert listing.created_at == datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc)


def test_normalize_listing_handles_missing_optional_fields() -> None:
    raw = _raw_listing()
    del raw["description"]
    del raw["price"]
    del raw["condition"]
    del raw["shop"]
    del raw["photos"]
    del raw["published_at"]

    listing = _connector().normalize_listing(raw)

    assert listing.title == raw["title"]
    assert listing.description is None
    assert listing.price is None
    assert listing.currency is None
    assert listing.condition is None
    assert listing.seller is None
    assert listing.image_url is None
    assert listing.created_at is None
    # Never invented - location has no confirmed source in this sample.
    assert listing.location is None


def test_normalize_listing_preserves_unicode_and_emoji_content() -> None:
    raw = _raw_listing(
        title="Fender Stratocaster \U0001f3b8 - Sünburst",
        description="Mañana café – ¡increíble!",
    )

    listing = _connector().normalize_listing(raw)

    assert listing.title == "Fender Stratocaster \U0001f3b8 - Sünburst"
    assert listing.description == "Mañana café – ¡increíble!"


def test_search_preserves_unicode_through_the_full_http_response_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the actual data pipeline (httpx JSON decoding, not just the
    normalizer) round-trips UTF-8 correctly - guards against the mojibake
    seen in one manual PowerShell test being an actual data bug rather
    than just that terminal's own display encoding."""
    raw = _raw_listing(title="Gibson Les Paul \U0001f3b8 – café au lait finish")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_body([raw])))

    results = _connector().search("Gibson")

    assert results[0].title == "Gibson Les Paul \U0001f3b8 – café au lait finish"


# --- credentials ---------------------------------------------------------


def test_missing_token_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_body([]))

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = ReverbMarketplaceConnector(api_token=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.search("Fender")

    assert called is False


def test_health_check_reflects_configuration() -> None:
    assert _connector().health_check() is True
    assert ReverbMarketplaceConnector(api_token=None).health_check() is False


def test_is_configured_false_without_token() -> None:
    assert ReverbMarketplaceConnector(api_token=None).is_configured is False
    assert ReverbMarketplaceConnector(api_token="x").is_configured is True


# --- result handling -------------------------------------------------------


def test_empty_results_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_body([])))
    assert _connector().search("Nonexistent Item Zyxwvut") == []


def test_multiple_results_returns_multiple_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body([_raw_listing(id=1), _raw_listing(id=2)])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 2
    assert {r.external_listing_id for r in results} == {"1", "2"}


def test_one_malformed_listing_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(id=1)
    bad = _raw_listing(id=2)
    del bad["id"]  # required field missing
    body = _body([good, bad])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


def test_listing_missing_web_link_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _raw_listing(id=1)
    bad = _raw_listing(id=2, **{"_links": {}})  # no web link at all
    body = _body([good, bad])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert results[0].external_listing_id == "1"


# --- malformed responses -----------------------------------------------


def test_response_missing_listings_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"_links": {}}))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_unexpected_pagination_shape_is_treated_as_no_next_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_links` present but not the expected {"next": {"href": ...}} shape
    must not crash - just means "no more pages"."""
    body = {"listings": [_raw_listing()], "_links": {"next": "not-an-object"}}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=body))

    results = _connector().search("Fender")

    assert len(results) == 1


# --- transport-level failures -----------------------------------------


def test_401_raises_a_clear_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    with pytest.raises(MarketplaceConnectorError, match="401"):
        _connector().search("Fender")


def test_403_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(403))
    with pytest.raises(MarketplaceConnectorError, match="403"):
        _connector().search("Fender")


def test_404_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404))
    with pytest.raises(MarketplaceConnectorError, match="404"):
        _connector().search("Fender")


def test_429_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(429))
    with pytest.raises(MarketplaceConnectorError, match="429"):
        _connector().search("Fender")


def test_429_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """A transient 429 must not fail the search outright if a retry
    succeeds - full retry-policy behavior is exhaustively tested in
    tests/test_connector_retry.py; this just confirms Reverb's connector
    is actually wired up to it."""
    responses = [httpx.Response(429), httpx.Response(200, json=_body([_raw_listing()]))]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    results = _connector().search("Fender")

    assert len(results) == 1
    assert len(_no_real_sleeps) == 1


def test_500_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


def test_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_connect_error(*args, **kwargs):
        raise httpx.ConnectError("simulated connection error")

    monkeypatch.setattr(httpx, "get", raise_connect_error)
    with pytest.raises(MarketplaceConnectorError):
        _connector().search("Fender")


# --- safety: never logs the token ---------------------------------------


def test_error_message_never_contains_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    connector = _connector(api_token="super-secret-token-value")
    try:
        connector.search("Fender")
    except MarketplaceConnectorError as exc:
        assert "super-secret-token-value" not in str(exc)
    else:
        pytest.fail("expected MarketplaceConnectorError")


def test_log_output_never_contains_the_token(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=_body([_raw_listing()])))
    with caplog.at_level("DEBUG"):
        _connector(api_token="super-secret-token-value").search("Fender")
    assert "super-secret-token-value" not in caplog.text


# --- get_listing_by_id (historical backfill) --------------------------------


def _mock_get_item(monkeypatch: pytest.MonkeyPatch, item_response: httpx.Response) -> dict:
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return item_response

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


def test_get_listing_by_id_requests_the_correct_url_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _mock_get_item(monkeypatch, httpx.Response(200, json=_raw_listing()))

    _connector().get_listing_by_id("84838674")

    assert captured["url"] == "https://api.reverb.com/api/listings/84838674"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["Accept"] == "application/hal+json"
    assert captured["headers"]["Accept-Version"] == "3.0"


def test_get_listing_by_id_returns_a_fully_normalized_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(200, json=_raw_listing()))

    listing = _connector().get_listing_by_id("84838674")

    assert listing is not None
    assert listing.price == 1419.30
    assert listing.condition == "Very Good"
    assert listing.seller == "Pedal Haus"
    assert str(listing.image_url) == "https://img.reverb.com/full.jpg"


def test_get_listing_by_id_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(404))
    assert _connector().get_listing_by_id("00000000") is None


def test_get_listing_by_id_missing_token_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json=_raw_listing())

    monkeypatch.setattr(httpx, "get", fake_get)

    connector = ReverbMarketplaceConnector(api_token=None)
    with pytest.raises(MarketplaceConnectorError):
        connector.get_listing_by_id("84838674")

    assert called is False


def test_get_listing_by_id_401_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(401))
    # MarketplaceAuthError specifically - not just any MarketplaceConnectorError
    # - so the historical backfill service can circuit-break on it (see
    # core/persistence/backfill.py's module docstring).
    with pytest.raises(MarketplaceAuthError):
        _connector().get_listing_by_id("84838674")


def test_get_listing_by_id_403_raises_marketplace_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(403))
    with pytest.raises(MarketplaceAuthError):
        _connector().get_listing_by_id("84838674")


def test_get_listing_by_id_429_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]
) -> None:
    responses = [httpx.Response(429), httpx.Response(200, json=_raw_listing())]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: responses.pop(0))

    listing = _connector().get_listing_by_id("84838674")

    assert listing is not None
    assert len(_no_real_sleeps) == 1


def test_get_listing_by_id_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_listing()
    del raw["title"]
    _mock_get_item(monkeypatch, httpx.Response(200, json=raw))
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("84838674")


def test_get_listing_by_id_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_get_item(monkeypatch, httpx.Response(200, content=b"not valid json"))
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("84838674")


def test_get_listing_by_id_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _connector().get_listing_by_id("84838674")


def test_get_listing_by_id_never_logs_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _mock_get_item(monkeypatch, httpx.Response(200, json=_raw_listing()))
    with caplog.at_level("DEBUG"):
        _connector(api_token="super-secret-token-value").get_listing_by_id("84838674")
    assert "super-secret-token-value" not in caplog.text
