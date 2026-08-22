# Architecture

## Overview

Marketplace Alert is built around one core idea: **the search engine never
knows anything about a specific marketplace.** It only knows about
`MarketplaceConnector` (an interface) and `Listing` (a normalized data
model). Everything marketplace-specific — API clients, scraping, HTML
parsing, auth, rate limiting, pagination quirks — lives inside a connector
and never leaks out.

```
                         +----------------------+
                         |     Core Engine      |
                         |  (search, dedup,      |
                         |   alerts, storage)    |
                         +----------+-----------+
                                    |
                         talks only to the
                         MarketplaceConnector
                         interface + Listing model
                                    |
        +---------------------------+---------------------------+
        |                           |                           |
+----------------+          +------------------+        +---------------+
| Etsy Connector |          | eBay Connector    |        | ... Connector |
+----------------+          +------------------+        +---------------+
```

Duplicate detection and notifications sit inside the core engine,
downstream of every connector: `search()` results (`Listing` objects) flow
into the persistence service, and only the listings it classifies as *new*
flow onward into the notification service. Neither persistence nor
notifications know a database or Telegram exists outside their own module —
see "Local persistence and duplicate detection" and "Notifications" below.

## The connector interface

Defined in [`marketplace_alert/core/connectors/base.py`](marketplace_alert/core/connectors/base.py):

```python
class MarketplaceConnector(ABC):
    marketplace_name: str

    def search(self, query: str, filters: dict | None = None) -> list[Listing]: ...
    def normalize_listing(self, raw_listing: Any) -> Listing: ...
    def health_check(self) -> bool: ...
```

Rules for connectors:

- A connector is the **only** place that knows the shape of a marketplace's
  raw API/HTML response.
- `search()` must return a `list[Listing]` — never raw marketplace data.
- `normalize_listing()` converts one raw item into a `Listing`. Keeping it
  separate from `search()` makes each connector's mapping logic directly
  testable without hitting the network.
- `health_check()` should be cheap and side-effect free (e.g. a lightweight
  ping/auth check), used later for monitoring connector availability.
- Adding a new marketplace means adding a new subclass in
  `marketplace_alert/connectors/<name>/` (created when the first real
  connector is implemented) — it must never require changes to the core
  engine, the `Listing` model, or other connectors.

**Resolving a connector by name**: `marketplace_alert/connectors/registry.py`
maps a marketplace name (e.g. `"mock"`, `"etsy"`, `"ebay"`, `"reverb"`,
`"bonanza"`) to a connector instance - `get_connector(name)` - and answers
`is_marketplace_supported(name)`. It is the *only* place outside a
connector's own module allowed to import a concrete connector class.
Anything in `core/` that needs to resolve a connector (currently:
`SavedSearchRunner`, via `SavedSearchService`'s marketplace validation)
takes a resolver function / predicate injected by `main.py` instead of
importing the registry - `core/` stays free of any concrete-connector
import, including the registry itself. Adding a marketplace means adding
one line to the registry's factory dict; nothing in `core/` changes -
proved in practice four times now: when Etsy was added, again when eBay
was added, again when Reverb was added, and again when Bonanza was added
(nothing outside `connectors/registry.py` and `connectors/bonanza/` had to
change the fourth time either). Each registry entry is a zero-arg
callable rather than strictly a bare class, since a real connector (Etsy,
eBay, Reverb, Bonanza) needs credentials wired in from `settings` that a
plain class reference can't supply; `MockMarketplaceConnector` still
works unchanged as an entry since a class is itself a valid zero-arg
callable. `list_supported_marketplaces()`
returns every registered name, sorted - the single source of truth
anything listing marketplaces should use (the dashboard's marketplace
checkboxes/status panel and the mobile API's `GET /api/v1/marketplaces` -
see "The management dashboard" and "Mobile API" below) rather than
hard-coding the marketplace list a second time somewhere else.
`display_name_for(name)` is the same idea for a marketplace's
presentational name (e.g. `"ebay"` → `"eBay"`) - one small mapping,
defined once in the registry, that both the dashboard and the mobile API
call rather than each keeping an independent copy (see "Why these
choices" below for why this was worth generalizing when Reverb was
added, rather than adding a third hard-coded copy).

## Shared connector retry helper

`marketplace_alert/core/connectors/retry.py` (`request_with_retry()`) is
a small helper every real connector (Etsy, eBay, Reverb, Bonanza) wraps
its outbound HTTP call in - added during a production-hardening pass
after auditing found connectors previously failed an entire search
immediately on a transient rate-limit or upstream gateway blip, even
though `SavedSearchRunner`/`BackgroundScanner` already isolate one
marketplace's failure from the others on the same saved search. This
closes the gap that isolation was compensating for, rather than relying
on it alone.

```python
response = request_with_retry(
    lambda: httpx.get(url, params=params, headers=headers, timeout=...),
    marketplace_name="Etsy",
)
```

- **Retries only genuinely transient responses**: HTTP 429, 502, 503, 504
  - up to 2 additional attempts (3 total), exponential backoff
  (`retry_base_seconds * 2^(attempt-1)`), honoring the response's own
  `Retry-After` header when present instead of guessing a wait time.
- **Never retries a permanent failure** - 401/403 (auth), 404, 400, or any
  other status not in the retriable set - since retrying those could
  never succeed and would only add latency before failing anyway. A
  connector's own status-code-specific handling stays entirely outside
  this helper: eBay's 401/403 token invalidation
  (`EbayTokenManager.invalidate()`) still happens exactly where it always
  has, in the connector, after `request_with_retry()` returns whatever
  final response it got.
- **A network error or timeout (`httpx.HTTPError`) propagates
  immediately, unretried** - a connector retrying into the same scan
  tick's time budget after it has already timed out once is a deliberate,
  separate scope decision this helper doesn't make, not an oversight.
- **Same retry *policy* as `notifications/telegram/provider.py`'s own
  retry logic, deliberately not the same *code*.** Telegram's retry hint
  comes from a JSON response body field (`parameters.retry_after`); this
  helper's comes from the standard HTTP `Retry-After` header. The two are
  also used in structurally different places - a single notification send
  versus every connector's own search request - so sharing the policy
  shape without forcing a shared abstraction across genuinely different
  call sites was the deliberate choice; see PROJECT_CONTEXT.md decision
  #19.
- Each retry attempt is logged once at `WARNING` (marketplace name,
  status code, attempt number, wait time) - never the request URL's query
  string or any credential/header value.

`tests/test_connector_retry.py` covers the helper directly (success,
retry-then-succeed for each retriable code, exhaustion, every
non-retriable code including an explicit check that 500 is *not*
retried, zero/negative `max_retries`, backoff math, `Retry-After`
honored vs. falling back to backoff, and network errors passing through
unretried). Each connector's own test file additionally has one test
proving it's actually wired to this helper (not just that the helper
works in isolation), plus an autouse `_no_real_sleeps` fixture -
monkeypatching `retry.time.sleep` to record durations instead of
sleeping, the same pattern `tests/test_telegram_provider.py` already
established - so a 429/502/503/504 test case doesn't make the suite
actually wait out an exponential backoff.

## The normalized `Listing` model

Defined in [`marketplace_alert/core/models/listing.py`](marketplace_alert/core/models/listing.py)
as a Pydantic model. Every connector's `normalize_listing()` must return one
of these, regardless of source marketplace:

| Field | Notes |
|---|---|
| `marketplace` | Identifier of the source marketplace (e.g. `"ebay"`) |
| `external_listing_id` | The listing's ID on the source marketplace |
| `title` | Required |
| `description` | Optional |
| `price`, `currency` | Optional — some listings have no fixed price |
| `location` | Optional, free text |
| `seller` | Optional, free text |
| `condition` | Optional, free text (marketplaces use inconsistent vocab) |
| `listing_url` | Required — link back to the original listing |
| `image_url` | Optional |
| `created_at` | When the listing was created on the marketplace, if known |
| `discovered_at` | When *our* system first saw it (defaults to now) |

`external_listing_id` + `marketplace` together are the dedup key used by
persistence (below) — that logic belongs to the core engine, not to
connectors.

## Layout

```
marketplace_alert/
    main.py                    FastAPI app entry point (incl. GET / dashboard route)
    dependencies.py            Shared singletons/dependency providers (notification service, run guard, ...)
    config.py                  Settings loaded from environment / .env
    api/
        v1/
            __init__.py          Aggregates every /api/v1 sub-router
            schemas.py            Mobile API request/response schemas
            status.py              GET /api/v1/status
            marketplaces.py         GET /api/v1/marketplaces
            saved_searches.py       /api/v1/saved-searches* (CRUD + run)
            listings.py              GET /api/v1/listings
    templates/
        dashboard.html          Jinja2 template for the management dashboard
    static/
        dashboard.css            Dashboard styling (no framework, no build step)
        dashboard.js              Dashboard interactivity (vanilla JS, calls existing API)
    core/
        logging_config.py      Structured (JSON) logging setup
        connectors/
            base.py            MarketplaceConnector interface
        models/
            listing.py         Normalized Listing model (Pydantic)
        persistence/
            database.py        SQLAlchemy engine/session/init_db, DATABASE_URL selection
            models.py           DiscoveredListing table (SQLAlchemy)
            repository.py       Raw DB access (ListingRepository)
            service.py          Duplicate detection (ListingDiscoveryService)
            cleanup.py           Historical relevance re-evaluation/removal (maintenance-only)
        relevance/
            text.py              Normalization + n-gram phrase matching (shared primitive)
            brands.py             Extensible brand vocabulary + conflict detection
            families.py            Configurable product-family synonym mapping
            accessories.py          Accessory-term vocabulary
            query.py                 Query parsing (brand extraction, core tokens)
            models.py                 RelevanceEvaluation / RelevanceFilterResult
            evaluator.py               The scoring engine (evaluate_relevance)
            service.py                  Shared entrypoint (filter_relevant_listings)
        notifications/
            base.py            NotificationProvider interface, NotificationError
            service.py          Alert dispatch (NotificationService)
        saved_searches/
            models.py            SavedSearch + SavedSearchMarketplace tables
            schemas.py            SavedSearchCreate/Update/Read (Pydantic)
            repository.py         Raw DB access (SavedSearchRepository)
            service.py             Validated CRUD (SavedSearchService)
            runner.py              Run-one-search logic (SavedSearchRunner)
            migration.py            One-off legacy-column migration (no Alembic)
        scheduler/
            guard.py              Overlap prevention (SavedSearchRunGuard)
            scanner.py             The background loop (BackgroundScanner)
    connectors/
        registry.py             get_connector / is_marketplace_supported / display_name_for
        mock/
            connector.py        MockMarketplaceConnector (fake, in-memory)
        etsy/
            connector.py         EtsyMarketplaceConnector (Etsy Open API v3)
        ebay/
            connector.py          EbayMarketplaceConnector (eBay Buy Browse API)
            token_manager.py      EbayTokenManager (OAuth client_credentials)
        reverb/
            connector.py          ReverbMarketplaceConnector (Reverb API v3)
        bonanza/
            connector.py          BonanzaMarketplaceConnector (Bonanza "Bonapitit" API)
    notifications/
        telegram/
            provider.py          TelegramNotificationProvider
alembic/
    env.py                       Resolves DATABASE_URL the same way the app does
    versions/                    One migration per schema change (baseline so far)
alembic.ini                     Alembic config (sqlalchemy.url deliberately left blank)
scripts/
    cleanup_historical_listings.py   Manual, explicit-only historical relevance cleanup (see "Historical listing cleanup")
tests/                          Mirrors the package layout
```

As the project grows, real connectors will live under
`marketplace_alert/connectors/<marketplace_name>/`, each isolated in its own
module/package, importing only from `core` (never the other way around).

## The mock connector

`marketplace_alert/connectors/mock/connector.py` implements
`MarketplaceConnector` against a small fixed, in-memory catalog of fake
listings instead of a real marketplace. It exists so the rest of the system
can be developed and tested without external API credentials — currently
relevant because the real eBay connector is blocked on eBay Developer API
approval (see `PROJECT_CONTEXT.md`).

It follows every rule real connectors follow (same interface, same
`Listing` output, case-insensitive substring matching on `query`, basic
optional `filters` like `min_price`/`max_price`/`condition` that are simply
ignored if unsupported) so that code written against it keeps working
unchanged once a real connector exists. It lives in its own subpackage,
sibling to where real connectors will go, and must never be edited in place
to "become" a real connector — a real connector is a new subclass in its own
module.

`marketplace_alert/main.py` currently exposes a **temporary** `GET /search`
endpoint backed directly by `MockMarketplaceConnector`
(e.g. `GET /search?q=Maccabi`). It has no marketplace selection, filters,
persistence, or auth, is stateless, and should be replaced once a real
connector and a proper search API are designed.

