# Changelog

All notable changes to this project are recorded here. Format is roughly
[Keep a Changelog](https://keepachangelog.com/); dates are `YYYY-MM-DD`.

## [Unreleased]

## 2026-08-22 — Automatic PostgreSQL migrations for Render Free

**Discovered while following up on the previous hardening pass's one
open item ("confirm whether Render's `DATABASE_URL` is set and apply the
new index migration accordingly"): `DATABASE_URL` is set - production
has been running on Render's managed PostgreSQL the whole time, not
local SQLite - and Render's Free plan (what this project runs on) has no
Pre-Deploy Command.** That meant there was no automatic mechanism left
to actually apply a migration (like the previous pass's
`first_discovered_at` index) to the live database at all - only a
manual `alembic upgrade head` run by a human before each deploy, which
isn't a sustainable process and was never actually done. Solved without
requiring a paid Render plan and without needing manual shell access for
routine deploys.

### Added
- `marketplace_alert/core/persistence/migrations.py`
  (`run_pending_migrations()`) - runs `alembic upgrade head`
  automatically at FastAPI startup, PostgreSQL only (a no-op for SQLite,
  local dev/tests unaffected). Uses FastAPI's own ASGI `lifespan`
  protocol rather than a wrapper/entrypoint script - uvicorn already
  blocks serving any request until startup completes, so this alone
  satisfies "before the app begins serving requests" with no change to
  Render's Start Command. Guarded by a Postgres session-level advisory
  lock (`pg_try_advisory_lock`, polled with a bounded wait - new
  `settings.migration_lock_timeout_seconds`, default 30s) so a brief
  overlap during a deploy transition or two close-together manual
  restarts can't run migrations concurrently; released on the same
  connection that acquired it (required for correctness - PostgreSQL
  advisory locks are session-scoped). Fails fast on any error - a bad
  migration, a lock timeout, a connectivity problem - which fails
  FastAPI/uvicorn startup outright, so Render keeps serving the previous
  successful deploy rather than ever going live with a schema/code
  mismatch. Idempotent (an already-current database is a documented
  no-op) and never runs a downgrade. Never logs `DATABASE_URL` or any
  credential. Wired into `main.py`'s `lifespan()` first, before
  `init_db()`, the legacy marketplace-column migration, and the
  background scanner start.
- `settings.migration_lock_timeout_seconds` (default 30.0) in
  `config.py`.
- `tests/test_startup_migrations.py` (16 new tests) - dialect gating
  (SQLite skipped entirely), the PostgreSQL path (lock acquired before
  upgrade runs, lock released and connection closed on both success and
  failure, credentials never logged on either path), a simulated
  already-held lock timing out without ever calling upgrade or
  incorrectly unlocking, the `alembic.ini` path resolving correctly, and
  two lifespan-ordering integration tests that invoke `main.py`'s real
  `lifespan()` directly (bypassing `tests/conftest.py`'s autouse
  no-op-lifespan safety net on purpose, every internal call
  individually monkeypatched) proving migrations run before
  `init_db`/the legacy migration/the scanner start, and that a migration
  failure prevents all of them from running at all. None of this
  connects to a real PostgreSQL server - same lazy-`Engine`/
  mocked-connection approach `tests/test_database_config.py` already
  established. Separately (not part of the automated suite, a one-off
  manual check), `_upgrade_to_head()`'s `Config`/`alembic.ini` path
  resolution was verified for real against a throwaway SQLite database
  from a temp working directory, confirming it's genuinely
  CWD-independent.
- `tests/test_lifespan_isolation.py` - extended the existing
  "TestClient never runs real startup side effects" regression test to
  also cover `run_pending_migrations`, so it's proven (not just assumed)
  that the test suite can never accidentally attempt a real migration
  against the real database.

### Changed
- `marketplace_alert/main.py` - `lifespan()` now calls
  `run_pending_migrations(engine)` before `init_db()`.
- PROJECT_CONTEXT.md - corrected a now-stale claim ("the production
  Render Web Service is not yet connected to PostgreSQL") and added
  decision #20 (the full reasoning above, plus why this reverses part of
  decision #13's original "never migrate at app startup" stance
  specifically for Render Free's constraints).
- ARCHITECTURE.md - corrected the same stale claim in "Database
  selection and PostgreSQL support"'s intro, replaced the now-outdated
  "migrations are not run automatically" bullet, and added a new
  "Automatic migrations on Render Free" section with the full mechanism.
- README.md - "Migrations (Alembic)" now documents the automatic
  startup behavior (including that it also applies if a local `.env`
  points at a real PostgreSQL server) alongside the still-available
  manual commands.
- ROADMAP.md - Phase 8/8.5 updated to reflect that the PostgreSQL
  cutover is done (`DATABASE_URL` is set in production) and that
  migrations now apply automatically.

### Not changed
- The migration files themselves, `alembic/env.py`'s URL resolution,
  the local SQLite bootstrap (`init_db()`), the legacy marketplace-
  column migration, the scheduler, every marketplace connector, the
  relevance engine, and the mobile app - none of this was touched.

## 2026-08-22 — Production hardening and release-readiness pass

A full-repository audit for reliability and correctness, not new
features: no new marketplace, no visual redesign, and the only schema
change is one purely-additive index. Confirmed sound by direct code
reading (nothing changed): `BackgroundScanner`'s single-thread scan loop
structurally can't overlap itself (the next tick's wait only starts after
the current one fully returns), and `SavedSearchRunGuard` is a genuinely
thread-safe (`threading.Lock`-backed) singleton shared by the scheduler
and both manual-run endpoints (legacy + `/api/v1`) - no duplicate-run
risk found there. Full backend suite: 489/489 passing.

### Fixed
- **Relevance engine: legitimate kit listings that mention a bundled
  accessory were being wrongly rejected** - e.g. "Makita XFD131 18V
  Cordless Drill Driver Kit with Carrying Case" scored below the relevance
  threshold once `battery`/`charger`/`case` were added as accessory terms
  (below), because the accessory penalty didn't distinguish "this listing
  is fundamentally an accessory" from "this listing is the core product
  and also happens to mention a bundled accessory." Fixed in
  `evaluate_relevance()` (`core/relevance/evaluator.py`): a match via a
  **2+ word family synonym** (e.g. "cordless drill", "drill driver") is
  now treated as unambiguous core-product evidence and suppresses the
  accessory penalty for that listing; a bare single-word overlap does
  not qualify, since that's exactly the ambiguous case the penalty exists
  to catch. Deliberately **not** extended to the lenient fallback path
  used for unregistered product categories (e.g. guitars, which have no
  registered `families.py` entry) - an earlier version of this fix did
  extend it there and was reverted after "Gibson Les Paul Guitar Stand"
  and "Gibson Les Paul Pickup Set" started scoring as relevant, since a
  multi-word *model name* like "Les Paul" overlaps just as completely in
  an accessory listing for that model as in a listing for the model
  itself. See `evaluator.py`'s docstring for the full reasoning and the
  accepted residual limitation (guitar accessories don't get the kit
  exemption). Regression coverage added to `tests/test_relevance.py`.
- `DiscoveredListing.first_discovered_at` had no database index despite
  being the `ORDER BY` column on every `GET /api/v1/listings` page load
  (the mobile app's Listings screen) - a full-table sort that only gets
  slower as more listings accumulate. Added `index=True`
  (`core/persistence/models.py`) plus a new, purely-additive Alembic
  migration (`alembic/versions/c8800c505bb9_...py`, `create_index` only -
  verified via a full upgrade/downgrade/re-upgrade cycle against a
  throwaway database before finalizing).

### Added
- `marketplace_alert/core/connectors/retry.py` - a shared
  `request_with_retry()` helper, wired into all four real connectors
  (Etsy, eBay, Reverb, Bonanza). Retries only genuinely transient HTTP
  responses - 429, 502, 503, 504 - up to 2 additional times with
  exponential backoff, honoring a `Retry-After` header when a marketplace
  sends one; a permanent failure (401/403 auth, 404, any other status) is
  never retried, since retrying those could never succeed. A connector's
  own status-code-specific handling (e.g. eBay's 401/403 token
  invalidation) stays outside the retry wrapper, unchanged. A network
  error/timeout (`httpx.HTTPError`) propagates immediately, unretried - a
  deliberate scope decision, not an oversight. This closes a real gap:
  connectors previously failed immediately on a transient rate-limit or
  gateway blip, even though `SavedSearchRunner`/`BackgroundScanner`
  already isolate one marketplace's failure from the others in the same
  saved search - this makes a single marketplace less likely to fail at
  all in the first place. `tests/test_connector_retry.py` (17 new tests)
  covers the helper directly; each connector's test file gained one test
  proving it's actually wired to it, plus an autouse fixture
  (`_no_real_sleeps`, mirroring the existing pattern in
  `tests/test_telegram_provider.py`) so retry tests don't really sleep.
- `core/relevance/brands.py` - registered common guitar brands (Fender,
  Squier, Gibson, Epiphone, Ibanez, PRS, Martin, Taylor, Yamaha, ESP,
  Jackson, Charvel, Gretsch, Rickenbacker, Schecter). Previously only
  power-tool brands were registered, so a Reverb/Bonanza search like
  "Fender Stratocaster" got no brand-conflict protection at all - a
  Gibson listing wouldn't have been rejected for a Fender query.
- `core/relevance/accessories.py` - registered common guitar/instrument
  accessory terms (battery, charger, bag, pickguard, pickup, neck, body,
  strap, stand, manual) alongside the existing tool-accessory vocabulary.

### Removed
- `mobile/src/utils/format.ts` - `titleCase()` and
  `formatMarketplacesDisplay()`, dead code confirmed unused by any real
  screen (`SavedSearchCard` renders a saved search's marketplace ids
  directly) and subtly incorrect (wrong brand casing, e.g. "Ebay" instead
  of "eBay"). Their tests removed from `format.test.ts` along with them.

### Reviewed, not changed
- Considered wiring proper backend-driven marketplace display names
  (`display_name_for()`, already used by the dashboard and
  `GET /api/v1/marketplaces`) into the mobile app's `SavedSearchCard`/
  `SavedSearchDetailScreen`, which currently show raw lowercase ids like
  "ebay" instead of "eBay". Deliberately deferred - purely cosmetic, zero
  functional/reliability impact, and out of proportion for a hardening
  pass whose mobile-side brief was "prioritize reliability, avoid
  unnecessary visual redesign."
- Git history, tracked files, and the working tree were checked for
  committed secrets, tokens, `.env` files, or other credential-shaped
  content - none found; `.gitignore` (root and `mobile/`) already covers
  `.claude/`/`*.apk` from the previous task. No git history rewrite was
  needed or performed.

### Not yet implemented
- The remaining items already tracked in PROJECT_CONTEXT.md "Things that
  have NOT yet been implemented" (Bonanza awaiting `BONANZA_DEV_NAME`,
  Mercado Libre/OLX blocked on external auth/approval, mobile
  marketplace-display-name cosmetics, etc.) - this pass hardened the
  existing product, it did not close any of those out.

## 2026-08-22 — Marketplace expansion: nine candidates evaluated, Bonanza added

A broad feasibility pass across nine named marketplace candidates
(Craigslist, OfferUp, Gumtree, Kleinanzeigen, OLX, Vinted, Discogs,
Mercado Libre, Facebook Marketplace) - each checked against its own
current documentation (never assumed) for a real, legitimate, self-serve
API with marketplace-wide keyword search. Exactly one qualified: Bonanza.
The rest are documented, not silently dropped - see PROJECT_CONTEXT.md
decision #18 for the specific reason each one wasn't implemented. No
change to eBay/Etsy/Reverb behavior, no PostgreSQL schema change, no
scraping anywhere, no auth/payments/push-notification work.

### Added
- `marketplace_alert/connectors/bonanza/connector.py`
  (`BonanzaMarketplaceConnector`) - Bonanza's own "Bonapitit" API,
  deliberately modeled on eBay's (deprecated) Finding API
  (`findItemsByKeywords`, confirmed against a real third-party SDK's HTTP
  client since Bonanza's own docs didn't preserve a literal example
  response). `POST` to a single shared endpoint, with the operation name
  and JSON parameters combined into one form-encoded body field (Bonanza's
  own wire format) and `X-BONANZLE-API-DEV-NAME: <BONANZA_DEV_NAME>`.
  Paginates via `pageNumber` (bounded by `bonanza_result_limit` and a
  hard `MAX_PAGES` safety cap - Bonanza's API isn't HAL-based like
  Reverb's, so there's no `_links` to follow). Handles missing dev name,
  401/403/429/5xx, timeout, connection error, malformed JSON, a missing/
  malformed response envelope, and an `ack` of `"Failure"` - matching
  every other connector's failure-handling conventions. Never logs or
  exposes the dev name. See the module's docstring for the full citation
  list and an explicit accounting of what's confirmed vs. defensively
  inferred (the price field's exact shape, and whether `condition` is
  present at all, weren't independently confirmable from Bonanza's public
  docs).
- `settings.bonanza_dev_name` / `BONANZA_DEV_NAME` (a single developer
  name, not a secret token) and `settings.bonanza_result_limit` (default
  25) in `config.py`/`.env.example`. Missing dev name -> `is_configured`
  is False, `search()` raises a clear configuration error, app startup
  and every other connector are completely unaffected.
- `"bonanza": lambda: BonanzaMarketplaceConnector(...)` in
  `connectors/registry.py` - `get_connector("bonanza")` now resolves it,
  `list_supported_marketplaces()` includes it, and the dashboard,
  `GET /api/v1/marketplaces`, and the mobile Create Search screen all
  pick it up automatically with no marketplace-specific code of their
  own (same registry-driven guarantee proven for Reverb, now proven a
  fourth time).
- `tests/test_bonanza_connector.py` (30 new tests) - request construction
  (URL, the dev-name header, the form-encoded body shape), pagination
  (advancing page numbers, stopping on a short page, stopping once
  `result_limit` is reached, the `MAX_PAGES` safety cap), normalization
  of every mapped field (including the price value/attribute-object vs.
  plain-number fallback, condition as an object or plain string), missing
  optional fields left `null`, Unicode content, credentials missing/401/
  403/429/500/timeout/connection error/malformed JSON/missing response
  envelope/`ack: Failure`, a missing `item` key treated as zero results
  (not malformed), one malformed listing skipped rather than failing the
  whole search, and the dev name never appearing in an error message or a
  log line.
- Bonanza-specific additions to `tests/test_connector_registry.py`
  (resolution, support, display name), `tests/test_dashboard.py` /
  `tests/test_api_v1.py` (Bonanza appears in the dashboard status panel
  and `GET /api/v1/marketplaces`, reflects its dev name's configured
  state, never leaks the dev name), `tests/test_saved_search_scheduler.py`
  (scheduler survives a real Bonanza connector failure; other
  marketplaces still run if Bonanza fails in the same saved search; a
  Bonanza duplicate is not notified twice; Bonanza results pass through
  the same relevance engine, with an irrelevant Bonanza listing neither
  persisted nor notified), and
  `mobile/src/screens/CreateSearchScreen.test.tsx` (a second new
  marketplace, added only to mocked API data, still renders/selects/
  submits correctly alongside the first).

### Not implemented, with reasons (PROJECT_CONTEXT.md decision #18 has
the full write-up for each)
- **Craigslist** - no read/search API (only a seller bulk-post API).
- **OfferUp** - no public API.
- **Gumtree** / **Kleinanzeigen** - no official API; scraping-only
  alternatives exist and were not used.
- **Vinted** - the only official API is manually-allowlisted to approved
  Pro sellers for their own inventory, not general search.
- **Discogs** - has a real, self-serve personal-access-token API, but its
  Marketplace endpoints only support browsing a *known* seller's
  inventory or a *known* listing ID - no marketplace-wide keyword search
  across sellers exists.
- **OLX** - requires a formal partner application with manual review
  before any credentials are issued. **BLOCKED BY EXTERNAL APPROVAL.**
- **Mercado Libre** - requires OAuth's `authorization_code` grant (a real
  user completing a browser consent flow) for every access token; no
  app-only grant for read-only search exists. **BLOCKED BY USER AUTH.**
