"""Expo push notification provider - native mobile push, Phase 1.

Implements `NotificationProvider` (see `core/notifications/base.py`) -
unlike `notifications/email/provider.py:PasswordResetEmailSender`
(deliberately NOT that interface, since a password-reset email isn't
`Listing`-shaped), Expo push genuinely fits `send_listing_alert(listing,
destination)` - `destination` is a resolved per-user Expo push token,
the exact same shape `TelegramNotificationProvider` already uses for a
chat id. Isolated in its own subpackage, mirroring `notifications/
telegram/` and `notifications/email/` exactly - nothing outside
`dependencies.py` (which wires the concrete instance up from `settings`)
should ever import this module directly.

**Verified against Expo's own current documentation before writing this**
(`docs.expo.dev/push-notifications/sending-notifications/`), not assumed:
- `POST https://exp.host/--/api/v2/push/send`, headers `host: exp.host`,
  `accept: application/json`, `accept-encoding: gzip, deflate`,
  `content-type: application/json`.
- An `Authorization: Bearer <token>` header is optional - only needed if
  "push security" is explicitly enabled in the sending account's EAS
  dashboard; sending works with no credential at all otherwise. This is
  why `is_enabled` here is NOT gated on any credential being present
  (unlike Telegram/Resend, which need a real secret to function at
  all) - there is nothing to misconfigure that would make Expo push
  categorically unable to send. `is_enabled` instead reflects a plain
  operator on/off switch (`settings.expo_push_enabled`).
- The response body is `{"data": [{"status": "ok"|"error", "id": ...,
  "details": {"error": "<code>"}}], ...}` - a ticket-level `"error"`
  status can come back inside an otherwise-2xx HTTP response, so a
  successful transport-level POST is not by itself success; the ticket's
  own `status` must be checked too.

**Retry design mirrors `TelegramNotificationProvider` closely** (bounded
exponential backoff, retriable 429/5xx vs. permanent other-4xx), with the
same `_MAX_RETRY_DELAY_SECONDS` cap `notifications/email/provider.py`
was fixed to apply: this call runs synchronously inside a one-shot Cron
Job pass (`scripts/drain_notification_outbox.py`) that may be delivering
many other users' notifications in the same run - an unbounded wait here
would delay all of them, not just one.

**Security - the following must never appear in a log line or an
exception raised by this module**: the optional Expo access token, the
recipient's push token, and the outbound JSON payload/response body.
Only `type(exc).__name__`, the numeric HTTP status code, and Expo's own
bounded, fixed-enum ticket error code (e.g. `"DeviceNotRegistered"` -
never the free-text `message` field) are ever logged - the same
"provider-supplied fixed field is safe, free text is not" distinction
`TelegramNotificationProvider` already applies to Telegram's own
`description` field.
"""

import logging
import time

import httpx

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider

logger = logging.getLogger(__name__)

_EXPO_PUSH_API_URL = "https://exp.host/--/api/v2/push/send"

# HTTP statuses worth retrying: 429 (rate limited) and 5xx (transient
# server-side trouble). Everything else (400 malformed request, 401/403
# bad/revoked access token, etc.) is a permanent failure.
_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Same reasoning and same value as `notifications/email/provider.py`'s
# identical constant - this call runs inside a one-shot Cron Job pass
# delivering potentially many notifications; an unbounded retry delay
# here would stall every other pending row in the same drain run.
_MAX_RETRY_DELAY_SECONDS = 5.0


