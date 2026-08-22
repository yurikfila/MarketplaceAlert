"""Reverb marketplace connector - Reverb API v3 (musical instruments and
gear marketplace), no HTML scraping.

Endpoint used: ``GET https://api.reverb.com/api/listings``. Verified
before implementation against three independent sources:

1. **The developer's own manually-verified request** - a real personal
   access token (``public`` scope), ``GET
   https://api.reverb.com/api/listings?query=Fender&per_page=1`` with
   ``Authorization: Bearer <token>``, ``Accept: application/hal+json``,
   ``Accept-Version: 3.0`` - returned a real listing successfully. This is
   the source of truth for the auth headers and the ``query``/``per_page``
   parameter names used below.
2. **Reverb's own official API docs**
   (https://www.reverb-api.com/docs/getting-started), confirming the
   response is a HAL+JSON document with the listings themselves under a
   top-level ``"listings"`` array, and pagination via
   ``_links.next``/``_links.prev`` - a full ``href`` to follow verbatim,
   never hand-reconstructed. Quoting the docs' own stated design
   principle: "Clients should never construct their own URLs... follow
   the ``_links`` element."
3. **A real, working third-party Reverb API client**
   (https://github.com/jamesdlow/reverb-api-java)'s JSON-property
   constants, confirming ``_links.web.href`` is a listing's canonical web
   page URL (as opposed to ``_links.self``, the API resource URL) and
   that ``photos`` is the correct top-level key for a listing's images.

**What is NOT independently confirmed from Reverb's own published docs**
(they do not publish a complete example listing object as of this
implementation): the exact nesting of ``price``/``condition``/``shop``/
``photos`` entries. These are inferred from Reverb's own
listing-*creation* payload shape (``price: {amount, currency}``,
``condition: {uuid}`` - both from the official Create Listings docs) and
cross-referenced against real Reverb listing fields independently
documented by a third-party tool built directly against this same public
API (id, title, description, price+currency, a condition display name, a
shop name, a photo URL, the listing's web url, and a published-at
timestamp - all present in some form). Because of that,
``normalize_listing`` below is written defensively: every optional field
tries its most likely shape first, then one or two reasonable structural
fallbacks, and lands on ``None`` - never an invented value - if nothing
matches. If Reverb's actual field names turn out to differ from what's
coded here, the effect is a missing (``None``) field for that one
listing, never fabricated data.

Authentication: a single, static personal access token (created with only
the ``public`` scope, per the task this connector was built for) sent as
``Authorization: Bearer <REVERB_API_TOKEN>``. Unlike eBay's OAuth
client-credentials flow, there is no token refresh/expiry to manage here
- a 401/403 means the token is missing, wrong, or has been revoked, and is
reported as a clear, safe configuration/auth error (see
``_fetch_results_page``), never retried automatically.

Transient failures (HTTP 429/502/503/504) on any one page request ARE
retried, with bounded exponential backoff, via
`core/connectors/retry.py`'s shared `request_with_retry` - each page
fetched during pagination gets its own independent retry budget. 401/403/
404 and any other permanent failure are unaffected, exactly as before.
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from marketplace_alert.core.connectors.base import MarketplaceConnector, MarketplaceConnectorError
from marketplace_alert.core.connectors.retry import request_with_retry
from marketplace_alert.core.models.listing import Listing

logger = logging.getLogger(__name__)

_REVERB_LISTINGS_URL = "https://api.reverb.com/api/listings"
_ACCEPT_VERSION = "3.0"

# Reverb's own documented maximum per_page/result count is not published
# (see module docstring) - these are conservative, safe defaults, well
# below common REST API ceilings, chosen the same way Etsy's/eBay's
# defaults were: a small default that works out of the box, with an
# explicit configurable ceiling so a misconfigured/very large
# `result_limit` can never turn into an unbounded request.
DEFAULT_RESULT_LIMIT = 25
MAX_RESULT_LIMIT = 100
_PER_PAGE_CAP = 50

# A hard ceiling on how many pages one search() call will ever follow via
# `_links.next`, independent of `result_limit` - protects against a
# pathological response (e.g. every page reporting a `next` link but
# returning zero/near-zero listings) turning into unbounded polling of
# Reverb's API for a single saved-search scan.
MAX_PAGES = 5


class ReverbMarketplaceConnector(MarketplaceConnector):
    """Searches active Reverb listings via the Reverb API v3.

    Reads its token from whatever it's constructed with - the connector
    registry wires this from `settings.reverb_api_token` (the
    `REVERB_API_TOKEN` environment variable), never hard-coded. If it's
    missing, `search()` raises `MarketplaceConnectorError` with a
    configuration message rather than attempting a request - startup,
    and every other connector, is unaffected either way (see
    `connectors/registry.py`).
    """

    def __init__(
        self,
        api_token: str | None,
        timeout: float = 10.0,
        result_limit: int = DEFAULT_RESULT_LIMIT,
    ) -> None:
        self._api_token = api_token
        self._timeout = timeout
        self._result_limit = max(1, min(result_limit, MAX_RESULT_LIMIT))
        self._per_page = min(self._result_limit, _PER_PAGE_CAP)

    @property
    def marketplace_name(self) -> str:
        return "reverb"

    @property
    def is_configured(self) -> bool:
        return bool(self._api_token)

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        if not self.is_configured:
            raise MarketplaceConnectorError(
                "Reverb connector is not configured: REVERB_API_TOKEN is not set"
            )

        listings: list[Listing] = []
        next_href: str | None = None
        pages_fetched = 0

        while pages_fetched < MAX_PAGES and len(listings) < self._result_limit:
            if next_href is None:
                body = self._fetch_results_page(
                    _REVERB_LISTINGS_URL, params={"query": query, "per_page": self._per_page}
                )
            else:
                # HAL convention (and Reverb's own stated design
                # principle - see module docstring): follow the href
                # verbatim, never reconstruct query params for it.
                body = self._fetch_results_page(next_href, params=None)
            pages_fetched += 1

            raw_listings = body.get("listings")
            if not isinstance(raw_listings, list):
                if not listings:
                    logger.error("Reverb API response was missing a 'listings' list")
                    raise MarketplaceConnectorError("Reverb API returned a malformed response")
                # A later page being malformed just means "stop
                # paginating" - we already have good results from an
                # earlier page, no reason to discard them.
                break

            for raw_listing in raw_listings:
                try:
                    listings.append(self.normalize_listing(raw_listing))
                except (KeyError, TypeError, ValueError, ValidationError):
                    logger.error("Skipping a malformed Reverb listing in the response")
                    continue
                if len(listings) >= self._result_limit:
                    break

            next_href = self._extract_next_href(body)
            if next_href is None:
                break

        return listings[: self._result_limit]

    def _fetch_results_page(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Accept": "application/hal+json",
            "Accept-Version": _ACCEPT_VERSION,
        }

        try:
            response = request_with_retry(
                lambda: httpx.get(url, params=params, headers=headers, timeout=self._timeout),
                marketplace_name="Reverb",
            )
        except httpx.HTTPError as exc:
            logger.error("Reverb API request failed (%s)", type(exc).__name__)
            raise MarketplaceConnectorError("Reverb API request failed") from None

        self._log_rate_limit_metadata(response)

        if response.status_code in (401, 403):
            logger.error("Reverb API returned HTTP %s - token missing, invalid, or revoked", response.status_code)
            raise MarketplaceConnectorError(
                f"Reverb API returned HTTP {response.status_code} (check REVERB_API_TOKEN)"
            )
        if response.status_code == 404:
            logger.error("Reverb API returned HTTP 404")
            raise MarketplaceConnectorError("Reverb API returned HTTP 404 (not found)")
        if response.status_code == 429:
            logger.error("Reverb API rate limit exceeded (HTTP 429)")
            raise MarketplaceConnectorError("Reverb API rate limit exceeded (HTTP 429)")
        if response.status_code != 200:
            logger.error("Reverb API returned HTTP %s", response.status_code)
            raise MarketplaceConnectorError(f"Reverb API returned HTTP {response.status_code}")

        try:
            # httpx decodes the JSON body as UTF-8 (per RFC 8259, JSON is
            # always UTF-8/16/32; httpx auto-detects from the response's
            # actual bytes/charset) - titles/descriptions with accents,
            # symbols, or emoji round-trip correctly here. Any mojibake
            # seen in a terminal is that terminal's own display encoding,
            # never a corruption of the data itself - see
            # tests/test_reverb_connector.py's Unicode test.
            return response.json()
        except ValueError:
            logger.error("Reverb API returned a non-JSON response")
            raise MarketplaceConnectorError("Reverb API returned a malformed response") from None

    @staticmethod
    def _log_rate_limit_metadata(response: httpx.Response) -> None:
        """Best-effort, safe visibility into rate-limit headroom - Reverb
        does not publish exact rate-limit header names, so this checks a
        few common conventions and simply does nothing if none are
        present. Never logs the Authorization header or the token."""
        remaining = response.headers.get("X-RateLimit-Remaining") or response.headers.get("RateLimit-Remaining")
        limit = response.headers.get("X-RateLimit-Limit") or response.headers.get("RateLimit-Limit")
        if remaining is not None or limit is not None:
            logger.info("Reverb API rate limit headroom: %s/%s remaining", remaining, limit)
        retry_after = response.headers.get("Retry-After")
        if response.status_code == 429 and retry_after is not None:
            logger.info("Reverb API requested Retry-After: %s", retry_after)

    @staticmethod
    def _extract_next_href(body: dict[str, Any]) -> str | None:
        links = body.get("_links")
        if not isinstance(links, dict):
            return None
        next_link = links.get("next")
        if not isinstance(next_link, dict):
            return None
        href = next_link.get("href")
        return href if isinstance(href, str) and href else None

    def normalize_listing(self, raw_listing: dict[str, Any]) -> Listing:
        price, currency = self._parse_price(raw_listing)
        shop_name = self._parse_shop_name(raw_listing)

        return Listing(
            marketplace=self.marketplace_name,
            external_listing_id=str(raw_listing["id"]),
            title=raw_listing["title"],
            description=raw_listing.get("description"),
            price=price,
            currency=currency,
            location=self._parse_location(raw_listing),
            seller=shop_name,
            condition=self._parse_condition(raw_listing),
            listing_url=self._parse_listing_url(raw_listing),
            image_url=self._parse_image_url(raw_listing),
            created_at=self._parse_published_at(raw_listing),
        )

    @staticmethod
    def _parse_price(raw_listing: dict[str, Any]) -> tuple[float | None, str | None]:
        price_obj = raw_listing.get("price")
        if isinstance(price_obj, dict):
            amount = price_obj.get("amount")
            currency = price_obj.get("currency")
            if amount is not None:
                try:
                    return float(amount), currency
                except (TypeError, ValueError):
                    pass
            # Amount present in cents but not as a display amount - a
            # documented alternate shape for Reverb money fields.
            price_cents = raw_listing.get("price_cents")
            if isinstance(price_cents, (int, float)):
                return price_cents / 100, currency
            return None, currency

        # No structured `price` object at all - never fall back to
        # parsing a human-readable display string (e.g. "$1,419.30") when
        # no safe structured numeric field exists, per this connector's
        # own requirements.
        price_cents = raw_listing.get("price_cents")
        currency = raw_listing.get("currency")
        if isinstance(price_cents, (int, float)):
            return price_cents / 100, currency
        return None, currency

    @staticmethod
    def _parse_condition(raw_listing: dict[str, Any]) -> str | None:
        condition_obj = raw_listing.get("condition")
        if isinstance(condition_obj, dict):
            return condition_obj.get("display_name") or condition_obj.get("displayName")
        if isinstance(condition_obj, str) and condition_obj:
            return condition_obj
        return None

    @staticmethod
    def _parse_shop_name(raw_listing: dict[str, Any]) -> str | None:
        shop_obj = raw_listing.get("shop")
        if isinstance(shop_obj, dict):
            name = shop_obj.get("name")
            if name:
                return name
        shop_name = raw_listing.get("shop_name")
        return shop_name if isinstance(shop_name, str) and shop_name else None

    @staticmethod
    def _parse_location(raw_listing: dict[str, Any]) -> str | None:
        # No confirmed source documents a listing-location field for
        # Reverb (see module docstring) - only used if one of these
        # plausible shapes actually happens to be present; never
        # constructed from unrelated fields.
        shop_obj = raw_listing.get("shop")
        if isinstance(shop_obj, dict):
            location = shop_obj.get("location")
            if isinstance(location, str) and location:
                return location
            if isinstance(location, dict):
                parts = [location.get(key) for key in ("locality", "region", "country_code")]
                parts = [part for part in parts if part]
                if parts:
                    return ", ".join(parts)
        location = raw_listing.get("location")
        return location if isinstance(location, str) and location else None

    @staticmethod
    def _parse_listing_url(raw_listing: dict[str, Any]) -> str:
        links = raw_listing["_links"]
        return links["web"]["href"]

    @staticmethod
    def _parse_image_url(raw_listing: dict[str, Any]) -> str | None:
        photos = raw_listing.get("photos")
        if not isinstance(photos, list) or not photos:
            return None
        first_photo = photos[0]

        if isinstance(first_photo, str) and first_photo:
            return first_photo

        if isinstance(first_photo, dict):
            links = first_photo.get("_links")
            if isinstance(links, dict):
                for size in ("full", "large", "small_crop", "thumbnail"):
                    link = links.get(size)
                    if isinstance(link, dict) and link.get("href"):
                        return link["href"]
            for key in ("link", "url"):
                value = first_photo.get(key)
                if isinstance(value, str) and value:
                    return value

        return None

    @staticmethod
    def _parse_published_at(raw_listing: dict[str, Any]) -> datetime | None:
        for key in ("published_at", "created_at"):
            raw_timestamp = raw_listing.get(key)
            if isinstance(raw_timestamp, str) and raw_timestamp:
                try:
                    return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None

    def health_check(self) -> bool:
        return self.is_configured
