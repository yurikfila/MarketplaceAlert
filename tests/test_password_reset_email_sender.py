"""Tests for `marketplace_alert/notifications/email/provider.py`:
`PasswordResetEmailSender`.

Pure unit tests - no database, no real network. Follows the exact
`monkeypatch.setattr(httpx, "post", fake_post)` convention already
established in `tests/test_telegram_provider.py` for the sibling Telegram
provider - no new mocking library, no real HTTP.

Not wired into any route yet (Phase 4B) - these tests exercise the
provider directly, never through `/forgot-password`.
"""

import httpx
import pytest

from marketplace_alert.notifications.email import provider as email_provider
from marketplace_alert.notifications.email.provider import (
    PasswordResetEmailError,
    PasswordResetEmailSender,
)


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Never actually sleep in tests - record requested durations instead.

    Autouse so every test in this file runs at full speed regardless of
    retry/backoff configuration - same reasoning as
    `test_telegram_provider.py`'s identical fixture.
    """
    calls: list[float] = []
    monkeypatch.setattr(email_provider.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _sender(**overrides: object) -> PasswordResetEmailSender:
    kwargs: dict[str, object] = {
        "api_key": "fake-resend-key",
        "from_address": "MarketplaceAlert <noreply@example.com>",
        "max_retries": 1,
        "retry_base_seconds": 1.0,
    }
    kwargs.update(overrides)
    return PasswordResetEmailSender(**kwargs)


def _fake_post_returning(response: httpx.Response):
    return lambda url, json, headers, timeout: response


# =====================================================================
# is_enabled
# =====================================================================


def test_is_enabled_false_with_missing_api_key() -> None:
    sender = PasswordResetEmailSender(api_key=None, from_address="noreply@example.com")
    assert sender.is_enabled is False


def test_is_enabled_false_with_missing_from_address() -> None:
    sender = PasswordResetEmailSender(api_key="fake-key", from_address=None)
    assert sender.is_enabled is False


def test_is_enabled_false_with_both_missing() -> None:
    sender = PasswordResetEmailSender(api_key=None, from_address=None)
    assert sender.is_enabled is False


def test_is_enabled_true_only_when_both_required_settings_exist() -> None:
    sender = PasswordResetEmailSender(api_key="fake-key", from_address="noreply@example.com")
    assert sender.is_enabled is True


def test_is_enabled_ignores_reply_to() -> None:
    """reply_to is optional and must never affect is_enabled."""
    with_reply_to = PasswordResetEmailSender(
        api_key="fake-key", from_address="noreply@example.com", reply_to="support@example.com"
    )
    without_reply_to = PasswordResetEmailSender(api_key="fake-key", from_address="noreply@example.com", reply_to=None)
    assert with_reply_to.is_enabled is True
    assert without_reply_to.is_enabled is True


def test_send_raises_immediately_when_not_configured_and_never_calls_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.__setitem__("count", calls["count"] + 1))

    sender = PasswordResetEmailSender(api_key=None, from_address=None)
    with pytest.raises(PasswordResetEmailError):
        sender.send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert calls["count"] == 0


# =====================================================================
# Request shape
# =====================================================================


def test_send_uses_the_correct_resend_url_and_post_method(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender().send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert captured["url"] == "https://api.resend.com/emails"


def test_authorization_header_is_correctly_constructed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(api_key="my-fake-key-for-this-test").send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert captured["headers"]["Authorization"] == "Bearer my-fake-key-for-this-test"


def test_user_agent_header_is_present_and_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["headers"] = headers
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender().send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert captured["headers"].get("User-Agent")


def test_from_to_subject_text_are_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(from_address="MarketplaceAlert <noreply@example.com>").send_password_reset_code(
        email="recipient@example.com", code="483921", expires_minutes=10
    )

    body = captured["json"]
    assert body["from"] == "MarketplaceAlert <noreply@example.com>"
    assert body["to"] == ["recipient@example.com"]
    assert body["subject"] == "MarketplaceAlert password reset code"
    assert "483921" in body["text"]
    assert "10 minutes" in body["text"]


def test_reply_to_omitted_from_payload_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(reply_to=None).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert "reply_to" not in captured["json"]


def test_reply_to_included_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(reply_to="support@example.com").send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert captured["json"]["reply_to"] == "support@example.com"


def test_timeout_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["timeout"] = timeout
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(timeout=3.5).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert captured["timeout"] == 3.5


# =====================================================================
# Success
# =====================================================================


def test_successful_200_returns_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(200, json={"id": "abc"})))

    _sender().send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)  # must not raise


def test_successful_202_also_returns_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything < 300 is treated as success, not just a literal 200."""
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(202, json={"id": "abc"})))

    _sender().send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)


