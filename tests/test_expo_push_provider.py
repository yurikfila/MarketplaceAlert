"""Tests for `marketplace_alert/notifications/push/provider.py`:
`ExpoPushProvider`.

Pure unit tests - no database, no real network. Follows the exact
`monkeypatch.setattr(httpx, "post", fake_post)` convention already
established in `tests/test_telegram_provider.py`/
`test_password_reset_email_sender.py` for every other provider in this
codebase - no new mocking library, no real HTTP.
"""

import httpx
import pytest

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError
from marketplace_alert.notifications.push import provider as push_provider
from marketplace_alert.notifications.push.provider import ExpoPushProvider


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep in tests - record requested durations instead.
    Autouse so every test in this file runs at full speed regardless of
    retry/backoff configuration - same reasoning as the sibling provider
    test files' identical fixture."""
    calls: list[float] = []
    monkeypatch.setattr(push_provider.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _provider(**overrides: object) -> ExpoPushProvider:
    kwargs: dict[str, object] = {"enabled": True, "max_retries": 1, "retry_base_seconds": 1.0}
    kwargs.update(overrides)
    return ExpoPushProvider(**kwargs)


def _listing(**overrides: object) -> Listing:
    fields: dict[str, object] = {
        "marketplace": "mock",
        "external_listing_id": "abc123",
        "title": "Vintage Rolex Submariner",
        "listing_url": "https://example.com/listings/abc123",
    }
    fields.update(overrides)
    return Listing(**fields)


def _ok_response() -> httpx.Response:
    """The list-wrapped shape Expo documents for a batch send."""
    return httpx.Response(200, json={"data": [{"status": "ok", "id": "ticket-1"}]})


def _ok_response_single_object() -> httpx.Response:
    """The shape Expo actually returns for a single-recipient send - this
    provider's only real usage (`send_listing_alert` always sends to
    exactly one `to`) - a plain ticket object, not list-wrapped. See
    `ExpoPushProvider._ticket_error`'s own docstring for the real-device
    production bug this shape's absence from these tests once caused."""
    return httpx.Response(200, json={"data": {"status": "ok", "id": "ticket-1"}})


def _error_ticket_response(error_code: str = "DeviceNotRegistered") -> httpx.Response:
    return httpx.Response(
        200, json={"data": [{"status": "error", "message": "some free text", "details": {"error": error_code}}]}
    )


def _error_ticket_response_single_object(error_code: str = "DeviceNotRegistered") -> httpx.Response:
    """The single-recipient counterpart of `_error_ticket_response` above."""
    return httpx.Response(
        200, json={"data": {"status": "error", "message": "some free text", "details": {"error": error_code}}}
    )


# =====================================================================
# is_enabled
# =====================================================================


def test_is_enabled_reflects_the_plain_config_switch_not_any_credential() -> None:
    """Unlike Telegram/Resend, Expo push needs no credential at all to
    send - see the provider's own docstring."""
    assert _provider(enabled=True, access_token=None).is_enabled is True
    assert _provider(enabled=False, access_token="some-token").is_enabled is False


def test_send_raises_immediately_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not make a network call when disabled")

    monkeypatch.setattr(httpx, "post", _fail_if_called)

    with pytest.raises(NotificationError):
        _provider(enabled=False).send_listing_alert(_listing(), "ExponentPushToken[abc]")


def test_send_raises_immediately_for_an_empty_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not make a network call with no destination")

    monkeypatch.setattr(httpx, "post", _fail_if_called)

    with pytest.raises(NotificationError):
        _provider().send_listing_alert(_listing(), "")


# =====================================================================
# Request shape
# =====================================================================


def test_send_uses_the_correct_expo_url_method_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _ok_response()

    monkeypatch.setattr(httpx, "post", fake_post)

    _provider().send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert captured["url"] == "https://exp.host/--/api/v2/push/send"
    assert captured["headers"]["host"] == "exp.host"
    assert captured["headers"]["accept"] == "application/json"
    assert captured["headers"]["accept-encoding"] == "gzip, deflate"
    assert captured["headers"]["content-type"] == "application/json"
    assert "Authorization" not in captured["headers"]


def test_send_includes_an_authorization_header_only_when_an_access_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["headers"] = headers
        return _ok_response()

    monkeypatch.setattr(httpx, "post", fake_post)

    _provider(access_token="expo-access-token-value").send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert captured["headers"]["Authorization"] == "Bearer expo-access-token-value"


def test_send_payload_carries_the_destination_title_body_and_listing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured.update(json)
        return _ok_response()

    monkeypatch.setattr(httpx, "post", fake_post)

    listing = _listing(title="A Very Specific Title", marketplace="ebay", external_listing_id="xyz789")
    _provider().send_listing_alert(listing, "ExponentPushToken[abc]")

    assert captured["to"] == "ExponentPushToken[abc]"
    assert captured["body"] == "A Very Specific Title"
    assert captured["data"]["listing_url"] == "https://example.com/listings/abc123"
    assert captured["data"]["marketplace"] == "ebay"
    assert captured["data"]["external_listing_id"] == "xyz789"


# =====================================================================
# Ticket-level success/failure
# =====================================================================


def test_send_succeeds_on_a_2xx_response_with_an_ok_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list-wrapped shape (a batch send)."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok_response())
    _provider().send_listing_alert(_listing(), "ExponentPushToken[abc]")  # must not raise


def test_send_succeeds_on_a_2xx_response_with_a_single_object_ok_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape Expo actually returns in practice, since this provider
    only ever sends to one recipient at a time - see
    `_ok_response_single_object`'s own docstring."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok_response_single_object())
    _provider().send_listing_alert(_listing(), "ExponentPushToken[abc]")  # must not raise


def test_send_raises_on_a_2xx_response_with_a_single_object_error_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _error_ticket_response_single_object("DeviceNotRegistered")
    )

    with pytest.raises(NotificationError, match="DeviceNotRegistered"):
        _provider().send_listing_alert(_listing(), "ExponentPushToken[abc]")


