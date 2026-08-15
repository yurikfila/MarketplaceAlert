"""eBay marketplace connector - eBay Buy Browse API only, no legacy Finding
API, no HTML scraping.

Endpoint used: ``GET https://api.ebay.com/buy/browse/v1/item_summary/search``.
Verified before implementation against eBay's own sources, not guessed:
  - the official Browse API `search` method reference
    (https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search),
    confirming the endpoint path and the `q`/`limit`/`offset`/`fieldgroups`
    query parameters (`limit` default 50, max 200);
  - the official `ItemSummary` type reference and its nested types
    (`ConvertedAmount`, `Image`, `Seller`, `ItemLocationImpl`), confirming
    every field name mapped below;
  - manually verified by the developer beforehand: this exact endpoint,
    with a real application access token, already returns HTTP 200 with
    real search results.

Authentication: see `token_manager.py` for the OAuth client_credentials
flow (`EbayTokenManager`) - every search request carries
``Authorization: Bearer <application access token>``, obtained from the
shared, cached token manager, never requested fresh per search.
``X-EBAY-C-MARKETPLACE-ID: EBAY_US`` is also sent (required for
marketplaces other than the US default; sent explicitly here rather than
relying on the implicit default).
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from marketplace_alert.connectors.ebay.token_manager import EbayTokenManager
from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.models.listing import Listing

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_MARKETPLACE_ID = "EBAY_US"

# eBay's own documented maximum is 200 per page; this connector fetches a
# single page (no automatic multi-page looping yet), so keep the default
# conservative - same pattern as the Etsy connector.
DEFAULT_RESULT_LIMIT = 25
MAX_RESULT_LIMIT = 200


class EbayMarketplaceConnector(MarketplaceConnector):
    """Searches active eBay listings via the eBay Buy Browse API.

    Reads credentials from whatever it's constructed with - the connector
    registry wires these from `settings.ebay_app_id` / `settings.ebay_cert_id`
    (i.e. `EBAY_APP_ID`/`EBAY_CERT_ID`), never hard-coded. If either is
    missing, `search()` raises `MarketplaceConnectorError` (via the token
    manager) rather than attempting a request.
    """

    def __init__(
        self,
        app_id: str | None,
        cert_id: str | None,
        timeout: float = 10.0,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> None:
        self._token_manager = EbayTokenManager(app_id=app_id, cert_id=cert_id, timeout=timeout)
        self._timeout = timeout
        self._result_limit = max(1, min(result_limit, MAX_RESULT_LIMIT))

    @property
    def marketplace_name(self) -> str:
        return "ebay"

    @property
    def is_configured(self) -> bool:
        return self._token_manager.is_configured

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        logger.info("eBay search started (query=%r)", query)
        body = self._fetch_results_page(query, offset=0)

        if "itemSummaries" not in body:
            # eBay omits this key entirely when there are zero matching
            # results, rather than returning an empty array - not a
            # malformed response.
            logger.info("eBay search returned 0 results")
            return []

        item_summaries = body["itemSummaries"]
        if not isinstance(item_summaries, list):
            logger.error("eBay API response's itemSummaries was not a list")
            raise MarketplaceConnectorError("eBay API returned a malformed response")

        listings: list[Listing] = []
        for raw_listing in item_summaries:
            try:
                listings.append(self.normalize_listing(raw_listing))
            except (KeyError, TypeError, ValueError, ValidationError):
                logger.error("Skipping a malformed eBay listing in the response")
                continue

        logger.info("eBay search returned %d result(s)", len(listings))
        return listings

    def _fetch_results_page(self, query: str, offset: int) -> dict[str, Any]:
        """Fetch one page of results. Not looped over multiple pages yet -
        `offset` exists so a future change can add pagination without
        touching the request/response handling below."""
        # May raise MarketplaceConnectorError (missing credentials, OAuth
        # request failure) - propagates unchanged, same as any other
        # failure here.
        token = self._token_manager.get_token()

        params: dict[str, Any] = {
            "q": query,
            "limit": self._result_limit,
            "offset": offset,
            "fieldgroups": "EXTENDED",  # includes shortDescription when available
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": _MARKETPLACE_ID,
        }

        try:
            response = httpx.get(_SEARCH_URL, params=params, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.error("eBay API request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("eBay API request failed") from None

        if response.status_code in (401, 403):
            logger.error(
                "eBay API returned HTTP %s - invalidating cached token", response.status_code
            )
            self._token_manager.invalidate()
            raise MarketplaceConnectorError(f"eBay API returned HTTP {response.status_code}")

        if response.status_code == 429:
            logger.error("eBay API rate limit exceeded (HTTP 429)")
            raise MarketplaceConnectorError("eBay API rate limit exceeded (HTTP 429)")

        if response.status_code != 200:
            logger.error("eBay API returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(f"eBay API returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError:
            logger.error("eBay API returned a non-JSON response")
            raise MarketplaceConnectorError("eBay API returned a malformed response") from None

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        price, currency = self._parse_price(raw_listing.get("price"))

        created_at = None
        created_at_raw = raw_listing.get("itemCreationDate")
        if created_at_raw:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))

        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=str(raw_listing["itemId"]),
            title=raw_listing["title"],
            description=raw_listing.get("shortDescription"),
            price=price,
            currency=currency,
            location=self._parse_location(raw_listing.get("itemLocation")),
            seller=self._parse_seller(raw_listing.get("seller")),
            condition=raw_listing.get("condition"),
            listing_url=raw_listing["itemWebUrl"],
            image_url=self._parse_image_url(raw_listing.get("image")),
            created_at=created_at,
        )

    @staticmethod
    def _parse_price(price_obj: Any) -> tuple[float | None, str | None]:
        if not isinstance(price_obj, dict):
            return None, None
        value = price_obj.get("value")
        currency = price_obj.get("currency")
        if value is None:
            return None, currency
        return float(value), currency  # eBay sends value as a decimal string, e.g. "123.45"

    @staticmethod
    def _parse_image_url(image_obj: Any) -> str | None:
        if not isinstance(image_obj, dict):
            return None
        return image_obj.get("imageUrl")

    @staticmethod
    def _parse_location(location_obj: Any) -> str | None:
        if not isinstance(location_obj, dict):
            return None
        parts = [location_obj.get(key) for key in ("city", "stateOrProvince", "country")]
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else None

    @staticmethod
    def _parse_seller(seller_obj: Any) -> str | None:
        if not isinstance(seller_obj, dict):
            return None
        return seller_obj.get("username")

    def health_check(self) -> bool:
        return self.is_configured
