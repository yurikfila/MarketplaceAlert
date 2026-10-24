"""MA-SCALE-02: HTTP request metrics and shared eBay OAuth tokens.

- Every HTTP attempt (retries and transport failures included) is counted per marketplace,
  eBay OAuth requests under their own label, and HTTP 429 responses separately - counts only,
  never a URL, query, token or credential.
- One eBay token manager is shared per exact credentials, so a token is fetched once and reused
  until it nears expiry; different credentials never share a token; refresh is thread-safe.

No test touches the network; time is simulated.
"""

import threading

import httpx
import pytest

from marketplace_alert.config import settings
from marketplace_alert.connectors.ebay import token_manager as ebay_tokens
from marketplace_alert.connectors.ebay.token_manager import (
    EbayTokenManager,
    get_shared_token_manager,
    reset_shared_token_managers,
)
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.connectors.metrics import collect_request_metrics, record_request
from marketplace_alert.core.connectors.retry import request_with_retry


class _Resp:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


def test_every_http_attempt_including_retries_is_counted_with_429s(monkeypatch) -> None:
    monkeypatch.setattr("marketplace_alert.core.connectors.retry.time.sleep", lambda s: None)
    responses = iter([_Resp(429), _Resp(429), _Resp(200)])

    with collect_request_metrics() as metrics:
        request_with_retry(lambda: next(responses), marketplace_name="Etsy")
        request_with_retry(lambda: _Resp(200), marketplace_name="Tradera")

    assert metrics.attempts == {"etsy": 3, "tradera": 1}
    assert metrics.http_429 == {"etsy": 2}
    assert metrics.total_attempts == 4 and metrics.total_429 == 2
    assert "etsy=3" in metrics.summary() and "http_429=2" in metrics.summary()


def test_a_transport_failure_counts_as_an_attempt_and_still_raises() -> None:
    def boom():
        raise httpx.ConnectTimeout("t")

    with collect_request_metrics() as metrics:
        with pytest.raises(httpx.ConnectTimeout):
            request_with_retry(boom, marketplace_name="Reverb")

    assert metrics.attempts == {"reverb": 1} and metrics.network_errors == {"reverb": 1}


def test_recording_outside_a_collector_is_a_harmless_noop_and_collectors_do_not_leak() -> None:
    record_request("ebay", 200)  # no active collector
    with collect_request_metrics() as first:
        record_request("ebay", 200)
    with collect_request_metrics() as second:
        pass
    assert first.total_attempts == 1 and second.total_attempts == 0


def test_metrics_summary_contains_no_urls_or_secrets() -> None:
    with collect_request_metrics() as metrics:
        record_request("ebay", 200)
        record_request("ebay_oauth", 200)
    text = metrics.summary()
    assert "http" not in text.replace("http_429", "") and "ebay_oauth=1" in text


# ---------------------------------------------------------------- eBay token sharing


class _Clockwork:
    """Stand-in for time.monotonic in the token manager module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def ebay_http(monkeypatch):
    clock = _Clockwork()
    monkeypatch.setattr(ebay_tokens.time, "monotonic", clock)
    posts: list[dict] = []

    def fake_post(url, headers=None, data=None, timeout=None):
        posts.append({"auth": headers["Authorization"]})
        return httpx.Response(200, json={"access_token": f"tok-{len(posts)}", "expires_in": 7200})

    monkeypatch.setattr(ebay_tokens.httpx, "post", fake_post)
    return clock, posts


def test_one_oauth_request_serves_many_connectors_and_searches(ebay_http, monkeypatch) -> None:
    from marketplace_alert.connectors import registry

    clock, posts = ebay_http
    monkeypatch.setattr(settings, "ebay_app_id", "app-1")
    monkeypatch.setattr(settings, "ebay_cert_id", "cert-1")

    connectors = [registry.get_connector("ebay") for _ in range(5)]  # exactly how the scanner builds them
    tokens = {c._token_manager.get_token() for c in connectors}

    assert len(posts) == 1 and len(tokens) == 1
    assert len({id(c._token_manager) for c in connectors}) == 1


def test_a_token_is_refreshed_only_after_it_nears_expiry(ebay_http) -> None:
    clock, posts = ebay_http
    manager = get_shared_token_manager("app", "cert")

    first = manager.get_token()
    clock.now += 7200 - 61  # still outside the 60 s safety margin
    assert manager.get_token() == first and len(posts) == 1
    clock.now += 2  # now inside the margin
    assert manager.get_token() != first and len(posts) == 2


def test_different_credentials_never_share_a_manager_or_a_token(ebay_http) -> None:
    clock, posts = ebay_http
    one = get_shared_token_manager("app-A", "cert-A")
    two = get_shared_token_manager("app-B", "cert-B")
    swapped = get_shared_token_manager("app-A", "cert-B")

    assert one is not two and one is not swapped and two is not swapped
    assert get_shared_token_manager("app-A", "cert-A") is one  # same credentials: same manager
    tokens = {one.get_token(), two.get_token(), swapped.get_token()}
    assert len(tokens) == 3 and len(posts) == 3


def test_unconfigured_credentials_are_never_shared_and_make_no_request(ebay_http) -> None:
    clock, posts = ebay_http
    a, b = get_shared_token_manager(None, None), get_shared_token_manager("app", None)
    assert a is not b
    with pytest.raises(MarketplaceConnectorError):
        a.get_token()
    assert posts == []


def test_invalidate_forces_a_new_token_for_everyone_sharing_the_manager(ebay_http) -> None:
    clock, posts = ebay_http
    manager = get_shared_token_manager("app", "cert")
    first = manager.get_token()
    manager.invalidate()
    assert get_shared_token_manager("app", "cert").get_token() != first and len(posts) == 2


def test_concurrent_callers_trigger_a_single_refresh(monkeypatch) -> None:
    calls = []
    gate = threading.Event()

    def slow_post(url, headers=None, data=None, timeout=None):
        calls.append(1)
        gate.wait(timeout=2)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

    monkeypatch.setattr(ebay_tokens.httpx, "post", slow_post)
    manager = get_shared_token_manager("app", "cert")
    results = []
    threads = [threading.Thread(target=lambda: results.append(manager.get_token())) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(timeout=5)

    assert len(calls) == 1 and results == ["tok"] * 8


def test_oauth_requests_are_counted_separately_from_searches(ebay_http) -> None:
    with collect_request_metrics() as metrics:
        get_shared_token_manager("app", "cert").get_token()
    assert metrics.attempts == {"ebay_oauth": 1}


def test_oauth_failure_is_counted_and_not_cached(monkeypatch) -> None:
    monkeypatch.setattr(ebay_tokens.httpx, "post", lambda *a, **k: httpx.Response(429, json={}))
    manager = get_shared_token_manager("app", "cert")
    with collect_request_metrics() as metrics:
        for _ in range(2):
            with pytest.raises(MarketplaceConnectorError):
                manager.get_token()
    assert metrics.attempts == {"ebay_oauth": 2} and metrics.http_429 == {"ebay_oauth": 2}


def test_shared_managers_can_be_reset() -> None:
    manager = get_shared_token_manager("app", "cert")
    reset_shared_token_managers()
    assert get_shared_token_manager("app", "cert") is not manager
    assert isinstance(manager, EbayTokenManager)
