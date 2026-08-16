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
maps a marketplace name (e.g. `"mock"`, `"etsy"`, `"ebay"`) to a connector
instance - `get_connector(name)` - and answers `is_marketplace_supported(name)`.
It is the *only* place outside a connector's own module allowed to import a
concrete connector class. Anything in `core/` that needs to resolve a
connector (currently: `SavedSearchRunner`, via `SavedSearchService`'s
marketplace validation) takes a resolver function / predicate injected by
`main.py` instead of importing the registry - `core/` stays free of any
concrete-connector import, including the registry itself. Adding a
marketplace means adding one line to the registry's factory dict; nothing
in `core/` changes - proved in practice twice now: when Etsy was added, and
again when eBay was added (nothing outside `connectors/registry.py` and
`connectors/ebay/` had to change the second time either). Each registry
entry is a zero-arg callable rather than strictly a bare class, since a
real connector (Etsy, eBay) needs credentials wired in from `settings` that
a plain class reference can't supply; `MockMarketplaceConnector` still
works unchanged as an entry since a class is itself a valid zero-arg
callable. `list_supported_marketplaces()` returns every registered name,
sorted - the single source of truth anything listing marketplaces should
use (currently: the dashboard's marketplace dropdown - see "The management
dashboard" below) rather than hard-coding `["mock", "etsy", "ebay"]` a
second time somewhere else.

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
    config.py                  Settings loaded from environment / .env
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
        registry.py             get_connector / is_marketplace_supported
        mock/
            connector.py        MockMarketplaceConnector (fake, in-memory)
        etsy/
            connector.py         EtsyMarketplaceConnector (Etsy Open API v3)
        ebay/
            connector.py          EbayMarketplaceConnector (eBay Buy Browse API)
            token_manager.py      EbayTokenManager (OAuth client_credentials)
    notifications/
        telegram/
            provider.py          TelegramNotificationProvider
alembic/
    env.py                       Resolves DATABASE_URL the same way the app does
    versions/                    One migration per schema change (baseline so far)
alembic.ini                     Alembic config (sqlalchemy.url deliberately left blank)
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

## Database selection and PostgreSQL support

The system is live on Render; a Render PostgreSQL database
(`marketplacealert-db`) already exists, though the deployed Web Service
isn't connected to it yet - this section covers the *capability* now in
the codebase, not a production cutover (no Render config was touched, and
nothing was deployed as part of this).

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
- **Migrations are not run automatically inside the app's own startup**
  (`main.py`'s `lifespan`) - deliberately. Render can run more than one
  instance/worker process; several of them independently attempting
  `alembic upgrade head` at the same moment as the app boots is a real
  race with no benefit over running it once, separately, before any new
  instance starts serving traffic (e.g. a Render Pre-Deploy Command, or a
  manual `alembic upgrade head` before a deploy). `init_db()` still runs
  in `lifespan` unconditionally, same as before - it's simply a no-op for
  PostgreSQL now (see above), so this required no change to `main.py`'s
  startup sequence itself, only to what `init_db()` does once it's there.
- **Tests never require a real PostgreSQL server.** `create_engine()`
  builds an `Engine` object lazily - it only actually opens a connection
  when one is checked out of the pool - so `tests/test_database_config.py`
  asserts on the resolved URL/dialect/driver without connecting anywhere,
  and `tests/test_alembic_migrations.py` runs the real baseline migration
  (upgrade, downgrade, re-upgrade, the `stamp`-vs-`upgrade` distinction)
  against throwaway temp-file SQLite databases only.

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
    -> list_supported_marketplaces()           (registry - checkbox source)
    -> NotificationService.is_enabled           (status: Telegram configured?)
    -> EtsyMarketplaceConnector.is_configured    (status: Etsy configured?)
    -> EbayMarketplaceConnector.is_configured    (status: eBay configured?)
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
- **Status is booleans only, never credential values.** "Telegram
  configured" reads `NotificationService.is_enabled`; "Etsy configured" and
  "eBay configured" read `EtsyMarketplaceConnector.is_configured` /
  `EbayMarketplaceConnector.is_configured` (via `get_connector("etsy")` /
  `get_connector("ebay")`) - all already-existing properties, reused rather
  than re-derived, and none capable of returning anything but `True`/`False`.
  No route, template, or JS file ever touches `settings.telegram_bot_token`,
  `settings.etsy_api_key`, `settings.ebay_app_id`/`settings.ebay_cert_id`,
  or any other credential value.
- **Errors shown to the user are always the API's own sanitized messages.**
  `dashboard.js` displays whatever `detail` string a failed `fetch()`
  response carries (e.g. "Saved search not found", "query cannot be
  empty") - the same messages `/docs`/`curl` users already see - and a
  generic "Could not reach the server" for network-level failures. Since
  none of the existing API error responses include stack traces or
  credentials (see the notification/connector sections above), the
  dashboard inherits that safety for free rather than needing its own
  error-sanitizing logic.

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

## Non-goals (for now)

See `PROJECT_CONTEXT.md` for the full list of what has not been built yet.
