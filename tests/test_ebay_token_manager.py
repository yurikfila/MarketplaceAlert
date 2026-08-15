import base64
import time

import httpx
import pytest

from marketplace_alert.connectors.ebay.token_manager import EbayTokenManager
from marketplace_alert.core.connectors.base import MarketplaceConnectorError


def _manager(**overrides) -> EbayTokenManager:
    kwargs = {"app_id": "test-app-id", "cert_id": "test-cert-id"}
    kwargs.update(overrides)
    return EbayTokenManager(**kwargs)


# --- configuration -----------------------------------------------------


def test_missing_app_id_is_not_configured() -> None:
    manager = _manager(app_id=None)
    assert manager.is_configured is False


def test_missing_cert_id_is_not_configured() -> None:
    manager = _manager(cert_id=None)
    assert manager.is_configured is False


def test_both_credentials_present_is_configured() -> None:
    assert _manager().is_configured is True


def test_get_token_raises_before_any_network_call_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={"access_token": "x", "expires_in": 7200})

    monkeypatch.setattr(httpx, "post", fake_post)

    manager = _manager(app_id=None, cert_id=None)
    with pytest.raises(MarketplaceConnectorError):
        manager.get_token()

    assert called is False


# --- request construction ----------------------------------------------


def test_token_request_uses_correct_url_headers_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, headers, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return httpx.Response(200, json={"access_token": "abc123", "expires_in": 7200})

    monkeypatch.setattr(httpx, "post", fake_post)

    token = _manager().get_token()

    assert token == "abc123"
    assert captured["url"] == "https://api.ebay.com/identity/v1/oauth2/token"
    expected_credentials = base64.b64encode(b"test-app-id:test-cert-id").decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected_credentials}"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["scope"] == "https://api.ebay.com/oauth/api_scope"


# --- caching and refresh -------------------------------------------------


def test_token_is_cached_and_not_refetched_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": "cached-token", "expires_in": 7200})

    monkeypatch.setattr(httpx, "post", fake_post)

    manager = _manager()
    first = manager.get_token()
    second = manager.get_token()

    assert first == "cached-token"
    assert second == "cached-token"
    assert call_count == 1


def test_token_is_refreshed_once_near_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": f"token-{call_count}", "expires_in": 100})

    monkeypatch.setattr(httpx, "post", fake_post)

    manager = _manager()
    first = manager.get_token()
    assert first == "token-1"
    assert call_count == 1

    # 100s expiry - 60s safety margin = the cached token is already treated
    # as stale after ~40 monotonic seconds pass.
    real_monotonic = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic + 41)
    second = manager.get_token()

    assert second == "token-2"
    assert call_count == 2


def test_invalidate_forces_a_fresh_token_on_next_call(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": f"token-{call_count}", "expires_in": 7200})

    monkeypatch.setattr(httpx, "post", fake_post)

    manager = _manager()
    assert manager.get_token() == "token-1"
    manager.invalidate()
    assert manager.get_token() == "token-2"
    assert call_count == 2


# --- failure handling ----------------------------------------------------


def test_oauth_request_transport_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "post", raise_timeout)
    with pytest.raises(MarketplaceConnectorError):
        _manager().get_token()


def test_oauth_non_200_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    with pytest.raises(MarketplaceConnectorError):
        _manager().get_token()


def test_oauth_non_json_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, content=b"not json"))
    with pytest.raises(MarketplaceConnectorError):
        _manager().get_token()


def test_oauth_response_missing_access_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"expires_in": 7200}))
    with pytest.raises(MarketplaceConnectorError):
        _manager().get_token()


def test_oauth_response_missing_expires_in_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"access_token": "abc"}))
    with pytest.raises(MarketplaceConnectorError):
        _manager().get_token()


# --- secrecy --------------------------------------------------------------


def test_token_error_messages_never_contain_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))
    manager = _manager(app_id="super-secret-app-id", cert_id="super-secret-cert-id")
    with pytest.raises(MarketplaceConnectorError) as exc_info:
        manager.get_token()

    message = str(exc_info.value)
    assert "super-secret-app-id" not in message
    assert "super-secret-cert-id" not in message