## The Etsy connector

`marketplace_alert/connectors/etsy/connector.py` (`EtsyMarketplaceConnector`)
is the first *real* connector - it calls Etsy Open API v3 over HTTP, never
scrapes an HTML page. It's registered in the connector registry alongside
`"mock"`, so a saved search with `marketplace="etsy"` flows through the
exact same duplicate-detection, scheduler, and notification pipeline the
mock connector already proved out - no changes were needed anywhere else
to add it, which is the actual test of whether the connector interface
does what it claims.

**Endpoint and auth were verified against Etsy's own sources before
writing any code** (not guessed - see the module's docstring for the exact
URLs checked): Etsy's generated OpenAPI v3 spec
(`https://www.etsy.com/openapi/generated/oas/3.0.0.json`), the official
Quickstart tutorial, and the Definitions page.

- **Endpoint**: `GET https://api.etsy.com/v3/application/listings/active`
  (operation `findAllListingsActive`) - searches active listings
  marketplace-wide by keyword (`keywords` query param), not scoped to one
  shop. `limit`/`offset` paginate (Etsy's own max `limit` is 100);
  `includes=Images` is passed so image URLs come back in the same request
  instead of a second call per listing.
- **Auth**: every request needs an `x-api-key` header of the form
  `"<ETSY_API_KEY>:<ETSY_SHARED_SECRET>"` - keystring and shared secret,
  colon-separated, confirmed from Etsy's own quickstart code samples. No
  OAuth user-authorization flow is required for this endpoint - it's
  public and read-only. **The shared secret is required** even for this
  plain read, since it's part of the `x-api-key` value itself, not
  something needed only for OAuth.
- **Price**: Etsy returns a `money` object (`amount`, `divisor`,
  `currency_code`), not a plain number - actual price is `amount / divisor`
  (e.g. `{"amount": 2500, "divisor": 100, ...}` is $25.00). Getting this
  wrong silently produces prices 100x too high, so it's verified against
  Etsy's own Definitions page, not assumed.
- **Field mapping**: `listing_id` → `external_listing_id`, `title`,
  `description`, `url` → `listing_url`, computed price/`currency_code`,
  `images[0].url_570xN` (falling back to `url_fullxfull`) → `image_url`,
  `original_creation_timestamp` (Unix seconds) → `created_at`.
  `location`/`seller`/`condition` are left `null` - Etsy has no direct
  "condition" concept (it's a handmade/vintage/craft marketplace, not
  general resale), and mapping shop name/location would need an
  `includes=Shop` request and schema verification not done yet. Per the
  connector-interface rule, a field that isn't confidently known is left
  `null`, never invented.
- **Configuration**: `EtsyMarketplaceConnector.__init__` takes
  `api_key`/`shared_secret` explicitly (the registry wires these from
  `settings.etsy_api_key`/`settings.etsy_shared_secret`, i.e.
  `ETSY_API_KEY`/`ETSY_SHARED_SECRET`) - it never reads settings itself.
  If either is missing, `is_configured` is False and `search()` raises
  `MarketplaceConnectorError` with a configuration message *before*
  attempting any request - construction itself never fails, so a missing
  Etsy credential can't crash app startup, and `"mock"` (or any other
  connector) is completely unaffected.
- **Failure handling**: network errors, timeouts, non-200 responses
  (including 429 rate-limiting), and non-JSON or missing-`results` bodies
  all raise `MarketplaceConnectorError` (defined in `core/connectors/base.py`,
  alongside `MarketplaceConnector` - the connector-level equivalent of
  `NotificationError`) after logging a sanitized message - never the raw
  exception, response body, or credentials. A single malformed *listing*
  inside an otherwise-valid response is logged and skipped rather than
  failing the whole search. Because `SavedSearchRunner`/`BackgroundScanner`
  already catch and log any exception from `connector.search()` per saved
  search (see "Saved searches and background scanning" below), a failing
  Etsy search behaves exactly like any other connector failure - it never
  needed special-casing there.

## The eBay connector

`marketplace_alert/connectors/ebay/connector.py` (`EbayMarketplaceConnector`)
is the second *real* connector - it calls the eBay **Buy Browse API** over
HTTP, never the legacy Finding API, never scraping. It's registered in the
connector registry alongside `"mock"`/`"etsy"`, so a saved search targeting
`"ebay"` (alone, or alongside `"etsy"`/`"mock"` on the same search) flows
through the exact same duplicate-detection, scheduler, and notification
pipeline the other two connectors already proved out - no changes were
needed anywhere else to add it, proving the connector interface's claim a
second time.