- **Facebook Marketplace** - Meta has never published a public search
  API; the only API that exists is a restricted, partner-approval-gated
  seller commerce API, structurally unable to support general search.

### Changed
- PROJECT_CONTEXT.md - new architectural decision #18 (the full
  nine-candidate investigation and why each wasn't implemented, Bonanza's
  eBay-Finding-API lineage, and the "awaiting credentials" status) and
  updated "Things that have NOT yet been implemented" entries.
- ARCHITECTURE.md - new "The Bonanza connector" section (mirroring "The
  Reverb connector"'s structure), updated "The connector interface" for
  the fourth proof of zero-core-changes, and a new "Why these choices"
  entry.
- `.env.example` - added `BONANZA_DEV_NAME=` (no real value).

## 2026-08-22 — Reverb: the third real marketplace connector

Reverb API access was manually verified before this task
(`GET https://api.reverb.com/api/listings?query=Fender&per_page=1` with a
real personal access token, `public` scope only, returned a real
listing). Adds `ReverbMarketplaceConnector`, registered alongside mock/
Etsy/eBay, flowing through the exact same saved-search/scheduler/
relevance/duplicate-detection/notification/mobile-API/dashboard pipeline
every other connector already uses - zero changes needed outside
`connectors/registry.py` and the new `connectors/reverb/` package. No
other marketplace added, no PostgreSQL schema change, eBay/Etsy/Telegram
behavior unchanged, no auth/payments/push-notification work.

### Added
- `marketplace_alert/connectors/reverb/connector.py`
  (`ReverbMarketplaceConnector`) - searches
  `GET https://api.reverb.com/api/listings` (`query`/`per_page`),
  `Authorization: Bearer <REVERB_API_TOKEN>` +
  `Accept: application/hal+json` + `Accept-Version: 3.0`. Paginates by
  following `_links.next.href` verbatim (Reverb's own stated HAL design
  principle - "never construct your own URLs"), bounded by
  `reverb_result_limit` and a hard `MAX_PAGES` (5) safety cap. Handles
  missing token, 401/403/404/429/5xx, timeout, connection error,
  malformed JSON, and a missing/malformed `listings` collection (a hard
  error on the first page; just stops paginating on a later one) -
  matching Etsy's/eBay's existing failure-handling conventions. Logs
  common rate-limit header conventions at INFO if present; never logs or
  exposes the token. See the module's docstring for the full research
  citation list and an explicit accounting of which parts of Reverb's
  response shape are independently confirmed vs. defensively inferred
  (Reverb's own public docs don't publish a complete example listing
  object).
- `settings.reverb_api_token` / `REVERB_API_TOKEN` (single static
  personal access token, not a client id/secret pair) and
  `settings.reverb_result_limit` (default 25) in `config.py`/`.env.example`.
  Missing token -> `is_configured` is False, `search()` raises a clear
  configuration error, app startup and every other connector are
  completely unaffected.
- `"reverb": lambda: ReverbMarketplaceConnector(...)` in
  `connectors/registry.py` - `get_connector("reverb")` now resolves it,
  `list_supported_marketplaces()` includes it, and both the dashboard's
  marketplace checkboxes and `GET /api/v1/marketplaces` pick it up
  automatically with no marketplace-specific code of their own.
- `connectors/registry.py:display_name_for()` - one shared mapping for a
  marketplace's brand-cased display name (e.g. `"ebay"` -> `"eBay"`,
  `"reverb"` -> `"Reverb"`), now used by both the dashboard and the
  mobile API instead of each keeping an independent copy (see "Changed"
  below).
- `tests/test_reverb_connector.py` (40 new tests) - request construction
  (URL, `query`/`per_page`, the Bearer token, `Accept-Version: 3.0`),
  pagination (following `_links.next` verbatim, stopping once
  `result_limit` is reached, stopping when no `next` link is present, the
  `MAX_PAGES` safety cap), normalization of every mapped field (title,
  description, price incl. a `price_cents` fallback and never parsing a
  display string, currency, condition as an object or plain string,
  seller/shop with a fallback, image incl. plain-string and multi-size
  `_links` shapes, listing URL, `published_at` incl. a `created_at`
  fallback), missing/optional fields left `null`, Unicode/emoji content
  surviving the full request pipeline (not just the normalizer),
  credentials missing/401/403/404/429/500/timeout/connection error/
  malformed JSON/missing `listings` key/unexpected pagination shape, one
  malformed listing (or one missing its web link) being skipped rather
  than failing the whole search, and the token never appearing in an
  error message or a log line.
- Reverb-specific additions to `tests/test_connector_registry.py`
  (resolution, support, display name), `tests/test_dashboard.py` /
  `tests/test_api_v1.py` (Reverb appears in the dashboard status panel
  and `GET /api/v1/marketplaces`, reflects its token's configured state,
  never leaks the token), `tests/test_saved_search_scheduler.py`
  (scheduler survives a real Reverb connector failure; Etsy/eBay still
  run if Reverb fails in the same saved search; a Reverb duplicate is not
  notified twice; a Reverb listing and an eBay listing sharing the same
  external id remain independent; Reverb results pass through the same
  relevance engine, with an irrelevant Reverb listing neither persisted
  nor notified), and `mobile/src/screens/CreateSearchScreen.test.tsx`
  (the marketplace selector renders, respects the `configured` flag of,
  and can submit a saved search for a marketplace added only to the
  *mocked API response* - proof the mobile app needs no code change to
  support a new backend marketplace).