def test_send_raises_on_a_2xx_response_with_an_error_ticket_never_retrying(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    calls = {"count": 0}

    def fake_post(url, json, headers, timeout):
        calls["count"] += 1
        return _error_ticket_response("DeviceNotRegistered")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(NotificationError, match="DeviceNotRegistered"):
        _provider(max_retries=3).send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert calls["count"] == 1  # permanent - never retried
    assert sleep_calls == []


def _raise_unexpectedly(self: ExpoPushProvider, response: httpx.Response) -> str | None:
    """Stands in for `ExpoPushProvider._ticket_error` to simulate an
    unforeseen bug/edge case in ticket interpretation - deliberately not
    tied to any one specific malformed-response shape, since the
    guarantee under test is general: *whatever* goes wrong interpreting
    an already-2xx response must never cause a retry."""
    raise RuntimeError("unexpected parsing failure - simulated for this test only")


def test_send_returns_normally_when_ticket_interpretation_raises_unexpectedly_after_a_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By the time `_ticket_error` runs, Expo has already returned a
    genuine 2xx - it already accepted the message. An unexpected failure
    interpreting the ticket body must be treated as accepted, not as a
    reason to retry (which could duplicate an already-delivered push) -
    see `send_listing_alert`'s own docstring and the try/except around
    `_ticket_error` inside it."""
    calls = {"count": 0}

    def fake_post(url, json, headers, timeout):
        calls["count"] += 1
        return _ok_response_single_object()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(ExpoPushProvider, "_ticket_error", _raise_unexpectedly)

    _provider(max_retries=3).send_listing_alert(_listing(), "ExponentPushToken[abc]")  # must not raise

    assert calls["count"] == 1  # never retried - exactly one send attempt


def test_send_logs_nothing_sensitive_when_ticket_interpretation_raises_unexpectedly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok_response_single_object())
    monkeypatch.setattr(ExpoPushProvider, "_ticket_error", _raise_unexpectedly)

    with caplog.at_level("DEBUG"):
        _provider(access_token="super-secret-access-token", max_retries=0).send_listing_alert(
            _listing(), "ExponentPushToken[do-not-log-me]"
        )

    assert "super-secret-access-token" not in caplog.text
    assert "ExponentPushToken[do-not-log-me]" not in caplog.text
    assert "unexpected parsing failure - simulated for this test only" not in caplog.text


def test_send_never_includes_the_free_text_ticket_message_in_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "data": [
                    {"status": "error", "message": "super secret internal detail", "details": {"error": "SomeError"}}
                ]
            },
        ),
    )

    with pytest.raises(NotificationError) as exc_info:
        _provider().send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert "super secret internal detail" not in str(exc_info.value)


# =====================================================================
# Retry behavior
# =====================================================================


def test_send_retries_a_429_and_succeeds_on_the_next_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [httpx.Response(429), _ok_response()]

    def fake_post(url, json, headers, timeout):
        return responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)

    _provider(max_retries=1).send_listing_alert(_listing(), "ExponentPushToken[abc]")  # must not raise
    assert responses == []


def test_send_retries_a_5xx_and_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(url, json, headers, timeout):
        calls["count"] += 1
        return httpx.Response(503)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(NotificationError):
        _provider(max_retries=2).send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert calls["count"] == 3  # first attempt + 2 retries


def test_send_does_not_retry_a_permanent_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_post(url, json, headers, timeout):
        calls["count"] += 1
        return httpx.Response(400)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(NotificationError):
        _provider(max_retries=3).send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert calls["count"] == 1


def test_send_retries_a_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectTimeout("boom")
        return _ok_response()

    monkeypatch.setattr(httpx, "post", fake_post)

    _provider(max_retries=1).send_listing_alert(_listing(), "ExponentPushToken[abc]")  # must not raise
    assert attempts["count"] == 2


def test_retry_delay_is_capped_even_with_a_large_retry_base(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    """Same fix already proven necessary for `PasswordResetEmailSender` -
    this call runs inside a one-shot drain pass that may be delivering
    many other users' notifications; an unbounded wait here would stall
    all of them."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503))

    with pytest.raises(NotificationError):
        _provider(max_retries=1, retry_base_seconds=1000.0).send_listing_alert(_listing(), "ExponentPushToken[abc]")

    assert all(delay <= push_provider._MAX_RETRY_DELAY_SECONDS for delay in sleep_calls)


# =====================================================================
# Security - never logs/raises a secret or the raw payload/response
# =====================================================================


def test_never_logs_the_access_token_or_push_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503))

    with caplog.at_level("DEBUG"):
        with pytest.raises(NotificationError):
            _provider(access_token="super-secret-access-token", max_retries=0).send_listing_alert(
                _listing(), "ExponentPushToken[do-not-log-me]"
            )

    assert "super-secret-access-token" not in caplog.text
    assert "ExponentPushToken[do-not-log-me]" not in caplog.text
