# Project Context

**Read this file before making any architectural changes.**

## This is a brand-new project

Marketplace Alert was started from scratch. There is no prior
implementation, database, or codebase to preserve — nothing in this
repository predates this project.

## Purpose

A scalable marketplace monitoring and alert platform. A user enters any
keyword or phrase (e.g. "Maccabi", "Rolex Submariner", "Makita drill"),
picks which marketplaces to search, and gets alerted when a *new* matching
listing appears — with a direct link back to the original listing.

The system must support adding new marketplaces (eBay, Etsy, Facebook
Marketplace, Mercari, Yad2, and others) over time **without modifying the
core search engine** — see `ARCHITECTURE.md` for how the connector
interface makes that possible.

## Current status

**Phase 1 — local prototype (in progress).** See `ROADMAP.md` for the full
phase list.

Implemented so far:
- Project structure and Python package layout.
- A basic FastAPI application: `/health`, temporary `/search` and `/scan`
  (with Telegram alerts), and a `/saved-searches` CRUD API plus
  `/saved-searches/{id}/run` for manually triggering one. **`GET /` is now
  a server-rendered management dashboard** (see below), not the old JSON
  service-info response.
- A local web dashboard (`GET /`, `marketplace_alert/templates/dashboard.html`
  + `marketplace_alert/static/`) — create/list/run/enable/disable/delete
  saved searches from a browser, no `curl`/Swagger/code editing required.
  Server-rendered with Jinja2 plus a small vanilla-JS file that calls the
  *existing* `/saved-searches*` JSON endpoints — no saved-search business
  logic is duplicated for the UI. Shows basic system status (backend
  running, Telegram/Etsy configured — booleans only, credential values are
  never rendered) and the count of active saved searches / supported
  marketplaces. No auth yet (local MVP only — see "Things that have NOT
  yet been implemented"). See `ARCHITECTURE.md`.
- The abstract `MarketplaceConnector` interface (no real connector yet).
- The normalized `Listing` Pydantic model.
- A `MockMarketplaceConnector`
  (`marketplace_alert/connectors/mock/connector.py`) with a fixed catalog
  of fake listings, so the system can be built and tested end-to-end
  without external API credentials.
- Local persistence and duplicate detection: a SQLite (via SQLAlchemy)
  `discovered_listings` table, a repository/service layer
  (`marketplace_alert/core/persistence/`), and a temporary `/scan` endpoint
  that persists newly-seen listings and reports which ones are new vs.
  already seen. See `ARCHITECTURE.md`.
- Telegram notifications for newly discovered listings: a
  `NotificationProvider` interface
  (`marketplace_alert/core/notifications/base.py`), a
  `TelegramNotificationProvider` implementation
  (`marketplace_alert/notifications/telegram/`), and a `NotificationService`
  that `/scan` calls with each scan's new listings only — already-seen
  listings never trigger a message. Credentials come from
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` env vars only; if either is
  missing, notifications are disabled (logged once) and everything else
  keeps working. **Delivery is now robust under bursts**: a real saved
  search returning many new listings at once had some Telegram sends fail
  outright ("Telegram API request failed") from being sent back-to-back
  with no pacing or retry. `NotificationService` now paces sends between
  listings (`telegram_send_delay_seconds`, default 1s - Telegram's own
  guideline for one chat), and `TelegramNotificationProvider` retries a
  single send's *transient* failures (429/5xx/timeout) with bounded,
  Retry-After-aware backoff (`telegram_max_retries`/`telegram_retry_base_seconds`),
  never retrying *permanent* ones (bad token, bad chat ID, malformed
  request). A listing is persisted as discovered *before* notification is
  attempted, so a Telegram failure - even after every retry - can never
  make it look new again on a later scan. See `ARCHITECTURE.md`.
- Saved searches and automatic background scanning: a persistent
  `SavedSearch` model (`marketplace_alert/core/saved_searches/models.py`),
  a validated CRUD service and repository, a connector registry
  (`marketplace_alert/connectors/registry.py`, `get_connector(name)` /
  `is_marketplace_supported(name)` — `"mock"`, `"etsy"`, and `"ebay"`
  registered), and
  a `BackgroundScanner` (`marketplace_alert/core/scheduler/`) that runs on
  one central background thread, polling for due saved searches and
  running each through the same `SavedSearchRunner` the manual
  `POST /saved-searches/{id}/run` endpoint uses. **One saved search can now
  target multiple marketplaces** — `marketplaces: list[str]` (a normalized
  `SavedSearchMarketplace` join table, never a comma-separated string), each
  searched independently by `SavedSearchRunner`, which now tolerates one
  marketplace failing without affecting the others *in the same search* (a
  new resilience layer, distinct from the scheduler's existing "one saved
  search failing doesn't stop another"). A one-time, hand-written, idempotent
  migration (`core/saved_searches/migration.py`, since this project has no
  Alembic) converts any pre-existing single-`marketplace` rows into the new
  join table on startup, without touching `query`/`scan_interval_seconds`/
  timestamps — verified against a real copy of the developer's production
  database (see "Important architectural decisions" below). See
  `ARCHITECTURE.md`.
- **`EtsyMarketplaceConnector`** (`marketplace_alert/connectors/etsy/connector.py`)
  — the first *real* marketplace connector, using Etsy Open API v3's
  `GET /application/listings/active` (no scraping). Reads
  `ETSY_API_KEY`/`ETSY_SHARED_SECRET` from the environment only; missing
  credentials produce a clear `MarketplaceConnectorError` when the
  connector is actually used, not a startup crash. A saved search
  targeting `"etsy"` runs through the exact same duplicate-detection,
  scheduler, and notification pipeline as `"mock"` - including now, when
  it's one of several marketplaces on the same saved search. See
  `ARCHITECTURE.md` for the endpoint/auth details and what was verified
  before implementing.
- **`EbayMarketplaceConnector`** (`marketplace_alert/connectors/ebay/connector.py`)
  — the second *real* connector, using the eBay **Buy Browse API**'s
  `GET /buy/browse/v1/item_summary/search` (never the legacy Finding API,
  never scraping). Authenticates with an OAuth 2.0 client_credentials
  Application Access Token (`marketplace_alert/connectors/ebay/token_manager.py`,
  `EbayTokenManager`) - cached in memory and refreshed only when it's
  missing or near expiry, never fetched fresh per search. Reads
  `EBAY_APP_ID`/`EBAY_CERT_ID` from the environment only (`EBAY_DEV_ID` is
  not used - the Browse API's application-token flow doesn't need it);
  missing credentials produce a clear `MarketplaceConnectorError` when the
  connector is actually used, not a startup crash. A saved search
  targeting `"ebay"` (alone or alongside `"etsy"`/`"mock"`) runs through the
  exact same duplicate-detection, scheduler, and notification pipeline as
  every other marketplace. See `ARCHITECTURE.md` for the endpoint/auth
  details and what was verified before implementing.
- Tests confirming the app, models, mock connector, Etsy connector, eBay
  connector and its token manager, persistence layer, notification layer,
  connector registry, saved searches (single- and multi-marketplace), the
  legacy-column migration, and scheduler load and behave correctly, using
  an isolated temp database and a fake notification provider (never the
  developer's local `marketplace_alert.db`, real Telegram credentials, or
  a real Etsy/eBay API call, even though a real `.env` with working
  production eBay credentials is present).
- **PostgreSQL support, alongside SQLite - not replacing it.** The system
  is live on Render, and a Render PostgreSQL database (`marketplacealert-db`)
  already exists but the web service isn't connected to it yet - this adds
  the *capability* to connect it later without deploying or touching Render
  config now. `DATABASE_URL` set -> PostgreSQL (normalized centrally for
  Render's `postgres://`/`postgresql://` URL forms and the `psycopg` (v3)
  driver - `marketplace_alert/core/persistence/database.py:resolve_database_url`);
  unset -> the exact same local SQLite default as before. SQLite keeps its
  automatic `create_all()` bootstrap on startup (safe - additive only);
  PostgreSQL does not - its schema is managed by
  [Alembic](https://alembic.sqlalchemy.org/) migrations instead
  (`alembic/`, one baseline migration representing today's schema), applied
  deliberately (e.g. a Render Pre-Deploy Command), never implicitly
  re-derived at every app startup. See `ARCHITECTURE.md` and README.md
  "Database" for the full picture, and "Important architectural decisions"
  below for why.
- **A versioned Mobile API, under `/api/v1`** - groundwork for a future
  Android/iOS app (not built yet - see "Things that have NOT yet been
  implemented"). Added *alongside* every existing route, never replacing
  any of them: `GET /api/v1/status` (mobile-safe status - booleans and
  marketplace ids only, no secrets), `GET /api/v1/marketplaces`
  (marketplace metadata, entirely from the connector registry),
  `GET`/`POST /api/v1/saved-searches`, `GET`/`PATCH`/`DELETE
  /api/v1/saved-searches/{id}`, `POST /api/v1/saved-searches/{id}/run`
  (same `SavedSearchService`/`SavedSearchRunner`/run-guard the legacy
  routes already use - a mobile-shaped response, not a second
  implementation), and `GET /api/v1/listings` (browse discovered listings,
  paginated - see "Important architectural decisions" below for two real
  limitations documented rather than faked: several listing fields aren't
  persisted yet, and there's no stored relationship to filter listings by
  saved search). CORS is prepared (`CORS_ALLOWED_ORIGINS`, empty/off by
  default - native mobile apps don't need it) but no authentication yet -
  deliberately out of scope for this change, see below. See
  `ARCHITECTURE.md` "Mobile API".

## Current milestone

Phase 1 (local prototype scaffold; mock connector, local
persistence/duplicate detection, Telegram notifications, and saved
searches/background scanning) is done. Phase 2's goal — prove one real
`MarketplaceConnector` works end-to-end against a live marketplace — is
met via **Etsy**. A second real connector, **eBay** (Buy Browse API, OAuth
client_credentials), has now been added on top of that, proving the same
connector-interface claim a second time - registering it required exactly
one line in the connector registry's factory dict and nothing else outside
`connectors/ebay/`. A slice of **Phase 7 (web interface)** exists early:
the `GET /` dashboard covers creating/managing saved searches from a
browser, which is half of Phase 7's stated scope ("A UI for
creating/managing searches and viewing results") — *viewing discovered
listings* remains open. **Phase 5 (multiple marketplaces)'s core claim is
now proven with three connectors, not just two**: one saved search can
target `"mock"`, `"etsy"`, and/or `"ebay"` at once, each scanned
independently with its own failure isolation - the "add a marketplace
without touching core" property this phase asks to prove held again here,
since adding eBay required zero changes to any other connector, to
duplicate detection, to the scheduler, or to the dashboard template logic
(only a new status row). Since then, a real burst of new listings exposed
Telegram sends failing under load with no pacing or retry - now fixed (see
"Important architectural decisions" #12 and `ARCHITECTURE.md`); this was a
reliability fix to Phase 4's existing alerting, not a new phase. The system
is now live on Render; PostgreSQL *support* has been added (selection,
normalization, Alembic migrations - #13) since a Render PostgreSQL database
already exists but the deployed Web Service isn't using it yet - this is
schema/code readiness, not the actual production cutover (no Render config
changed, nothing deployed, no local data migrated). A versioned Mobile API
(`/api/v1` - #14) now exists too, as groundwork for the next major goal (a
real Android/iOS app) - it's a stable, JSON-only, secrets-safe contract
reusing every existing service/repository, added alongside the legacy
routes without touching them. **This is API-side preparation only**: no
mobile app was built, no authentication was added (the architecture is
just structured so it can be, later, without rewriting these endpoints -
see #14), and no new marketplace was added. None of this is a signal to
start the rest of Phase 7's scope, Phase 6 (user accounts), Phase 9 (the
actual mobile app), or any other later phase, or to add a fourth
marketplace connector yet.

## Chosen technology

- Python 3.12+ (developed against 3.14 locally; no 3.14-only syntax used)
- FastAPI for the backend API
- Pydantic v2 for data models and settings
- SQLAlchemy for persistence — SQLite locally (the default when
  `DATABASE_URL` is unset), PostgreSQL in production (`DATABASE_URL` set,
  via the `psycopg` v3 driver) - same models, same code, selected purely
  by that one environment variable
- Alembic for PostgreSQL schema migrations (SQLite keeps its original
  automatic `create_all()` bootstrap - see "Important architectural
  decisions" #13 and README.md "Database")
- FastAPI's own `CORSMiddleware` for the Mobile API's CORS prep (no new
  dependency) - off by default, see "Important architectural decisions" #14
- httpx for outbound HTTP calls (the Telegram Bot API; Etsy Open API v3;
  eBay's OAuth token endpoint and Buy Browse API)
- Jinja2 for server-rendered HTML (the `/` dashboard) plus a small amount
  of dependency-free vanilla JavaScript - no frontend framework, no
  Node/npm/build step
- pytest for tests
- Environment variables via `.env` (see `.env.example`)
- Structured (JSON) logging via stdlib `logging` (no extra dependency yet)

## Important architectural decisions

1. **Connector interface is the only boundary between core and
   marketplaces.** The core engine depends on `MarketplaceConnector` and
   `Listing` only — never on a specific marketplace's client/scraping code.
   See `ARCHITECTURE.md`.
2. **Persistence layer added ahead of a second connector.** Originally
   planned for Phase 3 once a second connector existed, but duplicate
   detection needed *something* to detect duplicates against, and the mock
   connector's fixed catalog already gives repeat listings across scans —
   so `discovered_listings` (SQLite via SQLAlchemy) was built now. The
   dedup key is `(marketplace, external_listing_id)`, enforced by a unique
   constraint, and the logic lives in `marketplace_alert/core/persistence/`
   (repository + service layer) — never inline in route handlers. See
   `ARCHITECTURE.md`.
3. **No dependency on scraping.** Where a marketplace offers an official
   API, it should be preferred; scraping is a fallback, isolated entirely
   inside that marketplace's connector.
4. **Filters are marketplace-agnostic at the interface level.**
   `search(query, filters)` takes a generic `dict`; a connector may ignore
   filters it doesn't support rather than erroring.
5. **Notification provider is a second interface, alongside the connector
   interface.** `NotificationProvider.send_listing_alert(listing)` is the
   only thing `/scan` and `NotificationService` know about — Telegram's bot
   token, chat ID, HTTP calls, and message formatting all live inside
   `TelegramNotificationProvider` and never leak out. Adding a second
   channel (email, etc.) later means a new provider class, not changes here.
   Credentials are read from environment variables only, never hard-coded,
   and a missing/incomplete credential disables the provider rather than
   crashing the app. See `ARCHITECTURE.md`. Note: `httpx`'s own logger logs
   every outbound request URL at INFO by default, which for Telegram
   embeds the bot token - `configure_logging()` explicitly silences it
   (`logging.getLogger("httpx").setLevel(logging.WARNING)`) so this can
   never leak through, independent of our own (already token-free) logging.
6. **`core/` never imports a concrete connector, even for the connector
   registry.** The registry (`marketplace_alert/connectors/registry.py`,
   `get_connector`/`is_marketplace_supported`) is the one place allowed to
   import `MockMarketplaceConnector` (and real connectors later) — it lives
   outside `core/`. `SavedSearchRunner` and `SavedSearchService` take a
   resolver function / predicate injected by `main.py` instead of importing
   the registry directly, so `core/saved_searches/` and `core/scheduler/`
   stay connector-agnostic, same as the persistence and notification
   layers. See `ARCHITECTURE.md`.
7. **One background thread for all saved searches, not one per search.**
   `BackgroundScanner` polls on a fixed tick (`scheduler_tick_seconds`,
   default 5s — separate from each saved search's own
   `scan_interval_seconds`) and runs every due search through the same
   `SavedSearchRunner` the manual `/run` endpoint uses, so there is exactly
   one implementation of "run one saved search". A `SavedSearchRunGuard`
   (shared between the scheduler and the manual endpoint) stops the same
   saved search from being scanned twice at once; a failure in one saved
   search is logged and never stops the others or the loop itself. Because
   the scanner runs in a background thread with no FastAPI request to hang
   a `Depends` off, it's bound to the real `NotificationService` and
   `SessionLocal` at startup rather than being overridable per-call — tests
   exercise it directly (constructing their own `BackgroundScanner`) rather
   than through the app. See `ARCHITECTURE.md`.
8. **The first real connector (Etsy) never got special treatment.** No
   endpoint, model, or service anywhere had to change to add it — only the
   registry's factory dict gained one entry
   (`marketplace_alert/connectors/registry.py`). This is the actual proof
   that the connector interface does what `ARCHITECTURE.md` claims. The
   registry's factory type also generalized from "a bare connector class"
   to "a zero-arg callable" specifically so Etsy's credentials could be
   wired in from `settings` without a per-connector special case in
   `get_connector()`. Endpoint choice and auth mechanism were verified
   against Etsy's own generated OpenAPI v3 spec and official docs before
   writing any code — not guessed — see
   `marketplace_alert/connectors/etsy/connector.py`'s module docstring for
   exactly what was checked and where.
9. **The dashboard has no saved-search logic of its own.** `GET /`
   (`marketplace_alert/main.py`) reads through the exact same
   `SavedSearchService` the JSON API uses, and its browser-side JS calls
   the exact same `/saved-searches*` endpoints for every action (create,
   run, enable/disable, delete) — there is no second implementation of
   "create a saved search" or "run a saved search" for the UI to drift out
   of sync with. Server-rendered HTML (Jinja2) plus vanilla JS was chosen
   over a JS framework/build step - see `ARCHITECTURE.md` "Why these
   choices". Marketplace choices in the dropdown come from
   `list_supported_marketplaces()` (new, in the connector registry) - the
   same single source of truth `is_marketplace_supported` already used for
   validation, never a separately maintained list. Status booleans
   (Telegram/Etsy configured) reuse existing state
   (`NotificationService.is_enabled`, `EtsyMarketplaceConnector.is_configured`)
   rather than re-deriving it - and only ever render as booleans, never the
   underlying credential values.
10. **Multi-marketplace saved searches, and two real bugs caught by testing
    against real data before this shipped.** `SavedSearch` now owns a
    `marketplaces: list[str]` via a normalized `SavedSearchMarketplace` join
    table (never a comma-separated string) - see ARCHITECTURE.md. Two
    things only surfaced once tested against structurally-real data, not
    hand-picked test fixtures:
    - **Collection-replace ordering.** Reassigning
      `saved_search.marketplace_links` to a brand-new list in one step (to
      implement "PATCH replaces the marketplace set") let SQLAlchemy
      interleave the DELETE/INSERT statements, which trips the
      `(saved_search_id, marketplace_name)` unique constraint whenever a
      marketplace is unchanged across the edit (e.g. `["mock"]` ->
      `["etsy", "mock"]` - re-adding "mock" races the deletion of the old
      "mock" row). Fixed by clearing the collection and flushing *before*
      extending it with the new list, in `SavedSearchRepository.update()`.
    - **The legacy-column migration's index.** The original single-
      marketplace model had `index=True` on that column; SQLite's
      `ALTER TABLE ... DROP COLUMN` doesn't clean up indexes that reference
      the dropped column, so the migration failed the first time it ran
      against a real copy of the developer's database (a hand-built test
      fixture without that index had passed, which is exactly why it
      didn't catch this - fixed by rebuilding the test fixture to match
      the real schema exactly, including the index, and by having the
      migration look up and drop any index touching the column before the
      `ALTER TABLE`). The failed attempt rolled back cleanly (wrapped in
      one `engine.begin()` transaction) - real data was never at risk, but
      this is why a real backup was taken before ever running the new code
      against the real database, and why the fix was re-verified against
      that same real (backed-up) database afterward, not just the test
      suite.
    - **Runner resilience split in two, deliberately.** A saved search now
      has two independent failure boundaries: `SavedSearchRunner` catches a
      failure in *one marketplace* so it doesn't stop the *other
      marketplaces in the same search* (new); `BackgroundScanner` still
      catches a failure in *one saved search* so it doesn't stop *other
      saved searches* (unchanged, pre-existing). Neither one subsumes the
      other - both are needed now that "one saved search" no longer implies
      "one marketplace, one thing that can fail".
11. **The eBay connector (second real connector) needed one new piece the
    others didn't: OAuth token management, kept in its own module.**
    `marketplace_alert/connectors/ebay/token_manager.py` (`EbayTokenManager`)
    owns requesting, caching, and refreshing the client_credentials
    Application Access Token entirely on its own - `EbayMarketplaceConnector`
    just calls `get_token()` before each search and `invalidate()` if a
    search comes back 401/403 (in case the cached token was revoked before
    its tracked expiry). Nothing outside `connectors/ebay/` knows an OAuth
    flow is involved at all - same isolation rule as Etsy's API key. Endpoint
    (`GET /buy/browse/v1/item_summary/search`, never the legacy Finding
    API) and field mapping were verified against eBay's own official Browse
    API references before writing any code - see
    `marketplace_alert/connectors/ebay/connector.py` and `token_manager.py`'s
    module docstrings for exactly what was checked - and the developer had
    already manually confirmed the same request shape returns real HTTP 200
    responses in production before this was built. The token itself is
    never logged (not even at DEBUG), never persisted to disk, and never
    exposed through any API response.
12. **Telegram delivery robustness under bursts, split the same way retry
    logic and pacing naturally split: a single send's own resilience lives
    in the provider, pacing across many sends lives in the service.**
    Triggered by a real observed failure: a new Etsy + eBay saved search
    returned many new listings in one scan, and some Telegram sends failed
    with "Telegram API request failed" from being fired back-to-back with
    no pacing or retry. `TelegramNotificationProvider.send_listing_alert`
    now retries *transient* failures (HTTP 429/500/502/503/504, timeout,
    connection error) with bounded, exponential backoff, honoring
    Telegram's own `retry_after` on 429 instead of guessing a wait time -
    but never retries *permanent* failures (bad bot token, bad chat ID,
    malformed request), since retrying those would never succeed.
    `NotificationService.notify_new_listings` separately paces the *gap
    between* different listings' sends (a `send_delay_seconds` sleep, never
    before the first send) - this is deliberately not folded into the
    provider, since it's about spacing out a *batch*, a concern of whoever
    loops over multiple listings, not of a single send. Both pieces stay
    fully synchronous - no persistent background queue/worker thread was
    added (a `BackgroundScanner`-style thread was considered and rejected
    for this; see `ARCHITECTURE.md` "Why these choices" for the reasoning)
    - so `notify_new_listings` still means exactly what it always has: by
    the time it returns, every reasonable attempt has already been made.
    Neither change touches *when* a listing is persisted as discovered:
    `ListingDiscoveryService.process_listings` still runs, and still
    commits, before `NotificationService` is ever called (see
    `SavedSearchRunner._run_one_marketplace`) - so a Telegram failure, even
    after every retry is exhausted, can never make an already-discovered
    listing look new again on a later scan. The database was already the
    source of truth for "already discovered" before this change; this
    change only makes delivery of the *alert* more reliable, never
    duplicate detection itself.
13. **PostgreSQL is opt-in via `DATABASE_URL`, resolved and normalized in
    exactly one place, and gets a different schema-management strategy
    than SQLite - deliberately, not an oversight.** Three separate,
    narrow decisions, each worth calling out on its own:
    - **Selection.** `resolve_database_url()`
      (`core/persistence/database.py`) is the only place "which database"
      is decided: `settings.database_url` set -> use it (normalized);
      unset -> the pre-existing local SQLite default, byte-for-byte
      unchanged (`sqlite:///./marketplace_alert.db`). `config.py`'s
      `database_url` field changed from a hard-coded SQLite default to
      `None`, specifically so "unset" is representable and this function
      is the single place that fills in what "unset" means - not two
      places quietly agreeing (or disagreeing) on a default.
    - **Normalization.** Render (like Heroku) can issue either
      `postgres://` or `postgresql://` - both are rewritten to
      `postgresql+psycopg://` in `normalize_database_url()`, so the
      `psycopg` (v3) driver this project now depends on
      (`pyproject.toml`) is always the one SQLAlchemy actually uses,
      never an implicit/ambient default. `alembic/env.py` calls the exact
      same `resolve_database_url()`/`settings.database_url` as the running
      app (never alembic.ini's own `sqlalchemy.url`, left blank on
      purpose) - migrations can never target a different database than
      the app itself would use.
    - **Schema strategy split by backend, not by environment flag.**
      `init_db()` checks the *engine's actual dialect* (`sqlite` vs.
      anything else) before calling `Base.metadata.create_all()` - SQLite
      keeps the original zero-setup bootstrap (safe: `create_all()` only
      ever adds missing tables, never drops or alters one); anything else
      no-ops, logging why, rather than silently doing nothing. PostgreSQL
      schema changes go through Alembic instead
      (`alembic/versions/<rev>_baseline_schema.py` - one baseline
      migration, autogenerated by diffing `Base.metadata` against a fresh,
      *empty* temp SQLite database, never against the developer's real
      local database or any production database - purely additive,
      `create_table`/`create_index` only). This was a deliberate choice
      over running migrations automatically at app startup: Render may run
      more than one instance/worker, and several of them independently
      attempting `alembic upgrade head` at the same moment is a real race;
      a single, separate, explicit step (a Render Pre-Deploy Command, or a
      manual `alembic upgrade head`) run once before the new app code
      starts serving traffic avoids that entirely. See README.md
      "Database" for the exact commands, including the `stamp` vs.
      `upgrade` distinction for a database that already has the current
      schema (e.g. the developer's existing local SQLite file) - `upgrade`
      would fail there with "table already exists" (verified, not
      guessed - a clean failure, not data loss) since Alembic has no
      record of it being already-current; `stamp` records that without
      executing any DDL.
    - **Session handling needed no changes.** Reviewed as part of this
      work: `get_db_session()` (web requests) and `BackgroundScanner`'s
      `session_factory()` calls (scheduler) already create a fresh
      `Session` per request/run and close it in `finally` - never one
      `Session` reused globally across threads. What's shared globally
      (`engine`, `SessionLocal`) is exactly what SQLAlchemy's own
      thread-safety model says is safe to share (the `Engine` and its
      connection pool). The one concrete addition: `pool_pre_ping=True` on
      every engine (harmless for SQLite, important for PostgreSQL - a
      managed Postgres instance can close idle connections server-side,
      and pre-ping transparently detects and replaces a dead pooled
      connection instead of a request failing with one).
    - Nothing in this change touched the Etsy/eBay connectors, OAuth
      logic, saved searches, multi-marketplace architecture, duplicate
      detection, or the dashboard - and no local SQLite data was migrated
      to PostgreSQL (out of scope for this change, to be handled
      separately) or deleted.
14. **The Mobile API (`/api/v1`) is a thin, versioned adapter layer -
    every endpoint reuses an existing service/repository, none duplicate
    business logic, and two real persistence gaps were documented rather
    than papered over.**
    - **A shared-dependencies extraction, to avoid a circular import, not
      a behavior change.** `main.py` used to construct
      `NotificationService`, `SavedSearchRunner`, and `SavedSearchRunGuard`
      as its own module-level singletons. Both `main.py`'s legacy routes
      and the new `/api/v1` routers need the *exact same* singletons (so
      the run guard actually prevents the same saved search running
      through both an old and a new endpoint at once) - since `api/v1/`
      can't import from `main.py` without a cycle (`main.py` mounts
      `api/v1`'s router), these singletons moved to a new
      `marketplace_alert/dependencies.py`, and `main.py` re-imports them
      under their original names. Every existing test import
      (`from marketplace_alert.main import get_notification_service`,
      `_saved_search_run_guard`, etc.) keeps working unchanged, because
      Python re-exports still expose the identical objects under
      `main`'s namespace - confirmed by the full existing test suite
      passing unmodified, not just asserted.
    - **`GET /api/v1/listings` documents two real limitations instead of
      faking them**, per this task's own explicit instruction:
      - `price`/`currency`/`location`/`condition`/`image_url` are always
        `null` - `DiscoveredListing` (`core/persistence/models.py`) only
        ever stored `marketplace`/`external_listing_id`/`title`/
        `listing_url` and two timestamps, going all the way back to when
        persistence was first built (Phase 3) - nothing about *this*
        change removed data that used to be there. Extending persistence
        to store the rest of `Listing`'s fields is a separate, larger
        change (a new column set, a migration, and deciding what to do
        about already-discovered rows) - deliberately not bundled into
        this one.
      - No `saved_search_id` filter is offered - `DiscoveredListing` has
        no relationship to `SavedSearch` at all; its dedup identity
        (`marketplace`, `external_listing_id`) is intentionally *global*
        across every saved search that happens to match it (see decision
        #2 and `ListingDiscoveryService`), not scoped to whichever search
        happened to discover it first. Inventing that relationship for
        this endpoint would misrepresent what's actually stored.
      - `only_new` is not offered either, for the same reason: "new" is a
        property of one specific scan run (`ListingDiscoveryResult`), not
        a persisted column on the row - there's nothing reliable to filter
        on without pretending otherwise.
    - **The manual-run response is intentionally reshaped, not
      duplicated.** `POST /api/v1/saved-searches/{id}/run` calls the exact
      same `SavedSearchRunner`/`SavedSearchRunGuard` the legacy
      `POST /saved-searches/{id}/run` does - the only new code is mapping
      the existing `SavedSearchRunResult` (a list of per-marketplace
      results) into a dict keyed by marketplace name plus a `query` field
      and renamed totals, genuinely easier for a mobile client to consume
      without duplicating the run logic itself.
    - **CORS is prepared, not left wide open.**
      `settings.cors_allowed_origins` (from `CORS_ALLOWED_ORIGINS`,
      comma-separated) defaults to an empty list - no cross-origin browser
      access at all until explicitly configured, never `"*"`. Native
      mobile apps don't need browser CORS in the first place; this exists
      only for possible future web-based tooling.
    - **No authentication was added, and none was faked.** Every
      `/api/v1` endpoint already takes its dependencies via FastAPI
      `Depends(...)` (services, sessions) - the same mechanism a future
      `Depends(require_authenticated_user)` would use, added to each
      endpoint's signature later without restructuring anything. No
      placeholder/no-op auth check and no fabricated user id were added
      now - see "Things that have NOT yet been implemented".
15. **Relevance filtering (`marketplace_alert/core/relevance/`) was added
    because keyword matching alone was letting through results nobody
    wanted.** Real mobile testing surfaced the trigger case: a "Makita
    drill" saved search returned Etsy listings for DeWalt/Milwaukee
    battery holders and generic tool organizers - each a legitimate
    keyword hit, none of them what anyone searching "Makita drill" wants.
    - **Deterministic, rule-based, dependency-free - no LLM/embeddings/
      vector database, as explicitly required.** A transparent 0-100
      score built from four independently-checked signals: a required
      brand's *conflicting* competing brand rejects outright; a matching
      or brand-neutral listing is not penalized; the query's core product
      terms are checked against a small configurable product-family
      synonym table (`families.py` - e.g. "hammer drill"/"cordless
      drill" score as strong drill matches, "impact driver" as a lower-
      scored related one); and an accessory-only listing (holder, mount,
      case, ...) is penalized *unless the query itself asked for that
      accessory* ("Makita battery holder" must still return battery
      holders). See `ARCHITECTURE.md` "Relevance filtering" for the full
      scoring table and every named test scenario's outcome.
    - **The brand vocabulary is a registry, not a hard-coded four-brand
      list.** `brands.register_brand(canonical, aliases=[...])` is the
      one extension point; the default catalog (Makita, Bosch, DeWalt,
      Milwaukee, and a dozen more common power-tool brands) is just what
      gets registered at import time, not special-cased architecture.
      Same pattern for product families (`families.py`) and accessory
      terms (`accessories.py`).
    - **One shared entrypoint, not four copies of the same logic.**
      `filter_relevant_listings()` (`core/relevance/service.py`) is
      called from exactly two places in application code -
      `SavedSearchRunner._run_one_marketplace()` and `main.py`'s legacy
      `/scan` route - which between them cover all four ways a scan can
      run (the background scheduler and both the legacy and mobile Run
      Now endpoints all go through the same `SavedSearchRunner`, proven
      by `tests/test_relevance.py`'s integration tests hitting each entry
      point independently). Filtering happens *before*
      `ListingDiscoveryService.process_listings()`, so a rejected listing
      is never persisted, never marked "already seen", and never
      notified about - only ever logged, once, at INFO level, with the
      saved search id, query, marketplace, external listing id, score,
      and rejection reason (never a secret, never the full listing body).
    - **`/search` was deliberately left unfiltered.** It's stateless (no
      persistence, no notification - see decision above on `/scan` vs
      `/search`), so it isn't one of the four paths this task named, and
      filtering a raw exploratory-search endpoint would just be
      surprising.
    - **Counts are post-filter, with pre-filter visibility added
      additively.** `MarketplaceRunResult.new_count`/`already_seen_count`
      (and the mobile/legacy schema equivalents) now reflect listings
      *after* relevance filtering; a new optional `raw_count`/
      `rejected_count` pair on the same result types exposes how many
      results the connector actually returned and how many were dropped
      - additive fields only, no existing field changed shape or meaning
      for a client that ignores them.
16. **Historical listing cleanup (`core/persistence/cleanup.py`,
    `scripts/cleanup_historical_listings.py`) re-evaluates pre-existing
    `discovered_listings` rows against the current relevance engine,
    instead of leaving relevance filtering as forward-only.** Mobile
    testing confirmed the relevance engine works correctly for new scans,
    but the mobile Listings screen still showed old irrelevant listings
    (battery holders, wrong-brand items) persisted *before* relevance
    filtering existed.
    - **`DiscoveredListing` has no relationship to `SavedSearch` (see
      decision #14) - there is no stored query and no foreign key to
      reliably determine "which saved search found this specific row."**
      Rather than invent that relationship or guess, each row is
      re-evaluated against every saved search **currently** targeting its
      marketplace (active or paused) and kept if relevant to at least one
      - the closest honest proxy available. A row whose marketplace has
      no current saved search at all is left untouched rather than
      deleted on a guess. In the real local database this task ran
      against, every marketplace with listings also had at least one
      matching saved search, so this case didn't arise in practice - but
      the code path (and a dedicated test) exists for when it does.
    - **A real bug in the relevance engine was found and fixed as part of
      this work, not a hypothetical one.** Re-evaluating real historical
      data (not synthetic test fixtures) showed a bare brand-only saved
      search (e.g. the literal query "Makita", which two of the real
      saved searches in this database actually use) was being treated as
      relevant to almost anything that didn't explicitly mention a
      *different* brand - including listings that didn't mention Makita
      at all. Fixed so a brand-only query requires the listing to
      actually mention that brand. This is scoped narrowly to the
      brand-only-query case; every other scoring rule (brand conflict,
      product-family match, accessory penalty) is unchanged. Two existing
      scheduler tests that paired `query="Makita"` with an unrelated
      "Pokemon Charizard" fixture (arbitrary placeholder text, not an
      actual relevance test) were updated to use a matching query
      ("Charizard"), consistent with how sibling tests in the same file
      already do it.
    - **A manual script, not automatic.** `scripts/cleanup_historical_listings.py`
      defaults to a dry-run report and only deletes with an explicit
      `--apply` flag; it is never invoked from app startup, the
      scheduler, or an API endpoint - deleting persisted data is a
      deliberate, explicit developer action, not a side effect of running
      the app. `SavedSearch` rows are read-only inputs; nothing in this
      path modifies or deletes a saved search.
17. **Reverb (`marketplace_alert/connectors/reverb/`) is the third real
    marketplace connector, added after manually verifying real API
    access - `GET https://api.reverb.com/api/listings?query=Fender&per_page=1`
    with a real personal access token (`public` scope only) returned a
    real listing.** Reverb flows through the exact same duplicate-
    detection/scheduler/relevance/notification/mobile-API/dashboard
    pipeline every other connector does - the connector interface's claim
    proven a third time, with zero changes needed outside
    `connectors/registry.py` and `connectors/reverb/`.
    - **Auth is a single static personal access token
      (`REVERB_API_TOKEN`), not a client id/secret pair or OAuth flow** -
      simpler than eBay's `EbayTokenManager`, since there's no token to
      refresh: a 401/403 just means the token is missing, wrong, or
      revoked, reported as a clear error. Missing entirely -> `search()`
      raises before any request; app startup, and every other connector,
      is completely unaffected.
    - **Reverb's own published API docs don't include a complete example
      listing object**, unlike Etsy's/eBay's fully-documented schemas.
      Extensive research (Reverb's official docs, the developer's own
      verified request, and a real third-party Reverb API client's source
      code) confirmed the load-bearing facts with high confidence (auth
      headers, the `listings`/`_links.next`/`_links.prev` response shape,
      `_links.web.href` for the listing URL, `photos` as the image key) -
      but the exact nesting of `price`/`condition`/`shop`/`photos` entries
      is inferred, not independently confirmed. `normalize_listing` is
      written defensively as a direct result: every optional field tries
      its most likely shape, then a reasonable fallback or two, and lands
      on `null` - never invented data - if nothing matches. See
      `connector.py`'s module docstring for the full citation list and an
      honest accounting of what's confirmed vs. inferred.
    - **This connector paginates - Etsy/eBay still don't.** Reverb's API
      is HATEOAS/HAL and explicitly documents "follow `_links`, never
      construct your own URLs" as its design principle, so
      `ReverbMarketplaceConnector` follows `_links.next.href` verbatim
      until it has enough results, there's no next link, or a hard
      `MAX_PAGES` safety cap (5) is hit - never unbounded polling for one
      search.
    - **Adding Reverb surfaced (and fixed) a small pre-existing
      duplication**: the dashboard's system-status panel used to
      hard-code one row each for "Etsy configured?"/"eBay configured?" -
      adding a third hard-coded "Reverb configured?" row would have
      repeated the exact "don't hard-code a marketplace in multiple UIs"
      mistake this project otherwise avoids for the marketplace list
      itself. Generalized instead: the status panel now loops over the
      registry's marketplace list the same way the checkboxes already
      did, and a new `display_name_for()` in `connectors/registry.py` is
      the one place a marketplace's brand-cased display name (e.g.
      "eBay") is defined, used by both the dashboard and
      `GET /api/v1/marketplaces` - see ARCHITECTURE.md "Why these
      choices."

## Things that have NOT yet been implemented

- Any marketplace beyond mock/Etsy/eBay/Reverb (Vinted, Yad2, Facebook
  Marketplace, Mercari, etc.).
- Etsy pagination beyond the first page — `EtsyMarketplaceConnector`
  fetches one page (a safe, configurable `etsy_result_limit`, default 25,
  Etsy's own max 100) per search; multi-page looping isn't implemented yet,
  though the code is structured so adding it doesn't require changing the
  request/response handling (see `_fetch_results_page`'s `offset` param).
  `EbayMarketplaceConnector` fetches one page the same way (`ebay_result_limit`,
  default 25, eBay's own max 200) - same structure, same reasoning.
  `ReverbMarketplaceConnector` is the one exception - it *does* paginate
  (via `_links.next`), bounded by `reverb_result_limit` and a hard
  `MAX_PAGES` safety cap - see `ARCHITECTURE.md` "The Reverb connector".
- Etsy `location`/`seller`/`condition` on normalized listings — left `null`
  rather than guessed; Etsy's shop-location and shop-name fields would need
  an `includes=Shop` request and schema verification not yet done (Etsy has
  no real equivalent of "condition" at all - it's a handmade/vintage/craft
  marketplace, not general resale). eBay's connector *does* map
  `location`/`seller`/`condition` where the Browse API actually returns
  them - see `ARCHITECTURE.md`.
- Reverb `location` on normalized listings — left `null` unless a
  plausible `shop.location`/top-level `location` field happens to be
  present; Reverb's public docs don't confirm a location field for a
  listing at all, so this was never guessed at with false confidence -
  see decision #17 and `ARCHITECTURE.md` "The Reverb connector".
- A live eBay or Reverb search has not been run as part of adding either
  connector (deliberately, per scope for those tasks) - only mocked-HTTP
  tests, plus a dashboard-only smoke check confirming credentials are
  recognized as configured. For eBay, the developer had already manually
  verified the real OAuth token request and a real Browse API search both
  return HTTP 200 in production before that connector was written. For
  Reverb, the developer had already manually verified a real
  `GET /api/listings?query=Fender&per_page=1` request (with a real
  personal access token) returns a real listing - but this codebase has
  not yet issued either request itself.
- Schema migrations (Alembic or similar) — the current schema is simple
  enough that `Base.metadata.create_all()` is sufficient for now.
- Alerting beyond Telegram (email, push, webhook, etc.) — the
  `NotificationProvider` interface supports adding these, but only Telegram
  is implemented.
- Multiple Telegram recipients / per-user chat IDs — one chat ID, from
  config, for everyone.
- `vinted` (and other future marketplace names) selectable in saved
  searches — recognized as future connector names conceptually, but
  `is_marketplace_supported` (and therefore saved-search creation/editing)
  only accepts `"mock"`, `"etsy"`, `"ebay"`, and `"reverb"` until each has
  a registered connector.
- User accounts, auth, or multi-user support — including on the new
  dashboard, which is unauthenticated by design for now (local MVP only;
  do not expose this outside localhost as-is).
- Editing marketplaces from the dashboard UI — `PATCH /saved-searches/{id}`
  fully supports replacing the marketplace set (tested), but the dashboard
  itself only exposes checkboxes on the *create* form; there's no "edit an
  existing search" control yet, so changing marketplaces on an existing
  saved search currently means the API directly (`/docs`, `curl`), not the
  dashboard.
- A listings browser in the dashboard — it manages saved-search
  *definitions* (create/list/run/enable/disable/delete) but does not show
  the discovered listings themselves; that's the rest of Phase 7's stated
  scope ("...and viewing results"), not covered here.
- **The actual mobile application** (React Native, Expo, Flutter, native
  Android/iOS - none chosen yet) - `/api/v1` (this change) is groundwork
  for it, not the app itself.
- **User accounts / authentication on `/api/v1`** - every endpoint is
  currently as open as the legacy routes (no auth anywhere yet, consistent
  with "Things that have NOT yet been implemented" below). The dependency-
  injection structure is deliberately ready for a `Depends(...)`-based auth
  check to be added later per endpoint - see "Important architectural
  decisions" #14 - but nothing enforces it today, and no user id is
  fabricated anywhere.
- **Extending `DiscoveredListing` to persist price/currency/location/
  condition/image_url**, and a `saved_search_id`/listing relationship -
  both real gaps surfaced (not hidden) while building `GET
  /api/v1/listings` - see "Important architectural decisions" #14.
- Push notifications (beyond the existing Telegram alerting).
- Payments / subscriptions.
- **The production Render Web Service is not yet connected to PostgreSQL.**
  PostgreSQL *support* now exists in the codebase (`DATABASE_URL` selection,
  normalization, the `psycopg` driver, Alembic migrations - see "Important
  architectural decisions" #13) and a Render PostgreSQL database
  (`marketplacealert-db`) already exists, but wiring the two together -
  setting `DATABASE_URL` on the Web Service, running the baseline migration
  against it - is a deliberate, separate, not-yet-done step (no Render
  configuration was touched as part of adding this support, and nothing
  was deployed).
- **Existing local SQLite data has not been copied to PostgreSQL.** Not
  needed for the support added here (schema compatibility + selection
  only) - production data initialization/migration is explicitly separate,
  future work.
- Rate limiting, retries, or resilience handling for **connectors** (Etsy,
  eBay) - a slow/rate-limited marketplace API still fails immediately
  today, same as before. This is now implemented for **Telegram
  notification delivery only** (bounded retry with backoff, paced sends -
  see "Important architectural decisions" below and `ARCHITECTURE.md`),
  not for the marketplace connectors, which is a different, not-yet-done
  piece of work.
- **Relevance filtering's product-family/accessory vocabulary only covers
  power tools today.** `families.py` registers one family ("drill");
  `accessories.py` registers common accessory *words* (holder, mount,
  case, organizer, ...) that are domain-general enough to apply outside
  tools, but there's no "impact driver"-style related-product mapping for
  any category besides drills yet. A query for an unregistered product
  category (say, a specific watch model) falls back to lenient token-
  overlap scoring rather than family-aware scoring - see `ARCHITECTURE.md`
  "Relevance filtering" for exactly what that fallback does and why it's
  deliberately lenient, not stricter.
- **Relevance filtering runs per-listing with no cross-listing context or
  per-user feedback loop.** There's no mechanism yet for a user to mark a
  specific rejection as wrong (or a specific acceptance as unwanted) and
  have that adjust future scoring - every evaluation is a pure function of
  (query, listing) against the shared static vocabulary.
- **Historical relevance cleanup is manual, not automatic or scheduled.**
  `scripts/cleanup_historical_listings.py` (see decision #16) has to be
  run explicitly by a developer; nothing re-evaluates old
  `discovered_listings` rows on its own after a future relevance-engine
  change (e.g. adding a new product family or brand) - the same script
  can be re-run any time to pick up a new set of rules, but there's no
  mechanism that reminds anyone to do so or does it for them.
- **Historical cleanup's row-marketplace-to-saved-search matching is a
  proxy, not a certainty.** Because `DiscoveredListing` doesn't record
  which saved search found it (decision #14), "relevant to at least one
  saved search currently targeting this marketplace" is the closest
  available approximation - if two saved searches target the same
  marketplace with very different queries, a row could be kept because it
  satisfies one of them even though it was actually discovered by (and is
  irrelevant to) the other. This can only ever make cleanup more
  conservative (keep something that arguably should go), never remove
  something that's still wanted by an existing saved search.

## How to keep this file useful

Whenever an architectural decision changes (a new dependency, a change to
the connector interface, a change in how persistence works, etc.), update
this file and `ARCHITECTURE.md` in the same change.