**Endpoint and auth were verified against eBay's own sources before
writing any code** (not guessed - see `connector.py` and
`token_manager.py`'s module docstrings for the exact sources checked): the
OAuth client credentials grant guide, the Browse API `item_summary/search`
method reference, and the `ItemSummary` type reference (including its
nested `ConvertedAmount`/`Image`/`Seller`/`ItemLocationImpl` types). The
developer had also already manually confirmed, outside this codebase, that
the same OAuth token request and the same search request both return real
HTTP 200 responses against eBay's production API before this connector was
written.

- **Auth**: OAuth 2.0 **client_credentials** grant (an *Application* Access
  Token - no user login involved). `EbayTokenManager`
  (`marketplace_alert/connectors/ebay/token_manager.py`) is a small, self-
  contained module the connector delegates all token handling to:
  - `POST https://api.ebay.com/identity/v1/oauth2/token`, with
    `EBAY_APP_ID` as `client_id` and `EBAY_CERT_ID` as `client_secret`
    (`Authorization: Basic base64(client_id:client_secret)`,
    `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`
    form-encoded body). `EBAY_DEV_ID` is never read - the Browse API's
    application-token flow doesn't need it.
  - The returned token is cached **in memory** and reused across every
    search - never refetched per request. It's refreshed only when none is
    cached yet, or the cached one is within a small safety margin (60s) of
    its own `expires_in`, tracked via `time.monotonic()` so wall-clock
    changes can't affect it. `invalidate()` additionally drops the cached
    token if a search request itself comes back 401/403, in case it was
    revoked before its tracked expiry.
  - The token value is never logged (not even at DEBUG), never persisted
    to disk or source code, and never exposed through any API response -
    only its *presence* (`is_configured`) is ever observable from outside
    `token_manager.py`.
- **Endpoint**: `GET https://api.ebay.com/buy/browse/v1/item_summary/search`
  - free-text keyword search (`q`), a safe configurable page size (`limit`,
    `ebay_result_limit` setting, default 25, eBay's own max 200), and an
    `offset` param for pagination (structured for future multi-page looping,
    same pattern as Etsy's connector, but only one page is fetched today).
    `fieldgroups=EXTENDED` is passed so `shortDescription` comes back where
    available. Every request carries `Authorization: Bearer <token>` (from
    `EbayTokenManager.get_token()`) and `X-EBAY-C-MARKETPLACE-ID: EBAY_US`
    (sent explicitly rather than relying on eBay's implicit default).
- **Field mapping**: `itemId` → `external_listing_id`, `title`,
  `shortDescription` → `description`, `price.value`/`price.currency` (eBay
  sends `value` as a decimal string, e.g. `"89.99"`, not a scaled integer
  like Etsy's `money` object - cast straight to `float`), `itemWebUrl` →
  `listing_url`, `image.imageUrl` → `image_url` (primary image only),
  `itemLocation` (`city`/`stateOrProvince`/`country`, joined) → `location`,
  `seller.username` → `seller`, `condition` (eBay's own condition text)
  → `condition`, `itemCreationDate` → `created_at`. Any field eBay doesn't
  return for a given listing is left `null` on the `Listing`, never
  invented - same rule as Etsy.
- **Zero results vs. malformed response**: eBay omits the `itemSummaries`
  key entirely when a search matches nothing, rather than returning an
  empty array - the connector treats a missing key as "0 results" (not an
  error), but a *present*, non-list `itemSummaries` as malformed.
- **Configuration**: `EbayMarketplaceConnector.__init__` takes
  `app_id`/`cert_id` explicitly (the registry wires these from
  `settings.ebay_app_id`/`settings.ebay_cert_id`) - it never reads settings
  itself, same rule as Etsy. If either is missing, `is_configured` is False
  and `search()` raises `MarketplaceConnectorError` *before* attempting any
  request (including the token request) - construction itself never fails,
  so a missing eBay credential can't crash app startup, and `"mock"`/`"etsy"`
  are completely unaffected.
- **Failure handling**: network errors, timeouts, non-200 responses
  (401/403 additionally invalidate the cached token; 429 rate-limiting
  logged distinctly), and non-JSON or malformed bodies all raise
  `MarketplaceConnectorError` after logging a sanitized message - never the
  raw exception, response body, or credentials. A single malformed
  *listing* inside an otherwise-valid response is logged and skipped
  rather than failing the whole search - same as Etsy. Because
  `SavedSearchRunner`/`BackgroundScanner` already catch and log any
  exception from `connector.search()` per marketplace (see "Saved searches
  and background scanning" below), a failing eBay search - including a
  failing *token* request - never needed special-casing there: if eBay
  fails on a saved search that also targets Etsy (or vice versa), the other
  marketplace still runs and the saved search still completes.

## The Reverb connector

`marketplace_alert/connectors/reverb/connector.py`
(`ReverbMarketplaceConnector`) is the third *real* connector - musical
instruments/gear marketplace Reverb, via its public **API v3**, never HTML
scraping. Registered in the connector registry alongside
`"mock"`/`"etsy"`/`"ebay"`, so a saved search targeting `"reverb"` (alone,
or alongside any other marketplace) flows through the exact same
duplicate-detection, scheduler, relevance-filtering, and notification
pipeline every other connector already proved out - no changes were
needed anywhere else to add it, a third proof of the connector
interface's claim.

**Verified before writing any code, from three independent sources** (see
`connector.py`'s module docstring for the full citations): the
developer's own manually-verified request (a real personal access token,
`GET https://api.reverb.com/api/listings?query=Fender&per_page=1` with
`Authorization: Bearer <token>`, `Accept: application/hal+json`,
`Accept-Version: 3.0`, returning a real listing) - the source of truth
for the auth headers and the `query`/`per_page` parameter names; Reverb's
own official API docs, confirming the response is a HAL+JSON document
with listings under a top-level `"listings"` array and pagination via
`_links.next`/`_links.prev` (a full `href` to follow verbatim - Reverb's
own stated design principle is "never construct your own URLs... follow
`_links`"); and a real, working third-party Reverb API client, whose
JSON-property constants confirm `_links.web.href` is a listing's
canonical web page URL and that `photos` is the correct top-level image
key.

**One honest gap, handled deliberately, not glossed over**: Reverb's own
published docs do not include a complete example listing object, so the
exact nesting of `price`/`condition`/`shop`/`photos` is inferred (from
Reverb's own listing-*creation* payload shape, cross-referenced against
real Reverb listing fields independently documented by a third-party
tool built against this same public API) rather than independently
confirmed the way eBay's/Etsy's field mappings were. `normalize_listing`
is written defensively as a direct consequence: every optional field
tries its most likely shape first, then one or two reasonable structural
fallbacks, landing on `None` - never an invented value - if nothing
matches. A wrong guess about one field's exact nesting only ever means
that field is `null` for a listing, never fabricated data.

- **Auth**: a single, static personal access token
  (`REVERB_API_TOKEN`, created with only the `public` scope for this
  integration) sent as `Authorization: Bearer <token>` on every request,
  alongside `Accept: application/hal+json` and `Accept-Version: 3.0`.
  Unlike eBay's OAuth client-credentials flow, there is no token
  refresh/expiry to manage - a 401/403 means the token is missing, wrong,
  or revoked, surfaced as a clear, safe error and never retried
  automatically (see "Failure handling" below).
- **Endpoint**: `GET https://api.reverb.com/api/listings` - free-text
  keyword search (`query`), a safe configurable page size (`per_page`,
  `reverb_result_limit` setting, default 25). Unlike Etsy/eBay (which
  fetch a single page today), this connector *does* paginate: it follows
  `_links.next.href` verbatim (never hand-reconstructing query params for
  it, per Reverb's own HAL convention) until it has `result_limit`
  listings, there's no further `next` link, or a hard `MAX_PAGES` safety
  cap (5) is reached - whichever comes first. The cap exists specifically
  so a pathological response (every page reporting a `next` link while
  returning almost nothing) can never turn into unbounded polling for a
  single saved-search scan.
- **Field mapping**: `id` → `external_listing_id`, `title`, `description`,
  `price.amount`/`price.currency` (falling back to a `price_cents`
  field, divided by 100, if no structured `amount` is present - never
  parsing a human-readable display string like `"$1,419.30"` when no
  safe numeric field exists), `condition.display_name` (or a plain
  string `condition`) → `condition`, `shop.name` (falling back to a
  top-level `shop_name`) → `seller`, `photos[0]._links.full.href` (with
  `large`/a plain string/`link`/`url` fallbacks) → `image_url`,
  `_links.web.href` → `listing_url` (the canonical web page, not
  `_links.self`, the API resource URL), `published_at` (falling back to
  `created_at`) → `created_at`. `location` is only ever populated from a
  plausible `shop.location`/top-level `location` field if one is actually
  present - Reverb's docs don't confirm a location field for a listing at
  all, so this is never invented from unrelated data. Any field not
  present for a given listing is left `null`, never guessed - same rule
  as Etsy/eBay.
- **Character encoding**: `httpx`'s own JSON decoding (`response.json()`)
  correctly handles UTF-8 per RFC 8259 - titles/descriptions with
  accents, symbols, or emoji round-trip intact. A dedicated test
  (`tests/test_reverb_connector.py`) proves this through the full request
  pipeline, not just the normalizer in isolation - any mojibake seen in a
  terminal is that terminal's own display encoding, never a corruption of
  the underlying response data.
- **Configuration**: `ReverbMarketplaceConnector.__init__` takes
  `api_token` explicitly (the registry wires this from
  `settings.reverb_api_token`) - it never reads settings itself, same
  rule as Etsy/eBay. If it's missing, `is_configured` is False and
  `search()` raises `MarketplaceConnectorError` *before* attempting any
  request - construction itself never fails, so a missing Reverb
  credential can't crash app startup, and every other connector is
  completely unaffected.
- **Failure handling**: network errors, timeouts, non-200 responses
  (401/403, 404, 429 each logged and reported distinctly; any other
  non-200 reported generically), and non-JSON or malformed bodies all
  raise `MarketplaceConnectorError` after logging a sanitized message -
  never the raw exception, response body, or token. A single malformed
  *listing*, or one missing its `_links.web.href`, inside an otherwise-
  valid response is logged and skipped rather than failing the whole
  search - same as Etsy/eBay. A malformed/missing `listings` collection
  on the *first* page is a hard error (nothing to return); the same
  problem on a *later* page during pagination just stops paginating and
  returns whatever was already collected, rather than discarding good
  results over a later page's issue. Because `SavedSearchRunner`/
  `BackgroundScanner` already catch and log any exception from
  `connector.search()` per marketplace, a failing Reverb search never
  needed special-casing there: if Reverb fails on a saved search that
  also targets Etsy/eBay, the other marketplace(s) still run and the
  saved search still completes.
- **Rate-limit visibility, not enforcement**: Reverb doesn't publish
  exact rate-limit header names, so `_log_rate_limit_metadata` checks a
  few common conventions (`X-RateLimit-Remaining`/`RateLimit-Remaining`,
  etc.) and logs them at INFO if present - a no-op, never an error, if
  none are. `Retry-After` is logged the same way on a 429. This connector
  doesn't poll any faster than a saved search's own configured interval
  in the first place (same as every other connector - see "Saved
  searches and background scanning" below), so this is visibility for a
  human reading logs, not a rate-limiting mechanism of its own.

## The Bonanza connector

`marketplace_alert/connectors/bonanza/connector.py`
(`BonanzaMarketplaceConnector`) is the fourth *real* connector - Bonanza,
a general (eBay-like) US marketplace, via its own "Bonapitit" API, never
HTML scraping. Added as part of a broader marketplace-expansion effort
that evaluated nine other named candidates first (Craigslist, OfferUp,
Gumtree, Kleinanzeigen, OLX, Vinted, Discogs, Mercado Libre, Facebook
Marketplace) - Bonanza was the only one found with both a genuinely
self-serve credential and a real marketplace-wide keyword-search endpoint
returning actual for-sale listings; see PROJECT_CONTEXT.md's marketplace
feasibility notes for the full write-up on every candidate, including
*why* each one that isn't implemented was ruled out.

**Bonanza's API was deliberately modeled on eBay's own (now-deprecated)
Finding API** - literally the same operation name
(`findItemsByKeywords`) and response shape - since Bonanza was founded by
former eBay/Amazon engineers specifically to make migrating an eBay
integration easy. That parallel is a genuine advantage here: this
project already had an eBay connector to reference for field-mapping
conventions, and it's independent evidence the field names below are
likely correct even though (see next paragraph) they couldn't all be
pinned down with Reverb-level confidence.

**Two-source verification, same honest-uncertainty approach as
Reverb**: the exact *wire protocol* - `POST
https://api.bonanza.com/api_requests/standard_request`, the call name and
JSON parameters combined into a single form-encoded body field (an
unusual convention, confirmed rather than guessed), and the
`X-BONANZLE-API-DEV-NAME` header - was confirmed directly from a real,
working third-party PHP SDK's HTTP client source
(github.com/Shoplo/bonapitit-bonanza-php-sdk). `findItemsByKeywords`'s
own parameters and response shape came from Bonanza's official API
reference docs, but - unlike Reverb's docs, which still had a literal
JSON pagination example to quote - the auto-converted documentation
didn't preserve a byte-exact example response for this specific call.
`normalize_listing` is written defensively as a direct result (same
philosophy as Reverb's connector): the price field in particular tries
the eBay-Finding-API-typical `{"__value__": ..., "@currencyId": ...}`
value/attribute shape first (a well-known quirk of that XML-derived API,
which Bonanza explicitly copied), then a plain-number fallback, landing
on `None` - never invented - if neither matches. A `condition` field
isn't confirmed to exist on search results at all, so it's `None` unless
one is actually present.

- **Auth**: a single developer name (`BONANZA_DEV_NAME`, sent as
  Bonanza's own `X-BONANZLE-API-DEV-NAME` header), obtained by
  registering a free developer account at
  `https://api.bonanza.com/accounts/new`. This connector only ever makes
  Bonanza's "non-secure" class of call (read-only search) - it never
  sends a Certificate ID, which Bonanza only requires for calls that act
  on a specific user's own account (managing their own listings/orders),
  something this connector never does.
- **Endpoint**: `findItemsByKeywords` via the shared `standard_request`
  endpoint - free-text keyword search (`keywords`), a safe configurable
  page size (`paginationInput.entriesPerPage`, `bonanza_result_limit`
  setting, default 25, Bonanza's own documented max 100). Like Reverb
  (and unlike Etsy/eBay, which fetch a single page today), this connector
  paginates - by incrementing `paginationInput.pageNumber` (Bonanza's API
  isn't HAL/`_links`-based like Reverb's, so there's no link to follow)
  until it has `result_limit` listings, a page comes back shorter than
  the requested page size (Bonanza's own signal there's nothing more), or
  a hard `MAX_PAGES` safety cap (5) is reached - the same bounded-pages
  reasoning as Reverb's connector, so a single saved-search scan can
  never turn into unbounded polling.
- **Field mapping**: `itemId` → `external_listing_id`, `title`,
  `sellingStatus.currentPrice` (value/attribute object or plain number,
  see above) → `price`/`currency`, `condition`/`condition.conditionDisplayName`
  (if present at all) → `condition`, `sellerInfo.sellerUserName` →
  `seller`, `location` + `country` (joined) → `location`, `galleryURL` →
  `image_url`, `viewItemURL` → `listing_url`, `listingInfo.startTime` →
  `created_at`. `description` is always `null` - Bonanza's search results
  don't include one (only a single-item fetch would, and this connector
  never makes that call). Any field not present for a given listing is
  left `null`, never guessed - same rule as every other connector.
- **Zero results vs. malformed response**: like eBay's Finding API (which
  Bonanza's mirrors), a zero-match search may omit the `item` key
  entirely rather than returning an empty array - treated as "0 results",
  not an error; a *present*, non-list `item` (or a missing
  `findItemsByKeywordsResponse` envelope, or an `ack` of `"Failure"`) on
  the *first* page is malformed and raises; the same problem on a later
  page during pagination just stops paginating and keeps whatever was
  already collected.
- **Configuration**: `BonanzaMarketplaceConnector.__init__` takes
  `dev_name` explicitly (the registry wires this from
  `settings.bonanza_dev_name`) - it never reads settings itself, same
  rule as every other connector. If it's missing, `is_configured` is
  False and `search()` raises `MarketplaceConnectorError` *before*
  attempting any request - construction itself never fails, so a missing
  Bonanza credential can't crash app startup, and every other connector
  is completely unaffected.
- **Failure handling**: network errors, timeouts, non-200 responses
  (401/403, 429 logged and reported distinctly; any other non-200
  reported generically), non-JSON bodies, and an `ack` of `"Failure"` all
  raise `MarketplaceConnectorError` after logging a sanitized message -
  never the raw exception, response body, or dev name. A single malformed
  listing inside an otherwise-valid response is logged and skipped rather
  than failing the whole search. Because `SavedSearchRunner`/
  `BackgroundScanner` already catch and log any exception from
  `connector.search()` per marketplace, a failing Bonanza search never
  needed special-casing there: if Bonanza fails on a saved search that
  also targets any other marketplace, the others still run and the saved
  search still completes.
- **Not yet live-validated**: unlike eBay/Etsy/Reverb, no real
  `BONANZA_DEV_NAME` was available while building this connector - it's
  fully implemented and tested against mocked HTTP responses only (see
  `tests/test_bonanza_connector.py`), and is explicitly marked
  "awaiting credentials" until someone registers a developer account and
  confirms a real search against production Bonanza. See
  PROJECT_CONTEXT.md's marketplace feasibility notes.

## Local persistence and duplicate detection

`marketplace_alert/core/persistence/` remembers which listings have already
been discovered, so the same listing isn't reported as new twice. It has
three layers, each only aware of the one below it:

```
routes (main.py)
    -> ListingDiscoveryService.process_listings(listings: list[Listing])
        -> ListingRepository (get / save_new / touch_last_seen)
            -> SQLAlchemy engine/session (database.py) -> SQLite (or PostgreSQL later)
```

- **`database.py`** builds the SQLAlchemy engine and session from
  `settings.database_url` - **SQLite locally (the default when
  `DATABASE_URL` is unset), PostgreSQL in production (`DATABASE_URL` set)
  - the same models and code either way, selected by that one setting.**
  See "Database selection and PostgreSQL support" below for exactly how.
  `get_db_session()` is a FastAPI dependency, which is what lets tests
  override it with an isolated temporary database via
  `app.dependency_overrides` instead of touching the real
  `marketplace_alert.db` file - and creates a fresh `Session` per call,
  closed in `finally`, never one reused globally across requests or
  threads (see "Database selection and PostgreSQL support" for why this
  needed no changes for PostgreSQL).
- **`models.py`** defines `DiscoveredListing`, the SQLAlchemy table:
  `id`, `marketplace`, `external_listing_id`, `title`, `listing_url`,
  `first_discovered_at`, `last_seen_at`. A unique constraint on
  `(marketplace, external_listing_id)` is the actual dedup guarantee — it
  holds even if application code has a bug, not just because the service
  layer happens to check first.
- **`repository.py`** (`ListingRepository`) is the only place that writes
  SQL/ORM queries. It exposes `get`, `save_new`, `touch_last_seen` — nothing
  above it touches a `Session` or the ORM model directly.
- **`service.py`** (`ListingDiscoveryService`) is what routes call. Its
  `process_listings(listings: list[Listing])` classifies each listing as
  new (persists it) or already-seen (bumps `last_seen_at`), and returns a
  `ListingDiscoveryResult(new_listings, already_seen_count)`. It works with
  `Listing` objects from **any** connector — it has never imported
  `MockMarketplaceConnector` or any real connector, and must not.

`marketplace_alert/main.py` exposes this through a **temporary**
`GET /scan?q=...` endpoint: it runs the mock connector's `search()`, feeds
the results through `ListingDiscoveryService`, and returns only the new
listings plus a count of how many were already seen. Unlike `/search`,
`/scan` has side effects — running the same query twice returns fewer (or
zero) new listings the second time. Route handlers only orchestrate
(connector search -> service call -> shape the response) and never contain
raw persistence logic themselves.

## Relevance filtering

`marketplace_alert/core/relevance/` decides whether a listing a connector
returned actually matches what the saved search's query asked for, before
it's ever treated as discovered — persisted, counted, or notified about.
It sits directly between the connector and the persistence layer:

```
connector.search(query) -> list[Listing]  (raw results, keyword-matched only)
    -> filter_relevant_listings(query, listings, marketplace, saved_search_id)
        -> evaluate_relevance(query, listing) for each listing
    -> RelevanceFilterResult(relevant_listings, raw_count, relevant_count, rejected_count)
        -> ListingDiscoveryService.process_listings(relevant_listings)
```

**Why this exists**: real mobile testing showed a "Makita drill" saved
search returning Etsy listings for DeWalt/Milwaukee battery holders and
generic tool organizers. Every marketplace connector does its own keyword
matching (or none at all, for the mock connector — a plain substring
check), which is precise enough to find candidates but not precise enough
to guarantee they're what the user actually meant. Rather than asking each
connector to get smarter independently (three different keyword-matching
implementations to keep in sync), one shared, connector-agnostic layer
sits after every connector and before persistence/notification.

**Deliberately deterministic and dependency-free** — no LLM/AI API, no
embeddings, no vector database, nothing that isn't reproducible from the
same (query, listing) pair every time. Every module here is either a plain
function over strings/tokens or a small in-memory registry (`dict`),
because the actual task ("does this listing match this query well enough
to bother someone about it") doesn't need more than that, and a rule-based
approach is fully explainable — every rejection has one of a small,
enumerated set of reasons, not an opaque score from a model.

### The building blocks

- **`text.py`** — `normalize_text()` (lowercase, strip punctuation/
  separators, collapse whitespace) and `tokenize()` (the above, split on
  whitespace, plus a conservative trailing-s/-ies singularization —
  "batteries" -> "battery", "holders" -> "holder"). `find_phrase_matches()`
  is the one generic n-gram matcher every vocabulary lookup in this
  package uses: given a list of tokens and a `{phrase tuple: value}`
  vocabulary, it returns every vocabulary phrase present as a *contiguous
  token sequence* — never a raw substring check, so "drill" can never
  accidentally match inside an unrelated longer word.
- **`brands.py`** — the brand vocabulary. `register_brand(canonical,
  aliases=[...])` is the extension point; the default catalog (Makita,
  Bosch, DeWalt, Milwaukee, Ryobi, Black+Decker, and about a dozen more
  common power-tool brands) is just what's registered at import time, not
  a hard-coded architectural limit. `reset_to_defaults()` exists mainly
  for test isolation.
- **`families.py`** — product-family synonyms. A `ProductFamily` has a
  `core_term`, a tuple of `strong_synonyms` (genuinely the same product —
  e.g. the "drill" family's `hammer drill`, `cordless drill`, `driver
  drill`), and a tuple of `related_synonyms` (adjacent but not identical —
  `impact driver`). `register_family()` is how a new category gets added;
  only "drill" is registered today (see PROJECT_CONTEXT.md "Things that
  have NOT yet been implemented" for what that means for other
  categories).
- **`accessories.py`** — a flat vocabulary of accessory-only terms
  (holder, mount, organizer, case, clip, rack, adapter, bit holder,
  battery holder, wall mount). Registered the same way as brands/families
  (`register_accessory_term()`), deliberately domain-general rather than
  tool-specific, since "holder"/"case"/"mount" apply far beyond power
  tools.
- **`query.py`** — `parse_query(query)` tokenizes the query, finds any
  recognized brand phrase (via `find_phrase_matches` against
  `brands.brand_vocabulary()`), and splits the tokens into `brand_tokens`
  (removed) and `core_tokens` (everything else) — what's left to actually
  score the listing's core product match against.

### The scoring engine (`evaluator.py`)

`evaluate_relevance(query, listing) -> RelevanceEvaluation` (`is_relevant:
bool`, `score: int` 0–100, `matched_terms: list[str]`, `rejected_reason:
str | None`) runs four independently-checked signals against the
listing's title + description:

1. **Brand conflict (checked first, overrides everything else).** If the
   query names a recognized brand and the listing mentions a *different*
   recognized brand, reject immediately (`rejected_reason="brand_conflict"`,
   `score=15`) — no amount of core-term matching rescues a wrong-brand
   listing.
2. **Brand match bonus (+30).** A listing whose brand matches the query's
   gets a bonus. A **brand-neutral** listing (mentions no recognized brand
   at all) is not penalized — plenty of real titles just don't include
   the brand.
3. **Core-term match**, scored against the query's core tokens (the query
   with any brand removed):
   - If a registered product family applies: a strong synonym present in
     the listing scores **+55**; only a related synonym present scores
     **+35**; neither present scores **0** and the listing fails the core
     match entirely.
   - Else if the query's core phrase is itself a registered accessory
     term (e.g. "battery holder"): the listing must contain that same
     phrase to count as a core match (**+55** if it does, **0** and fails
     otherwise).
   - Else (no registered family or accessory phrase applies — an
     unregistered product category): **any single shared token** between
     the query's core tokens and the listing counts as a full **+55**
     match. This fallback is deliberately lenient, not proportional to
     how many words overlap — see "Why these choices" below for why a
     stricter fallback was rejected.
   - A query that, after brand removal, has **no core tokens left** (a
     brand-only query, e.g. just "Makita") is relevant only if the
     listing actually mentions that brand (`brand_score > 0`) — a
     brand-*neutral* listing is rejected (`brand_only_query_not_mentioned`),
     since with no core term left there's no other positive signal tying
     it to the query. (Earlier versions of this engine treated "doesn't
     conflict with a different brand" as sufficient here, which made a
     bare brand query match almost anything brand-neutral — a real bug
     found by re-evaluating production data via
     `core/persistence/cleanup.py`; see CHANGELOG.md.)
4. **Accessory penalty (−45).** If the listing itself contains an
   accessory term (from `accessories.py`) and the **query's own core
   phrase did not ask for one**, the listing is penalized. This is what
   rejects "Makita battery holder" for a "Makita drill" search while
   still allowing it for a "Makita battery holder" search (step 3 already
   confirmed the query's core phrase is the exact accessory in question,
   so `query_is_accessory_seeking` is true and the penalty never applies).

The final score is `brand_score + core_score - accessory_penalty`,
clamped to `[0, 100]`. `is_relevant` requires **both** a core-term match
**and** `score >= 50` — a listing can match a brand perfectly and still be
rejected if it fails every core-term check (e.g. "Makita battery holder"
against a "Makita drill" search: brand matches, but "battery holder" is
not a drill-family synonym, so the core match fails outright).

`rejected_reason` is always one of exactly five values, so every
rejection is explainable: `"brand_conflict"`,
`"accessory_without_core_product_match"`, `"no_core_product_match"`,
`"low_relevance_score"`, or `"brand_only_query_not_mentioned"` (a
brand-only query, e.g. just "Makita", against a listing that doesn't
mention that brand at all).

### Worked examples

| Query | Listing | Verdict | Score | Reason |
|---|---|---|---|---|
| Makita drill | Makita Cordless Drill 18V | relevant | 85 | — |
| Makita drill | Makita Hammer Drill XPH12Z | relevant | 85 | — |
| Makita drill | Makita Impact Driver XDT131 | relevant (related, lower score) | 65 | — |
| Makita drill | Cordless Drill Generic Brand | relevant (brand-neutral) | 55 | — |
| Makita drill | Makita Battery Holder Wall Mount | **rejected** | 0 | accessory_without_core_product_match |
| Makita drill | DeWalt 20V Drill DCD771C2 | **rejected** | 15 | brand_conflict |
| Makita drill | Milwaukee Battery Holder Rack | **rejected** | 15 | brand_conflict |
| Bosch drill | Bosch Rotary Hammer Drill GBH | relevant | 85 | — |
| Bosch drill | Bosch Drill Bit Holder Organizer | **rejected** | 40 | accessory_without_core_product_match |
| Bosch drill | Makita Cordless Drill 18V | **rejected** | 15 | brand_conflict |
| Makita battery holder | Makita Battery Holder Wall Mount | relevant | 85 | — |
| Makita battery holder | DeWalt Battery Holder Rack | **rejected** | 15 | brand_conflict |
| Makita battery holder | Makita Cordless Drill 18V | **rejected** | 30 | no_core_product_match |
| drill (no brand) | Bosch / Makita / DeWalt drill | relevant (any brand) | 55 | — |
| drill (no brand) | Generic Battery Holder Organizer | **rejected** | 0 | accessory_without_core_product_match |
| Makita (brand only) | Makita Plunge Base Adaptor | relevant | 85 | — |
| Makita (brand only) | Pokemon Charizard Holo Card | **rejected** | 0 | brand_only_query_not_mentioned |

See `tests/test_relevance.py` for every one of these as an executable
test, plus punctuation/case/whitespace-normalization robustness checks
(`"  MAKITA,, drill!!  "` scores identically to `"Makita drill"`).

### The shared entrypoint (`service.py`)

`filter_relevant_listings(*, query, listings, marketplace,
saved_search_id=None) -> RelevanceFilterResult` runs `evaluate_relevance`
over every listing and splits them into `relevant_listings` plus
`raw_count`/`relevant_count`/`rejected_count`. This is the **only**
function application code calls — never `evaluate_relevance` directly —
and it's called from exactly two places, which between them cover every
way a scan can run:

- **`SavedSearchRunner._run_one_marketplace()`** (`core/saved_searches/
  runner.py`) — used by the background scheduler (`BackgroundScanner`),
  the legacy `POST /saved-searches/{id}/run`, and the mobile
  `POST /api/v1/saved-searches/{id}/run` (all three ultimately call
  `SavedSearchRunner.run()`/`run_by_id()` — there is only one runner
  implementation for all of them to share).
- **`main.py`'s legacy `GET /scan`** — the other place a connector's raw
  results turn into persisted/notified listings.

`GET /search` is deliberately **not** filtered — it's stateless (no
persistence, no notification; see "Local persistence and duplicate
detection" above), so it was never one of the paths this exists to
protect, and filtering a raw exploratory-search endpoint would just be
surprising for a caller inspecting what a marketplace actually returned.

Each rejection is logged once, at `INFO` level (not `WARNING`/`ERROR` —
an irrelevant result is routine, not a fault), with exactly the saved
search id, query, marketplace, external listing id, score, and rejection
reason — never the listing's full title/description/URL, and never a
credential. Accepted listings are not logged at all, to keep this from
becoming spam on a normally-behaving scan.

`SavedSearchRunner`'s `MarketplaceRunResult` (and the mobile/legacy
Pydantic equivalents, `MarketplaceRunOutcome`/`MarketplaceRunResult` in
`api/v1/schemas.py`/`core/saved_searches/schemas.py`) gained two optional
fields, additive only: `raw_count` (what the connector returned) and
`rejected_count` (how many of those were filtered out) sit alongside the
existing `new_count`/`already_seen_count`, which now reflect **post-filter**
results — a saved search that gets 25 raw eBay results but only 10
relevant ones operates on those 10 from that point on (persistence,
dedup, and notification never see the other 15).

### Historical listing cleanup

Relevance filtering only applies going forward — `discovered_listings`
rows persisted *before* it existed were never filtered, so a listing like
"Makita Battery Holder Wall Mount" could still sit in the table and show
up in `GET /api/v1/listings` even though a fresh scan would now reject
it. `marketplace_alert/core/persistence/cleanup.py` is a one-off,
explicitly-invoked (never automatic) maintenance operation that
re-evaluates every existing row against the current relevance engine and
removes the ones it would now reject.

**The schema limitation, and how this works around it honestly.**
`DiscoveredListing` has no relationship to `SavedSearch` (see "Local
persistence and duplicate detection" above) — there is no stored query
and no foreign key, so there is no way to know which specific saved
search originally discovered a given row. Rather than guess, each row is
re-evaluated against **every saved search currently targeting that row's
marketplace** (active or paused — pausing isn't the same as deleting, and
a deleted saved search naturally drops out since it no longer exists to
query) and is kept if it's relevant to **at least one** of them. A row
whose marketplace has **no** saved search left at all to evaluate it
against is left untouched — there's nothing to compare it to, and
deleting it would be guessing, not concluding
(`HistoricalCleanupResult.skipped_no_saved_search_count`). A second, minor
limitation: `DiscoveredListing` never stored a listing's description
(only its title), so historical re-evaluation runs on title text alone —
this can only make a re-evaluation *more* conservative than the original
live one would have been (a description could only add matching signal,
never remove it), so it never risks removing something that's actually
relevant.

**Two functions, one shared evaluation core.**
`preview_historical_cleanup(session)` computes exactly what would be
removed without deleting anything; `run_historical_cleanup(session)`
deletes those same rows (via two small `ListingRepository` additions,
`list_all()`/`delete()`, used only by this maintenance path — the normal
scan/dedup path never deletes a row) and returns the identical
`HistoricalCleanupResult` accounting (`total_rows`, `evaluated_count`,
`skipped_no_saved_search_count`, `kept_count`, `removed_count`, plus the
list of what was removed). Both call the same private `_evaluate()`, so a
preview and a real run can never disagree about what qualifies.

**Invocation: a manual script, never wired into the app.**
`scripts/cleanup_historical_listings.py` opens a session against
whichever database `settings.database_url` already resolves to (the
exact same one the app uses — nothing here points anywhere else),
defaults to a dry-run report, and only deletes/commits with an explicit
`--apply` flag. It is not called from `main.py`'s lifespan, the
scheduler, or any API endpoint — deleting data is a deliberate action a
developer takes on purpose, not something that happens as a side effect
of the app starting or a scan running. `SavedSearch`/
`SavedSearchMarketplace` rows are read-only inputs to this whole
operation; nothing in this path ever writes to them.

## Database selection and PostgreSQL support

The system is live on Render, and the deployed Web Service's
`DATABASE_URL` is set - production runs on Render's managed PostgreSQL
(`marketplacealert-db`), not local SQLite. See "Automatic migrations on
Render Free" below for how schema changes actually reach it, since
Render's Free plan (which this project runs on) doesn't offer the
Pre-Deploy Command mechanism this section originally assumed.

```
DATABASE_URL unset  -> resolve_database_url() -> sqlite:///./marketplace_alert.db  (unchanged default)
DATABASE_URL set    -> resolve_database_url() -> normalize_database_url()
                         (postgres:// or postgresql:// -> postgresql+psycopg://)
                        -> create_db_engine()  (pool_pre_ping=True, no check_same_thread)
```

- **Selection**: `core/persistence/database.py:resolve_database_url()` is
  the *one* place "which database" is decided - `settings.database_url`
  set -> use it; unset -> the exact same local SQLite default as before
  PostgreSQL support existed (`sqlite:///./marketplace_alert.db`).
  `config.py`'s `database_url` field is `str | None = None` (changed from
  a hard-coded SQLite default) specifically so "unset" is representable,
  with this one function deciding what "unset" means, rather than the
  default living in two places that could drift.
- **Normalization**: `normalize_database_url()` rewrites Render's
  `postgres://` and `postgresql://` URL forms to `postgresql+psycopg://`,
  so SQLAlchemy always resolves to the `psycopg` (v3) driver this project
  depends on (`pyproject.toml`) - not an ambient/implicit default (plain
  `postgresql://` would otherwise resolve to `psycopg2`, which isn't a
  dependency here). SQLite URLs pass through unchanged. This is the single
  central place this normalization happens - `alembic/env.py` resolves its
  URL the exact same way (see below), so migrations can never target a
  different database than the running app does.
- **Never logged**: neither `resolve_database_url()` nor
  `create_db_engine()` ever logs the URL they're given - it may contain a
  real password. The one log line `init_db()` emits when skipping
  PostgreSQL (below) names only the dialect (`"postgresql"`), never the URL.
- **Engine construction**: `create_db_engine()` sets
  `connect_args={"check_same_thread": False}` for SQLite only (needed for
  FastAPI's threaded request handling; meaningless for other backends) and
  `pool_pre_ping=True` for every backend - cheap for SQLite, and
  meaningful for PostgreSQL specifically: a managed Postgres instance
  (Render's included) can close idle connections server-side, and
  pre-ping detects and transparently replaces a dead pooled connection
  instead of a request failing with one.
- **Session handling needed no changes for PostgreSQL** - reviewed as part
  of adding this support, not assumed. `get_db_session()` (web requests)
  and `BackgroundScanner`'s per-run `session_factory()` calls (the
  scheduler) already create a fresh `Session` per call and close it in
  `finally` (see both sections above) - never one `Session` shared/reused
  globally across requests or threads. What *is* shared globally (the
  module-level `engine` and `SessionLocal` factory) is exactly what
  SQLAlchemy's own thread-safety model says is safe to share - the
  `Engine` and its connection pool, not individual `Session` objects. No
  code changed here; this was a review, not a fix.
- **Schema strategy differs by actual dialect, not by an environment
  flag**: `init_db()` checks `engine.dialect.name` - `"sqlite"` keeps the
  original automatic `Base.metadata.create_all()` bootstrap on every
  startup (safe: additive only, never drops or alters an existing table);
  anything else (PostgreSQL) no-ops, logging why, rather than either
  crashing or silently doing nothing unexplained. **Do not rely on
  `create_all()`/`init_db()` for PostgreSQL schema changes** - that's
  Alembic's job now, below.

### Migrations (Alembic)

Introduced specifically because relying on `create_all()` in production
against a real, persistent PostgreSQL database is unsafe as the schema
evolves - it can only ever *add* missing tables, never alter an existing
one to match a changed model, so a real schema change would need a manual,
undocumented, one-off fix every time (exactly the problem the hand-written
`core/saved_searches/migration.py` already solved once, out of necessity,
for SQLite - see "Saved searches and background scanning" below; Alembic
is the general-purpose version of that same problem, going forward, for
any backend).

- **`alembic/env.py` resolves its database URL from
  `marketplace_alert.config.settings.database_url`** (via the exact same
  `resolve_database_url()`/`normalize_database_url()` the running app
  uses) - **not** from `alembic.ini`'s `sqlalchemy.url` (left blank in
  `alembic.ini` on purpose, with a comment explaining why). This guarantees
  a migration command can never accidentally target a different database
  than the app itself would connect to. It also imports
  `core/persistence/models.py` and `core/saved_searches/models.py` purely
  for the side effect of registering their tables on `Base.metadata` -
  `target_metadata = Base.metadata` is what `alembic revision
  --autogenerate` diffs against, so a model module never imported here
  would be invisible to it.
- **`alembic/versions/<rev>_baseline_schema.py`** is the first, baseline
  migration - it represents *today's* model schema (post multi-marketplace:
  `saved_searches` has no `marketplace` column; `saved_search_marketplaces`
  is the join table), autogenerated by diffing `Base.metadata` against a
  fresh, completely *empty* temporary SQLite database - never against the
  developer's real local database, and never against any production
  database. Purely additive (`create_table`/`create_index` only) on the
  upgrade path - no destructive operations. `downgrade()` is the standard
  reverse and only runs if explicitly invoked (`alembic downgrade`), never
  automatically.
- **Adopting Alembic against a database that already has the current
  schema needs `stamp`, not `upgrade`** - verified directly, not assumed:
  running `alembic upgrade head` against a SQLite database that already
  has these tables (e.g. the developer's existing local
  `marketplace_alert.db`, built by `create_all()` before Alembic existed)
  fails cleanly with "table already exists" (no data touched, nothing
  dropped - a clean failure, not silent corruption), because Alembic has
  no record of that schema being already-current. `alembic stamp head`
  is the correct tool for that case - it records the revision as applied
  without executing any DDL at all. A brand-new, empty database (a fresh
  production PostgreSQL instance) uses `upgrade head` normally. See
  README.md "Database" for the exact commands, and
  `tests/test_alembic_migrations.py` for both scenarios verified against
  real (temp-file) SQLite databases.
- **Migrations now run automatically inside the app's own startup
  (`main.py`'s `lifespan`), for PostgreSQL only - see "Automatic
  migrations on Render Free" immediately below for why this reverses
  what this project originally planned, and how the obvious risk (more
  than one process racing to migrate at once) is guarded against.**
  `init_db()` still runs in `lifespan` unconditionally, same as before -
  it remains a no-op for PostgreSQL (see above), now running immediately
  *after* the automatic migration rather than being the only PostgreSQL-
  relevant step in that part of startup.
- **Tests never require a real PostgreSQL server.** `create_engine()`
  builds an `Engine` object lazily - it only actually opens a connection
  when one is checked out of the pool - so `tests/test_database_config.py`
  asserts on the resolved URL/dialect/driver without connecting anywhere,
  and `tests/test_alembic_migrations.py` runs the real baseline migration
  (upgrade, downgrade, re-upgrade, the `stamp`-vs-`upgrade` distinction)
  against throwaway temp-file SQLite databases only.

### Automatic migrations on Render Free

**Render's Free plan - what this project actually runs on - has no
Pre-Deploy Command.** The paragraph above originally assumed migrations
would be applied "before any new instance starts serving traffic (e.g. a
Render Pre-Deploy Command, or a manual `alembic upgrade head` before a
deploy)" - Pre-Deploy Command is a paid-plan-only feature, and requiring
a manual `alembic upgrade head` before every single deploy isn't a
realistic, sustainable process. Discovered only after confirming
`DATABASE_URL` really is set in production (see above) - the
`first_discovered_at` index added in the previous hardening pass had no
automatic path to ever actually reach the live database. See
PROJECT_CONTEXT.md decision #20 for the full reasoning; this section is
the mechanism itself.

```
main.py's lifespan():
    run_pending_migrations(engine)        # PostgreSQL only - see below
    init_db()                              # SQLite only (unchanged)
    migrate_legacy_marketplace_column(engine)
    _background_scanner.start()
```

`core/persistence/migrations.py:run_pending_migrations(bind)`:

- **No-ops immediately for any non-PostgreSQL dialect** (`bind.dialect.name
  != "postgresql"`) - SQLite's existing `create_all()` bootstrap in
  `init_db()` is completely untouched; this function adds a PostgreSQL-only
  path, it doesn't change local dev/test behavior at all.
- **Runs via FastAPI's ASGI `lifespan` protocol, not a wrapper/entrypoint
  script.** Uvicorn (and any ASGI server) blocks accepting connections
  until the `lifespan` startup phase completes - this alone already means
  "runs before the app begins serving requests," with no new entrypoint
  script, no change to Render's Start Command, and none of the
  process-signal-handling/`exec` concerns a wrapper script would
  introduce. A wrapper script was the documented fallback option if this
  couldn't be made safe and deterministic - it could, so it wasn't
  needed.
- **A Postgres session-level advisory lock guards the actual migration
  run**, acquired via the *non-blocking* `pg_try_advisory_lock`, polled
  from Python with a bounded wait (`settings.migration_lock_timeout_seconds`,
  default 30s) rather than the blocking `pg_advisory_lock` (whose
  interaction with Postgres's `lock_timeout` setting isn't consistently
  documented enough across versions to depend on for this). Render Free
  only ever runs a single instance of this service, so true concurrent
  *instances* racing to migrate isn't realistically possible - the lock
  is defense-in-depth for a brief overlap during a deploy transition or
  two close-together manual restarts, not a response to horizontal
  scaling this plan doesn't have. **Released on the exact same
  connection/session that acquired it** - PostgreSQL's session-level
  advisory locks are tied to the session that took them, so releasing
  from a different connection would silently do nothing, not actually
  free the lock.
- **Fails fast.** Any failure - a bad migration, a lock-acquire timeout,
  a connectivity problem - propagates out of `run_pending_migrations()`,
  out of `lifespan()`, and fails FastAPI/uvicorn startup outright. Render
  then keeps serving the previous successful deploy rather than ever
  letting a broken migration or a schema/code mismatch go live - a failed
  startup is the intended, safe outcome here.
- **Idempotent, additive-only, never destructive** - identical guarantees
  to every other Alembic usage in this project: `alembic upgrade head`
  against an already-current database is a documented no-op, so restarts
  and redeploys are safe to run this on repeatedly; only `command.upgrade`
  is ever called, never `command.downgrade`.
- **Never logs `DATABASE_URL` or any credential** - only the dialect name
  and generic progress/failure messages; the advisory-lock key is a fixed,
  arbitrary constant unrelated to the connection string.
- **Tested without a real PostgreSQL server** (`tests/test_startup_migrations.py`),
  using the same lazy-`Engine`/mocked-connection approach
  `tests/test_database_config.py` already established, plus two tests
  that invoke `main.py`'s real `lifespan()` directly (bypassing
  `tests/conftest.py`'s autouse no-op-lifespan safety net on purpose) to
  prove the actual startup ordering and fail-fast behavior end to end.

## Notifications

`marketplace_alert/core/notifications/` alerts on newly discovered listings.
It mirrors the connector pattern exactly: an interface in `core/`, concrete
providers outside it.

```
routes (main.py)
    -> NotificationService.notify_new_listings(new_listings: list[Listing])
        -> paced, in order: NotificationProvider.send_listing_alert(listing)
            -> TelegramNotificationProvider -> Telegram Bot API
               (retries its own transient failures internally, bounded)
```

- **`core/notifications/base.py`** defines `NotificationProvider`
  (`is_enabled` property, `send_listing_alert(listing)`) and
  `NotificationError`. Same rule as `MarketplaceConnector`: this is the only
  contract the core system depends on, never a specific provider's SDK.
- **`core/notifications/service.py`** (`NotificationService`) is what routes
  call. `notify_new_listings(listings)` sends one alert per listing, **in
  order**, via the provider it was given. It checks `is_enabled` first — a
  disabled provider (e.g. missing credentials) means "skip and log", not an
  error. If a provider raises `NotificationError` for one listing (i.e. that
  provider has already given up on it, retries included), the service logs
  it and keeps going to the next listing; `notify_new_listings` itself
  **never raises**, so a notification failure can never turn into a failed
  scan. `NotificationService.is_enabled` (a thin pass-through to the
  provider's own `is_enabled`) exists so callers - currently: the
  dashboard's status section - can ask "is notifying configured?" without
  reaching into the service's private `_provider` attribute.
  - **Burst pacing.** `NotificationService.__init__` takes an optional
    `send_delay_seconds` (default `0.0`, main.py wires in
    `settings.telegram_send_delay_seconds`). Between each listing's send
    (never before the first one, so a single new listing is never
    artificially delayed) it sleeps that long before continuing - a saved
    search that discovers 10, 25, or 50 new listings at once sends them
    spaced out rather than back-to-back. This is a generic pacing knob, not
    Telegram-specific logic - it lives here rather than in the provider
    because it governs the gap *between separate listings' sends*, a
    concern of whoever's looping over the batch, not of a single send
    itself. See "Why these choices" for why this isn't a persistent
    background queue/thread.
- **`notifications/telegram/provider.py`** (`TelegramNotificationProvider`)
  is the only concrete provider so far. It calls the Telegram Bot API's
  `sendMessage` directly via `httpx` — no SDK dependency. It reads
  `bot_token`/`chat_id` from whatever it's constructed with (`main.py` wires
  these from `settings.telegram_bot_token` / `settings.telegram_chat_id`,
  which come from the `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` environment
  variables — never hard-coded). If either is missing, `is_enabled` is
  False and a warning is logged once, at construction. `format_listing_message()`
  is a standalone function (title, marketplace, price + currency if
  present, location if present, listing URL) so message formatting is
  testable without any HTTP call.
  - **Bounded retry with backoff, for one send.** `send_listing_alert`
    retries *transient* failures - HTTP 429, 500, 502, 503, 504, and
    timeout/connection errors - up to `max_retries` times (`main.py` wires
    in `settings.telegram_max_retries`, default 3; so up to 4 attempts
    total). Each retry waits, then tries again: on HTTP 429, Telegram's own
    `parameters.retry_after` (seconds) from the response body is used
    directly when present, since it reflects Telegram's actual rate-limit
    window rather than a guess; otherwise (429 without that hint, or any
    5xx/timeout) it waits an exponentially growing delay -
    `retry_base_seconds * 2^(attempt-1)` (`settings.telegram_retry_base_seconds`,
    default 2.0s, so 2s/4s/8s). *Permanent* failures - any other non-200
    status (400 malformed request, 401/403 bad credentials, 404 chat not
    found, etc.) and a 200 response with `"ok": false` in the body - are
    never retried; they raise `NotificationError` on the first attempt,
    since retrying a bad bot token or chat ID would never succeed. Once
    retries are exhausted (or a permanent failure hits), it raises
    `NotificationError` exactly as before this change - callers
    (`NotificationService`) don't need to know whether a failure was
    transient-then-exhausted or permanent; the contract is unchanged.

`marketplace_alert/main.py` wires a single `TelegramNotificationProvider`
into a `NotificationService` once at import time, exposed through a
`get_notification_service()` FastAPI dependency (same override-in-tests
pattern as `get_db_session` — see "Local persistence" above). `/scan` calls
`notification_service.notify_new_listings(result.new_listings)` — only the
listings `ListingDiscoveryService` classified as new. Already-seen listings
never reach the notification service at all, so they can never trigger a
second alert - and this is true regardless of whether a *previous* attempt
to notify about that listing succeeded or failed: `ListingDiscoveryService`
persists a listing as discovered *before* `NotificationService` is ever
called (see `SavedSearchRunner._run_one_marketplace` in "Saved searches and
background scanning" below), so a Telegram failure - even after every
retry is exhausted - can never make a listing look "new again" on a later
scan. The database, not delivery success, is the source of truth for
"already discovered".

**Security**: the Telegram bot token is part of the request URL
(`https://api.telegram.org/bot<TOKEN>/sendMessage`), so
`TelegramNotificationProvider` is written to never log that URL, the raw
`httpx` exception, or the raw response object — only sanitized details
(exception type name, HTTP status code, Telegram's own `description`
field, retry attempt counts and wait times). Never log or return
`TELEGRAM_BOT_TOKEN` anywhere, including in API responses, documentation,
or any of the new retry/backoff log lines. `httpx` itself also logs every
outbound request URL at INFO by default (independent of anything our own
code logs) — a real `bot<TOKEN>` URL leaked into a local log file this way
during manual testing of the scheduler before this was caught.
`configure_logging()` (`core/logging_config.py`) now explicitly sets
`logging.getLogger("httpx")` to `WARNING`, so this can never happen
regardless of the app's own configured log level.

**Testing**: automated tests must never send a real Telegram message, even
though a developer's real `.env` may be loaded during test runs (it only
affects `Settings`, not what any test actually calls). `tests/conftest.py`'s
`client` fixture always overrides `get_notification_service` with a
`FakeNotificationProvider` that just records what it was asked to send.
Provider-level tests (`tests/test_telegram_provider.py`) construct their own
`TelegramNotificationProvider` with fake credentials and monkeypatch
`httpx.post` — no network call ever happens; an autouse fixture also
monkeypatches `time.sleep` to record requested durations instead of
actually pausing, so retry/backoff tests (bounded retry count, exponential
backoff values, `retry_after` handling) run instantly rather than actually
waiting seconds per test.

## Saved searches and background scanning

Until now, finding new listings meant a human calling `/scan`. Saved
searches let the system do that itself, on a schedule, for many queries at
once - and one saved search can target **multiple marketplaces**.

```
SavedSearchRepository (CRUD, list_due_for_scan)
    <- SavedSearchService (validated CRUD - used by the API routes)
    <- SavedSearchRunner (per marketplace: search -> dedup -> notify; then mark_scanned)
        <- POST /saved-searches/{id}/run            (manual, one search, now)
        <- BackgroundScanner._run_one                (automatic, on a tick)
```

- **`core/saved_searches/models.py`** defines `SavedSearch` (`id`, `query`,
  `is_active`, `scan_interval_seconds`, `created_at`, `updated_at` - bumped
  only on a *definition* edit, `last_scanned_at` - set only after a *scan*,
  never confused with `updated_at`) and `SavedSearchMarketplace` (`id`,
  `saved_search_id`, `marketplace_name`, unique on the pair) - a normalized
  join table, never a comma-separated string on `SavedSearch`. `SavedSearch`
  exposes a `marketplaces: list[str]` property over the relationship
  (`marketplace_links`, `cascade="all, delete-orphan"` - deleting a
  `SavedSearch`, or replacing its marketplace list, safely cleans up the
  old association rows) so nothing above the model touches the join table
  directly.
- **`core/saved_searches/schemas.py`** validates shape: `query` can't be
  blank, `marketplaces` can't be empty and can't contain duplicates,
  `scan_interval_seconds` can't be below `MIN_SCAN_INTERVAL_SECONDS` (60,
  so the scanner can't hammer a marketplace or Telegram). Whether each
  marketplace name is actually supported is a *business* rule needing the
  connector registry, so it's checked one layer up, in the service - never
  in the schema. `SavedSearchUpdate.marketplaces`, when provided,
  *replaces* the full set (add/remove a marketplace by sending the new
  complete list) - the same "set to this value if provided" semantics
  every other field on that schema already has.
- **`core/saved_searches/repository.py`** (`SavedSearchRepository`) is the
  only module issuing SQL/ORM queries for saved searches, same rule as
  `ListingRepository`. `list_due_for_scan()` returns active searches never
  scanned, or whose `last_scanned_at + scan_interval_seconds <= now`.
  (SQLite drops tzinfo on datetime round-trips even for
  `DateTime(timezone=True)` columns - every value written is UTC, so this
  method reattaches it before comparing, rather than crashing on a
  naive-vs-aware comparison or silently comparing wrong.) `update()`
  replacing `marketplaces` clears the collection and **flushes before**
  extending it with the new list, deliberately in two steps: reassigning
  the collection in one step lets SQLAlchemy interleave the DELETE/INSERT
  statements, which trips the `(saved_search_id, marketplace_name)` unique
  constraint whenever a marketplace is unchanged across the edit (found by
  testing an edit from `["mock"]` to `["etsy", "mock"]`, not anticipated
  up front).
- **`core/saved_searches/service.py`** (`SavedSearchService`) is what the
  API routes call for CRUD. Every marketplace in the list is checked via
  an injected `is_marketplace_supported` predicate (`main.py` wires in the
  real registry function) - the service itself never imports the registry.
- **`core/saved_searches/runner.py`** (`SavedSearchRunner`) is the *one*
  place "run this saved search" is implemented, across **all** of its
  marketplaces: for each one, resolve the connector (via an injected
  `resolve_connector` function - never a concrete connector class),
  `connector.search(query)`, feed results through `ListingDiscoveryService`,
  feed the new ones through `NotificationService` - then, once every
  marketplace has been attempted, `SavedSearchRepository.mark_scanned()`
  once for the whole search. Both the manual `POST /saved-searches/{id}/run`
  endpoint and the background scanner call this same class/method - there
  is exactly one implementation of the run logic, so the two paths can't
  drift apart. **A failure in one marketplace is caught inside the runner**
  (`MarketplaceConnectorError` logged with its sanitized message; anything
  else logged in full server-side but reported back as a generic message)
  and recorded as that marketplace's result - it never stops the *other
  marketplaces in the same search*. This is a deliberate change from the
  single-marketplace version, where errors propagated out of `run`/
  `run_by_id` for the caller to handle: with multiple independent
  marketplaces per run, there's nothing left for a per-marketplace failure
  to usefully propagate *to*. Resilience *across saved searches* (one
  saved search failing must not stop another) is still the scheduler's
  job, unchanged - the two failure boundaries are independent and neither
  subsumes the other now that "one saved search" no longer implies "one
  marketplace, one thing that can fail". The result
  (`SavedSearchRunResult.results: list[MarketplaceRunResult]`, each with
  `marketplace`, `new_count`, `already_seen_count`, `error`) reports every
  marketplace's outcome individually, plus `new_count`/`already_seen_count`
  totals for simple display.
- **`core/saved_searches/migration.py`** (`migrate_legacy_marketplace_column`)
  is a one-time, idempotent, hand-written migration - this project has no
  Alembic, and `init_db()`'s `Base.metadata.create_all()` only ever adds
  tables that don't exist, it never alters an existing one. Multi-marketplace
  support changed `saved_searches`' shape itself (the single `marketplace`
  column is gone), so real saved searches created before this change needed
  an explicit, safe conversion path rather than being silently destroyed.
  Runs once at startup, right after `init_db()`: if `saved_searches` still
  has the old `marketplace` column, copy each row's value into
  `saved_search_marketplaces` (skipping any already-linked, so reruns are
  safe), then drop the legacy column and any index on it (SQLite's
  `DROP COLUMN` doesn't clean up indexes on the dropped column by itself -
  the original model had `index=True` there, and the migration failed the
  first time it ran against a real copy of the developer's database until
  this was added; a hand-built test fixture without that index had passed,
  which is exactly why it hadn't caught the bug). The whole thing runs in
  one `engine.begin()` transaction, so a failure rolls back cleanly rather
  than leaving a half-migrated table. Verified against a real backup of the
  developer's database before being run against the real one.

**The scheduler** (`core/scheduler/`) is one central background thread, not
one thread or process per saved search:

- **`scanner.py`** (`BackgroundScanner`) ticks every `scheduler_tick_seconds`
  (default 5s - a polling granularity, unrelated to any individual saved
  search's own `scan_interval_seconds`). Each tick:
  `list_due_for_scan()` → for each due id, acquire the run guard → run it
  in its own session/transaction via `SavedSearchRunner.run_by_id()` →
  commit on success, rollback and log-and-continue on failure → release the
  guard. One saved search raising never stops the tick, the loop, or any
  other saved search.
- **`guard.py`** (`SavedSearchRunGuard`) is a small thread-safe "is this
  saved search id currently running?" set, shared between the scanner and
  the manual `/run` endpoint - both acquire before running and release
  after, so the same saved search can never be scanned by both paths (or
  two overlapping ticks) at once. The manual endpoint returns `409` if the
  saved search is already running or currently inactive.
- Started/stopped in `main.py`'s `lifespan` (`_background_scanner.start()`
  / `.stop()`), alongside `init_db()`. Because it's a background thread
  with no FastAPI request to hang a `Depends` off, `main.py` binds it to
  the *real* `NotificationService` and the real `SessionLocal` at startup -
  it is not overridable per-request like `get_db_session`/
  `get_notification_service`. Tests never trigger `lifespan` (see "Local
  persistence" above), so the real thread never starts during `pytest`;
  scheduler tests construct their own `BackgroundScanner` directly, with a
  fake connector resolver and a fake notification provider, and call
  `run_due_searches()` synchronously - no real thread, timer, or sleep
  needed to test due-detection, inactive-search exclusion, or one-search-
  fails-without-stopping-others.

## The management dashboard

`GET /` (`marketplace_alert/main.py`) is a local, unauthenticated,
server-rendered dashboard - the first UI on top of the API, so a
non-technical user can manage saved searches without `curl`, Swagger, or
touching code. It is deliberately thin:

```
GET /  (main.py: dashboard())
    -> SavedSearchService.list_all()          (same service the JSON API uses)
    -> list_supported_marketplaces()           (registry - checkbox + status-panel source)
    -> NotificationService.is_enabled           (status: Telegram configured?)
    -> get_connector(name).is_configured, per marketplace  (status: configured?)
    -> templates/dashboard.html (Jinja2, autoescaped)

Browser (dashboard.js, vanilla JS, no framework)
    -> POST   /saved-searches            (create)
    -> POST   /saved-searches/{id}/run   (Run Now)
    -> PATCH  /saved-searches/{id}       (Enable/Disable - {"is_active": ...})
    -> DELETE /saved-searches/{id}       (Delete, after a confirm() dialog)
```

- **No duplicated business logic.** The initial page load reads through
  `SavedSearchService` - the exact same service `POST/GET/PATCH/DELETE
  /saved-searches` already use (via the existing `get_saved_search_service`
  dependency). Every *action* (create, run, enable/disable, delete) is the
  browser's JS calling those exact same JSON endpoints - there is no
  second "create a saved search" or "run a saved search" implementation
  anywhere in the dashboard code to drift out of sync with the API.
- **Server-rendered HTML + a little vanilla JS, deliberately no framework.**
  `marketplace_alert/templates/dashboard.html` (Jinja2, autoescaping is on
  by default - user-entered query text can never inject markup, see the
  XSS test in `tests/test_dashboard.py`) renders the initial page;
  `marketplace_alert/static/dashboard.js` handles form submission and the
  per-row Run Now/Enable-Disable/Delete buttons via `fetch()`, then reloads
  the page so server-rendered state stays the single source of truth - no
  client-side templating or state management to keep in sync. A one-shot
  flash message is passed across that reload via `sessionStorage` (set
  before reload, read and cleared on the next page's load) rather than a
  session/cookie mechanism, since there's no auth layer yet to hang a
  session off. `marketplace_alert/static/dashboard.css` is hand-written,
  no framework - mobile-first (a wrapped/stacked form, a horizontally
  scrollable table) rather than adding a CSS framework dependency for an
  internal MVP tool.
- **Marketplace list has one source of truth.** The create form's
  marketplace checkboxes (plus a "select all" convenience checkbox that
  just toggles the others via JS) are populated server-side from
  `list_supported_marketplaces()` (the connector registry) - not a
  hard-coded `["mock", "etsy"]` list living separately in a template or JS
  file that could drift from what `is_marketplace_supported` actually
  accepts. `dashboard.js` collects every *checked* box's `value` into the
  `marketplaces` array the create request sends - a saved search targets
  as many marketplaces as are checked, one logical `SavedSearch`, never
  one record per marketplace. The saved-searches table shows each search's
  selected marketplaces joined into one cell (e.g. "Etsy, Mock").
- **Status is booleans only, never credential values - and, like the
  marketplace checkboxes, driven by the registry rather than one
  hard-coded line per marketplace.** "Telegram configured" reads
  `NotificationService.is_enabled`; every marketplace's configured state
  comes from one loop over `list_supported_marketplaces()`, each calling
  `getattr(get_connector(name), "is_configured", True)` (the same
  fallback `api/v1/marketplaces.py` uses, since a connector like the mock
  one has no credentials concept at all) - a `{name, display_name,
  configured}` entry per marketplace, rendered by the template with one
  `{% for %}` loop instead of one hand-written `<li>` per marketplace.
  This replaced an earlier version of this panel that hard-coded an
  "Etsy" and an "eBay" row individually - it worked, but would have meant
  writing a *third* hard-coded row for Reverb, exactly the kind of
  per-marketplace UI duplication this project's own rules rule out (see
  "Why these choices" below). No route, template, or JS file ever
  touches `settings.telegram_bot_token`, `settings.etsy_api_key`,
  `settings.ebay_app_id`/`settings.ebay_cert_id`,
  `settings.reverb_api_token`, or any other credential value - only each
  connector's already-boolean `is_configured`.
- **Errors shown to the user are always the API's own sanitized messages.**
  `dashboard.js` displays whatever `detail` string a failed `fetch()`
  response carries (e.g. "Saved search not found", "query cannot be
  empty") - the same messages `/docs`/`curl` users already see - and a
  generic "Could not reach the server" for network-level failures. Since
  none of the existing API error responses include stack traces or
  credentials (see the notification/connector sections above), the
  dashboard inherits that safety for free rather than needing its own
  error-sanitizing logic.

## Mobile API

`/api/v1` (`marketplace_alert/api/v1/`) is a versioned, JSON-only REST API
- groundwork for a future Android/iOS app (not built yet - see
PROJECT_CONTEXT.md). Added *alongside* every existing route (`/`, `/docs`,
`/health`, `/search`, `/scan`, the legacy `/saved-searches*`) - nothing
about it removes or changes any of them. It depends on no HTML/dashboard
behavior at all, unlike the dashboard, which is itself just a browser
client of the *legacy* JSON API.

```
main.py
    -> app.include_router(api_v1_router)   (marketplace_alert/api/v1/__init__.py, prefix "/api/v1")

GET  /api/v1/status            -> list_supported_marketplaces(), NotificationService.is_enabled, SELECT 1
GET  /api/v1/marketplaces      -> list_supported_marketplaces() + get_connector(id).health_check()/is_configured
GET  /api/v1/saved-searches    -> SavedSearchService.list_all()          (same service the legacy routes use)
POST /api/v1/saved-searches    -> SavedSearchService.create(...)
GET/PATCH/DELETE .../{id}      -> SavedSearchService.get/update/delete(...)
POST .../{id}/run              -> SavedSearchRunner.run(...)             (same runner + run guard, reshaped response)
GET  /api/v1/listings          -> ListingRepository.list_recent/count(...)
```

- **A shared-dependencies module, to avoid a circular import.**
  `marketplace_alert/dependencies.py` now owns the singletons `main.py`
  used to construct itself: the real `NotificationService`, the
  `SavedSearchRunner`/`SavedSearchRunGuard` the scheduler and manual-run
  endpoints share. Both `main.py`'s legacy routes and the `/api/v1`
  routers depend on the *exact same* objects from here - `main.py` can't
  be imported by `api/v1/` (it mounts `api/v1`'s router, so that would be
  circular), and duplicating the singletons would let the same saved
  search run through both an old and a new endpoint at once, bypassing the
  run guard. `main.py` re-imports everything from `dependencies.py` under
  its original (leading-underscore) names, so this is a pure extraction -
  every existing test import (`from marketplace_alert.main import
  get_notification_service`, `_saved_search_run_guard`, etc.) still
  resolves to the identical objects, unchanged.
- **No saved-search business logic is duplicated.**
  `api/v1/saved_searches.py`'s five CRUD endpoints are thin adapters over
  the exact same `SavedSearchService` (`get_saved_search_service`,
  `dependencies.py`) the legacy routes use, and its manual-run endpoint
  constructs a `SavedSearchRunner` and acquires `saved_search_run_guard`
  exactly like the legacy `POST /saved-searches/{id}/run` does - the
  *only* new code is reshaping the resulting `SavedSearchRunResult` (a
  list of per-marketplace results) into the mobile response shape (a dict
  keyed by marketplace name, plus `query` and renamed totals - see
  `api/v1/schemas.py:SavedSearchRunResult`), genuinely more convenient for
  a mobile client, not a second implementation of "run a saved search".
  Request/response schemas for saved searches
  (`SavedSearchCreate`/`Update`/`Read`) are literally re-exported from
  `core/saved_searches/schemas.py`, not re-declared - the mobile contract
  and the legacy one share one validated definition, so they can't
  quietly drift out of sync on something like the minimum scan interval.
- **`GET /api/v1/marketplaces` is entirely registry-driven.** Every id
  comes from `list_supported_marketplaces()` - never a separately
  maintained list (the only marketplace-specific data here is a small,
  purely cosmetic id -> display-name lookup for brand casing, e.g.
  `"ebay"` -> `"eBay"`; a marketplace missing from it still appears, just
  title-cased instead). `configured` uses `getattr(connector,
  "is_configured", True)` since not every connector has a credentials
  concept (the mock one has nothing to configure); `available` is the
  connector's own `health_check()` - part of the `MarketplaceConnector`
  interface every connector implements, and for Etsy/eBay it happens to
  return `is_configured` today (see their sections above), but the
  contract here is `health_check()`, not `is_configured`, so a future
  connector could make `available` a real liveness probe without changing
  this endpoint.
- **`GET /api/v1/listings` documents two real gaps instead of faking
  data or a relationship that isn't stored** - required explicitly by
  this feature's own brief, not just good practice:
  - `price`/`currency`/`location`/`condition`/`image_url` are always
    `null`. `DiscoveredListing` (`core/persistence/models.py`) has never
    stored them - only `marketplace`, `external_listing_id`, `title`,
    `listing_url`, and the two discovery timestamps, since Phase 3.
    `api/v1/listings.py`'s `_to_listing_out()` maps `DiscoveredListing` ->
    `ListingOut` *explicitly*, field by field, rather than via Pydantic's
    `from_attributes` auto-mapping - deliberately, so the fact that five
    fields don't exist on the source row is impossible to miss reading the
    code, not hidden behind a generic mapper quietly defaulting them.
  - **No `saved_search_id` filter.** `DiscoveredListing` has no
    relationship to `SavedSearch` at all - its dedup identity
    (`marketplace`, `external_listing_id`) is deliberately *global* across
    every saved search that happens to match it (see "Local persistence
    and duplicate detection" above), not scoped to whichever search first
    discovered it. A `saved_search_id` filter would have to invent that
    relationship.
  - **No `only_new` filter.** "New" is a property of one specific scan run
    (`ListingDiscoveryResult.new_listings`), never a column persisted on
    the row itself - there is no reliable, stored "is this new" flag to
    filter on.
- **Pagination is `limit`/`offset` with a hard cap (100), sorted
  newest-first** (`first_discovered_at DESC`, `id DESC` as a stable
  tiebreaker) - `ListingRepository.list_recent()`/`count()`
  (`core/persistence/repository.py`), the only new persistence-layer code
  this added; no new table, no new migration.
- **Errors stay in the existing `{"detail": "..."}` shape** (FastAPI's own
  `HTTPException`), the same one the legacy routes and the dashboard's
  error handling already depend on - a different mobile-specific error
  envelope was deliberately not invented, since that would mean either two
  incompatible error shapes across the same app or changing the legacy
  routes' (and the dashboard's) existing error handling, which this task
  explicitly rules out breaking.
- **CORS is prepared, off by default.** `CORSMiddleware` (FastAPI's own,
  no new dependency) is always added, with `allow_origins` from
  `settings.cors_allowed_origins` (`CORS_ALLOWED_ORIGINS`, comma-separated)
  - empty unless explicitly configured, so no cross-origin browser access
  happens until someone opts in; never `"*"`. Native mobile apps don't use
  browser CORS at all - this exists only for possible future browser-based
  tooling.
- **No authentication yet - not implemented, and not faked either.**
  Every `/api/v1` endpoint is exactly as open as the legacy routes today
  (consistent with "no auth anywhere yet" - see PROJECT_CONTEXT.md). The
  extension point is already in place, though: every endpoint already
  takes its dependencies via FastAPI `Depends(...)` (a service, a session),
  so adding `Depends(require_authenticated_user)` (or similar) to each
  endpoint's signature later is a small, additive change per endpoint, not
  a rewrite - no placeholder/no-op check and no fabricated user id were
  added now, since neither would be meaningfully different from having no
  auth at all, just more confusing about it.
- **Tags/OpenAPI**: each sub-router carries its own descriptive tag
  (`"Mobile API - Status"`, `"- Marketplaces"`, `"- Saved Searches"`,
  `"- Listings"`, registered with descriptions via `FastAPI(openapi_tags=...)`
  in `main.py`) and every operation has a `summary`/`description`, so
  `/docs` groups and documents `/api/v1` clearly, separately from the
  legacy routes' operations.

## Why these choices

- **FastAPI + Pydantic**: fast to build, validates data at the boundary,
  and the same models double as API schemas later.
- **Abstract base class over duck typing**: `ABC` + `@abstractmethod` gives
  a hard failure at instantiation time if a connector is incomplete, rather
  than a silent `AttributeError` at runtime.
- **SQLAlchemy + SQLite locally, PostgreSQL in production via the same
  models/code**: gives duplicate detection a real uniqueness guarantee (a
  DB constraint, not just application logic) while staying file-based and
  zero-setup for local dev. `DATABASE_URL` is the only thing that changes
  to move to PostgreSQL - see "Database selection and PostgreSQL support"
  above for exactly how that selection/normalization works.
- **`psycopg` (v3), not `psycopg2`**: the actively-maintained, modern
  PostgreSQL driver, with first-class SQLAlchemy 2.x support - no reason
  to add the older `psycopg2` as a new dependency on a fresh integration.
- **Alembic, not `create_all()`, for PostgreSQL schema changes**: a real
  production database needs schema *changes* (add/alter/drop a column) to
  be reviewed and applied deliberately, not implicitly re-derived from
  whatever the model code happens to say at whatever moment the app
  happens to restart - `create_all()` can only ever add a missing table,
  never alter an existing one, so relying on it alone would mean every
  real schema change needs an undocumented manual fix (this project
  already hit exactly that problem once, for SQLite, before Alembic
  existed - see `core/saved_searches/migration.py`, "Saved searches and
  background scanning" below). SQLite keeps `create_all()` deliberately -
  it's genuinely safe there (ephemeral, single-file, local/test-only) and
  changing it would add ceremony to local dev for no benefit.
- **Repository + service layers, not raw DB calls in routes**: keeps
  `main.py` thin and keeps every SQL/ORM detail in one place
  (`persistence/repository.py`), so persistence logic is testable without
  spinning up the FastAPI app.
- **NotificationProvider interface + httpx, no Telegram SDK**: sending a
  Telegram message is one HTTP POST, so a full SDK dependency isn't worth
  it - and the interface means Telegram is not privileged over any future
  channel (email, etc.).
- **Etsy Open API v3 over httpx, no SDK, no scraping**: one GET request
  with an API-key header is simple enough not to need a client library, and
  scraping was explicitly ruled out. Endpoint and auth were verified
  against Etsy's own OpenAPI spec and docs before implementation, not
  guessed - see "The Etsy connector" above.
- **eBay Buy Browse API over httpx, no SDK, legacy Finding API explicitly
  ruled out**: same reasoning as Etsy - a plain OAuth token POST plus a
  plain GET search doesn't need a client library, and eBay's older Finding
  API was deliberately avoided in favor of the current, actively maintained
  Browse API. A dedicated `EbayTokenManager` module (rather than folding
  token logic into the connector class itself) keeps "how to get an OAuth
  token" and "how to search and normalize results" independently readable
  and testable - the connector doesn't need to know *how* a token was
  obtained, only that `get_token()` returns a currently-valid one. Endpoint
  and auth were verified against eBay's own official Browse API references
  before implementation, not guessed - see "The eBay connector" above.
- **Reverb API v3 over httpx, no SDK, no scraping, following `_links`
  rather than hand-building pagination URLs**: same reasoning as Etsy/
  eBay for the "no SDK, no scraping" half. The pagination choice is
  Reverb-specific: its HAL+JSON API explicitly documents "never construct
  your own URLs... follow `_links`" as its design principle, so
  `ReverbMarketplaceConnector` follows `_links.next.href` verbatim rather
  than reconstructing `page`/`per_page` query params itself - the more
  correct approach for a HATEOAS API, and the one Reverb's own docs
  actually ask integrators to use. Endpoint and auth were verified
  against the developer's own manually-confirmed request plus Reverb's
  official docs before implementation; the exact nesting of a few
  optional fields (price/condition/shop/photos) wasn't independently
  confirmable from Reverb's public docs as of this implementation, so
  those are coded defensively (multiple plausible shapes, `None` if none
  match) rather than guessed with false confidence - see "The Reverb
  connector" above for the full citation list and what's confirmed vs.
  inferred.
- **Bonanza's own eBay-Finding-API-shaped `findItemsByKeywords`, verified
  from a real third-party SDK's HTTP client rather than guessed at**: the
  official docs' auto-converted pages didn't preserve a literal example
  response for this call the way Reverb's did, so the actual wire
  protocol (a `POST` with the operation name and JSON parameters combined
  into one form-encoded field - not a plain JSON body, and not something
  that would have been guessable) was confirmed against
  github.com/Shoplo/bonapitit-bonanza-php-sdk's real client code instead
  of assumed from the docs' schema-only description. Page-number
  pagination (not a HAL `_links.next`, since Bonanza's API doesn't use
  HAL) stops on a short page, `result_limit`, or a hard page cap -
  whichever comes first, the same three-way stop condition as Reverb's
  pagination, just adapted to Bonanza's page-number-based paging instead
  of link-following. Nine other named marketplace candidates were
  evaluated before settling on Bonanza as the one worth implementing next
  - see PROJECT_CONTEXT.md's marketplace feasibility notes for the full
  investigation and why each of the others was ruled out (no API, an API
  that doesn't support marketplace-wide keyword search, or a credential
  that requires a manual approval/human OAuth step this connector
  architecture doesn't yet have a mechanism for).
- **One shared `display_name_for()` in the connector registry, not a
  per-UI display-name dict**: adding Reverb was the second time a
  marketplace needed proper brand casing (e.g. "eBay", "Reverb") in more
  than one place (the dashboard's status panel and the mobile API's `GET
  /api/v1/marketplaces`) - keeping two independent copies in sync by hand
  is exactly the kind of "hard-code it in multiple UIs" this project
  otherwise avoids for the marketplace *list* itself
  (`list_supported_marketplaces()`). Moving the mapping into
  `connectors/registry.py` (the one place already allowed to know about
  concrete connectors) and having both call sites import it fixes that
  for good, not just for Reverb - the dashboard's status panel also
  stopped hard-coding one `<li>` per marketplace in the same change (see
  "The management dashboard" above), for the same reason.
- **A stdlib `threading` loop for the scheduler, not APScheduler or asyncio**:
  the rest of the app (routes, SQLAlchemy sessions) is already synchronous,
  so a plain background thread ticking on an interval needed no new
  dependency and no sync/async bridging. One central loop, not one
  thread/process per saved search, per the requirement that adding more
  saved searches must not mean adding more OS-level concurrency.
- **stdlib logging, JSON-formatted**: "structured logging" without adding a
  dependency; can be swapped for `structlog` later if needed.
- **Rate-controlled Telegram delivery as a synchronous, paced loop - not a
  persistent background queue/worker thread**: a burst of many new
  listings (e.g. a fresh saved search matching dozens of existing items)
  needs to be sent to Telegram spaced out, not back-to-back, or Telegram's
  own rate limiting kicks in. A separate always-running queue/worker thread
  (like `BackgroundScanner`) was considered and deliberately not used: it
  would decouple "the scan finished" from "the alerts were actually sent",
  which complicates the manual `/saved-searches/{id}/run` response's
  meaning and - more importantly - would need thread-safe coordination and
  a way for tests to deterministically wait for delivery before asserting,
  for no real benefit at this scale (a single local user, one Telegram
  chat). Instead, `NotificationService.notify_new_listings` paces sends
  in-place with a plain `time.sleep` between them, and
  `TelegramNotificationProvider` retries a single send's own transient
  failures before giving up - both fully synchronous, both trivially
  testable by monkeypatching `time.sleep`, and both keeping "notify" mean
  exactly what it always has: by the time the call returns, every
  reasonable attempt has been made. If a future scale (many concurrent
  users, many channels) ever needs real async delivery, that's a
  reassessment for a later phase, not this one.
- **Jinja2 + vanilla JS for the dashboard, not React/a SPA build**: the
  brief was an internal MVP for a non-technical user, not a product
  frontend - a server-rendered page plus `fetch()` calls to the API that
  already exists needed no Node/npm/build step, and reusing the existing
  JSON endpoints for every action was the only way to guarantee zero
  duplicated saved-search logic.
- **A normalized join table for multi-marketplace saved searches, not a
  comma-separated string column**: a real relational design gets a real
  uniqueness guarantee (no duplicate marketplace per search, enforced by
  the database, not just application code - same reasoning as the
  `discovered_listings` dedup constraint) and scales cleanly to "dozens of
  connectors later" without ever needing to parse/serialize a delimited
  string. A hand-written, idempotent migration (no Alembic) converts
  pre-existing single-marketplace rows on startup rather than requiring a
  destructive reset, since real saved searches already existed before this
  change - see `core/saved_searches/migration.py` above.
- **`APIRouter`s under `marketplace_alert/api/v1/`, not more routes bolted
  onto `main.py`**: `main.py` was already carrying the dashboard route,
  every legacy JSON route, and app startup/lifespan wiring - adding a
  second, versioned API's worth of endpoints directly into it would make
  it the one file everything depends on understanding. Splitting `/api/v1`
  into its own package (one module per resource, one shared schemas
  module) keeps each concern small and gives FastAPI's own tag/description
  metadata something clean to group in `/docs`. The `dependencies.py`
  extraction (see "Mobile API" above) exists specifically so this split
  doesn't introduce a circular import or a second set of singletons.
- **Reshape the response, never re-implement the operation**: every
  `/api/v1` endpoint that has a legacy equivalent (saved-search CRUD, the
  manual run) calls the identical service/runner the legacy route calls -
  the only mobile-specific code is how the *response* is shaped
  (`api/v1/schemas.py`). This was the deliberate alternative to writing a
  second "mobile saved-search service" that would need to be kept in sync
  with the first one by hand forever.
- **Document persistence/relationship gaps in `GET /api/v1/listings`
  rather than inventing data or a relationship that isn't stored**: the
  tempting shortcut - guessing a plausible `saved_search_id` from
  matching titles, or silently omitting the null fields from the response
  - would both misrepresent what the database actually contains to a
  mobile client that has no way to know better. Returning explicit `null`
  fields and simply not offering a filter the data model can't support
  keeps the API honest about its own current limits; extending
  `DiscoveredListing`'s schema is real, separate future work, not
  something to fake around in an API-shape task.
- **A deterministic rule-based scorer for relevance filtering, not an
  LLM/embeddings call**: explicitly out of scope for this feature, and
  unnecessary for it - a small set of legible rules (brand conflict,
  product-family synonym, accessory penalty) already explains every
  required scenario (see "Relevance filtering" above) without adding a
  network call, an API cost, or a source of nondeterminism to something
  that runs on every scheduled scan tick.
- **A lenient "any shared token counts" fallback for unregistered product
  categories, not a stricter proportional-overlap score**: an earlier
  design scaled the core-match score by the fraction of query tokens
  found in the listing, which technically implements "don't accept a
  result on one weak token match" more strictly - but it also silently
  rejected many existing saved searches using generic, non-tool queries
  (single collectible/fashion/watch terms with no registered family) that
  had nothing to do with the brand-conflict/accessory problem this
  feature exists to fix. Since the brand-conflict and accessory-penalty
  checks apply independently of which core-scoring branch fires, the
  lenient fallback doesn't reopen the original problem for the one
  registered category (drills) or for accessory terms - it only affects
  categories this vocabulary has no opinion about yet.
- **A brand-only query (no core terms left after removing the brand)
  requires the listing to actually mention that brand, not merely avoid
  conflicting with a different one.** An earlier version of this rule
  treated "no core terms left" the same as "brand-neutral listings aren't
  penalized" (the rule one layer up, which only makes sense when there's
  still a core term supplying independent evidence the listing is
  on-topic) and made a bare "Makita" query relevant to *any* listing that
  simply didn't mention a different recognized brand - including a
  Pokemon card, confirmed while re-evaluating real production data via
  `core/persistence/cleanup.py` (see CHANGELOG.md's historical-cleanup
  entry). Fixed to require an actual brand mention (`brand_score > 0`) for
  this specific case - a targeted fix, not a change to the brand-neutral
  rule itself, which still applies normally whenever a core term is
  present.

## Non-goals (for now)

See `PROJECT_CONTEXT.md` for the full list of what has not been built yet.
