# Roadmap

Broad milestones. Each phase should leave the system working end-to-end at
a bigger scope than the last — no phase depends on skipping ahead.

## Phase 1 — Local prototype
Project structure, documentation, FastAPI skeleton, connector interface,
normalized `Listing` model, tests, mock connector, local persistence and
duplicate detection, Telegram alerts on new listings, and saved searches
with automatic background scanning (Phases 3 and 4 pulled forward — see
below; saved searches/scheduling wasn't a separately numbered phase but
belongs here for the same reason). **Done.**

## Phase 2 — First marketplace connector
**Done, via Etsy, then proven again via eBay, again via Reverb, and again
via Bonanza.** `EtsyMarketplaceConnector` (`marketplace_alert/connectors/etsy/`)
uses Etsy Open API v3 (no scraping) and proves `search()` →
`normalize_listing()` end-to-end against a live marketplace, exactly as
this phase asked — nothing outside the connector's own module and the
registry's factory dict had to change to add it. `EbayMarketplaceConnector`
(`marketplace_alert/connectors/ebay/`) - the Buy Browse API, OAuth
client_credentials, never the legacy Finding API - was added the same way
afterward, confirming the same claim a second time with zero core changes.
`ReverbMarketplaceConnector` (`marketplace_alert/connectors/reverb/`) -
Reverb API v3, a single personal access token, real API access manually
verified before implementation - confirmed it a third time.
`BonanzaMarketplaceConnector` (`marketplace_alert/connectors/bonanza/`) -
Bonanza's own eBay-Finding-API-shaped API, added after a broad feasibility
pass across nine other named candidates (Craigslist, OfferUp, Gumtree,
Kleinanzeigen, OLX, Vinted, Discogs, Mercado Libre, Facebook Marketplace -
see PROJECT_CONTEXT.md decision #18 for why each was ruled out) found
none of them offered both a self-serve credential and real marketplace-
wide search - confirmed the zero-core-changes claim a fourth time.
Further connectors beyond these four remain separate, not-yet-started
pieces of work, gated on either a new candidate emerging or someone
completing the human step OLX/Mercado Libre each specifically need (a
partner-approval review, and a one-time OAuth consent flow, respectively)
— this phase's goal only ever asked for proof via *a* real connector, not
an exhaustive list.

## Phase 3 — Database and duplicate detection
**Done ahead of schedule**, in Phase 1, against the mock connector: SQLAlchemy
models and persistence (SQLite locally, PostgreSQL later), a
`discovered_listings` table, and a service layer that tells new listings
from already-seen ones. Now that Phase 2 has landed (via Etsy), a saved
search with `marketplace="etsy"` runs through this exact same persistence
layer, unchanged - confirmed by tests (with the Etsy HTTP call mocked; see
`PROJECT_CONTEXT.md` for why a real Etsy search hasn't been run yet).

## Phase 4 — Alerts
**Done ahead of schedule**, in Phase 1, against the mock connector: a
`NotificationProvider` interface and a Telegram implementation, wired into
both `/scan` and the saved-search runner (manual `/run` and the automatic
background scanner) so each newly discovered listing gets one alert and
already-seen listings never do. Now that Phase 2 has landed (via Etsy and
eBay), a real multi-marketplace saved search returning many new listings
at once exposed the gap in this alerting: some Telegram sends failed
outright under a burst, with no pacing or retry. **Now fixed**:
`TelegramNotificationProvider` retries transient failures (429/5xx/timeout)
with bounded, Retry-After-aware backoff, and `NotificationService` paces
sends between listings - both configurable, both provider-scoped, no
external queue infrastructure added. Duplicate detection is unaffected -
a listing is persisted as discovered before notification is attempted, so
a Telegram failure never makes it look new again later. Other channels
(email, push, webhook) and multi-recipient support are still open.

### Relevance filtering (not originally its own phase)
Also folded into this phase, since it's about alert *precision* rather
than delivery: real mobile testing showed a "Makita drill" saved search
alerting on DeWalt/Milwaukee battery holders and generic tool organizers
- each a legitimate keyword hit from the connector, none of them what
anyone searching "Makita drill" wants. `marketplace_alert/core/relevance/`
adds a deterministic, rule-based scoring layer (brand-conflict rejection,
a configurable product-family synonym table, an accessory penalty that
respects a query explicitly asking for an accessory) between every
connector and persistence/notification - no LLM/embeddings, fully
explainable, wired into the scheduler and both Run Now endpoints via the
same `SavedSearchRunner`, plus the legacy `/scan` route. See
ARCHITECTURE.md "Relevance filtering" for the full design and
CHANGELOG.md's 2026-08-22 entry. Still open: the product-family/accessory
vocabulary only covers power tools today (see PROJECT_CONTEXT.md "Things
that have NOT yet been implemented").

Relevance filtering only ever applied going forward, so pre-existing
`discovered_listings` rows (persisted before it existed) kept showing old
irrelevant results on the mobile Listings screen even after new scans
were correctly filtered. `core/persistence/cleanup.py` +
`scripts/cleanup_historical_listings.py` re-evaluate those historical
rows against the current engine and remove the ones it would now reject,
using every current saved search targeting a row's marketplace as the
best available proxy for "does this row have a reason to still exist"
(the schema has no direct row-to-saved-search link - see
PROJECT_CONTEXT.md decision #14/#16). Re-running this against real local
data also caught and fixed a genuine relevance-engine bug (a bare
brand-only saved search, e.g. just "Makita", was matching almost any
listing that didn't mention a *different* brand) - see ARCHITECTURE.md
"Historical listing cleanup" and CHANGELOG.md's 2026-08-22 entry. Manual
and explicit only (`--apply` flag required to actually delete) - not
wired into the scheduler or app startup.

### Saved searches and background scanning (not originally its own phase)
Also done ahead of schedule, in Phase 1: a persistent `SavedSearch` model,
a CRUD API (`/saved-searches`), a connector registry
(`get_connector`/`is_marketplace_supported` — `"mock"`, `"etsy"`, and
`"ebay"` registered), and a `BackgroundScanner` that runs due saved
searches on one central background thread, reusing the same run logic
(`SavedSearchRunner`) as the manual `POST /saved-searches/{id}/run`
endpoint. A saved search can target `"etsy"`, `"ebay"`, `"mock"`, or any
combination at once and run automatically like any other (Phase 5's
multi-marketplace work, also pulled forward - see below).
A local web dashboard (`GET /`) now covers creating/managing these from a
browser too. Still open: user-scoped saved searches (Phase 6), and a
dashboard view of discovered listings themselves, not just saved-search
definitions (Phase 7).

## Phase 5 — Multiple marketplaces
**Core claim proven, ahead of schedule, now with five connectors.** One
`SavedSearch` can now target several marketplaces at once (a normalized
`SavedSearchMarketplace` join table, not a comma-separated string), each
scanned independently by `SavedSearchRunner` with its own failure isolation
- a connector failing for one marketplace never stops the others in the
same search (verified concretely: an eBay failure doesn't stop an Etsy
search on the same saved search and vice versa; a Reverb failure doesn't
stop an Etsy or eBay search; a Bonanza failure doesn't stop a Reverb
search on the same saved search either). Adding eBay as the third option
required zero changes to any other connector, to duplicate detection, or
to the notification layer; adding Reverb as the fourth proved the same
claim a third time (see "The Reverb connector" in ARCHITECTURE.md and
CHANGELOG.md's 2026-08-22 entry) - real API access was manually verified
first (a real personal access token, `public` scope, against
`GET https://api.reverb.com/api/listings`); adding Bonanza as the fifth
proved it a fourth time, this time after a broad feasibility pass across
nine other named candidates found none of them offered both a self-serve
credential and real marketplace-wide search (see PROJECT_CONTEXT.md
decision #18) - Bonanza's own API, deliberately modeled on eBay's
(deprecated) Finding API, flows through the exact same saved-search/
scheduler/relevance/duplicate-detection/notification/mobile-API/dashboard
pipeline every other connector already uses, though (unlike eBay/Etsy/
Reverb) it hasn't yet been live-validated against production - no real
`BONANZA_DEV_NAME` was available while building it. What's still open:
further connectors beyond mock/Etsy/eBay/Reverb/Bonanza - see Phase 2 for
the nine candidates already evaluated and ruled out, and what would need
to change (an external approval, or a new persistent-refresh-token
mechanism) to make OLX or Mercado Libre buildable.

## Phase 6 — User accounts
Multi-user support: accounts, auth, and searches scoped to a user.

## Phase 7 — Web interface
A UI for creating/managing searches and viewing results, on top of the
existing API. **Done, ahead of schedule:** a local, unauthenticated
`GET /` dashboard (`marketplace_alert/templates/dashboard.html` +
`marketplace_alert/static/`) covers creating/listing/running/enabling-
disabling/deleting saved searches, reusing the existing `/saved-searches*`
endpoints for every action (no duplicated logic). `GET /listings`
(added 2026-08-22, the Listings product-experience pass) covers the rest
of this phase's stated scope - viewing the *discovered listings*
themselves, with filtering (marketplace, saved search, price range) and
sorting - server-rendered, no JavaScript needed, reusing the exact same
`ListingRepository` `GET /api/v1/listings` uses. Still open: authentication
before this could ever be exposed beyond localhost (see Phase 6).

## Phase 8 — Cloud deployment
Move off local-only development; real hosting, real database, secrets
management. **Done, ahead of schedule:** the system is live on Render
(real hosting, GitHub-triggered deploys, secrets via Render's env vars -
not committed) on the Free plan, with `DATABASE_URL` set - production
runs on Render's managed PostgreSQL (`marketplacealert-db`), not local
SQLite. Schema changes reach it automatically at app startup
(`core/persistence/migrations.py`, added 2026-08-22 specifically because
Render Free has no Pre-Deploy Command - see ARCHITECTURE.md "Automatic
migrations on Render Free" and PROJECT_CONTEXT.md decision #20), fail-fast
and lock-guarded so a bad migration or a deploy-transition race can't
silently leave the database in a broken or mismatched state. Still open:
whether any pre-cutover local SQLite data was ever carried over to
PostgreSQL was not part of this work and isn't tracked here - production's
data has been accumulating in PostgreSQL independently since the cutover.

## Phase 8.5 — (informal) Local SQLite -> production PostgreSQL cutover
**Done.** The already-live Render Web Service's `DATABASE_URL` is
connected to `marketplacealert-db`, and the baseline (plus every
subsequent) Alembic migration reaches it automatically at startup - see
Phase 8. Superseded the original plan of a manual Pre-Deploy Command
step, since Render's Free plan doesn't offer one.

## Phase 9 — Android/iOS application
Mobile clients on top of the existing API. **Done, ahead of schedule:** a
real React Native/Expo/TypeScript app (`mobile/`, see `mobile/README.md`)
runs on Android and iOS via Expo Go, backed entirely by the versioned
`/api/v1` Mobile API - status, marketplace metadata (registry-driven, so
Reverb/Bonanza appear automatically), saved-search CRUD + manual run,
and a Listings screen with real filtering/sorting/pagination
(marketplace multi-select, saved-search selector, price range, sort -
added 2026-08-22, the Listings product-experience pass). Home, Saved
Searches, Create Search, Saved Search Detail, and Listings screens all
exist, with automatic background refresh (interval polling + app-resume
+ screen-focus, see `mobile/README.md` "Automatic refresh"), graceful
loading/empty/error states everywhere, and safe external-link opening
(`utils/linking.ts` - never traps the user in-app). CORS is prepared
(off by default) and the endpoint structure is ready for auth to be
added later, but **no authentication exists yet** - every `/api/v1`
endpoint is exactly as open as the legacy routes, and this app has no
login/per-user data. No App Store/Play Store release yet either - this
is a first version run through Expo Go/a dev build, not a store
submission.

## Phase 10 — Subscriptions/payments
Paid tiers, billing.

## Phase 11 — Scaling and monitoring
Performance under load, observability, alerting on the system itself
(not just marketplace listings). **A first, partial step taken ahead of
schedule (2026-08-22):** a production-hardening audit added bounded
retry-with-backoff for transient connector failures (429/502/503/504,
`core/connectors/retry.py`) so a rate-limited or momentarily-unavailable
marketplace is less likely to fail an entire scan outright, and added a
missing database index (`discovered_listings.first_discovered_at`) that
was becoming a full-table sort on every mobile Listings page load as the
table grew. Full write-up: CHANGELOG.md's 2026-08-22 "Production
hardening and release-readiness pass" entry and PROJECT_CONTEXT.md
decision #19. Still open: real observability (metrics, dashboards,
alerting on the system itself), load testing, and anything beyond these
two targeted fixes.
