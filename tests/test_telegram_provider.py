import httpx
import pytest

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError
from marketplace_alert.notifications.telegram import provider as telegram_provider
from marketplace_alert.notifications.telegram.provider import (
    TelegramNotificationProvider,
    format_listing_message,
)


def _listing(**overrides: object) -> Listing:
    defaults: dict[str, object] = {
        "marketplace": "mock",
        "external_listing_id": "mock-001",
        "title": "Maccabi Vintage Shirt",
        "listing_url": "https://mock-marketplace.example.com/listing/mock-001",
    }
    defaults.update(overrides)
    return Listing(**defaults)


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep in tests - record requested durations instead.

    Autouse so every test in this file runs at full speed regardless of
    retry/backoff configuration; tests that care about *what* was waited
    for just inspect the returned list.
    """
    calls: list[float] = []
    monkeypatch.setattr(telegram_provider.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _provider(**overrides: object) -> TelegramNotificationProvider:
    kwargs: dict[str, object] = {
        "bot_token": "fake-token",
        "max_retries": 3,
        "retry_base_seconds": 2.0,
    }
    kwargs.update(overrides)
    return TelegramNotificationProvider(**kwargs)


_DESTINATION = "12345"


def test_message_includes_title_marketplace_price_location_and_url() -> None:
    listing = _listing(price=45.0, currency="USD", location="Tel Aviv, Israel")
    message = format_listing_message(listing)
    assert "Maccabi Vintage Shirt" in message
    assert "mock" in message
    assert "45.0" in message
    assert "USD" in message
    assert "Tel Aviv, Israel" in message
    assert "https://mock-marketplace.example.com/listing/mock-001" in message


def test_message_omits_price_and_location_when_absent() -> None:
    listing = _listing(price=None, currency=None, location=None)
    message = format_listing_message(listing)
    assert "Price" not in message
    assert "Location" not in message


def test_missing_bot_token_disables_provider_and_raises_on_send() -> None:
    provider = TelegramNotificationProvider(bot_token=None)
    assert provider.is_enabled is False
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)


def test_bot_token_present_enables_provider() -> None:
    """The provider holds no chat destination of its own (see this
    module's docstring/security rule) - `is_enabled` reflects only
    whether the bot token is configured."""
    assert TelegramNotificationProvider(bot_token="fake-token").is_enabled is True


def test_send_without_a_destination_raises_and_never_calls_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller must never invoke this with an empty destination - see
    `core/notifications/outbox.py`'s "SECURITY RULE"; this is a defensive
    backstop, never expected to actually fire in production."""
    calls = {"count": 0}
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: calls.__setitem__("count", calls["count"] + 1))

    provider = _provider()
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), "")

    assert calls["count"] == 0


def test_successful_send_posts_to_telegram_api(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    call_count = 0

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider()
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert captured["url"] == "https://api.telegram.org/botfake-token/sendMessage"
    assert captured["json"]["chat_id"] == "12345"
    assert "Maccabi Vintage Shirt" in captured["json"]["text"]
    assert call_count == 1  # a successful first attempt never retries


def test_http_error_status_raises_notification_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda url, json, timeout: httpx.Response(404, json={"ok": False})
    )
    provider = _provider()
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)


def test_telegram_level_error_raises_notification_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json, timeout: httpx.Response(
            200, json={"ok": False, "description": "chat not found"}
        ),
    )
    provider = _provider()
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)


def test_network_failure_raises_notification_error_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_connect_error(url: str, json: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "post", raise_connect_error)

    provider = _provider()
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)


# --- retry: transient failures ------------------------------------------


def test_timeout_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.TimeoutException("simulated timeout")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3, retry_base_seconds=1.0)
    provider.send_listing_alert(_listing(), _DESTINATION)  # must not raise

    assert attempts["count"] == 3
    assert sleep_calls == [1.0, 2.0]  # exponential: base, 2x base


def test_connection_failure_is_retried(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("simulated connection failure")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3, retry_base_seconds=1.0)
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 2
    assert sleep_calls == [1.0]


def test_http_500_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3, retry_base_seconds=1.5)
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 2
    assert sleep_calls == [1.5]


@pytest.mark.parametrize("status_code", [502, 503, 504])
def test_other_5xx_statuses_are_retried(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float], status_code: int
) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(status_code)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=2, retry_base_seconds=0.5)
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 2


def test_http_429_waits_for_telegrams_retry_after(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    # A large exponential-backoff base to prove retry_after (7s) wins over
    # the backoff formula (which would otherwise wait retry_base_seconds).
    provider = _provider(max_retries=3, retry_base_seconds=100.0)
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 2
    assert sleep_calls == [7.0]


def test_http_429_falls_back_to_backoff_without_retry_after(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3, retry_base_seconds=3.0)
    provider.send_listing_alert(_listing(), _DESTINATION)

    assert sleep_calls == [3.0]


# --- retry: bounded ---------------------------------------------------------


def test_retries_are_bounded_then_raises(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500)  # always fails

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3, retry_base_seconds=0.1)
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)

    # 1 initial attempt + 3 retries = 4 total attempts, never more.
    assert attempts["count"] == 4
    assert len(sleep_calls) == 3


def test_zero_max_retries_means_a_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=0)
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 1


# --- no retry: permanent failures -------------------------------------------


def test_permanent_400_does_not_retry(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3)
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 1
    assert sleep_calls == []


def test_permanent_401_invalid_bot_token_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3)
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 1


def test_permanent_ok_false_response_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 with "ok": false (e.g. bot was blocked by the user) has no
    documented retry semantics from Telegram - treated as permanent."""
    attempts = {"count": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(200, json={"ok": False, "description": "bot was blocked by the user"})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = _provider(max_retries=3)
    with pytest.raises(NotificationError):
        provider.send_listing_alert(_listing(), _DESTINATION)

    assert attempts["count"] == 1


# --- secrecy across retries -------------------------------------------------


def test_credentials_never_appear_in_a_raised_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: httpx.Response(500))

    provider = _provider(bot_token="super-secret-bot-token", max_retries=1, retry_base_seconds=0.1)
    with pytest.raises(NotificationError) as exc_info:
        provider.send_listing_alert(_listing(), "super-secret-chat-id")

    message = str(exc_info.value)
    assert "super-secret-bot-token" not in message
    assert "super-secret-chat-id" not in message


def test_credentials_never_logged_during_retries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: httpx.Response(500))

    provider = _provider(bot_token="super-secret-bot-token", max_retries=2, retry_base_seconds=0.1)
    with caplog.at_level("DEBUG"):
        with pytest.raises(NotificationError):
            provider.send_listing_alert(_listing(), "super-secret-chat-id")

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-bot-token" not in log_text
    assert "super-secret-chat-id" not in log_text