# =====================================================================
# Permanent failures - never retried
# =====================================================================


def test_400_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(400, json={"message": "bad request"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=3).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert attempts["count"] == 1


def test_401_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(401, json={"message": "invalid api key"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=3).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert attempts["count"] == 1


def test_403_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(403, json={"message": "forbidden"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=3).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert attempts["count"] == 1


def test_zero_max_retries_means_a_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=0).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert attempts["count"] == 1


# =====================================================================
# 429 - retried, Retry-After honored
# =====================================================================


def test_429_retries_only_within_configured_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(429, json={"message": "rate limited"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=2, retry_base_seconds=0.1).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert attempts["count"] == 3  # 1 initial + 2 retries, never more


def test_429_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert attempts["count"] == 2


def test_retry_after_of_two_seconds_sleeps_exactly_two_seconds(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    # A large exponential-backoff base to prove Retry-After (2s) wins over the backoff formula.
    _sender(max_retries=1, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [2.0]


def test_retry_after_honored_when_valid(monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [3.0]


def test_retry_after_of_120_seconds_is_capped_at_the_maximum(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    """The exact regression this review exists for: a large, well-formed
    `Retry-After` must never be honored verbatim, since this call blocks
    a synchronous, user-facing `/forgot-password` request."""
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=0.1).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [email_provider._MAX_RETRY_DELAY_SECONDS]
    assert sleep_calls == [5.0]


def test_malformed_retry_after_falls_back_to_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=3.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [3.0]


def test_malformed_retry_after_fallback_backoff_is_still_capped(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    # A backoff base large enough that, uncapped, it would sleep far
    # longer than _MAX_RETRY_DELAY_SECONDS.
    _sender(max_retries=1, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [email_provider._MAX_RETRY_DELAY_SECONDS]


def test_missing_retry_after_falls_back_to_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=2.5).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [2.5]


def test_negative_retry_after_falls_back_to_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "-5"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=1.5).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [1.5]


def test_negative_retry_after_fallback_backoff_is_still_capped(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "-5"}, json={"message": "rate limited"})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert sleep_calls == [email_provider._MAX_RETRY_DELAY_SECONDS]


def test_exponential_backoff_itself_cannot_exceed_the_cap(
    monkeypatch: pytest.MonkeyPatch, sleep_calls: list[float]
) -> None:
    """No `Retry-After` involved at all here (plain 5xx retries) - proves
    the cap applies to the exponential-backoff formula on its own merits,
    not only as a side effect of the 429/`Retry-After` path."""
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] < 4:
            return httpx.Response(500)
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    # base=100 -> uncapped this would be 100, 200, 400 - all must clamp to the cap.
    _sender(max_retries=3, retry_base_seconds=100.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert len(sleep_calls) == 3
    assert all(delay == email_provider._MAX_RETRY_DELAY_SECONDS for delay in sleep_calls)
    assert all(0 <= delay <= email_provider._MAX_RETRY_DELAY_SECONDS for delay in sleep_calls)


# =====================================================================
# 5xx - retried within bound
# =====================================================================


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_5xx_retries_only_within_configured_bound(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        return httpx.Response(status_code)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=1, retry_base_seconds=0.1).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert attempts["count"] == 2  # 1 initial + 1 retry, never more


def test_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=1.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert attempts["count"] == 2


# =====================================================================
# timeout / network failures - retried within bound
# =====================================================================


def test_timeout_retries_only_within_configured_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=1, retry_base_seconds=0.1).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert attempts["count"] == 2


def test_network_failure_retries_only_within_configured_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=1, retry_base_seconds=0.1).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert attempts["count"] == 2


def test_timeout_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def fake_post(url, json, headers, timeout):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.TimeoutException("simulated timeout")
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(max_retries=1, retry_base_seconds=1.0).send_password_reset_code(
        email="user@example.com", code="483921", expires_minutes=10
    )

    assert attempts["count"] == 2


# =====================================================================
# Idempotency-Key
# =====================================================================


def test_same_idempotency_key_reused_across_retries_of_one_send(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_keys = []

    def fake_post(url, json, headers, timeout):
        captured_keys.append(headers["Idempotency-Key"])
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(PasswordResetEmailError):
        _sender(max_retries=2, retry_base_seconds=0.1).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert len(captured_keys) == 3
    assert len(set(captured_keys)) == 1  # every retry of THIS send reused the same key


def test_separate_send_invocations_receive_different_idempotency_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_keys = []

    def fake_post(url, json, headers, timeout):
        captured_keys.append(headers["Idempotency-Key"])
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    sender = _sender()
    sender.send_password_reset_code(email="user@example.com", code="111111", expires_minutes=10)
    sender.send_password_reset_code(email="user@example.com", code="222222", expires_minutes=10)

    assert len(captured_keys) == 2
    assert captured_keys[0] != captured_keys[1]


def test_idempotency_key_does_not_contain_email_code_or_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["key"] = headers["Idempotency-Key"]
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender(api_key="super-secret-api-key").send_password_reset_code(
        email="recipient@example.com", code="483921", expires_minutes=10
    )

    key = captured["key"]
    assert "recipient@example.com" not in key
    assert "483921" not in key
    assert "super-secret-api-key" not in key


# =====================================================================
# Secrecy - API key, Authorization header, raw code, recipient email
# =====================================================================


def test_api_key_never_appears_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(500)))

    with pytest.raises(PasswordResetEmailError) as exc_info:
        _sender(api_key="super-secret-resend-key", max_retries=0).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert "super-secret-resend-key" not in str(exc_info.value)


def test_authorization_header_never_appears_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(500)))

    with pytest.raises(PasswordResetEmailError) as exc_info:
        _sender(api_key="super-secret-resend-key", max_retries=0).send_password_reset_code(
            email="user@example.com", code="483921", expires_minutes=10
        )

    assert "Bearer" not in str(exc_info.value)


