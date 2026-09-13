"""Password-reset verification-code email delivery via Resend's HTTP API.

**Deliberately NOT a `core.notifications.base.NotificationProvider`.** That
interface is shaped for listing alerts (`send_listing_alert(listing,
destination)`) - a password-reset email has no `Listing` and no per-call
destination-resolution concept, so forcing it through that abstraction
would be semantically wrong (see the Phase 4A audit). This is its own,
independent, purpose-specific class - `send_password_reset_code`, not a
generic `send(...)` - matching this codebase's existing preference for
purpose-specific provider methods over generic ones (compare
`TelegramNotificationProvider.send_listing_alert`).

**Isolated in its own subpackage**, mirroring `notifications/telegram/`
exactly - nothing outside `dependencies.py` (which wires the concrete
instance up from `settings`) should ever import this module directly.

**Not wired into any route yet.** This is Phase 4B of the approved
Resend-integration design: provider + config + tests only. Nothing in the
running application calls `send_password_reset_code` yet - that's Phase
4C's job (wiring it into `POST /api/v1/auth/forgot-password`, discarding
the result today - see that route's own docstring).

**Uses `httpx` directly, not the Resend Python SDK** - httpx is already a
project dependency and already the exact pattern used for every other
external integration here (Telegram, Etsy, eBay, Reverb, Bonanza); the
SDK would be a second, unfamiliar dependency/retry model for a single
JSON POST with three or four fields.

**Retry design mirrors `TelegramNotificationProvider` closely** (bounded
exponential backoff, retriable 429/5xx/network-failure vs. permanent
other-4xx), with three deliberate differences: (1) far smaller defaults
(`max_retries=1`, `timeout=5.0`) - this call happens synchronously inline
within a user-facing HTTP request (`/forgot-password`), not a background
scan, so added worst-case latency must stay small; (2) no response body
is ever logged or included in `PasswordResetEmailError`, not even
Resend's own error-message field - only the HTTP status code and the
exception type name. Telegram's provider trusts its own documented
`description` field to never echo request data; this module makes the
more conservative choice of never repeating anything Resend sends back,
since that specific guarantee was not independently verified for Resend
during the Phase 4A audit; (3) **every retry delay, regardless of source,
is capped at `_MAX_RETRY_DELAY_SECONDS`** - unlike Telegram's provider
(a background scan, where a long `retry_after`-driven wait is merely slow),
this call blocks a synchronous, user-facing request, so a
provider-supplied `Retry-After` value must never be trusted to be small
just because it's well-formed - see `_retry_wait_seconds`'s own docstring.

**Security - the following must never appear in a log line or an
exception raised by this module**: the Resend API key, the
`Authorization` header, the raw 6-digit reset code, and the recipient
email address. Also never logs the outbound JSON payload, the
`Idempotency-Key` value, or a raw `httpx` exception's own string
representation (which can embed request/response detail) - every
exception here is described only by `type(exc).__name__` and (for HTTP
responses) the numeric status code, never `str(exc)`/`response.text`/
`response.json()`.
"""

import logging
import secrets
import time

import httpx

from marketplace_alert import __version__

logger = logging.getLogger(__name__)

_RESEND_API_BASE = "https://api.resend.com"
_USER_AGENT = f"MarketplaceAlert/{__version__}"

_SUBJECT = "MarketplaceAlert password reset code"

# HTTP statuses worth retrying: 429 (rate limited - Resend's own
# `Retry-After` is honored when present, see `_retry_wait_seconds`) and
# 5xx (transient server-side trouble). Everything else (400 malformed
# request, 401/403 bad/revoked API key, 404, etc.) is a permanent
# failure - retrying it would never succeed.
_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Hard ceiling on every retry delay this module ever sleeps for -
# regardless of whether it came from Resend's own `Retry-After` header or
# this module's own exponential-backoff formula. `send_password_reset_code`
# runs synchronously inside a user-facing HTTP request (`POST /forgot-
# password`), not a background job - an unbounded wait (e.g. a
# well-formed but large `Retry-After: 3600`, or a misconfigured, very
# large `retry_base_seconds`) would otherwise be able to stall that
# request for an arbitrarily long time. Deliberately a private constant,
# not a `Settings`/env-configurable value - MVP-simple, and there is no
# legitimate reason a caller would ever want to raise it, only ways to
# accidentally make it dangerous.
_MAX_RETRY_DELAY_SECONDS = 5.0


