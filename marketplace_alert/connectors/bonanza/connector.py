"""Bonanza marketplace connector - Bonanza's "Bonapitit" API, no HTML
scraping. Bonanza is a general (eBay-like) US marketplace; its public API
was deliberately modeled on eBay's own (now-deprecated) Finding API, down
to reusing the same operation name (`findItemsByKeywords`) and response
shape - Bonanza was founded by former eBay/Amazon engineers specifically
to make migrating an eBay integration straightforward.

Endpoint used: ``POST https://api.bonanza.com/api_requests/standard_request``
with the operation name and JSON parameters sent as a single form-encoded
field (see `_fetch_results_page` for exactly why - this is Bonanza's own,
somewhat unusual wire format, not this connector's choice). Verified
before implementation from two independent sources (see below);
`findItemsByKeywords`'s own field-level documentation was not fully
extractable during research (see "What is NOT independently confirmed"),
so this connector - like the Reverb one - is written defensively for the
fields that couldn't be pinned down with full confidence.

1. **The exact wire protocol** (base URL, that the request is a `POST`
   with the call name and a JSON-serialized payload combined into one
   form-encoded body field, and the `X-BONANZLE-API-DEV-NAME` header) was
   confirmed directly from a real, working third-party PHP SDK's HTTP
   client source
   (https://github.com/Shoplo/bonapitit-bonanza-php-sdk/blob/master/src/Shoplo/BonanzaApi/Client/BonanzaClient.php) -
   not guessed. That SDK doesn't implement `findItemsByKeywords` itself
   (it's built around seller-side operations - booths, orders, listing
   management), so it doesn't confirm this specific call's exact
   response, only the transport envelope every call shares.
2. **`findItemsByKeywords`'s parameters and response shape** (`keywords`,
   `paginationInput.pageNumber`/`entriesPerPage` - both max 100 -
   `sortOrder`, and a `findItemsByKeywordsResponse.searchResult.item[]`
   array with `itemId`/`title`/`viewItemURL`/`galleryURL`/
   `sellingStatus.currentPrice`/`sellerInfo.sellerUserName`/
   `listingInfo.startTime`/`location`/`country`) came from Bonanza's own
   official API reference documentation
   (https://api.bonanza.com/docs/reference/find_items_by_keywords) - this
   is the intentionally eBay-Finding-API-compatible shape Bonanza
   publishes, not a guess, but the auto-converted doc page didn't
   preserve a byte-exact example response the way Reverb's docs did.

**What was NOT independently confirmed before 2026-09-27** (same
honest-uncertainty approach as the Reverb connector, see its module
docstring): whether `sellingStatus.currentPrice` is a bare number or -
matching a well-known quirk of eBay's own (XML-derived) Finding API,
which Bonanza explicitly copied - a `{"__value__": ..., "@currencyId":
...}` value/attribute object; whether a `condition` field is present at
all for a given item; and, most importantly, whether the item list is
really nested at `findItemsByKeywordsResponse.searchResult.item[]` as
the auto-converted docs page implied. `normalize_listing` tries the
historically-eBay-Finding-API-typical shape first, then a plain-number
fallback, landing on `None` - never an invented value - if neither
matches. A wrong guess about one field's exact shape only ever means
that field is `null` for a listing, never fabricated data.

**Verified against a real live response, 2026-09-27 - and two of the
above guesses were wrong.** The first live `findItemsByKeywords` call
(after the Bonapitit developer account became active) returned real
listings, but this connector's `search()` still returned `[]` for it -
a real production bug, not a hypothetical one. Root cause, confirmed
directly from the saved raw response: (1) `ack` is a sibling of
`findItemsByKeywordsResponse` at the response body's own top level, not
nested inside it as `search()` assumed; (2) the item list lives directly
at `findItemsByKeywordsResponse.item[]` - there is no `searchResult`
wrapper at all in the real API. Both are fixed in `search()`/
`_extract_raw_listings()` below (the `searchResult.item` shape is kept
as a defensive fallback only, never tried first anymore). Also newly
confirmed from that same response: `sellingStatus.currentPrice` was a
plain numeric string with no currency field anywhere on the item
(`currency` correctly lands on `None` for such a listing - never
guessed); no `condition` key was present; no `country` key was present
alongside `location`; and a real `descriptionBrief` field exists and is
now mapped into `Listing.description` (previously hard-coded to `None`
under the unverified assumption that no description is ever available
from search results). Everything else in this docstring (wire protocol,
parameter names, `itemId`/`title`/`viewItemURL`/`galleryURL`/
`sellerInfo.sellerUserName`/`listingInfo.startTime`) matched the real
response exactly, unchanged.

Authentication: a single developer name (`BONANZA_DEV_NAME`, Bonanza's
own `X-BONANZLE-API-DEV-NAME` header), obtained by registering a
developer account at https://api.bonanza.com/accounts/new. This
connector only ever makes the "non-secure" class of Bonanza API call
(read-only search) - it never sends a Certificate ID, since that's only
required for secure calls that act on a specific user's own account
(managing their own listings/orders), which this connector never does.

Transient failures (HTTP 429/502/503/504) on any one page request ARE
retried, with bounded exponential backoff, via
`core/connectors/retry.py`'s shared `request_with_retry`. 401/403 and any
other permanent failure are unaffected, exactly as before.

**No `get_listing_by_id()` override - deliberately.** The historical
listing-metadata backfill service (`core/persistence/backfill.py`) needs
a documented, authoritative single-item lookup to safely re-fetch an old
listing; no such endpoint for Bonanza's Bonapitit API was found with
enough confidence during research (eBay's old Finding API, which
Bonanza's is modeled on, did have a `GetSingleItem` call, but Bonanza's
own docs don't confirm an equivalent) - guessing at an unconfirmed
endpoint would risk silently doing the wrong thing rather than cleanly
failing. This connector simply doesn't override the base class's
default, which raises `ListingLookupNotSupportedError` - the backfill
service treats that as "skip this marketplace, log why," never a
connector bug. Moot in practice today anyway: Bonanza has never been
live (still awaiting `BONANZA_DEV_NAME`), so there are no Bonanza rows
to backfill yet.
"""

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.connectors.retry import request_with_retry
from marketplace_alert.core.models.listing import Listing