class ExpoPushProvider(NotificationProvider):
    """Sends listing alerts as native push notifications via Expo's Push API.

    Reads `access_token`/`enabled` from the values passed in
    (`dependencies.py` wires them from `settings`, which loads them from
    the environment / `.env` - never hard-coded).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        access_token: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
    ) -> None:
        self._enabled = enabled
        self._access_token = access_token
        self._timeout = timeout
        # A negative config value would otherwise turn into "retry forever
        # backwards" nonsense or a negative sleep - clamp to a sane floor
        # instead of trusting the environment blindly (same reasoning as
        # every other provider in this codebase).
        self._max_retries = max(0, max_retries)
        self._retry_base_seconds = max(0.0, retry_base_seconds)

    @property
    def is_enabled(self) -> bool:
        """A plain operator on/off switch - see this module's own
        docstring for why this is not gated on any credential being
        present, unlike every other provider in this codebase."""
        return self._enabled

    def send_listing_alert(self, listing: Listing, destination: str) -> None:
        """Send one push notification for `listing` to `destination` (an
        Expo push token), retrying transient failures up to `max_retries`
        times.

        Raises `NotificationError` once every attempt has been exhausted
        (transient failure), immediately for a permanent HTTP-level
        failure, or immediately for a ticket-level rejection (e.g.
        `DeviceNotRegistered`) - the caller (`core/notifications/
        outbox.py`'s push drain loop) doesn't need to know which. Also
        raises immediately, before any network call, if this provider is
        disabled or `destination` is falsy - this provider never guesses
        or falls back (see `NotificationProvider.send_listing_alert`'s
        own docstring); a caller with nothing to pass here must not call
        this method at all.
        """
        if not self.is_enabled:
            raise NotificationError("Expo push provider is not enabled")
        if not destination:
            raise NotificationError("No notification destination provided")

        headers = {
            "host": "exp.host",
            "accept": "application/json",
            "accept-encoding": "gzip, deflate",
            "content-type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"

        payload: dict[str, object] = {
            "to": destination,
            "title": "New matching listing",
            "body": listing.title,
            "data": {
                "listing_url": str(listing.listing_url),
                "marketplace": listing.marketplace,
                "external_listing_id": listing.external_listing_id,
            },
            "sound": "default",
            "priority": "high",
        }

        total_attempts = self._max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                response = httpx.post(_EXPO_PUSH_API_URL, json=payload, headers=headers, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if attempt >= total_attempts:
                    logger.error(
                        "Expo push request failed permanently after %d attempt(s) (%s)",
                        attempt,
                        type(exc).__name__,
                    )
                    raise NotificationError("Expo push request failed") from None
                self._retry_wait(attempt, total_attempts, self._backoff_seconds(attempt), reason=type(exc).__name__)
                continue

            if response.status_code in _RETRIABLE_STATUS_CODES:
                if attempt >= total_attempts:
                    logger.error(
                        "Expo push API returned HTTP %s - giving up after %d attempt(s)",
                        response.status_code,
                        attempt,
                    )
                    raise NotificationError(f"Expo push API returned HTTP {response.status_code}")
                self._retry_wait(
                    attempt, total_attempts, self._backoff_seconds(attempt), reason=f"HTTP {response.status_code}"
                )
                continue

            if response.status_code >= 300:
                logger.error(
                    "Expo push API returned HTTP %s (permanent failure, not retrying)", response.status_code
                )
                raise NotificationError(f"Expo push API returned HTTP {response.status_code}")

            ticket_error = self._ticket_error(response)
            if ticket_error is None:
                logger.info("Expo push sent (attempt %d/%d)", attempt, total_attempts)
                return

            # A ticket-level rejection (e.g. DeviceNotRegistered,
            # MessageTooBig) arrives inside an otherwise-2xx response -
            # always a permanent failure for this specific send, never
            # retried within this call.
            logger.error("Expo push API rejected the ticket (error=%s)", ticket_error)
            raise NotificationError(f"Expo push API rejected the ticket ({ticket_error})")

        # Unreachable: the loop above always returns or raises before
        # exhausting `total_attempts` iterations.
        raise NotificationError("Expo push request failed")

    @staticmethod
    def _ticket_error(response: httpx.Response) -> str | None:
        """`None` means the single ticket in the response reported
        `status: "ok"`. Otherwise returns Expo's own bounded, fixed-enum
        error code (`details.error`, e.g. `"DeviceNotRegistered"`) -
        never the free-text `message` field, which is not logged or
        included in any exception (see this module's own docstring).
        A malformed/unparseable/empty response body is treated as its
        own distinct error, never as silent success.
        """
        try:
            body = response.json()
        except ValueError:
            return "malformed_response"
        tickets = body.get("data") if isinstance(body, dict) else None
        if not tickets:
            return "no_ticket_data"
        ticket = tickets[0]
        if ticket.get("status") == "ok":
            return None
        details = ticket.get("details")
        if isinstance(details, dict) and details.get("error"):
            return str(details["error"])
        return "unknown_error"

    def _retry_wait(self, attempt: int, total_attempts: int, wait_seconds: float, reason: str) -> None:
        logger.warning(
            "Expo push send failed (%s) - retry attempt %d/%d in %.1fs",
            reason,
            attempt,
            total_attempts - 1,
            wait_seconds,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff bounded by `max_retries` AND by
        `_MAX_RETRY_DELAY_SECONDS`: base, 2x base, 4x base, ... - never
        exceeding the cap no matter how large `retry_base_seconds` or
        `attempt` are."""
        return min(self._retry_base_seconds * (2 ** (attempt - 1)), _MAX_RETRY_DELAY_SECONDS)