### Changed
- `marketplace_alert/main.py` - the dashboard's system-status panel
  (previously one hard-coded row each for "Etsy configured?"/"eBay
  configured?") now loops over every registered marketplace generically,
  the same rule the marketplace checkboxes already followed - adding a
  third hard-coded row for Reverb would have repeated exactly the
  duplication this project otherwise avoids. `templates/dashboard.html`
  updated to match (one `{% for %}` loop instead of two individual
  `<li>` elements).
- `marketplace_alert/api/v1/marketplaces.py` - its own local
  `_DISPLAY_NAMES` dict replaced with the new shared
  `connectors/registry.py:display_name_for()`, so a marketplace's brand
  casing is defined once, not duplicated between this route and the
  dashboard.
- PROJECT_CONTEXT.md - new architectural decision #17 (Reverb: the
  research/citations behind the field mapping, the pagination approach,
  the display-name-mapping generalization) and updated "Things that have
  NOT yet been implemented" entries.
- ARCHITECTURE.md - new "The Reverb connector" section (mirroring "The
  eBay connector"'s structure), updated "The connector interface"/"The
  management dashboard" sections for the registry-driven status panel,
  and two new "Why these choices" entries.
- `.env.example` - added `REVERB_API_TOKEN=` (no real value).

## 2026-08-22 — Historical listing cleanup + a real relevance-engine bug fix

Mobile verification confirmed the relevance engine (added earlier the
same day) works correctly for new scans, but the mobile Listings screen
still showed old irrelevant listings (wrong-brand items, battery holders)
because they were persisted *before* relevance filtering existed - it
only ever applied going forward. Adds a one-off, explicitly-invoked
cleanup that re-evaluates existing `discovered_listings` rows against the
current relevance engine and removes the ones it would now reject. No
new marketplace, no schema change, no scheduler/notification/connector
change, no automatic deletion, and no Saved Search was modified or
deleted.

### Added
- `marketplace_alert/core/persistence/cleanup.py` -
  `preview_historical_cleanup(session)` (dry run) and
  `run_historical_cleanup(session)` (deletes), both returning a
  `HistoricalCleanupResult` (`total_rows`, `evaluated_count`,
  `skipped_no_saved_search_count`, `kept_count`, `removed_count`,
  `removed`). `DiscoveredListing` has no relationship to `SavedSearch`
  (no stored query, no foreign key - see PROJECT_CONTEXT.md decision
  #14), so there is no way to know which specific saved search
  originally found a given row; each row is instead re-evaluated against
  every saved search **currently** targeting its marketplace (active or
  paused) and kept if relevant to at least one. A row whose marketplace
  has no current saved search at all is left untouched rather than
  deleted on a guess.
- `ListingRepository.list_all()` / `.delete(row)` (`core/persistence/
  repository.py`) - two small additions used only by this maintenance
  path; the normal scan/dedup path never deletes a row.
- `scripts/cleanup_historical_listings.py` - a manual CLI, defaulting to
  a dry-run report; only deletes and commits with an explicit `--apply`
  flag. Never invoked from app startup, the scheduler, or an API endpoint.
- `tests/test_historical_cleanup.py` (15 new tests) - relevant/irrelevant/
  brand-conflicting historical listings, a listing relevant to at least
  one of several current saved searches, the "no current saved search for
  this marketplace" preservation policy, paused saved searches still
  counting as current interest, dry-run-never-mutates, preview/run
  agreement, idempotency, row-count accounting, and confirmation that
  Saved Search rows are never modified or deleted by cleanup.

### Fixed
- **A real relevance-engine bug**, found by re-evaluating actual
  production-shaped data (not a synthetic test fixture): a bare
  brand-only saved search (no product term at all, e.g. the literal
  query "Makita" - two of the real saved searches in this project's local
  database use exactly that) was being treated as relevant to almost any
  listing that didn't explicitly mention a *different* brand, including
  listings that didn't mention the queried brand at all (confirmed
  directly: `evaluate_relevance("Makita", "Pokemon Charizard Holo Card")`
  returned `is_relevant=True`). On the real local database, this meant
  only 14 of 224 historical rows would have been flagged for removal,
  most of it clearly-irrelevant data kept purely on this technicality.
  Fixed in `marketplace_alert/core/relevance/evaluator.py`: a brand-only
  query is now relevant only if the listing actually mentions that brand
  (new `rejected_reason` value: `"brand_only_query_not_mentioned"`).
  Scoped narrowly to the brand-only-query case - every other scoring rule
  is unchanged. Two existing scheduler tests
  (`tests/test_saved_search_scheduler.py`) that paired `query="Makita"`
  with an unrelated "Pokemon Charizard" fixture (an arbitrary placeholder,
  not an actual relevance test - both tests exercise multi-marketplace
  scheduling/resilience, not relevance) were updated to use a matching
  query ("Charizard"), consistent with sibling tests in the same file.
  Under the corrected engine, the same real local database flagged 50 of
  224 rows for removal instead of 14.

### Changed
- PROJECT_CONTEXT.md - new architectural decision #16 (historical
  cleanup: the schema limitation and how it's honestly worked around, the
  bug found and fixed, the manual-only invocation) and two new "Things
  that have NOT yet been implemented" entries (cleanup isn't automatic or
  scheduled; the marketplace-to-saved-search matching is a proxy, not a
  certainty). Updated the now-superseded "existing rows were not
  re-evaluated" limitation to reflect that they now can be, manually.
- ARCHITECTURE.md - new "Historical listing cleanup" section; updated
  "Relevance filtering"'s brand-only-query description, the
  `rejected_reason` enumeration (four values -> five), the worked-example
  table, and the matching "Why these choices" entry to describe the fixed
  behavior instead of the buggy one.

## 2026-08-22 — Relevance filtering layer

Real mobile testing surfaced a data-quality problem: a "Makita drill"
saved search was returning Etsy listings for DeWalt/Milwaukee battery
holders and generic tool organizers - technically keyword hits, not
results anyone actually wants. Adds a deterministic, rule-based relevance
scoring layer between every marketplace connector and persistence/
notification, wired into all four ways a scan can run. No new
marketplace, no schema change, no authentication change, no mobile UI
change, and no LLM/embeddings/vector-database dependency - see
ARCHITECTURE.md "Relevance filtering" for the full design.

### Added
- `marketplace_alert/core/relevance/` (new package) - `text.py`
  (normalization + shared n-gram phrase matcher), `brands.py` (extensible
  brand vocabulary + conflict detection), `families.py` (configurable
  product-family synonym mapping - "drill" registered by default, with
  strong synonyms like "hammer drill"/"cordless drill" and a lower-scored
  related synonym, "impact driver"), `accessories.py` (accessory-term
  vocabulary - holder, mount, case, organizer, ...), `query.py` (query
  parsing: brand extraction, core tokens), `models.py`
  (`RelevanceEvaluation`, `RelevanceFilterResult`), `evaluator.py`
  (`evaluate_relevance` - the scoring engine), `service.py`
  (`filter_relevant_listings` - the one shared entrypoint).
- `evaluate_relevance(query, listing)` returns a transparent 0-100 score
  from four independently-checked signals: brand conflict (reject
  outright), brand match bonus, core product-term match (family synonym,
  accessory-phrase, or lenient token-overlap fallback for unregistered
  categories), and an accessory penalty that only applies when the
  query itself didn't ask for an accessory. `rejected_reason` is always
  one of `brand_conflict`, `accessory_without_core_product_match`,
  `no_core_product_match`, or `low_relevance_score`.
- `filter_relevant_listings()` wired into `SavedSearchRunner
  ._run_one_marketplace()` (covers the background scheduler and both the
  legacy and mobile Run Now endpoints - all three share this one runner)
  and `main.py`'s legacy `GET /scan` - filtering happens before
  `ListingDiscoveryService.process_listings()`, so a rejected listing is
  never persisted, never marked already-seen, and never notified about.
  `GET /search` (stateless, no persistence/notification) is intentionally
  left unfiltered.
- Optional `raw_count`/`rejected_count` fields, additive only, on
  `MarketplaceRunResult` (`core/saved_searches/runner.py` and
  `schemas.py`), `MarketplaceRunOutcome` (`api/v1/schemas.py`), and
  `ScanResult` (`main.py`) - existing `new_count`/`already_seen_count`
  fields now reflect post-filter results; these two expose how many raw
  results the connector returned and how many were dropped as
  irrelevant, for a client that wants that visibility.
- Each rejection is logged once, at `INFO` level, with the saved search
  id, query, marketplace, external listing id, score, and rejection
  reason only - never the full listing body, never a credential.
  Accepted listings are not logged, to avoid spam on a normal scan.
- `tests/test_relevance.py` (51 new tests) - unit coverage for every
  building block, every named scenario from this feature's own brief
  ("Makita drill", "Bosch drill", "Makita battery holder", bare "drill"
  with no brand - each brand/product/accessory combination named),
  punctuation/case/whitespace normalization robustness, and integration
  tests proving: the scheduler and both Run Now endpoints share the exact
  same relevance path, a rejected listing is never persisted, never
  notified, and duplicate detection still works correctly for the
  listings that do pass.

### Changed
- PROJECT_CONTEXT.md - new architectural decision #15 (relevance
  filtering: why it exists, the scoring signals, the extensible
  vocabulary pattern, the one shared entrypoint) and three new "Things
  that have NOT yet been implemented" entries (vocabulary coverage is
  power-tools-only today; no per-user feedback loop; existing
  `discovered_listings` rows were not re-evaluated).
- ARCHITECTURE.md - new "Relevance filtering" section (data flow, every
  building block, the full scoring breakdown, a worked-example table
  matching every named test scenario) plus new "Why these choices"
  entries explaining the rule-based-not-LLM choice, the lenient
  unregistered-category fallback, and the brand-only-query special case.

## 2026-08-21 — Versioned Mobile API foundation (`/api/v1`)

Groundwork for the next major goal - a real Android/iOS app - not the app
itself. Adds a stable, JSON-only, versioned REST API under `/api/v1`
alongside every existing route (dashboard, `/health`, `/search`, `/scan`,
the legacy `/saved-searches*`), depending on no HTML/dashboard behavior
and duplicating no business logic. No mobile app, authentication,
payments, push notifications, or new marketplace was built - see "Not yet
implemented" below.

### Added
- `GET /api/v1/status` - mobile-safe status (`status`, `backend`,
  `database` - a real `SELECT 1` check, `telegram_configured`,
  `supported_marketplaces`) - booleans and plain marketplace ids only,
  never a credential value or the database connection string.
- `GET /api/v1/marketplaces` - one entry per registered connector
  (`id`, `name`, `configured`, `available`), driven entirely by
  `list_supported_marketplaces()`/`get_connector()` - never a separately
  hard-coded marketplace list. `configured` uses
  `getattr(connector, "is_configured", True)` (not every connector has a
  credentials concept); `available` is the connector's own
  `health_check()`.