def test_raw_code_never_appears_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(500)))

    with pytest.raises(PasswordResetEmailError) as exc_info:
        _sender(max_retries=0).send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert "483921" not in str(exc_info.value)


def test_recipient_email_never_appears_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(500)))

    with pytest.raises(PasswordResetEmailError) as exc_info:
        _sender(max_retries=0).send_password_reset_code(
            email="very-specific-recipient@example.com", code="483921", expires_minutes=10
        )

    assert "very-specific-recipient@example.com" not in str(exc_info.value)


def test_secrets_never_logged_during_retries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(500)))

    with caplog.at_level("DEBUG"):
        with pytest.raises(PasswordResetEmailError):
            _sender(api_key="super-secret-resend-key", max_retries=2, retry_base_seconds=0.1).send_password_reset_code(
                email="very-specific-recipient@example.com", code="483921", expires_minutes=10
            )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-resend-key" not in log_text
    assert "Bearer" not in log_text
    assert "483921" not in log_text
    assert "very-specific-recipient@example.com" not in log_text


def test_outbound_payload_and_secrets_are_never_logged_on_the_success_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(httpx, "post", _fake_post_returning(httpx.Response(200, json={"id": "abc"})))

    with caplog.at_level("DEBUG"):
        _sender(api_key="super-secret-resend-key").send_password_reset_code(
            email="very-specific-recipient@example.com", code="483921", expires_minutes=10
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "super-secret-resend-key" not in log_text
    assert "483921" not in log_text
    assert "very-specific-recipient@example.com" not in log_text


def test_raw_code_appears_only_in_the_outbound_resend_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flip side of the secrecy tests above: the code must actually
    reach Resend somehow - proving it lands in the one sanctioned place
    (the captured outbound JSON body), not that it never went anywhere."""
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["json"] = json
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "post", fake_post)

    _sender().send_password_reset_code(email="user@example.com", code="483921", expires_minutes=10)

    assert "483921" in captured["json"]["text"]
