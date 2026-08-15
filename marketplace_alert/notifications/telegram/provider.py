"""Telegram notification provider.

Implements `NotificationProvider` (see
`marketplace_alert.core.notifications.base`) using the Telegram Bot API's
`sendMessage` method. Isolated in its own subpackage - nothing outside
`main.py` (which wires the concrete provider up at startup) should ever
import this module directly.

**Delivery robustness under bursts**: a saved search can discover many new
listings in one scan, and sending them all back-to-back risks Telegram's
own rate limiting (HTTP 429) or transient 5xx/timeout failures. This
provider retries those specific failures with a bounded exponential
backoff, honoring Telegram's own `retry_after` hint on 429 responses
rather than guessing a wait time. It deliberately does NOT retry permanent
failures (a malformed request, an invalid chat ID, an invalid bot token,
or any other non-retriable 4xx) - retrying those would never succeed and
would just waste time before giving up anyway. The pacing *between*
separate listings' sends (as opposed to *retries* of the same send) is a
different concern and lives one layer up, in
`marketplace_alert.core.notifications.service.NotificationService`.

Security: the bot token is part of every Telegram Bot API request URL
(``https://api.telegram.org/bot<TOKEN>/sendMessage``). This module never
logs that URL, the token, or the raw exception/response object it came
from - only sanitized, token-free details (exception type, HTTP status
code, Telegram's own "description" field, retry attempt counts/wait times).
"""

import logging
import time

import httpx

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# HTTP statuses worth retrying: 429 (rate limited - Telegram's own
# `retry_after` is honored, see `_retry_wait_seconds`) and 5xx (transient
# server-side trouble). Everything else (400 malformed request, 401/403
# bad credentials, 404 chat not found, etc.) is a permanent failure -
# retrying it would never succeed.
_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def format_listing_message(listing: Listing) -> str:
    """Build the alert text for one listing: title, marketplace, price, location, URL."""
    lines = [
        f"New listing: {listing.title}",
        f"Marketplace: {listing.marketplace}",
    ]
    if listing.price is not None:
        currency_suffix = f" {listing.currency}" if listing.currency else ""
        lines.append(f"Price: {listing.price}{currency_suffix}")
    if listing.location:
        lines.append(f"Location: {listing.location}")
    lines.append(f"Link: {listing.listing_url}")
    return "\n".join(lines)


class TelegramNotificationProvider(NotificationProvider):
    """Sends listing alerts to a Telegram chat via a bot.

    Reads the bot token and chat ID from the values passed in (main.py
    wires these from ``settings``, which loads them from the environment /
    ``.env`` - never hard-coded). If either is missing, the provider is
    disabled: `is_enabled` is False and callers must skip it rather than
    calling `send_listing_alert`.
    """

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        # A negative config value would otherwise turn into "retry forever
        # backwards" nonsense or a negative sleep - clamp to a sane floor
        # instead of trusting the environment blindly.
        self._max_retries = max(0, max_retries)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        if not self.is_enabled:
            logger.warning(
                "Telegram notifications disabled: TELEGRAM_BOT_TOKEN and/or "
                "TELEGRAM_CHAT_ID are not set"
            )

    @property
    def is_enabled(self) -> bool:
        return bool(self._bot_token) and bool(self._chat_id)

    def send_listing_alert(self, listing: Listing) -> None:
        """Send one alert, retrying transient failures up to `max_retries` times.

        Raises `NotificationError` only once every attempt has been
        exhausted (transient failure) or immediately for a permanent one -
        the caller (`NotificationService`) doesn't need to know which.
        """
        if not self.is_enabled:
            raise NotificationError("Telegram provider is not configured")

        url = f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": format_listing_message(listing)}
        total_attempts = self._max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                response = httpx.post(url, json=payload, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if attempt >= total_attempts:
                    logger.error(
                        "Telegram API request failed permanently after %d attempt(s) (%s)",
                        attempt,
                        type(exc).__name__,
                    )
                    raise NotificationError("Telegram API request failed") from None
                self._retry_wait(attempt, total_attempts, self._backoff_seconds(attempt), reason=type(exc).__name__)
                continue

            if response.status_code == 200:
                body = response.json()
                if body.get("ok", False):
                    logger.info("Telegram notification sent (attempt %d/%d)", attempt, total_attempts)
                    return
                # Telegram signals errors via non-200 status in practice, but
                # a 200 with "ok": false has no documented retry semantics -
                # treat it as permanent rather than guessing.
                description = body.get("description", "unknown error")
                logger.error("Telegram API returned an error (permanent, not retrying): %s", description)
                raise NotificationError(f"Telegram API returned an error: {description}")

            if response.status_code in _RETRIABLE_STATUS_CODES:
                if attempt >= total_attempts:
                    logger.error(
                        "Telegram API returned HTTP %s - giving up after %d attempt(s)",
                        response.status_code,
                        attempt,
                    )
                    raise NotificationError(f"Telegram API returned HTTP {response.status_code}")
                wait_seconds = self._retry_wait_seconds(response, attempt)
                self._retry_wait(
                    attempt, total_attempts, wait_seconds, reason=f"HTTP {response.status_code}"
                )
                continue

            # Any other non-200 status (400, 401, 403, 404, ...) is a
            # permanent failure - a bad request/credential/chat ID will
            # never succeed on retry, so fail fast without consuming one.
            logger.error(
                "Telegram API returned HTTP %s (permanent failure, not retrying)",
                response.status_code,
            )
            raise NotificationError(f"Telegram API returned HTTP {response.status_code}")

        # Unreachable: the loop above always returns or raises before
        # exhausting `total_attempts` iterations.
        raise NotificationError("Telegram API request failed")

    def _retry_wait(self, attempt: int, total_attempts: int, wait_seconds: float, reason: str) -> None:
        logger.warning(
            "Telegram send failed (%s) - retry attempt %d/%d in %.1fs",
            reason,
            attempt,
            total_attempts - 1,
            wait_seconds,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _retry_wait_seconds(self, response: httpx.Response, attempt: int) -> float:
        """How long to wait before the next attempt.

        Telegram's own `retry_after` (seconds, in the 429 response body's
        `parameters` object) is authoritative when present - it reflects
        Telegram's actual rate-limit window, which a generic backoff
        formula can only guess at. Falls back to exponential backoff for
        5xx responses (no such hint exists there) or if the 429 body didn't
        include one.
        """
        if response.status_code == 429:
            retry_after = self._parse_retry_after(response)
            if retry_after is not None:
                return retry_after
        return self._backoff_seconds(attempt)

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff bounded by `max_retries`: base, 2x base, 4x base, ..."""
        return self._retry_base_seconds * (2 ** (attempt - 1))

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        try:
            body = response.json()
        except ValueError:
            return None
        retry_after = body.get("parameters", {}).get("retry_after")
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after >= 0:
            return float(retry_after)
        return None