- `GET`/`POST /api/v1/saved-searches`, `GET`/`PATCH`/`DELETE
  /api/v1/saved-searches/{id}` - thin adapters over the exact same
  `SavedSearchService` the legacy `/saved-searches*` routes use;
  request/response schemas (`SavedSearchCreate`/`Update`/`Read`)
  re-exported from `core/saved_searches/schemas.py`, not re-declared, so
  the mobile and legacy contracts can't drift apart on something like the
  minimum scan interval.
- `POST /api/v1/saved-searches/{id}/run` - runs through the identical
  `SavedSearchRunner`/`SavedSearchRunGuard` the legacy manual-run endpoint
  uses (sharing the *same* run guard instance, so the same saved search
  can never run through both endpoints, or the scheduler, at once); only
  the response is reshaped - `marketplaces` keyed by marketplace name
  (`{"etsy": {"new_count": 1, "already_seen_count": 15, "error": null}, ...}`)
  plus `query`, `total_new_count`, `total_already_seen_count` - easier for
  a mobile client to consume than the legacy list-shaped response.
- `GET /api/v1/listings` - paginated (`limit`, default 20, max 100;
  `offset`), optionally filtered by `marketplace` (422 on an unrecognized
  one), sorted newest-discovered-first. Backed by two new
  `ListingRepository` methods (`list_recent`, `count`) - no new table, no
  new migration. **Two real, pre-existing limitations documented rather
  than faked**, per this feature's own requirements:
  - `price`/`currency`/`location`/`condition`/`image_url` are always
    `null` - `DiscoveredListing` has never persisted them (only since
    Phase 3's original design, not something this change removed).
    Mapped explicitly, field-by-field, rather than via Pydantic's
    `from_attributes` auto-mapping, so this is impossible to miss reading
    the code.
  - No `saved_search_id` or `only_new` filter - `DiscoveredListing` has no
    relationship to `SavedSearch` (dedup identity is global, by design -
    see Phase 3/`ListingDiscoveryService`), and "new" is a property of one
    scan run, never a persisted column. Both would have required
    inventing data or a relationship that isn't actually stored.
- `marketplace_alert/api/v1/` - the new package (`schemas.py`, `status.py`,
  `marketplaces.py`, `saved_searches.py`, `listings.py`, `__init__.py`
  aggregating every sub-router under one `APIRouter(prefix="/api/v1")`).
  Each sub-router carries its own OpenAPI tag
  (`"Mobile API - Status"`/`"- Marketplaces"`/`"- Saved Searches"`/
  `"- Listings"`, described via `FastAPI(openapi_tags=...)`) and every
  operation has a `summary`/`description`, so `/docs` documents `/api/v1`
  clearly and separately from the legacy operations.
- CORS preparation: `CORSMiddleware` (FastAPI's own, no new dependency)
  always added, `allow_origins` from the new
  `settings.cors_allowed_origins` (`CORS_ALLOWED_ORIGINS`, comma-separated)
  - empty/off by default, never `"*"`. Native mobile apps don't need
  browser CORS at all; this is for possible future web-based tooling only.
  `.env.example` updated with a placeholder (empty) value.

### Changed
- `marketplace_alert/dependencies.py` (new module) now owns the singletons
  `main.py` used to construct itself (`NotificationService`,
  `SavedSearchRunner`, `SavedSearchRunGuard`) - extracted so both the
  legacy routes and the new `/api/v1` routers depend on the *exact same*
  objects (critical for the run guard to actually prevent overlapping
  runs across both). `main.py` re-imports everything under its original
  (leading-underscore) names - a pure extraction, not a behavior change:
  every existing test import (`from marketplace_alert.main import
  get_notification_service`, `_saved_search_run_guard`, etc.) still
  resolves to the identical objects, confirmed by the full existing test
  suite passing unmodified.
- `core/persistence/repository.py` (`ListingRepository`): added
  `list_recent(limit, offset, marketplace=None)` and
  `count(marketplace=None)` - the only new persistence-layer code this
  change needed.

### Tests
- Tests (276/276 passing, up from 233): new `tests/test_api_v1.py`
  (status shape and secrets-never-exposed, marketplace metadata matching
  the registry and reflecting real credential configuration, saved-search
  create/list/get/update/delete, invalid-marketplace and invalid-interval
  validation (422), missing-saved-search 404s, manual run's structured
  mobile response, run-guard sharing with the legacy endpoint verified
  directly, listings pagination/ordering/marketplace-filtering, the
  always-null unpersisted listing fields, invalid marketplace filter
  (422), error responses never containing stack traces, and existing
  legacy routes (`/`, `/health`, `/saved-searches`, `/docs`,
  `/openapi.json`) still reachable alongside `/api/v1`) and
  `tests/test_cors_config.py` (unset/empty default, comma-separated
  parsing with whitespace/empty-entry handling, a real list passed
  directly, never `"*"` by default, and an integration check that the
  real app sends no CORS header for an unconfigured origin). All existing
  Etsy/eBay/mock, persistence, saved-search, scheduler, Telegram, and
  PostgreSQL/SQLite-selection tests continue to pass unchanged - no
  marketplace, notification, scheduling, or database-selection code was
  touched by this change. Every test uses the existing isolated-database,
  fake-notification-provider `client` fixture (`tests/conftest.py`) -
  `lifespan()` (and therefore the real background scanner) is never
  triggered during the suite.

### Not yet implemented
- The actual mobile application (React Native, Expo, Flutter, native
  Android/iOS) - `/api/v1` is groundwork for it, not the app.
- Authentication on `/api/v1` - every endpoint is exactly as open as the
  legacy routes today. The dependency-injection structure is ready for a
  `Depends(...)`-based auth check to be added per endpoint later, but
  nothing enforces it now, and no user id was fabricated.
- Persisting `price`/`currency`/`location`/`condition`/`image_url` on
  `DiscoveredListing`, and a `saved_search_id` relationship for listings -
  both real, separate future work (a new migration, and a decision about
  already-discovered rows), not addressed here.
- Push notifications, payments, new marketplaces - none touched.

## 2026-08-15 — PostgreSQL support, alongside SQLite

The system is live on Render; a Render PostgreSQL database
(`marketplacealert-db`) already exists but the deployed Web Service isn't
connected to it yet. This change adds that *capability* to the codebase -
schema compatibility and `DATABASE_URL` selection - without touching any
Render configuration, deploying anything, or migrating local data. SQLite
remains fully supported and is still the local dev/test default.

### Added
- `resolve_database_url()` / `normalize_database_url()`
  (`marketplace_alert/core/persistence/database.py`): `DATABASE_URL` set ->
  PostgreSQL, normalized (Render's `postgres://`/`postgresql://` URL forms
  both rewritten to `postgresql+psycopg://`, so the new `psycopg` (v3)
  driver dependency is always what SQLAlchemy actually uses); unset -> the
  exact same local SQLite default as before
  (`sqlite:///./marketplace_alert.db`). The single, central place this
  decision and normalization happen - `alembic/env.py` resolves its URL
  the same way, so migrations can never target a different database than
  the app does. Neither function - nor `create_db_engine()` - ever logs
  the URL, which may contain a real password.
  `settings.database_url` (`config.py`) changed from a hard-coded SQLite
  default to `str | None = None`, so "unset" is representable and this is
  the only place a default is decided.
- `psycopg[binary]>=3.2` and `alembic>=1.13` added to
  `pyproject.toml` dependencies - both installed by Render's existing
  `pip install .` build command, no build-command change needed.
- `create_db_engine()` now sets `pool_pre_ping=True` for every backend -
  cheap for SQLite, and meaningful for PostgreSQL: a managed Postgres
  instance (Render's included) can close idle connections server-side,
  and pre-ping transparently detects and replaces a dead pooled
  connection rather than a request failing with one.
- **Alembic** (`alembic/`, `alembic.ini`), initialized and configured to
  resolve its database URL from `marketplace_alert.config.settings`
  (never `alembic.ini`'s own `sqlalchemy.url`, left blank on purpose - see
  the comment there) - the exact same resolution the running app uses.
  One baseline migration
  (`alembic/versions/367e525494b6_baseline_schema.py`), autogenerated by
  diffing `Base.metadata` against a fresh, completely empty *temporary*
  SQLite database (never the developer's real local database, never any
  production database) - represents today's schema (`discovered_listings`,
  `saved_searches` - no legacy `marketplace` column - and
  `saved_search_marketplaces`, with all existing constraints/indexes/FKs
  intact). Purely additive (`create_table`/`create_index` only) on the
  upgrade path; no destructive migration logic. Verified directly (not
  assumed) against real temp-file SQLite databases: upgrade on a fresh
  database succeeds and is idempotent on rerun; upgrade+downgrade+upgrade
  round-trips cleanly; **upgrade against a database that already has the
  current schema (e.g. a pre-existing local SQLite file) fails cleanly
  with "table already exists" rather than doing anything destructive** -
  `alembic stamp head` is the correct, documented tool for that case
  instead (records the revision as applied without executing any DDL).

### Changed
- `init_db()` (`core/persistence/database.py`) now checks the engine's
  actual dialect before calling `Base.metadata.create_all()`: SQLite keeps
  the original automatic bootstrap on every startup (safe - additive only,
  never drops or alters a table); any other backend (PostgreSQL) now
  no-ops, logging why (dialect name only, never the URL), rather than
  either crashing or silently doing nothing unexplained. PostgreSQL schema
  is managed by Alembic migrations instead, applied separately and
  deliberately - not automatically inside the app's own startup
  (`main.py`'s `lifespan`), since Render can run more than one
  instance/worker and several independently racing to run migrations at
  boot has no benefit over running it once, first, via a Render Pre-Deploy
  Command (recommended - see below) or a manual `alembic upgrade head`
  before a deploy. `main.py`'s `lifespan` itself is otherwise unchanged -
  still calls `init_db()` unconditionally; it's simply a no-op for
  PostgreSQL now.
- `.env.example`: `DATABASE_URL` changed from a filled-in SQLite default to
  blank (`DATABASE_URL=`), with a comment explaining unset -> SQLite,
  set -> PostgreSQL in production. The developer's real `.env` was not
  modified.
- `README.md`: new "Database" section documenting the SQLite/PostgreSQL
  split and the exact Alembic commands (`alembic upgrade head` for a fresh
  database, `alembic stamp head` for one that already has the current
  schema, `alembic revision --autogenerate` for future model changes).

### Reviewed, not changed
- **Database session handling** (web requests via `get_db_session()`,
  the scheduler via `BackgroundScanner`'s per-run `session_factory()`
  calls) - confirmed already safe for PostgreSQL as-is: a fresh `Session`
  per request/run, closed in `finally`, never one shared/reused globally
  across threads. What *is* shared globally (`engine`, `SessionLocal`) is
  exactly what SQLAlchemy's own thread-safety model says is safe to share.
  No code changed here - this was a deliberate review, not a fix.
- Etsy connector, eBay connector, eBay OAuth logic, saved searches,
  multi-marketplace architecture, duplicate detection, and dashboard
  behavior - none of these were touched by this change.

### Tests
- Tests (233/233 passing, up from 205): new `tests/test_database_config.py`
  (DATABASE_URL absent -> SQLite selected, present -> PostgreSQL selected
  and normalized for both Render URL forms, driver-specific `connect_args`/
  `pool_pre_ping` construction, secrets never appearing in any log line or
  in the engine's own `repr()`/`str()`, `init_db()` skipping `create_all()`
  for non-SQLite, database session lifecycle - fresh `Session` per call,
  closed after use, rolled back on exception) and
  `tests/test_alembic_migrations.py` (the baseline migration creates
  exactly the tables `Base.metadata` defines with no legacy columns,
  upgrade is idempotent, upgrade+downgrade+upgrade round-trips, and the
  `upgrade`-fails/`stamp`-succeeds distinction against a database that
  already has the schema). None of these connect to a real PostgreSQL
  server - `create_engine()` builds an `Engine` lazily (no connection until
  a pool checkout), so asserting on the resolved URL/dialect/driver proves
  PostgreSQL selection without needing real Postgres infrastructure in
  CI - only throwaway temp-file SQLite databases are ever touched, never
  the developer's real `marketplace_alert.db`. All existing Etsy, eBay,
  mock, persistence, saved-search, multi-marketplace, scheduler, and
  dashboard tests continue to pass unchanged.

### Not yet implemented
- The production Render Web Service's `DATABASE_URL` is not set - it is
  not yet connected to `marketplacealert-db`. No Render configuration was
  touched and nothing was deployed as part of this change.
- Copying existing local SQLite data into PostgreSQL - explicitly out of
  scope for this change (schema compatibility + selection only); handled
  separately later.
- Running the baseline migration against the real `marketplacealert-db`
  - not done as part of this change (would require connecting to
  production infrastructure, which this change deliberately avoided).
- A Postgres-backed integration test suite - normal tests deliberately
  don't require a real PostgreSQL server; a separate optional integration
  configuration could be added later if genuinely needed.

## 2026-08-15 — Robust Telegram delivery under bursts

**Real-world issue observed**: a new Etsy + eBay saved search returned many
new listings in one scan. Telegram successfully sent many notifications,
but some failed outright with "Telegram API request failed" - burst
sending with no pacing hit Telegram's own rate limiting, and there was no
retry for transient failures. Fixed without adding any external queue
infrastructure (no Redis/Celery/RabbitMQ) - the delivery path stays fully
synchronous and provider-based, just more resilient.

### Changed
- `TelegramNotificationProvider.send_listing_alert`
  (`marketplace_alert/notifications/telegram/provider.py`) now retries
  *transient* failures - HTTP 429, 500, 502, 503, 504, and
  timeout/connection errors - up to `max_retries` times (bounded, never
  forever) with exponential backoff (`retry_base_seconds * 2^(attempt-1)`).
  On HTTP 429, Telegram's own `parameters.retry_after` from the response
  body is honored directly when present, instead of guessing a wait time.
  *Permanent* failures - any other non-200 status (400 malformed request,
  401/403 bad credentials, 404 chat not found, etc.) and a 200 response
  with `"ok": false` - are never retried, since retrying a bad bot token or
  chat ID could never succeed; they raise `NotificationError` on the first
  attempt, exactly as before this change. The external contract
  (`NotificationError` on failure, nothing on success) is unchanged -
  callers don't need to know a retry happened.
- `NotificationService.notify_new_listings`
  (`marketplace_alert/core/notifications/service.py`) now sends listings
  strictly in order, waiting `send_delay_seconds` between sends (never
  before the first one, so a single new listing is never delayed) - a
  generic pacing knob (not Telegram-specific logic) that governs the gap
  *between* separate listings' sends, distinct from the provider's own
  per-send retries. A failure on one listing (after the provider's own
  retries are exhausted) is still logged and skipped, exactly as before -
  it never stops the remaining listings in the batch, and
  `notify_new_listings` itself still never raises.
- `settings.telegram_send_delay_seconds` (default `1.0` - Telegram's own
  documented guideline for messages to the same chat), `settings.telegram_max_retries`
  (default `3`, so up to 4 attempts total), `settings.telegram_retry_base_seconds`
  (default `2.0`) added to `marketplace_alert/config.py`, wired into
  `NotificationService`/`TelegramNotificationProvider` construction in
  `main.py`. `.env.example` updated with placeholder/default values only -
  the developer's real `.env` was not modified.

### Added
- Structured logging (JSON, via the existing `configure_logging()`) for:
  notification queued, notification sent, retry attempt (with the reason
  and wait time), permanent failure, and final failure after retries
  exhausted - all sanitized, same rule as every existing log line here:
  never the bot token, chat ID, or any other credential.
- Tests (205/205 passing, up from 180): extensive additions to
  `tests/test_telegram_provider.py` (timeout/connection-error/500/502/503/504
  retried then succeeding, HTTP 429 honoring `retry_after` vs. falling back
  to backoff when absent, retries bounded then raising, `max_retries=0`
  meaning a single attempt, permanent 400/401 and `"ok": false` never
  retrying, credentials never appearing in a raised error message or in any
  log line across a full retry sequence - via a new autouse fixture that
  captures `time.sleep` calls instead of actually sleeping, so the whole
  file still runs in well under a second) and `tests/test_notification_service.py`
  (a burst of 5 listings paced with the expected gaps and no delay before
  the first, a single new listing never delayed, zero/negative delay
  configuration handled safely, send order preserved, one failed
  notification not stopping delivery to the others in the same batch); new
  cases in `tests/test_saved_search_scheduler.py` (a Telegram-side failure,
  not a connector failure, surviving both a manual run and a full scheduler
  tick with another saved search still completing in the same tick, and -
  the key semantic guarantee - a listing persisted during a run where
  notification failed is *not* re-notified on a later run, proving the
  database and not delivery success is the source of truth for "already
  discovered") and `tests/test_saved_searches_api.py` (the manual
  `POST /saved-searches/{id}/run` endpoint surviving a notification
  provider failure with a normal 200 response, not a 500). All existing
  Etsy, eBay, mock, connector-registry, persistence, and dashboard tests
  continue to pass unchanged - no marketplace connector, OAuth, saved-search,
  or duplicate-detection code was touched by this change.

### Not yet implemented
- A persistent background notification queue/worker thread - considered
  and deliberately not built for this; see `ARCHITECTURE.md` "Why these
  choices" for the reasoning (mainly: it would decouple "the scan
  finished" from "the alerts were sent" for no real benefit at this
  scale, and complicate testing). Delivery stays synchronous: a saved
  search with a very large burst of new listings will take proportionally
  longer to return while pacing sends, rather than returning immediately
  and delivering in the background.
- Retry/backoff/rate limiting for the marketplace **connectors**
  themselves (Etsy, eBay) - this change is Telegram notification delivery
  only.
- Redis, Celery, RabbitMQ, Kafka, or any other external queue
  infrastructure - explicitly out of scope for this change.
- User accounts, a web frontend beyond the existing MVP dashboard, mobile
  app, cloud deployment, payments.

## 2026-08-15 — Second real marketplace connector: eBay

Phase 2's "prove one real connector works" claim is now proven a second
time, via the eBay **Buy Browse API** - explicitly not the legacy Finding
API, and never scraping. Endpoint, auth, and field mappings were verified
against eBay's own official Browse API references before any code was
written (see the connector and token-manager modules' docstrings for exact
sources); the developer had also already manually confirmed, outside this
codebase, that the same OAuth token request and search request both return
real HTTP 200 responses against eBay's production API. No live eBay search
was run as part of this change itself - only mocked-HTTP tests plus a
dashboard-only smoke check confirming the connector reports itself
configured.

### Added
- `EbayMarketplaceConnector`
  (`marketplace_alert/connectors/ebay/connector.py`): calls
  `GET https://api.ebay.com/buy/browse/v1/item_summary/search`. Supports
  `q` (keyword), a safe configurable page size (`limit`, `ebay_result_limit`
  setting, default 25, eBay's own max 200), and `offset` for pagination
  (structured for future multi-page looping, not looped yet - same pattern
  as Etsy). Maps `itemId` → `external_listing_id`, `title`,
  `shortDescription` → `description`, `price.value`/`price.currency`
  (eBay's `value` is a decimal string, e.g. `"89.99"`, unlike Etsy's scaled
  integer `money` object), `itemWebUrl` → `listing_url`, `image.imageUrl`
  → `image_url` (primary image only), `itemLocation` → `location`,
  `seller.username` → `seller`, `condition` → `condition`,
  `itemCreationDate` → `created_at`. Any field eBay doesn't return is left
  `null`, never invented. Handles eBay's "0 results" shape correctly (the
  `itemSummaries` key is omitted entirely, not an empty array) as distinct
  from a genuinely malformed response.
- `EbayTokenManager` (`marketplace_alert/connectors/ebay/token_manager.py`):
  OAuth 2.0 **client_credentials** grant (an Application Access Token, no
  user login) - `POST https://api.ebay.com/identity/v1/oauth2/token` with
  `EBAY_APP_ID` as `client_id` and `EBAY_CERT_ID` as `client_secret`
  (`EBAY_DEV_ID` is never read - the application-token flow doesn't need
  it). The token is cached in memory and reused across every search -
  refreshed only when missing or within 60 seconds of its own `expires_in`
  (tracked via `time.monotonic()`), never fetched fresh per search.
  `invalidate()` drops the cached token if a search itself comes back
  401/403, in case it was revoked early. The token value is never logged
  (not even at DEBUG), never persisted, and never exposed through any API
  response.
- `MarketplaceConnectorError` raised (same type Etsy already uses) for:
  missing/partial credentials (checked before any network call, including
  the token request), OAuth token request failures and non-200 responses,
  401/403 (which also invalidate the cached token) and 429 on the search
  request itself, timeouts, and non-JSON or malformed bodies - always after
  logging a sanitized message, never the raw exception, response, or
  credentials. A single malformed listing inside an otherwise-valid
  response is skipped and logged, not fatal to the whole search.
- `"ebay"` registered in the connector registry
  (`marketplace_alert/connectors/registry.py`) - one line, same factory-dict
  pattern as Etsy, wiring `settings.ebay_app_id`/`settings.ebay_cert_id`/
  `settings.ebay_result_limit` in. `is_marketplace_supported("ebay")` is now
  `True`, so saved searches can use `"ebay"` alone or alongside `"etsy"`/
  `"mock"` with no other changes.
- `settings.ebay_app_id` / `settings.ebay_cert_id` (both optional - missing
  either disables the connector with a clear error when it's actually used,
  not a startup crash) and `settings.ebay_result_limit` (default 25) in
  `marketplace_alert/config.py`. `.env.example` updated with placeholder
  values only (`EBAY_APP_ID`, `EBAY_DEV_ID`, `EBAY_CERT_ID`).
- Dashboard: a new "eBay" status row (`marketplace_alert/templates/dashboard.html`),
  reading `EbayMarketplaceConnector.is_configured` exactly like the
  existing Etsy row - never a credential value. The marketplace checkbox
  list and the supported-marketplace count already pick up `"ebay"`
  automatically through the existing registry-based mechanism
  (`list_supported_marketplaces()`) - no template changes were needed for
  those.
- Tests (180/180 passing, up from 137): new `tests/test_ebay_token_manager.py`
  (configuration checks, request construction, caching, refresh near
  expiry, `invalidate()`, transport failure, non-200/malformed/missing-field
  responses, credentials never appear in a raised error message) and
  `tests/test_ebay_connector.py` (request construction, token reuse across
  searches, full and partial field normalization, missing/partial
  credentials raising before any network call, empty/multi/malformed
  results, a single malformed listing skipped, 401/403/429/500/timeout,
  OAuth failure propagation, `health_check`); updates to
  `tests/test_connector_registry.py` (eBay now resolves and is reported
  supported - the old "ebay is unsupported" assertions were flipped, not
  just added to), `tests/test_saved_searches_api.py` (the "unsupported
  marketplace" example switched from `"ebay"` to `"vinted"` now that eBay
  is real; a new create-with-Etsy-and-eBay case), and
  `tests/test_saved_search_scheduler.py` (the scheduler surviving a *real*
  `EbayMarketplaceConnector`'s HTTP failure; Etsy still running if eBay
  fails on the same saved search and vice versa; an eBay-sourced listing
  not notified twice across two runs; a same-titled "Makita" listing on
  Etsy and eBay staying two independent, both-new listings - duplicate
  detection never conflates marketplaces). All eBay tests monkeypatch
  `httpx.get`/`httpx.post` - no test makes a real eBay API or OAuth call,
  even though the developer's real, working production `.env` credentials
  are present and the connector reports itself configured.
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new "The
  eBay connector" section with the verified endpoint/auth/field-mapping
  details, updated registry/dashboard/layout sections), `ROADMAP.md`
  (Phase 2's connector proof now shown twice; Phase 5 now "three
  connectors", not two).

### Not yet implemented
- Additional real connectors beyond mock/Etsy/eBay (Vinted, Yad2, Facebook
  Marketplace, Mercari, etc.).
- eBay pagination beyond the first page - same structure/reasoning as
  Etsy's existing single-page limitation.
- A live eBay search has not been run as part of this change - deliberately,
  per scope for this task (mocked-HTTP tests plus a dashboard smoke check
  only).
- User accounts, a web frontend beyond the existing MVP dashboard, mobile
  app, cloud deployment, payments.

## 2026-08-15 — Multi-marketplace saved searches

The system was verified working end-to-end with real Etsy listings before
this change. One saved search can now target several marketplaces at once
(Phase 5's core claim), and existing real saved searches (Pokemon->mock,
Maccabi->etsy, Makita->etsy) were migrated safely - verified against a real
backup of the developer's database, not just the test suite. Two real bugs
were caught this way that a purely synthetic test fixture had missed; see
"Fixed" below.

### Changed
- **`SavedSearch.marketplace` (a single string) is now
  `SavedSearch.marketplaces` (`list[str]`)**, backed by a new
  `SavedSearchMarketplace` join table
  (`marketplace_alert/core/saved_searches/models.py`) - never a
  comma-separated string. `SavedSearchCreate`/`Update`/`Read` all changed
  from `marketplace: str` to `marketplaces: list[str]` accordingly
  (`marketplace_alert/core/saved_searches/schemas.py`).
- `SavedSearchRunResponse` now carries a `results: list[MarketplaceRunResult]`
  breakdown (`marketplace`, `new_count`, `already_seen_count`, `error`) in
  addition to the existing aggregate `new_count`/`already_seen_count`
  totals, so `POST /saved-searches/{id}/run` reports each marketplace's
  outcome individually - e.g. `etsy: 2 new, 25 already seen` /
  `mock: 0 new, 1 already seen` in one response.
- `SavedSearchRunner.run()` now loops over every marketplace on the saved
  search, independently. **Resilience is now split across two boundaries**:
  the runner catches a failure in one marketplace so it can't stop the
  other marketplaces in the *same* search (new); `BackgroundScanner` still
  catches a failure in one saved search so it can't stop *other* saved
  searches (unchanged). Neither replaces the other.
- Dashboard create form: the single marketplace dropdown is now a group of
  checkboxes (one per `list_supported_marketplaces()` entry, still the
  single source of truth) plus a "Select all" convenience checkbox
  (`marketplace_alert/templates/dashboard.html`,
  `marketplace_alert/static/dashboard.js`). The saved-searches table shows
  every selected marketplace per row (e.g. "Etsy, Mock").

### Added
- `SavedSearchMarketplace` model (`saved_search_marketplaces` table): `id`,
  `saved_search_id` (FK, cascades on delete), `marketplace_name`, unique on
  the pair - the actual duplicate-marketplace-per-search guarantee, at the
  database level, not just application logic (same pattern as the
  `discovered_listings` dedup constraint). `SavedSearch.marketplaces` is a
  read-only `list[str]` property over the relationship.
- Validation: `marketplaces` must be non-empty and duplicate-free (schema
  level), and every entry must have a registered connector (service level,
  via the existing injected `is_marketplace_supported` predicate - `core/`
  still never imports the registry directly).
- `marketplace_alert/core/saved_searches/migration.py`
  (`migrate_legacy_marketplace_column`): a one-time, idempotent, hand-
  written migration (this project has no Alembic). Runs once at startup,
  right after `init_db()`. If `saved_searches` still has the old single
  `marketplace` column, copies each row's value into
  `saved_search_marketplaces` (skipping already-linked rows, so reruns are
  safe) and drops the legacy column - a no-op once already applied, or on
  a database that never had the old column. Runs inside one
  `engine.begin()` transaction, so a failure rolls back cleanly.

### Fixed
- **Collection-replace ordering in `SavedSearchRepository.update()`.**
  Reassigning `saved_search.marketplace_links` to a brand-new list in one
  step let SQLAlchemy interleave the DELETE/INSERT statements, which
  tripped the `(saved_search_id, marketplace_name)` unique constraint
  whenever a marketplace was unchanged across an edit (e.g. `["mock"]` ->
  `["etsy", "mock"]` - re-adding "mock" raced the deletion of the old
  "mock" row). Fixed by clearing the collection and flushing *before*
  extending it with the new list. Caught by
  `tests/test_saved_searches_api.py::test_edit_saved_search_replaces_marketplace_selection`.
- **The legacy-column migration's dangling index.** The original single-
  marketplace model had `index=True` on that column. SQLite's
  `ALTER TABLE ... DROP COLUMN` doesn't clean up indexes referencing the
  dropped column, so the migration failed the first time it ran against a
  real copy of the developer's database
  (`error in index ix_saved_searches_marketplace after drop column`) - a
  hand-built test fixture that omitted that index had passed, which is
  exactly why it hadn't caught the bug. Fixed by having the migration look
  up and drop any index touching the column before the `ALTER TABLE`, and
  by rebuilding the test fixture to match the real schema exactly,
  including the index (`tests/test_saved_search_migration.py`). The failed
  attempt rolled back cleanly (one transaction) - real data was never at
  risk, but a real backup (`marketplace_alert.db.backup-before-multi-marketplace`)
  was taken before ever running the new code against the real database,
  and the fix was re-verified against that same real database afterward.
- Tests (137/137 passing, up from 118): every existing saved-search,
  scheduler, and dashboard test updated for the `marketplaces` list shape;
  new cases across `tests/test_saved_searches_api.py` (create with one and
  with multiple marketplaces, reject zero/duplicate/unsupported
  marketplaces on create *and* edit, retrieve/edit/remove marketplace
  selections, per-marketplace run breakdown), `tests/test_saved_search_scheduler.py`
  (the scheduler searching every marketplace on one saved search, one
  failing marketplace not stopping another *in the same search* - distinct
  from the existing one-saved-search-not-stopping-another test), new
  `tests/test_saved_search_migration.py` (migrates real-shaped legacy data,
  idempotent on rerun, no-op on a fresh database or a table-less one), and
  `tests/test_dashboard.py` (checkboxes match the registry, a "select all"
  checkbox exists, the saved-searches table shows every selected
  marketplace). All still use the isolated-DB `client` fixture; nothing
  touches the developer's real database or sends a real Telegram message.
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (rewritten
  "Saved searches and background scanning" section covering the join table,
  the runner's two-tier resilience, and the migration; updated dashboard
  section for checkboxes), `ROADMAP.md` (Phase 5's core claim marked proven).

### Not yet implemented
- Editing marketplaces from the dashboard UI - `PATCH` fully supports it
  and is tested, but the dashboard only exposes marketplace checkboxes on
  the *create* form, not an "edit an existing search" control.
- Additional real connectors beyond mock/Etsy (eBay still blocked on API
  approval) - "multiple marketplaces" so far means "up to two".
- User accounts, a web frontend beyond the existing MVP dashboard, mobile
  app, cloud deployment, payments.

## 2026-08-15 — Local web management dashboard

Etsy is now verified working end-to-end in real use (real searches, real
scheduled scans, real Telegram alerts on genuinely new listings). This
change adds the first browser UI so a non-technical user can manage saved
searches without `curl`, Swagger, or editing code - a local, unauthenticated
MVP dashboard, per scope (no auth, no mobile app, no backend redesign).

### Changed
- **`GET /` now serves the dashboard, not the old JSON service-info blob.**
  `{"name": ..., "version": ..., "environment": ...}` is no longer
  returned from `/` - that endpoint wasn't in this task's "must keep
  working" list (`/health`, `/search`, `/scan`, `/saved-searches*`,
  `/docs` all are, and all are unchanged). `tests/test_main.py`'s
  `test_root_endpoint` was updated (not removed) to assert the new,
  intentional behavior - it now checks for the dashboard's HTML instead of
  the old JSON fields, using the isolated-DB `client` fixture instead of
  the module-level raw `TestClient`, since `/` now reads from the database.

### Added
- Dashboard template and assets: `marketplace_alert/templates/dashboard.html`
  (Jinja2, autoescaped), `marketplace_alert/static/dashboard.css`
  (hand-written, mobile-first, no framework), `marketplace_alert/static/dashboard.js`
  (vanilla JS, no dependencies) - mounted at `/static` via `StaticFiles`.
  Three sections, as specified: create a saved search (keyword, marketplace
  dropdown, scan-interval dropdown with presets from 1 minute to 1 hour,
  active checkbox), the saved-searches table (query, marketplace,
  active/inactive, interval, last-scanned time, ID, and Run Now/Enable-
  Disable/Delete actions - Delete asks for confirmation first), and a
  system-status section (backend running; Telegram/Etsy configured as
  booleans only; count of active saved searches; count of supported
  marketplaces).
- **No duplicated saved-search logic**: the initial page read
  (`SavedSearchService.list_all()`, the same service the JSON API uses)
  and every dashboard *action* (create/run/enable-disable/delete) is the
  page's JS calling the pre-existing `/saved-searches*` endpoints - there
  is exactly one implementation of each operation.
- `list_supported_marketplaces()`
  (`marketplace_alert/connectors/registry.py`) - the marketplace
  dropdown's single source of truth, alongside the existing
  `is_marketplace_supported`, so the dropdown can never list a marketplace
  the backend would then reject.
- `NotificationService.is_enabled`
  (`marketplace_alert/core/notifications/service.py`) - a thin pass-
  through to the provider's own `is_enabled`, so the dashboard's "Telegram
  configured?" status doesn't need to reach into the service's private
  state.
- Success/error feedback: a one-shot banner shown after page actions
  ("Search created.", "Scan completed: N new, M already seen.", "Search
  deleted.", or the API's own sanitized error `detail` - e.g. "Saved
  search not found") - passed across the post-action page reload via
  `sessionStorage`, since there's no session/auth layer to hang it off yet.
- Tests (118/118 passing, up from 99): new `tests/test_dashboard.py`
  (all three sections present, empty-state message, saved searches listed,
  marketplace dropdown matches the registry, action buttons carry correct
  IDs, last-scanned display, active-count and marketplace-count badges,
  Telegram/Etsy configured *and* not-configured status states, HTML-escaping
  of a `<script>`-containing query to rule out stored XSS, no credential
  *values* or credential *env-var names* anywhere in the rendered page,
  static CSS/JS actually served, and existing `/saved-searches*` endpoints
  still reachable alongside the dashboard) plus the `test_main.py` update
  above. All dashboard tests use the isolated-DB `client` fixture - none
  touch the developer's real `marketplace_alert.db`.
- New dependency: `jinja2>=3.1` (`pyproject.toml`) - templating only, no
  other new dependency (static files use Starlette's `StaticFiles`,
  already available via FastAPI).
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new
  "The management dashboard" section), `ROADMAP.md` (Phase 7 marked
  partially done - saved-search management exists; a discovered-listings
  view and auth are still open).

### Not yet implemented
- Authentication on the dashboard (or anywhere) - local/unauthenticated by
  design for this MVP; do not expose it beyond localhost as-is.
- A discovered-listings view in the dashboard - it manages saved-search
  definitions only, not the listings they've found.
- Mobile app, cloud deployment, eBay integration, additional marketplaces,
  payments, user accounts.

## 2026-08-14 — First real marketplace connector: Etsy

Phase 2's goal (prove a real connector end-to-end) is met via Etsy, not
eBay - eBay is still blocked on Developer API approval and remains
separate future work. Endpoint and authentication were verified against
Etsy's official generated OpenAPI v3 spec, quickstart tutorial, and
definitions page before any code was written (see the connector module's
docstring for exact sources) - not guessed.

### Added
- `EtsyMarketplaceConnector`
  (`marketplace_alert/connectors/etsy/connector.py`): calls Etsy Open API
  v3's `GET /application/listings/active` (`findAllListingsActive`) - no
  HTML scraping. Auth is an `x-api-key` header of
  `"<ETSY_API_KEY>:<ETSY_SHARED_SECRET>"` (both required - the shared
  secret is part of the header value itself, not just for OAuth); no OAuth
  flow needed for this public, read-only search. Fetches one page per
  search (`etsy_result_limit` setting, default 25, Etsy's own max 100;
  structured with pagination in mind but not looped yet). Normalizes
  Etsy's `money` object (`amount`/`divisor`/`currency_code`) into a plain
  price - `amount / divisor`, not the raw integer. Maps `listing_id`,
  `title`, `description`, `url`, price/currency, `images[0].url_570xN`
  (or `url_fullxfull`), and `original_creation_timestamp` into `Listing`;
  leaves `location`/`seller`/`condition` `null` rather than guessing at
  unverified fields.
- `MarketplaceConnectorError`
  (`marketplace_alert/core/connectors/base.py`, alongside
  `MarketplaceConnector` - the connector-level equivalent of
  `NotificationError`). Raised for missing credentials, network errors,
  timeouts, non-200 responses (including 429), and malformed/missing-
  `results` bodies - always after logging a sanitized message, never the
  raw exception, response, or credentials. A single malformed listing
  inside an otherwise-valid response is skipped and logged, not fatal to
  the whole search.
- `"etsy"` registered in the connector registry
  (`marketplace_alert/connectors/registry.py`), which required
  generalizing its factory dict's value type from "a bare connector class"
  to "a zero-arg callable", since Etsy needs credentials wired in from
  `settings` that a plain class reference can't supply.
  `is_marketplace_supported("etsy")` is now `True`, so saved searches can
  use `marketplace="etsy"` with no other changes - proving the connector
  interface's "add a marketplace without touching core" claim in practice.
- `settings.etsy_api_key` / `settings.etsy_shared_secret` (both optional -
  missing either disables the connector with a clear error when it's
  actually used, not a startup crash) and `settings.etsy_result_limit`
  (default 25) in `marketplace_alert/config.py`. `.env.example` updated
  with placeholder values only.
- Tests (99/99 passing, up from 81): `tests/test_etsy_connector.py`
  (request construction incl. the `x-api-key` header and `keywords`/
  `min_price`/`max_price` params, full and partial field normalization,
  missing/partial credentials raising before any network call, empty and
  multi-result responses, a malformed response as a whole and a single
  malformed listing within an otherwise-good one, non-200 status including
  429, non-JSON body, timeout, `health_check`), plus new cases in
  `tests/test_saved_search_scheduler.py` (the background scanner surviving
  a *real* `EtsyMarketplaceConnector`'s HTTP failure without stopping a
  healthy saved search, and an Etsy-sourced listing not being notified
  twice across two runs) and updates to `tests/test_connector_registry.py`
  (etsy now resolves and is reported supported). All Etsy tests
  monkeypatch `httpx.get` - no test makes a real Etsy API call, even
  though the developer's real `.env` credentials are present and the
  connector reports itself configured.
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new
  "The Etsy connector" section with the verified endpoint/auth/field-
  mapping details), `ROADMAP.md` (Phase 2 marked done via Etsy; Phase 3/4's
  "prove it works against a real connector" caveats resolved).

### Not yet implemented
- The real eBay connector (blocked on API approval) or any other
  marketplace (Vinted, Yad2, etc.).
- Etsy pagination beyond the first page.
- Etsy `location`/`seller` (would need a verified `includes=Shop` field
  mapping) - `condition` has no real Etsy equivalent at all.
- An actual live Etsy search has not been run as part of this change (only
  mocked-HTTP tests) - deliberately, per scope for this task.
- Multiple marketplaces searched from a single saved search, user
  accounts, a web frontend, mobile app, cloud deployment, payments.

## 2026-08-14 — Saved searches and automatic background scanning

eBay Developer API approval is still pending, so this was built and tested
entirely against `MockMarketplaceConnector`. The system can now remember
search definitions and scan them itself, on a schedule, instead of relying
on someone manually hitting `/scan`.

### Security fix
- `httpx` logs every outbound request URL at INFO level by default,
  independent of any of our own logging calls. Since the Telegram Bot API
  URL embeds the bot token (`https://api.telegram.org/bot<TOKEN>/...`),
  this leaked the real token into a local log file during manual testing
  of the new scheduler. Fixed by having `configure_logging()`
  (`marketplace_alert/core/logging_config.py`) explicitly set
  `logging.getLogger("httpx")` to `WARNING`, regardless of the app's own
  configured log level. Added a regression test
  (`tests/test_logging_config.py`) asserting the httpx logger's effective
  level stays at `WARNING` or above after `configure_logging()` runs. The
  leaked log file was deleted; the token itself was not exposed elsewhere
  and did not need to be rotated as a result of this specific incident.

### Added
- Connector registry (`marketplace_alert/connectors/registry.py`):
  `get_connector(name)` / `is_marketplace_supported(name)`. The one place
  outside a connector's own module allowed to import a concrete connector
  class (`MockMarketplaceConnector` today). Everything in `core/` that
  needs to resolve a connector takes an injected resolver function /
  predicate instead of importing the registry, so `core/` stays free of
  concrete-connector imports.
- `SavedSearch` persistence
  (`marketplace_alert/core/saved_searches/`):
  - `models.py` — `SavedSearch` table: `id`, `query`, `marketplace`,
    `is_active`, `scan_interval_seconds`, `created_at`, `updated_at`
    (definition edits only), `last_scanned_at` (scan runs only).
  - `schemas.py` — `SavedSearchCreate`/`Update`/`Read`. Validates `query`
    isn't blank and `scan_interval_seconds >= MIN_SCAN_INTERVAL_SECONDS`
    (60). `SavedSearchRead` reattaches UTC tzinfo on read, since SQLite
    drops it on round-trip even for `DateTime(timezone=True)` columns.
  - `repository.py` — `SavedSearchRepository`: CRUD plus
    `list_due_for_scan()` (active searches never scanned, or overdue by
    their own interval).
  - `service.py` — `SavedSearchService`: validated CRUD for the API routes;
    marketplace support is checked via an injected predicate, not by
    importing the registry.
  - `runner.py` — `SavedSearchRunner`: the *one* implementation of "run
    this saved search" (resolve connector → search → dedup → notify →
    mark scanned), used by both the manual endpoint and the scheduler so
    they can never drift apart.
