"""Shared bounded-retry helper for marketplace connector HTTP requests.

Retries only genuinely transient failures - 429 (rate limited) and
502/503/504 (upstream gateway trouble, momentary unavailability) - with
exponential backoff, honoring a standard `Retry-After` header when a
marketplace sends one. Never retries a permanent failure (401/403 auth,
404, 400, or any other 4xx/5xx not in the retriable set) - retrying those
would never succeed and would just add latency before giving up anyway.
Connectors are still responsible for their own status-code handling
(e.g. eBay's token invalidation on 401/403) - this helper only decides
*whether to try again*, never what a given status code means.

Same policy as `notifications/telegram/provider.py`'s retry logic
(bounded attempts, exponential backoff, respect the server's own hint
when present) - kept as a separate implementation rather than literally
shared code, since Telegram's retry hint is a JSON body field
(`parameters.retry_after`) while this is the standard HTTP `Retry-After`
header, and the two are used in different places (a single notification
send vs. every connector's search request).
"""

import logging
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

RETRIABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_SECONDS = 1.0


def request_with_retry(
    make_request: Callable[[], httpx.Response],
    *,
    marketplace_name: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
) -> httpx.Response:
    """Call `make_request()`, retrying a 429/502/503/504 response up to
    `max_retries` additional times with exponential backoff.

    Returns the response as-is - including a final non-2xx one, once
    retries are exhausted - for the caller's own status handling; never
    raises itself for an HTTP-status reason. `httpx.HTTPError` raised by
    `make_request()` (network error, timeout) propagates immediately,
    unretried - a connector that has already timed out once retrying
    into the same tick's remaining time budget is a separate, deliberate
    scope decision (not requested here), not an oversight.
    """
    total_attempts = max(0, max_retries) + 1
    response: httpx.Response | None = None
    for attempt in range(1, total_attempts + 1):
        response = make_request()
        if response.status_code not in RETRIABLE_STATUS_CODES:
            return response
        if attempt >= total_attempts:
            return response
        wait_seconds = _wait_seconds(response, attempt, retry_base_seconds)
        logger.warning(
            "%s API returned HTTP %s - retry attempt %d/%d in %.1fs",
            marketplace_name,
            response.status_code,
            attempt,
            total_attempts - 1,
            wait_seconds,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    assert response is not None  # total_attempts >= 1, so the loop always ran at least once
    return response


def _wait_seconds(response: httpx.Response, attempt: int, retry_base_seconds: float) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            parsed = float(retry_after)
        except ValueError:
            parsed = None
        if parsed is not None and parsed >= 0:
            return parsed
    return retry_base_seconds * (2 ** (attempt - 1))
