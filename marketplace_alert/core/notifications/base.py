"""Abstract interface every notification provider must implement.

Mirrors `marketplace_alert.core.connectors.base.MarketplaceConnector`: the
core system only ever talks to `NotificationProvider`, never to a specific
provider's SDK or API client. Provider-specific details (auth, HTTP calls,
message formatting, rate limits) live entirely inside the concrete
subclass - e.g. `TelegramNotificationProvider` - and never leak out.
"""

from abc import ABC, abstractmethod

from marketplace_alert.core.models.listing import Listing


class NotificationError(Exception):
    """Raised by a provider when sending an alert fails.

    Messages must never include secrets (API tokens, keys) - they may end
    up in logs.
    """


class NotificationProvider(ABC):
    """Base class for all notification providers (Telegram, email, etc.)."""

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this provider is configured and able to send alerts.

        Callers must check this before calling `send_listing_alert` - a
        disabled provider (e.g. missing credentials) means "skip
        silently and log", not "raise an error".
        """
        raise NotImplementedError

    @abstractmethod
    def send_listing_alert(self, listing: Listing, destination: str) -> None:
        """Send an alert for one newly discovered listing to `destination`.

        `destination` is always supplied explicitly by the caller (e.g. a
        resolved per-user Telegram chat id) - no provider implementation
        may fall back to a stored/global default of its own. A caller
        that has no destination to give must never call this method at
        all (see `core/notifications/outbox.py`'s module docstring
        "SECURITY RULE") - there is no sentinel value that means "use
        something else instead".

        Raises `NotificationError` on failure (network error, API error
        response, etc). Never called when `is_enabled` is False.
        """
        raise NotImplementedError
