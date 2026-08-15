"""Abstract interface that every marketplace connector must implement.

The core search engine only ever talks to connectors through this interface.
It must never import or depend on a specific marketplace's implementation
details - new marketplaces are added by writing a new connector, not by
touching this file or the core engine.
"""

from abc import ABC, abstractmethod
from typing import Any

from marketplace_alert.core.models.listing import Listing


class MarketplaceConnectorError(Exception):
    """Raised by a connector when it can't complete a search.

    Covers configuration problems (missing credentials) as well as runtime
    failures (network error, timeout, non-2xx response, malformed body).
    Messages must never include credentials - they may end up in logs.
    """


class MarketplaceConnector(ABC):
    """Base class for all marketplace connectors.

    Concrete connectors (e.g. ``EbayConnector``, ``Yad2Connector``) subclass
    this and implement ``search``, ``normalize_listing`` and
    ``health_check``. Marketplace-specific logic (API clients, scraping,
    auth, rate limiting) lives entirely inside the subclass.
    """

    @property
    @abstractmethod
    def marketplace_name(self) -> str:
        """Stable, unique identifier for this marketplace (e.g. ``"ebay"``)."""
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        """Search the marketplace and return normalized listings.

        Args:
            query: Free-text keyword or phrase to search for.
            filters: Optional marketplace-agnostic filters (e.g. price range,
                location). Connectors should ignore filters they don't
                support rather than raising an error.
        """
        raise NotImplementedError

    @abstractmethod
    def normalize_listing(self, raw_listing: Any) -> Listing:
        """Convert a marketplace-specific raw listing into a ``Listing``."""
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the connector is currently able to reach the marketplace."""
        raise NotImplementedError
