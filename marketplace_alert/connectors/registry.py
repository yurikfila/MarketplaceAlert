"""Connector registry: resolves a marketplace name to its connector.

The one place allowed to import concrete connector implementations
(``MockMarketplaceConnector``, ``EtsyMarketplaceConnector``,
``EbayMarketplaceConnector`` now; a real ``VintedConnector``, etc. later).
Everything else - the background scanner, saved-search validation, API
routes - calls ``get_connector(name)`` / ``is_marketplace_supported(name)``
and never imports a connector class directly (see
``core/saved_searches/runner.py`` and ``core/scheduler/``, which take a
resolver function injected by ``main.py`` instead of importing this
module). Adding a marketplace means adding one line to
``_CONNECTOR_FACTORIES`` below - nothing in ``core/`` changes.
"""

from collections.abc import Callable

from marketplace_alert.config import settings
from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.core.connectors.base import MarketplaceConnector


class UnsupportedMarketplaceError(ValueError):
    """Raised when a marketplace name has no registered connector."""


# Each entry is a zero-arg callable returning a connector instance, not
# necessarily a bare class - Etsy/eBay need credentials wired in from
# settings, which a plain `type[MarketplaceConnector]` reference can't do.
_CONNECTOR_FACTORIES: dict[str, Callable[[], MarketplaceConnector]] = {
    "mock": MockMarketplaceConnector,
    "etsy": lambda: EtsyMarketplaceConnector(
        api_key=settings.etsy_api_key,
        shared_secret=settings.etsy_shared_secret,
        result_limit=settings.etsy_result_limit,
    ),
    "ebay": lambda: EbayMarketplaceConnector(
        app_id=settings.ebay_app_id,
        cert_id=settings.ebay_cert_id,
        result_limit=settings.ebay_result_limit,
    ),
}


def is_marketplace_supported(marketplace_name: str) -> bool:
    """Whether a connector is currently registered for this marketplace name."""
    return marketplace_name in _CONNECTOR_FACTORIES


def list_supported_marketplaces() -> list[str]:
    """All marketplace names with a registered connector, sorted for stable display order.

    The single source of truth for anything that needs to list marketplaces
    (e.g. the dashboard's marketplace dropdown) - never hard-code the name
    list anywhere else.
    """
    return sorted(_CONNECTOR_FACTORIES)


def get_connector(marketplace_name: str) -> MarketplaceConnector:
    """Return a fresh connector instance for the given marketplace name.

    Raises UnsupportedMarketplaceError if no connector is registered.
    """
    try:
        factory = _CONNECTOR_FACTORIES[marketplace_name]
    except KeyError:
        raise UnsupportedMarketplaceError(
            f"No connector implementation registered for marketplace {marketplace_name!r}"
        ) from None
    return factory()