- Background scanner (`marketplace_alert/core/scheduler/`):
  - `guard.py` — `SavedSearchRunGuard`, a thread-safe overlap guard shared
    between the scanner and the manual `/run` endpoint, so the same saved
    search is never scanned by both (or two overlapping ticks) at once.
  - `scanner.py` — `BackgroundScanner`: one central background thread
    (not one per saved search) that ticks every `scheduler_tick_seconds`
    (new setting, default 5s), runs each due saved search in its own
    session/transaction, and logs-and-continues on a per-search failure
    without stopping the tick, the loop, or any other saved search.
    Started/stopped in `main.py`'s `lifespan`, bound to the real
    `NotificationService` and `SessionLocal` (a background thread has no
    FastAPI request to hang a `Depends` off).
- API: `POST /saved-searches`, `GET /saved-searches`,
  `GET /saved-searches/{id}`, `PATCH /saved-searches/{id}`,
  `DELETE /saved-searches/{id}`, and `POST /saved-searches/{id}/run`
  (manual trigger — 404 if not found, 409 if inactive or already running,
  otherwise runs through the exact same `SavedSearchRunner` as the
  scheduler). `/search` and `/scan` are unchanged.
- Tests (81/81 passing, up from 79): `tests/test_connector_registry.py`,
  `tests/test_saved_searches_api.py` (create/list/get/edit/disable/delete,
  empty-query and interval-minimum and unsupported-marketplace validation,
  manual run and its 404/409 cases, no re-notification on an already-seen
  listing), `tests/test_saved_search_scheduler.py` (due/not-due/inactive
  detection, a full scanner tick notifying only new listings, no
  re-notification on an immediate second tick, one failing saved search
  not stopping another, the run guard's overlap prevention), and
  `tests/test_logging_config.py` (the httpx-logging fix above). Scheduler
  tests construct their own `BackgroundScanner` with fake connectors/
  notification providers and call `run_due_searches()` directly — no real
  thread, timer, or sleep, and (like every other test) an isolated temp
  database, never the developer's real one.
- `settings.scheduler_tick_seconds` (`marketplace_alert/config.py`,
  default 5.0) — the scanner's polling interval, distinct from each saved
  search's own `scan_interval_seconds`.
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new
  "Saved searches and background scanning" section, connector-registry
  rule, updated layout), `ROADMAP.md` (marked done-early against the mock
  connector).