logger = logging.getLogger(__name__)

_BONANZA_API_URL = "https://api.bonanza.com/api_requests/standard_request"
_DEV_NAME_HEADER = "X-BONANZLE-API-DEV-NAME"

# Bonanza's own documented maximum entriesPerPage is 100; this connector
# paginates (via pageNumber, since Bonanza's API - unlike Reverb's HAL
# style - doesn't publish `_links`), bounded by both a configurable
# result_limit and a hard MAX_PAGES safety cap - same reasoning as the
# Reverb connector's pagination safety net.
DEFAULT_RESULT_LIMIT = 25
MAX_RESULT_LIMIT = 100
_ENTRIES_PER_PAGE_CAP = 100
MAX_PAGES = 5


class BonanzaMarketplaceConnector(MarketplaceConnector):
    """Searches active Bonanza listings via the Bonapitit API.

    Reads its dev name from whatever it's constructed with - the
    connector registry wires this from `settings.bonanza_dev_name` (the
    `BONANZA_DEV_NAME` environment variable), never hard-coded. If it's
    missing, `search()` raises `MarketplaceConnectorError` with a
    configuration message rather than attempting a request - startup,
    and every other connector, is unaffected either way.
    """

    def __init__(
        self,
        dev_name: str | None,
        timeout: float = 10.0,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> None:
        self._dev_name = dev_name
        self._timeout = timeout
        self._result_limit = max(1, min(result_limit, MAX_RESULT_LIMIT))
        self._entries_per_page = min(self._result_limit, _ENTRIES_PER_PAGE_CAP)

    @property
    def marketplace_name(self) -> str:
        return "bonanza"

    @property
    def is_configured(self) -> bool:
        return bool(self._dev_name)

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        if not self.is_configured:
            raise MarketplaceConnectorError(
                "Bonanza connector is not configured: BONANZA_DEV_NAME is not set"
            )

        listings: list[Listing] = []
        page_number = 1

        while page_number <= MAX_PAGES and len(listings) < self._result_limit:
            body = self._fetch_results_page(query, page_number)

            # Confirmed via a real live response (2026-09-27): `ack` is a
            # sibling of `findItemsByKeywordsResponse`, at the response
            # body's own top level - NOT nested inside it, as this code
            # previously assumed. That earlier assumption meant a real
            # `ack != "Success"` was never actually seen by this check at
            # all (see below for why that mattered).
            ack = body.get("ack")
            if ack == "Failure":
                if not listings:
                    logger.error("Bonanza API reported ack=Failure")
                    raise MarketplaceConnectorError("Bonanza API reported a failure for this search")
                break

            envelope = body.get("findItemsByKeywordsResponse")
            if not isinstance(envelope, dict):
                if not listings:
                    logger.error("Bonanza API response was missing findItemsByKeywordsResponse")
                    raise MarketplaceConnectorError("Bonanza API returned a malformed response")
                break

            raw_listings = self._extract_raw_listings(envelope)
            if raw_listings is None:
                # Bonanza (like eBay's Finding API it mirrors) may omit
                # the `item` key entirely for a zero-result search rather
                # than returning an empty list - but that's only a safe
                # assumption when `ack` is a *confirmed* "Success". This
                # is the actual root-cause fix for a real bug: before it,
                # ANY response reaching this point with no recognized
                # item list silently became `[]`, regardless of what (or
                # whether) `ack` said - which is exactly how a real,
                # confirmed-live response silently returned zero results.
                # An `ack` that is missing or anything other than
                # "Success" combined with no item list at all is an
                # unexpected response shape, not a legitimate empty
                # search, and must not be swallowed the same way.
                if ack != "Success":
                    if not listings:
                        logger.error(
                            "Bonanza API response had no recognized item list and ack was %r (not 'Success')",
                            ack,
                        )
                        raise MarketplaceConnectorError("Bonanza API returned an unexpected response shape")
                break
            if not isinstance(raw_listings, list):
                if not listings:
                    logger.error("Bonanza API response's item list was not a list")
                    raise MarketplaceConnectorError("Bonanza API returned a malformed response")
                break

            for raw_listing in raw_listings:
                try:
                    listings.append(self.normalize_listing(raw_listing))
                except (KeyError, TypeError, ValueError, ValidationError):
                    logger.error("Skipping a malformed Bonanza listing in the response")
                    continue
                if len(listings) >= self._result_limit:
                    break

            if len(raw_listings) < self._entries_per_page:
                break  # short page - no reason to believe another page has more
            page_number += 1

        return listings[: self._result_limit]

    def _fetch_results_page(self, query: str, page_number: int) -> dict[str, Any]:
        params = {
            "keywords": query,
            "paginationInput": {"pageNumber": page_number, "entriesPerPage": self._entries_per_page},
            "sortOrder": "BestMatch",
        }
        headers = {
            _DEV_NAME_HEADER: self._dev_name,
            "Accept": "application/json",
        }
        # Bonanza's own wire format (confirmed from a real SDK's HTTP
        # client, see module docstring): one form-encoded field, keyed by
        # the (lower-first-letter) operation name, whose value is the
        # JSON-serialized parameters - never a JSON request body.
        form_body = {"findItemsByKeywords": json.dumps(params)}

        try:
            response = request_with_retry(
                lambda: httpx.post(_BONANZA_API_URL, data=form_body, headers=headers, timeout=self._timeout),
                marketplace_name="Bonanza",
            )
        except httpx.HTTPError as exc:
            logger.error("Bonanza API request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("Bonanza API request failed") from None

        if response.status_code in (401, 403):
            logger.error("Bonanza API returned HTTP %s - dev name missing, invalid, or revoked", response.status_code)
            raise MarketplaceConnectorError(
                f"Bonanza API returned HTTP {response.status_code} (check BONANZA_DEV_NAME)"
            )
        if response.status_code == 429:
            logger.error("Bonanza API rate limit exceeded (HTTP 429)")
            raise MarketplaceConnectorError("Bonanza API rate limit exceeded (HTTP 429)")
        if response.status_code != 200:
            logger.error("Bonanza API returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(f"Bonanza API returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError:
            logger.error("Bonanza API returned a non-JSON response")
            raise MarketplaceConnectorError("Bonanza API returned a malformed response") from None

    @staticmethod
    def _extract_raw_listings(envelope: dict[str, Any]) -> Any:
        """Where the item list actually lives. Confirmed via a real live
        response (2026-09-27, see PROJECT_CONTEXT.md): directly under the
        envelope (`findItemsByKeywordsResponse.item`) - NOT nested one
        level deeper under `searchResult`, as Bonanza's own auto-converted
        API reference page had suggested and this connector originally
        assumed (see this module's docstring - that assumption was never
        byte-exact-confirmed until now, and was wrong; it's what caused a
        real, live, non-empty search to silently return `[]`). The
        originally-assumed `searchResult.item` shape is kept as a
        defensive fallback only, never tried first anymore, in case a
        differently-configured account or a future response variant
        nests results that way.
        """
        if "item" in envelope:
            return envelope.get("item")
        search_result = envelope.get("searchResult")
        if isinstance(search_result, dict):
            return search_result.get("item")
        return None

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        price, currency = self._parse_price(raw_listing.get("sellingStatus"))

        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=str(raw_listing["itemId"]),
            title=raw_listing["title"],
            # Confirmed present in a real live response (2026-09-27) as
            # `descriptionBrief` - this connector's original assumption
            # that no description is ever available from search results
            # was wrong; it had never actually been checked against a
            # live response until now.
            description=raw_listing.get("descriptionBrief"),
            price=price,
            currency=currency,
            location=self._parse_location(raw_listing),
            seller=self._parse_seller(raw_listing.get("sellerInfo")),
            condition=self._parse_condition(raw_listing),
            listing_url=raw_listing["viewItemURL"],
            image_url=self._parse_image_url(raw_listing),
            created_at=self._parse_created_at(raw_listing.get("listingInfo")),
        )

    @staticmethod
    def _parse_price(selling_status: Any) -> tuple[float | None, str | None]:
        if not isinstance(selling_status, dict):
            return None, None
        current_price = selling_status.get("currentPrice")
        if isinstance(current_price, dict):
            # The eBay-Finding-API-style value/attribute shape Bonanza's
            # API is modeled on - see module docstring.
            value = current_price.get("__value__")
            currency = current_price.get("@currencyId")
            if value is not None:
                try:
                    return float(value), currency
                except (TypeError, ValueError):
                    return None, currency
            return None, currency
        if isinstance(current_price, (int, float, str)):
            try:
                return float(current_price), selling_status.get("currencyId")
            except (TypeError, ValueError):
                return None, None
        return None, None

    @staticmethod
    def _parse_condition(raw_listing: dict[str, Any]) -> str | None:
        condition = raw_listing.get("condition")
        if isinstance(condition, dict):
            return condition.get("conditionDisplayName") or condition.get("displayName")
        if isinstance(condition, str) and condition:
            return condition
        return None

    @staticmethod
    def _parse_seller(seller_info: Any) -> str | None:
        if not isinstance(seller_info, dict):
            return None
        username = seller_info.get("sellerUserName")
        return username if isinstance(username, str) and username else None

    @staticmethod
    def _parse_location(raw_listing: dict[str, Any]) -> str | None:
        parts = [raw_listing.get("location"), raw_listing.get("country")]
        parts = [part for part in parts if isinstance(part, str) and part]
        return ", ".join(dict.fromkeys(parts)) if parts else None

    @staticmethod
    def _parse_image_url(raw_listing: dict[str, Any]) -> str | None:
        gallery_url = raw_listing.get("galleryURL")
        return gallery_url if isinstance(gallery_url, str) and gallery_url else None

    @staticmethod
    def _parse_created_at(listing_info: Any) -> datetime | None:
        if not isinstance(listing_info, dict):
            return None
        start_time = listing_info.get("startTime")
        if not isinstance(start_time, str) or not start_time:
            return None
        try:
            return datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except ValueError:
            return None

    def health_check(self) -> bool:
        return self.is_configured
