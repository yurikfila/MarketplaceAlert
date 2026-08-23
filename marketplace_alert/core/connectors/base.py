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


class ListingLookupNotSupportedError(Exception):
    """Raised by ``get_listing_by_id()`` when a connector has no documented,
    authoritative single-item lookup - e.g. Bonanza's Bonapitit API has no
    confirmed single-item endpoint (see its connector module docstring).

    Distinct from ``MarketplaceConnectorError``: this means "this
    marketplace doesn't offer this capability at all", not "the request
    failed" - the historical backfill service (`core/persistence/backfill.py`)
    treats it as a clean, expected reason to skip a marketplace entirely,
    never as a connector failure to retry or report.
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

    def get_listing_by_id(self, external_listing_id: str) -> Listing | None:
        """Fetch the current, authoritative state of exactly one listing by
        its own id, via a documented single-item marketplace endpoint -
        never a new search, and never scraping.

        Used only by the historical backfill service
        (`core/persistence/backfill.py`) to re-fetch an older, incompletely
        -persisted listing - normal search/scan flows never call this.

        Returns ``None`` if the marketplace confirms the listing no longer
        exists (e.g. a 404) - a legitimate, expected outcome for an old
        listing (sold, ended, removed), not an error.

        Raises ``ListingLookupNotSupportedError`` by default. A connector
        with a real, documented single-item endpoint (Etsy, eBay, Reverb)
        overrides this; one without such a capability (Bonanza) simply
        doesn't override it, so calling this on an unsupported connector
        fails clearly and immediately rather than silently returning
        nothing or attempting to scrape/guess.
        """
        raise ListingLookupNotSupportedError(
            f"{self.marketplace_name} does not support listing lookup by id"
        )