### Not yet implemented
- The real eBay connector (blocked on API approval — do not implement yet)
  — `ebay`/`etsy`/`vinted` are recognized as future marketplace values but
  `is_marketplace_supported` only accepts `"mock"` until each is registered.
- User accounts / per-user saved searches, multiple Telegram recipients,
  a web frontend, mobile app, cloud deployment, payments.

## 2026-08-14 — Telegram notifications for new listings

eBay Developer API approval is still pending, so this was built and tested
entirely against `MockMarketplaceConnector`. Notifications depend only on
the normalized `Listing` model and the `NotificationProvider` interface,
not on any connector.

### Fixed
- The developer's local secrets file was saved as `.evn` instead of `.env`
  (a typo), so `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` were never actually
  loaded by `pydantic-settings`, and the file wasn't covered by
  `.gitignore`'s `.env` rule. Renamed to `.env`; no other changes to its
  contents.

### Added
- `NotificationProvider` interface and `NotificationError`
  (`marketplace_alert/core/notifications/base.py`), mirroring the
  `MarketplaceConnector` pattern: `is_enabled` (property) and
  `send_listing_alert(listing)`.
- `NotificationService.notify_new_listings(listings)`
  (`marketplace_alert/core/notifications/service.py`) — sends one alert per
  listing via whatever provider it's given, skips silently (with a log
  line) when the provider is disabled, and never raises: a provider failure
  on one listing is logged and the rest keep processing.
