"""Tradera marketplace connector - Tradera REST API v4 (Sweden's largest
general online marketplace), no HTML scraping, no legacy SOAP API.

Endpoint used: ``GET https://api.tradera.com/v4/search``. Verified before
implementation from two independent, mutually-corroborating sources:

1. **A real live request**, made directly against the production API with
   real `TRADERA_APP_ID`/`TRADERA_APP_KEY` credentials (`?query=Bosch`) -
   the response's exact field set for a search result item was inspected
   directly, not assumed. This is the source of truth for every field
   mapping below.
2. **A real, published, independently-verified TypeScript client**
   (https://github.com/ekdennisek/tradera-api-client), whose response
   schemas are explicitly annotated by its own author as "verified against
   the live API." It confirmed the base URL (`https://api.tradera.com/v4/`),
   the exact endpoint path/query parameter name (`GET search?query=...`),
   and the `X-App-Id`/`X-App-Key` header names - and its understanding
   matched the live request in point 1 exactly (down to the base URL and
   the top-level `errors` array), which is what makes it trustworthy here.

**No `title` field exists on a Tradera search result at all** - what every
other connector in this project calls `title`, Tradera's API calls
`shortDescription` (confirmed live: for a real Bosch listing, this held
the listing's actual name/headline, not a summary). `longDescription` is
the separate, full listing description. Neither mapping is guessed - both
were directly observed on a real response.

Authentication: two static, non-expiring app-level credentials
(`TRADERA_APP_ID`/`TRADERA_APP_KEY`), sent as the `X-App-Id`/`X-App-Key`
headers - no OAuth flow, no per-user token, unlike eBay's client-
credentials flow. Search requires only these two headers; other Tradera
v4 endpoints (selling, orders, BankID) additionally require a per-user
token this connector never needs and never requests.

**Price is a genuine point-in-time value for an auction listing, unlike
every other connector's `price`.** A Tradera search result only exposes
`buyItNowPrice` when the listing is a fixed-price ("Buy It Now") item;
plain auctions (confirmed live: a real Bosch auction listing had bids,
`maxBid`, `nextBid`, `bidCount`, and no `buyItNowPrice` at all) instead
expose the *current bidding state*. `_parse_price` below picks, in order:
`buyItNowPrice` (a real fixed price) > `maxBid` (the current highest bid,
only used once `hasBids` is true) > `nextBid` (the minimum bid that would
currently win, for a not-yet-bid-on auction) > `None`. Whichever of these
is used is genuinely the best available "what would I pay right now"
figure, but - unlike every other marketplace this project supports - it
can change from one search to the next for the exact same listing; this
is a deliberate, documented tradeoff, not an oversight.

**Currency is never present in the search response at all** - confirmed,
not just absent from the one inspected listing. Tradera is a Swedish
marketplace with no multi-currency listings, so `"SEK"` is used as a
fixed marketplace-level default for every listing, rather than left
`None` the way an unconfirmed-but-plausible field would be everywhere
else in this project - this is a deliberate, narrow exception, made
because the *marketplace itself* is single-currency, not because any one
listing's currency was individually confirmed.

**`condition`, `location`, and `source_created_at` are not available from
search results and are always `None`.** Confirmed absent from the search
response schema entirely (not merely missing on the one inspected
listing) - there is no marketplace-wide condition/location concept
exposed by `/v4/search`, and the only date-like field present, `endDate`,
is the auction's *end* time, not its creation time - mapping it to
`source_created_at` would misrepresent it, so it is never used for that
purpose anywhere in this connector.

Transient failures (HTTP 429/502/503/504) are retried with bounded
exponential backoff via `core/connectors/retry.py`'s shared
`request_with_retry`. Tradera's own documented rate limit (10,000 calls
per 24 hours, per method - generous relative to every other connector
this project has) needs no special handling beyond that shared retry
policy; a 429 is retried exactly like any other connector's.

**A non-empty top-level `errors` array is treated as a failed search even
on HTTP 200** - the same lesson this project already learned once from
the Bonanza connector's `ack` field: an API that can report failure
in-band, inside an otherwise-200 response, must have that path checked
explicitly, never silently ignored.

**No page-size parameter for this endpoint.** `GET /v4/search`'s own
query parameters (confirmed via the client above) are just `query`,
`categoryId`, `pageNumber`, `orderBy` - there is no `itemsPerPage`-style
control (that only exists on the separate `POST /v4/search/advanced`).
This connector therefore fetches a single page and keeps at most
`result_limit` of whatever it returns - the same pattern the eBay/Etsy
connectors already use for the same reason.

**No `get_listing_by_id()` override in this phase, deliberately - out of
scope, not because Tradera lacks the capability.** Tradera's
`GET /v4/items/{itemId}` is confirmed to exist and to need only the same
two app-level headers this connector already sends, and would likely
return richer data (a start date, a seller location) than search results
ever do - unlike Bonanza, where no such endpoint could be confirmed at
all. Left for a later phase; this connector simply doesn't override the
base class's default, which raises `ListingLookupNotSupportedError`.
"""

