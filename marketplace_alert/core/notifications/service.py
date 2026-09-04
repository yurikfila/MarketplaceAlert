"""Notification service: sends alerts for newly discovered listings to one
explicit destination.

Works with `Listing` objects and a `NotificationProvider` only - it never
imports Telegram (or any other concrete provider) directly. `main.py` wires
the concrete provider in; this service doesn't know or care which one it got.

**No destination of its own.** This service has no per-listing routing
concept - `notify_new_listings` sends an entire batch to the single
`destination` its caller supplies, explicitly, every call (see
`core/notifications/base.py`'s `NotificationProvider.send_listing_alert`).
It never reads a global default. The real, per-user-aware production
delivery path is `core/notifications/outbox.py`'s drain loop, which
resolves a distinct destination for each notification individually; this
service exists only for the legacy, single-batch, single-destination use
case (historically the `/scan` endpoint - see `main.py`, which no longer
has a legitimate destination to supply and so no longer calls this).

**Delivery pacing under bursts**: a single scan can discover many new
listings at once (e.g. a fresh saved search matching dozens of existing
items). Sending them all back-to-back risks hammering whatever provider is
behind `NotificationProvider` - so `notify_new_listings` sends them in
order, one at a time, waiting `send_delay_seconds` between sends (not
before the first one, so a single new listing is never artificially
delayed). This is a generic pacing knob, not provider-specific logic -
retrying a *single* send's transient failures (429/5xx/timeout, honoring
Telegram's `retry_after`) is the provider's own job, see
`marketplace_alert/notifications/telegram/provider.py`. A failure on one
listing (after the provider's own retries are exhausted) is logged and
skipped - it never stops the remaining listings in the batch, and
`notify_new_listings` itself never raises.
"""

import logging
import time

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends one alert per new listing via the given provider, paced to avoid bursts."""

    def __init__(self, provider: NotificationProvider, send_delay_seconds: float = 0.0) -> None:
        self._provider = provider
        # A negative config value would otherwise turn into a negative
        # sleep - clamp to zero (no delay) instead of trusting it blindly.
        self._send_delay_seconds = max(0.0, send_delay_seconds)

    @property
    def is_enabled(self) -> bool:
        """Whether the underlying provider is configured (e.g. for status displays)."""
        return self._provider.is_enabled

    def notify_new_listings(self, listings: list[Listing], destination: str) -> None:
        """Alert on each new listing, in order, paced between sends, all to
        `destination`. Never raises.

        A failure to notify about one listing must never stop the rest of a
        batch from being attempted, and must never surface as a failure to
        the caller (a saved-search run or the scheduler). `destination`
        must be supplied explicitly by the caller every time - see this
        module's docstring.
        """
        if not listings:
            return

        if not self._provider.is_enabled:
            logger.info(
                "Notification provider is disabled - skipping %d new listing alert(s)",
                len(listings),
            )
            return

        for index, listing in enumerate(listings):
            if index > 0 and self._send_delay_seconds > 0:
                logger.info(
                    "Waiting %.1fs before next notification send (rate control)",
                    self._send_delay_seconds,
                )
                time.sleep(self._send_delay_seconds)

            logger.info(
                "Notification queued for %s listing %s", listing.marketplace, listing.external_listing_id
            )
            try:
                self._provider.send_listing_alert(listing, destination)
            except NotificationError:
                logger.exception(
                    "Failed to send notification for %s listing %s",
                    listing.marketplace,
                    listing.external_listing_id,
                )