- `TelegramNotificationProvider`
  (`marketplace_alert/notifications/telegram/provider.py`) — calls the
  Telegram Bot API's `sendMessage` directly via `httpx` (no SDK). Reads
  `bot_token`/`chat_id` from what it's constructed with only, never
  hard-coded. Disabled (logs a warning once) if either is missing. Message
  includes title, marketplace, price + currency (if available), location
  (if available), and the listing URL
  (`format_listing_message()`, independently testable).
  Never logs the bot token, the request URL, or raw exception/response
  objects that could contain it — only exception type, HTTP status code,
  or Telegram's own `description` field.
- `/scan` now calls `NotificationService.notify_new_listings(result.new_listings)`
  after persisting — only listings `ListingDiscoveryService` classified as
  new trigger an alert; already-seen listings never do. A notification
  failure (network error, Telegram API error) never fails the scan itself.
- `settings.telegram_bot_token` / `settings.telegram_chat_id`
  (`marketplace_alert/config.py`), both optional, read from
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`. `.env.example` updated with
  placeholder values only.
- `main.py`: `get_notification_service()` FastAPI dependency wrapping a
  module-level `NotificationService(TelegramNotificationProvider(...))`,
  overridable in tests exactly like `get_db_session`.
- `tests/conftest.py`: `FakeNotificationProvider` (records sent listings,
  always enabled) and a `fake_notification_provider` fixture; the `client`
  fixture now overrides both the database session and the notification
  provider, so no test ever touches the real database or sends a real
  Telegram message — even though a real `.env` may be present locally.
- Tests: `tests/test_notification_service.py` (sends per new listing, no-op
  on empty list, skips silently when disabled, never raises on provider
  failure), `tests/test_telegram_provider.py` (message formatting with/without
  optional fields, missing/partial credentials disable the provider, a
  successful send posts the expected payload, non-200 responses / Telegram
  `"ok": false` / network errors all raise `NotificationError` without
  crashing - all via a monkeypatched `httpx.post`, no real network calls),
  and new `tests/test_scan_endpoint.py` cases (notification sent on first
  discovery, not sent on second/already-seen, `/scan` survives a failing
  provider). Full suite: 47/47 passing.
- New dependency: `httpx>=0.27` moved from dev-only to main dependencies
  (`pyproject.toml`) — it's now used in production code, not just tests.
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new
  "Notifications" section, updated layout and overview), `ROADMAP.md`
  (Phase 4 marked done-early against the mock connector).

### Not yet implemented
- The real eBay connector (blocked on API approval — do not implement yet).
- Notification channels other than Telegram (email, push, webhook).
- Multiple Telegram recipients / per-user chat IDs.
- Background scheduling (recurring scans) — `/scan` is still per-request.
- User accounts, cloud deployment, mobile app, payments.

## 2026-08-14 — Local persistence and duplicate detection

eBay Developer API approval is still pending, so this was built and tested
entirely against `MockMarketplaceConnector`. The persistence layer itself
depends only on the normalized `Listing` model, not on any connector.

### Added
- SQLAlchemy-based local persistence
  (`marketplace_alert/core/persistence/`):
  - `database.py` — engine/session setup from `DATABASE_URL` (SQLite
    locally; PostgreSQL later needs no code change beyond the URL),
    `init_db()`, and a `get_db_session()` FastAPI dependency.
  - `models.py` — `DiscoveredListing` table: `id`, `marketplace`,
    `external_listing_id`, `title`, `listing_url`, `first_discovered_at`,
    `last_seen_at`, with a unique constraint on
    `(marketplace, external_listing_id)` enforced at the database level.
  - `repository.py` — `ListingRepository`, the only module that issues
    SQL/ORM queries (`get`, `save_new`, `touch_last_seen`).
  - `service.py` — `ListingDiscoveryService.process_listings(listings)`,
    classifying each `Listing` as new (persisted) or already-seen
    (`last_seen_at` bumped), returning `ListingDiscoveryResult(new_listings,
    already_seen_count)`. Depends only on `Listing` — never imports any
    connector.
- Temporary `GET /scan?q=...` endpoint: runs `MockMarketplaceConnector`,
  passes results through `ListingDiscoveryService`, saves newly-seen
  listings, and returns only the new ones plus an already-seen count.
  Stateful, unlike `/search` (which is unchanged and still stateless —
  verified by test).
- App startup (`lifespan` in `main.py`) now calls `init_db()` so the local
  SQLite tables exist before the first request.
- `tests/conftest.py`: fixtures (`db_engine`, `db_session`, `client`) that
  build an isolated temp-file SQLite database per test and wire it into
  the app via `app.dependency_overrides` — no test touches the developer's
  real `marketplace_alert.db`.
- Tests: `tests/test_persistence.py` (DB init, first discovery is new,
  second is already-seen, same external ID allowed across marketplaces,
  duplicate on the same marketplace rejected at the DB level) and
  `tests/test_scan_endpoint.py` (`/scan` first request returns the new
  listing, second returns zero new / one already-seen, no-match query,
  `/search` stays stateless alongside `/scan`). Full suite: 32/32 passing.
- New dependency: `sqlalchemy>=2.0` (`pyproject.toml`).
- Documentation updates: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md` (new
  "Local persistence and duplicate detection" section, updated layout),
  `ROADMAP.md` (Phase 3 marked done-early against the mock connector).