import logging
from typing import Any

import httpx
from pydantic import ValidationError

from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.connectors.retry import request_with_retry
from marketplace_alert.core.models.listing import Listing

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.tradera.com/v4/search"

# Tradera's own marketplace-level currency - never present as a field on
# any search result (see module docstring's "Currency is never present"
# section).
_DEFAULT_CURRENCY = "SEK"

# No documented page-size parameter for this endpoint (see module
# docstring) - this connector fetches a single page and keeps at most
# this many normalized listings from it.
DEFAULT_RESULT_LIMIT = 25
MAX_RESULT_LIMIT = 100


class TraderaMarketplaceConnector(MarketplaceConnector):
    """Searches active Tradera listings via the Tradera REST API v4.

    Reads credentials from whatever it's constructed with - the connector
    registry wires these from `settings.tradera_app_id` /
    `settings.tradera_app_key` (i.e. `TRADERA_APP_ID`/`TRADERA_APP_KEY`),
    never hard-coded. If either is missing, `search()` raises
    `MarketplaceConnectorError` with a configuration message rather than
    attempting a request.
    """

    def __init__(
        self,
        app_id: str | None,
        app_key: str | None,
        timeout: float = 10.0,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._timeout = timeout
        self._result_limit = max(1, min(result_limit, MAX_RESULT_LIMIT))

    @property
    def marketplace_name(self) -> str:
        return "tradera"

    @property
    def is_configured(self) -> bool:
        return bool(self._app_id) and bool(self._app_key)

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        if not self.is_configured:
            raise MarketplaceConnectorError(
                "Tradera connector is not configured: TRADERA_APP_ID and/or "
                "TRADERA_APP_KEY are not set"
            )

        body = self._fetch_results(query)

        # A non-empty top-level `errors` array is a failed search, even on
        # HTTP 200 - see module docstring.
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            logger.error("Tradera API reported errors for this search: %r", errors)
            raise MarketplaceConnectorError("Tradera API reported a failure for this search")

        items = body.get("items")
        if items is None:
            # Tradera may omit/null the `items` key entirely for a
            # zero-result search rather than returning an empty array.
            return []
        if not isinstance(items, list):
            logger.error("Tradera API response's items was not a list")
            raise MarketplaceConnectorError("Tradera API returned a malformed response")

        listings: list[Listing] = []
        for raw_listing in items:
            try:
                listings.append(self.normalize_listing(raw_listing))
            except (KeyError, TypeError, ValueError, ValidationError):
                logger.error("Skipping a malformed Tradera listing in the response")
                continue
            if len(listings) >= self._result_limit:
                break

        return listings

    def _fetch_results(self, query: str) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query}
        headers = {
            "X-App-Id": self._app_id,
            "X-App-Key": self._app_key,
            "Accept": "application/json",
        }

        try:
            response = request_with_retry(
                lambda: httpx.get(_SEARCH_URL, params=params, headers=headers, timeout=self._timeout),
                marketplace_name="Tradera",
            )
        except httpx.HTTPError as exc:
            logger.error("Tradera API request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("Tradera API request failed") from None

        if response.status_code in (401, 403):
            logger.error(
                "Tradera API returned HTTP %s - app id/key missing, invalid, or revoked",
                response.status_code,
            )
            raise MarketplaceConnectorError(
                f"Tradera API returned HTTP {response.status_code} (check TRADERA_APP_ID/TRADERA_APP_KEY)"
            )
        if response.status_code == 429:
            logger.error("Tradera API rate limit exceeded (HTTP 429)")
            raise MarketplaceConnectorError("Tradera API rate limit exceeded (HTTP 429)")
        if response.status_code != 200:
            logger.error("Tradera API returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(f"Tradera API returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError:
            logger.error("Tradera API returned a non-JSON response")
            raise MarketplaceConnectorError("Tradera API returned a malformed response") from None

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        # `shortDescription` is this API's title field - see module
        # docstring. No usable value means no usable listing: raising
        # here (rather than inventing a placeholder) is what lets
        # `search()`'s per-item try/except cleanly skip just this one
        # listing, exactly like every other connector's required-field
        # handling.
        title = raw_listing.get("shortDescription")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Tradera listing has no usable shortDescription (title)")

        # Never invent a fallback URL - see module docstring.
        listing_url = raw_listing.get("itemUrl")
        if not isinstance(listing_url, str) or not listing_url.strip():
            raise ValueError("Tradera listing has no usable itemUrl")

        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=str(raw_listing["id"]),
            title=title,
            description=self._parse_description(raw_listing),
            price=self._parse_price(raw_listing),
            currency=_DEFAULT_CURRENCY,
            location=None,
            seller=self._parse_seller(raw_listing),
            condition=None,
            listing_url=listing_url,
            image_url=self._parse_image_url(raw_listing),
            created_at=None,
        )

    @staticmethod
    def _parse_description(raw_listing: dict[str, Any]) -> str | None:
        long_description = raw_listing.get("longDescription")
        return long_description if isinstance(long_description, str) and long_description else None

    @staticmethod
    def _parse_seller(raw_listing: dict[str, Any]) -> str | None:
        seller_alias = raw_listing.get("sellerAlias")
        return seller_alias if isinstance(seller_alias, str) and seller_alias else None

    @staticmethod
    def _parse_price(raw_listing: dict[str, Any]) -> float | None:
        """See module docstring's "Price is a genuine point-in-time
        value" section for the full reasoning behind this exact
        precedence order: a real fixed price beats a live bidding state,
        and the current high bid (once one exists) beats the minimum
        bid that would currently win.
        """
        buy_it_now_price = raw_listing.get("buyItNowPrice")
        if isinstance(buy_it_now_price, (int, float)):
            return float(buy_it_now_price)

        if raw_listing.get("hasBids"):
            max_bid = raw_listing.get("maxBid")
            if isinstance(max_bid, (int, float)):
                return float(max_bid)

        next_bid = raw_listing.get("nextBid")
        if isinstance(next_bid, (int, float)):
            return float(next_bid)

        return None

    @staticmethod
    def _parse_image_url(raw_listing: dict[str, Any]) -> str | None:
        thumbnail_link = raw_listing.get("thumbnailLink")
        if isinstance(thumbnail_link, str) and thumbnail_link:
            return thumbnail_link

        image_links = raw_listing.get("imageLinks")
        if isinstance(image_links, list):
            for image_link in image_links:
                if isinstance(image_link, dict):
                    url = image_link.get("url")
                    if isinstance(url, str) and url:
                        return url
        return None

    def health_check(self) -> bool:
        return self.is_configured