class PasswordResetEmailError(Exception):
    """Raised when sending a password-reset code by email fails.

    Messages must never include secrets (the Resend API key, the
    `Authorization` header) or user-identifying values (the raw reset
    code, the recipient email address) - they may end up in logs. Only
    ever constructed here with a fixed, generic string or the numeric
    HTTP status code - never `str(exc)` on a caught `httpx` exception,
    never a response body.
    """


def _build_body(code: str, expires_minutes: int) -> str:
    """The entire email body - plain text only, no HTML/template engine
    (see this module's own docstring for why). Both interpolated values
    are server-generated and fully trusted (a 6-digit code from
    `core.auth.security.generate_reset_code`, and a configured integer) -
    never user-controlled content, so there is nothing here to escape.
    """
    return (
        f"Your MarketplaceAlert password reset code is: {code}\n\n"
        f"This code expires in {expires_minutes} minutes.\n\n"
        "If you didn't request this, you can safely ignore this email."
    )


class PasswordResetEmailSender:
    """Sends a password-reset verification code by email via Resend.

    Reads `api_key`/`from_address` from the values passed in
    (`dependencies.py` wires them from `settings`, which loads them from
    the environment / `.env` - never hard-coded). If either is missing,
    the sender is disabled: `is_enabled` is `False` and callers must skip
    it rather than calling `send_password_reset_code` - same convention
    as `TelegramNotificationProvider`.
    """

    def __init__(
        self,
        api_key: str | None,
        from_address: str | None,
        *,
        reply_to: str | None = None,
        timeout: float = 5.0,
        max_retries: int = 1,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._from_address = from_address
        self._reply_to = reply_to
        self._timeout = timeout
        # A negative config value would otherwise turn into "retry
        # forever backwards" nonsense or a negative sleep - clamp to a
        # sane floor instead of trusting the environment blindly (same
        # reasoning as TelegramNotificationProvider).
        self._max_retries = max(0, max_retries)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        if not self.is_enabled:
            logger.warning(
                "Password-reset email disabled: RESEND_API_KEY or PASSWORD_RESET_EMAIL_FROM is not set"
            )

    @property
    def is_enabled(self) -> bool:
        """Whether both required settings are configured. `reply_to` is
        optional and never affects this - see this class's own docstring."""
        return bool(self._api_key and self._from_address)

    def send_password_reset_code(self, *, email: str, code: str, expires_minutes: int) -> None:
        """Send one password-reset code to `email`, retrying transient
        failures up to `max_retries` times (default: once).

        Raises `PasswordResetEmailError` once every attempt has been
        exhausted (transient failure) or immediately for a permanent one
        - the caller doesn't need to know which. Also raises immediately,
        before any network call, if this sender isn't configured - see
        `is_enabled`.

        A single `Idempotency-Key` (a fresh, cryptographically random,
        opaque value - never derived from `email`/`code`/the API key) is
        generated once per call, outside the retry loop, so every retry
        of *this* send reuses the exact same key - Resend then recognizes
        retried attempts as the same logical send rather than risking a
        duplicate email. Never logged.
        """
        if not self.is_enabled:
            raise PasswordResetEmailError("Password-reset email sender is not configured")

        url = f"{_RESEND_API_BASE}/emails"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": _USER_AGENT,
            "Idempotency-Key": secrets.token_urlsafe(32),
        }
        payload: dict[str, object] = {
            "from": self._from_address,
            "to": [email],
            "subject": _SUBJECT,
            "text": _build_body(code, expires_minutes),
        }
        if self._reply_to:
            payload["reply_to"] = self._reply_to

        total_attempts = self._max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
            except httpx.HTTPError as exc:
                if attempt >= total_attempts:
                    logger.error(
                        "Password-reset email request failed permanently after %d attempt(s) (%s)",
                        attempt,
                        type(exc).__name__,
                    )
                    raise PasswordResetEmailError("Password-reset email request failed") from None
                self._retry_wait(
                    attempt, total_attempts, self._backoff_seconds(attempt), reason=type(exc).__name__
                )
                continue

            if response.status_code < 300:
                logger.info("Password-reset email sent (attempt %d/%d)", attempt, total_attempts)
                return

            if response.status_code in _RETRIABLE_STATUS_CODES:
                if attempt >= total_attempts:
                    logger.error(
                        "Password-reset email provider returned HTTP %s - giving up after %d attempt(s)",
                        response.status_code,
                        attempt,
                    )
                    raise PasswordResetEmailError(
                        f"Password-reset email provider returned HTTP {response.status_code}"
                    )
                wait_seconds = self._retry_wait_seconds(response, attempt)
                self._retry_wait(
                    attempt, total_attempts, wait_seconds, reason=f"HTTP {response.status_code}"
                )
                continue

            # Any other non-2xx status (400, 401, 403, 404, ...) is a
            # permanent failure - a bad request/expired-or-revoked API
            # key will never succeed on retry, so fail fast without
            # consuming one. Never logs/raises the response body - see
            # this module's own docstring.
            logger.error(
                "Password-reset email provider returned HTTP %s (permanent failure, not retrying)",
                response.status_code,
            )
            raise PasswordResetEmailError(f"Password-reset email provider returned HTTP {response.status_code}")

        # Unreachable: the loop above always returns or raises before
        # exhausting `total_attempts` iterations.
        raise PasswordResetEmailError("Password-reset email request failed")

    def _retry_wait(self, attempt: int, total_attempts: int, wait_seconds: float, reason: str) -> None:
        logger.warning(
            "Password-reset email send failed (%s) - retry attempt %d/%d in %.1fs",
            reason,
            attempt,
            total_attempts - 1,
            wait_seconds,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _retry_wait_seconds(self, response: httpx.Response, attempt: int) -> float:
        """How long to wait before the next attempt - always within
        `[0, _MAX_RETRY_DELAY_SECONDS]`, regardless of source (see that
        constant's own docstring for why this bound is non-negotiable
        here).

        Resend's own `Retry-After` response header (seconds) is
        preferred when present and safely parseable - it reflects
        Resend's actual rate-limit window, which a generic backoff
        formula can only guess at - but is still capped, never trusted
        to be small just because it's well-formed. Falls back to
        (equally capped) exponential backoff for 5xx responses (no such
        hint exists there), or if the 429's header is missing/malformed/
        negative.
        """
        if response.status_code == 429:
            retry_after = self._parse_retry_after(response)
            if retry_after is not None:
                return min(retry_after, _MAX_RETRY_DELAY_SECONDS)
        return self._backoff_seconds(attempt)

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff bounded by `max_retries` AND by
        `_MAX_RETRY_DELAY_SECONDS`: base, 2x base, 4x base, ... - never
        exceeding the cap no matter how large `retry_base_seconds` or
        `attempt` are."""
        return min(self._retry_base_seconds * (2 ** (attempt - 1)), _MAX_RETRY_DELAY_SECONDS)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """`Retry-After` is a plain HTTP response header (seconds, per
        RFC 9110) - never trusted blindly: a missing, non-numeric, or
        negative value safely falls back to the exponential-backoff
        formula instead of raising or misbehaving."""
        header_value = response.headers.get("Retry-After")
        if header_value is None:
            return None
        try:
            value = float(header_value)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        return value
