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
**Done, via Etsy, then proven again via eBay.** `EtsyMarketplaceConnector`
(`marketplace_alert/connectors/etsy/`) uses Etsy Open API v3 (no scraping)
and proves `search()` → `normalize_listing()` end-to-end against a live
marketplace, exactly as this phase asked — nothing outside the connector's
own module and the registry's factory dict had to change to add it.
`EbayMarketplaceConnector` (`marketplace_alert/connectors/ebay/`) - the
Buy Browse API, OAuth client_credentials, never the legacy Finding API -
was added the same way afterward, confirming the same claim a second time
with zero core changes. Further connectors beyond these two (Vinted, Yad2,
Facebook Marketplace, Mercari, etc.) remain separate, not-yet-started
pieces of work — this phase's goal only asked for proof via *a* real
connector, not an exhaustive list.

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
**Core claim proven, ahead of schedule, now with three connectors.** One
`SavedSearch` can now target several marketplaces at once (a normalized
`SavedSearchMarketplace` join table, not a comma-separated string), each
scanned independently by `SavedSearchRunner` with its own failure isolation
- a connector failing for one marketplace never stops the others in the
same search (verified concretely: an eBay failure doesn't stop an Etsy
search on the same saved search, and vice versa). Adding eBay as the third
option required zero changes to any other connector, to duplicate
detection, or to the notification layer - the actual proof of "add a
marketplace without touching core" this phase asks for, proven a second
time. What's still open: further connectors beyond mock/Etsy/eBay (Vinted,
Yad2, Facebook Marketplace, Mercari, etc.) - see Phase 2.

## Phase 6 — User accounts
Multi-user support: accounts, auth, and searches scoped to a user.

## Phase 7 — Web interface
A UI for creating/managing searches and viewing results, on top of the
existing API. **Partially done, ahead of schedule:** a local, unauthenticated
`GET /` dashboard (`marketplace_alert/templates/dashboard.html` +
`marketplace_alert/static/`) covers creating/listing/running/enabling-
disabling/deleting saved searches, reusing the existing `/saved-searches*`
endpoints for every action (no duplicated logic). Still open, and still
this phase's job: a view of the *discovered listings* themselves (not just
saved-search definitions), and authentication before this could ever be
exposed beyond localhost (see Phase 6).

## Phase 8 — Cloud deployment
Move off local-only development; real hosting, real database, secrets
management. **Partially done, ahead of schedule:** the system is live on
Render (real hosting, GitHub-triggered deploys, secrets via Render's env
vars - not committed). A Render PostgreSQL database (`marketplacealert-db`)
has been created, and the codebase now *supports* PostgreSQL alongside
SQLite - `DATABASE_URL` selects it, normalized centrally for Render's URL
forms and the `psycopg` driver, with Alembic managing its schema instead of
the local-only `create_all()` bootstrap (see `ARCHITECTURE.md` "Database
selection and PostgreSQL support"). **Still open, and still this phase's
job**: the deployed Web Service is not yet connected to `DATABASE_URL`
(still running on its local SQLite file on Render, which is not
persistent-storage-safe long-term); no local data has been copied to
PostgreSQL; no Render configuration was changed and nothing was deployed
as part of adding this support - the actual production cutover is a
separate, later step, not implied or started by this one.

## Phase 8.5 — (informal) Local SQLite -> production PostgreSQL cutover
Not a numbered phase of its own, listed here for continuity: connecting
the already-live Render Web Service's `DATABASE_URL` to the already-created
`marketplacealert-db`, running the baseline Alembic migration against it,
and deciding whether/how to carry over existing local SQLite data. Blocked
on nothing technical - the schema/code support is in place - just not yet
done, and explicitly out of scope for the change that added that support.

## Phase 9 — Android/iOS application
Mobile clients on top of the existing API.

## Phase 10 — Subscriptions/payments
Paid tiers, billing.

## Phase 11 — Scaling and monitoring
Performance under load, observability, alerting on the system itself
(not just marketplace listings).