### Not yet implemented
- The real eBay connector (blocked on API approval — do not implement yet).
- Schema migrations (Alembic) — `Base.metadata.create_all()` is sufficient
  for the current single-table schema.
- Notifications / alerting on newly discovered listings.
- User accounts, scheduling/background jobs, cloud deployment, mobile app.

## 2026-08-14 — Mock marketplace connector

We are waiting on eBay Developer API approval, so a `MockMarketplaceConnector`
was added to unblock development and testing of the rest of the system in
the meantime.

### Added
- `MockMarketplaceConnector`
  (`marketplace_alert/connectors/mock/connector.py`), implementing
  `MarketplaceConnector` against a fixed, in-memory catalog of six fake
  listings (Maccabi vintage shirt, Hapoel scarf, Rolex Submariner, Makita
  drill, Pokemon card, Adidas jacket). Search is case-insensitive,
  substring-based on `title`, supports optional `min_price`/`max_price`/
  `condition` filters, and every listing has a unique `external_listing_id`.
- Temporary `GET /search?q=...` endpoint on the FastAPI app, backed by
  `MockMarketplaceConnector`, returning normalized `Listing` objects as JSON.
- Tests for the mock connector (loading, arbitrary keyword search,
  case-insensitivity, no-result search, normalized output, uniqueness of
  IDs, filters) and for the `/search` endpoint
  (`tests/test_mock_connector.py`, additions to `tests/test_main.py`).
  Full suite: 23/23 passing.
- Documentation updates: `PROJECT_CONTEXT.md` (current status, a new
  "Waiting on" section for the eBay approval blocker) and `ARCHITECTURE.md`
  (mock connector section, updated layout diagram).

### Not yet implemented
- The real eBay connector (blocked on API approval — do not implement yet).
- Scraping of any kind.
- Database / persistence.
- Notifications / alerting.
- Deployment.

## 2026-08-14 — Relocated project root

Moved the project root from `c:\UnityProjects\RussianNinja` (a shared
folder that also contains an unrelated Unity project) to its own dedicated
folder, `C:\Projects\MarketplaceAlert`. No source files changed; the venv
was recreated at the new location and the full test suite was re-verified
(10/10 passing).

## 2026-08-14 — Project scaffold (Phase 1)

### Added
- Initial project structure and Python package layout (`marketplace_alert/`).
- Documentation: `PROJECT_CONTEXT.md`, `ARCHITECTURE.md`, `ROADMAP.md`,
  `README.md`, `.env.example`, `.gitignore`.
- Basic FastAPI application (`marketplace_alert/main.py`) with `/` and
  `/health` endpoints.
- Structured (JSON) logging setup (`marketplace_alert/core/logging_config.py`).
- Settings loaded from environment variables / `.env`
  (`marketplace_alert/config.py`).
- Abstract `MarketplaceConnector` interface
  (`marketplace_alert/core/connectors/base.py`) — no concrete connector yet.
- Normalized `Listing` Pydantic model
  (`marketplace_alert/core/models/listing.py`).
- Tests confirming the app and models load and behave correctly
  (`tests/`).

### Not yet implemented
- Any real marketplace connector, database/persistence, duplicate
  detection, alerting, user accounts, web UI, mobile app, payments, or
  cloud deployment. See `PROJECT_CONTEXT.md` for the full list.
