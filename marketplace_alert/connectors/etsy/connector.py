"""Etsy marketplace connector - Etsy Open API v3 only, no HTML scraping.

Endpoint used: ``GET https://api.etsy.com/v3/application/listings/active``
(operation ``findAllListingsActive``) - searches active listings
marketplace-wide by keyword. Verified before implementation against Etsy's
own sources, not guessed:
  - the official generated OpenAPI v3 spec
    (https://www.etsy.com/openapi/generated/oas/3.0.0.json), which confirms
    the path, the `keywords`/`limit`/`offset`/`includes` query parameters,
    and that this specific operation's security requirement is the global
    `api_key` scheme only (no OAuth scopes attached to it);
  - the official Quickstart tutorial
    (https://developers.etsy.com/documentation/tutorials/quickstart/),
    whose literal code examples confirm the `x-api-key` header format;
  - the official Definitions page
    (https://developers.etsy.com/documentation/essentials/definitions/),
    which confirms the `money` object's `amount`/`divisor`/`currency_code`
    fields (actual price = amount / divisor - not a plain number).

Authentication: every request needs an `x-api-key` header of the form
``"<ETSY_API_KEY>:<ETSY_SHARED_SECRET>"`` (keystring and shared secret,
colon-separated) - confirmed directly from Etsy's own quickstart code
samples. No OAuth user-authorization flow is required for this endpoint;
it is a public, read-only, marketplace-wide search. The shared secret *is*
still required even though this is a plain read - it's part of the
x-api-key value itself, not something only needed for OAuth.

Transient failures (HTTP 429/502/503/504) are retried with bounded
exponential backoff via `core/connectors/retry.py`'s shared
`request_with_retry` - see that module's docstring for the exact policy.
Every other failure (network error, timeout, a permanent 4xx, malformed
JSON) still fails on the first attempt, exactly as before.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.connectors.retry import request_with_retry
from marketplace_alert.core.models.listing import Listing

logger = logging.getLogger(__name__)

_ETSY_API_BASE = "https://api.etsy.com/v3"
_ACTIVE_LISTINGS_PATH = "/application/listings/active"

# Etsy's own maximum is 100 per page; this connector fetches a single page
# (no automatic multi-page looping yet), so keep the default conservative.
DEFAULT_RESULT_LIMIT = 25
MAX_RESULT_LIMIT = 100


class EtsyMarketplaceConnector(MarketplaceConnector):
    """Searches active Etsy listings via Etsy Open API v3.

    Reads credentials from whatever it's constructed with - the connector
    registry wires these from `settings.etsy_api_key` /
    `settings.etsy_shared_secret`, which come from the `ETSY_API_KEY` /
    `ETSY_SHARED_SECRET` environment variables, never hard-coded. If either
    is missing, `search()` raises `MarketplaceConnectorError` with a
    configuration message rather than attempting a request.
    """

    def __init__(
        self,
        api_key: str | None,
        shared_secret: str | None,
        timeout: float = 10.0,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> None:
        self._api_key = api_key
        self._shared_secret = shared_secret
        self._timeout = timeout
        self._result_limit = max(1, min(result_limit, MAX_RESULT_LIMIT))

    @property
    def marketplace_name(self) -> str:
        return "etsy"

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._shared_secret)

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        if not self.is_configured:
            raise MarketplaceConnectorError(
                "Etsy connector is not configured: ETSY_API_KEY and/or "
                "ETSY_SHARED_SECRET are not set"
            )

        body = self._fetch_results_page(query, filters, offset=0)

        results = body.get("results")
        if not isinstance(results, list):
            logger.error("Etsy API response was missing a 'results' list")
            raise MarketplaceConnectorError("Etsy API returned a malformed response")

        listings: list[Listing] = []
        for raw_listing in results:
            try:
                listings.append(self.normalize_listing(raw_listing))
            except (KeyError, TypeError, ValueError, ValidationError):
                logger.error("Skipping a malformed Etsy listing in the response")
                continue
        return listings

    def _fetch_results_page(
        self, query: str, filters: dict[str, Any] | None, offset: int
    ) -> dict[str, Any]:
        """Fetch one page of results. Not looped over multiple pages yet -
        `offset` exists so a future change can add pagination without
        touching the request/response handling below."""
        filters = filters or {}
        params: dict[str, Any] = {
            "keywords": query,
            "limit": self._result_limit,
            "offset": offset,
            "includes": "Images",
        }
        if "min_price" in filters:
            params["min_price"] = filters["min_price"]
        if "max_price" in filters:
            params["max_price"] = filters["max_price"]

        headers = {"x-api-key": f"{self._api_key}:{self._shared_secret}"}

        try:
            response = request_with_retry(
                lambda: httpx.get(
                    f"{_ETSY_API_BASE}{_ACTIVE_LISTINGS_PATH}",
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                ),
                marketplace_name="Etsy",
            )
        except httpx.HTTPError as exc:
            logger.error("Etsy API request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("Etsy API request failed") from None

        if response.status_code != 200:
            logger.error("Etsy API returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(f"Etsy API returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError:
            logger.error("Etsy API returned a non-JSON response")
            raise MarketplaceConnectorError("Etsy API returned a malformed response") from None

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        price, currency = self._parse_price(raw_listing.get("price"))

        created_at = None
        created_ts = raw_listing.get("original_creation_timestamp")
        if created_ts is not None:
            created_at = datetime.fromtimestamp(created_ts, tz=timezone.utc)

        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=str(raw_listing["listing_id"]),
            title=raw_listing["title"],
            description=raw_listing.get("description"),
            price=price,
            currency=currency,
            location=None,
            seller=None,
            condition=None,
            listing_url=raw_listing["url"],
            image_url=self._parse_image_url(raw_listing.get("images")),
            created_at=created_at,
        )

    @staticmethod
    def _parse_price(price_obj: Any) -> tuple[float | None, str | None]:
        if not isinstance(price_obj, dict):
            return None, None
        amount = price_obj.get("amount")
        divisor = price_obj.get("divisor")
        currency = price_obj.get("currency_code")
        if amount is None or not divisor:
            return None, currency
        return amount / divisor, currency

    @staticmethod
    def _parse_image_url(images: Any) -> str | None:
        if not images:
            return None
        first_image = images[0]
        if not isinstance(first_image, dict):
            return None
        return first_image.get("url_570xN") or first_image.get("url_fullxfull")

    def health_check(self) -> bool:
        return self.is_configured
