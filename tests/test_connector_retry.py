"""Tests for `core/connectors/retry.py`'s shared `request_with_retry` -
the bounded-retry policy every real connector (Etsy, eBay, Reverb,
Bonanza) wraps its search request in. Connector-specific tests only need
to confirm they're actually wired up to this (see each connector's own
test file); the exhaustive retry-policy behavior lives here, once.
"""

import httpx
import pytest

from marketplace_alert.core.connectors import retry as retry_module
from marketplace_alert.core.connectors.retry import RETRIABLE_STATUS_CODES, request_with_retry


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep in tests - record requested durations instead."""
    calls: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _responses(*status_codes: int) -> list[httpx.Response]:
    return [httpx.Response(code) for code in status_codes]


def test_no_retry_needed_on_immediate_success() -> None:
    calls = _responses(200)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test")
    assert response.status_code == 200
    assert calls == []


@pytest.mark.parametrize("status_code", sorted(RETRIABLE_STATUS_CODES))
def test_retries_a_retriable_status_then_succeeds(status_code: int, sleep_calls: list[float]) -> None:
    calls = _responses(status_code, 200)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test")
    assert response.status_code == 200
    assert calls == []
    assert len(sleep_calls) == 1


def test_gives_up_after_max_retries_and_returns_the_last_response(sleep_calls: list[float]) -> None:
    calls = _responses(429, 429, 429)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test", max_retries=2)
    assert response.status_code == 429
    assert calls == []  # exactly 3 attempts made (1 + 2 retries), never a 4th
    assert len(sleep_calls) == 2  # waited between attempts 1->2 and 2->3, not after the last


@pytest.mark.parametrize("status_code", [401, 403, 404, 400, 500])
def test_does_not_retry_a_non_retriable_status(status_code: int, sleep_calls: list[float]) -> None:
    """401/403/404/400 are permanent failures. 500 is deliberately NOT in
    the retriable set either - this connector-retry policy matches this
    task's own explicit list (429/502/503/504 only), distinct from
    Telegram's own (separate) retry policy, which does include 500."""
    calls = _responses(status_code, 200)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test")
    assert response.status_code == status_code
    assert len(calls) == 1  # the second, queued response was never consumed
    assert sleep_calls == []


def test_zero_max_retries_means_exactly_one_attempt(sleep_calls: list[float]) -> None:
    calls = _responses(429)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test", max_retries=0)
    assert response.status_code == 429
    assert sleep_calls == []


def test_negative_max_retries_is_clamped_to_a_single_attempt(sleep_calls: list[float]) -> None:
    calls = _responses(429)
    response = request_with_retry(lambda: calls.pop(0), marketplace_name="Test", max_retries=-5)
    assert response.status_code == 429
    assert sleep_calls == []


def test_backoff_is_exponential_without_a_retry_after_header(sleep_calls: list[float]) -> None:
    calls = _responses(429, 429, 429, 200)
    request_with_retry(lambda: calls.pop(0), marketplace_name="Test", max_retries=3, retry_base_seconds=1.0)
    assert sleep_calls == [1.0, 2.0, 4.0]


def test_retry_after_header_is_honored_over_backoff(sleep_calls: list[float]) -> None:
    responses = [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200)]
    request_with_retry(lambda: responses.pop(0), marketplace_name="Test", retry_base_seconds=1.0)
    assert sleep_calls == [5.0]


def test_malformed_retry_after_header_falls_back_to_backoff(sleep_calls: list[float]) -> None:
    responses = [httpx.Response(429, headers={"Retry-After": "not-a-number"}), httpx.Response(200)]
    request_with_retry(lambda: responses.pop(0), marketplace_name="Test", retry_base_seconds=1.0)
    assert sleep_calls == [1.0]


def test_a_network_error_from_make_request_propagates_immediately_unretried(sleep_calls: list[float]) -> None:
    """Connection/timeout errors are the caller's responsibility (each
    connector already wraps its own `httpx.HTTPError` handling around
    this call) - this helper only retries HTTP status codes, never
    catches or retries an exception itself."""

    def raise_timeout():
        raise httpx.TimeoutException("simulated timeout")

    with pytest.raises(httpx.TimeoutException):
        request_with_retry(raise_timeout, marketplace_name="Test")
    assert sleep_calls == []
