# Marketplace Alert

A scalable marketplace monitoring and alert platform. Create keyword
searches (e.g. "Rolex Submariner", "Maccabi", "vintage Adidas"), pick which
marketplaces to search, and get alerted when a new matching listing
appears — with a link straight to the original listing.

> **Status:** live in production at
> [marketplacealert.onrender.com](https://marketplacealert.onrender.com).
> Real connectors for eBay, Etsy, and Reverb; Bonanza is implemented and
> registered but disabled pending a free developer credential (see
> `PROJECT_CONTEXT.md`). A React Native/Expo mobile app
> ([`mobile/`](mobile/README.md)) and a server-rendered web dashboard
> (`GET /`) both run against the same backend. See `PROJECT_CONTEXT.md`
> for exactly what exists today and `ROADMAP.md` for what's next.

## Documentation

- [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) — what this project is, current
  status, key decisions. **Read this first.**
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the connector-based architecture.
- [`ROADMAP.md`](ROADMAP.md) — planned phases.
- [`CHANGELOG.md`](CHANGELOG.md) — history of meaningful changes.

## Requirements

- Python 3.12+

## Setup

```bash
# From the project root:
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env   # Windows: copy .env.example .env
```

## Running the app

```bash
uvicorn marketplace_alert.main:app --reload
```

Then visit:
- http://127.0.0.1:8000/ — the management dashboard
- http://127.0.0.1:8000/health — health check
- http://127.0.0.1:8000/docs — interactive API docs (Swagger UI, includes both the legacy and `/api/v1` routes)

## Mobile API

**Base path: `/api/v1`** — a versioned, JSON-only REST API for a future
Android/iOS app, added alongside (not replacing) the existing dashboard
and legacy routes. It depends on no HTML/dashboard behavior and never
exposes secrets. See `PROJECT_CONTEXT.md`/`ARCHITECTURE.md` "Mobile API"
for the full design (including the known limitations of `GET
/api/v1/listings` - several fields are not persisted yet, and there's no
`saved_search_id` relationship to filter on).

| Method | Path | What |
|---|---|---|
| GET | `/api/v1/status` | Mobile-safe backend/database/Telegram status |
| GET | `/api/v1/marketplaces` | Marketplace metadata, from the connector registry |
| GET | `/api/v1/saved-searches` | List saved searches |
| POST | `/api/v1/saved-searches` | Create a saved search |
| GET | `/api/v1/saved-searches/{id}` | Get one saved search |
| PATCH | `/api/v1/saved-searches/{id}` | Update a saved search |
| DELETE | `/api/v1/saved-searches/{id}` | Delete a saved search |
| POST | `/api/v1/saved-searches/{id}/run` | Run a saved search now |
| GET | `/api/v1/listings` | Browse recently discovered listings (paginated) |

No mobile app (React Native, Expo, Flutter, native Android/iOS) or
authentication is built yet - this is API-side groundwork only.

## Database

Local development uses SQLite by default - no setup required, and nothing
below needs to be done to just run the app locally.

- **`DATABASE_URL` unset** (the normal local case): SQLite,
  `sqlite:///./marketplace_alert.db`, auto-created on startup.
- **`DATABASE_URL` set** (production): PostgreSQL. Render's URL, in either
  the `postgres://` or `postgresql://` form, is normalized automatically -
  see `marketplace_alert/core/persistence/database.py`.

### Migrations (Alembic)

SQLite (local dev/tests) keeps auto-creating any missing tables on startup,
same as before - safe, since that only ever adds tables, never drops or
alters one. **PostgreSQL does not** - its schema is managed by
[Alembic](https://alembic.sqlalchemy.org/) migrations instead.

**In production, this now happens automatically at app startup** -
`run_pending_migrations()` (`core/persistence/migrations.py`) runs
`alembic upgrade head` itself, before the app begins serving requests,
whenever it detects a PostgreSQL `DATABASE_URL` (a no-op for SQLite).
This exists because Render's Free plan - what this project runs on - has
no Pre-Deploy Command to run migrations as a separate step; see
PROJECT_CONTEXT.md decision #20 and ARCHITECTURE.md "Automatic
migrations on Render Free" for the full mechanism (fail-fast on error,
a bounded-wait advisory lock, idempotent, never a downgrade). **This
also applies if you point your own local `.env`'s `DATABASE_URL` at a
real PostgreSQL server** - starting the app (`uvicorn
marketplace_alert.main:app`) will run pending migrations against it
automatically too, the same way production does; only a SQLite
`DATABASE_URL` (or none at all) skips this.

You can still run migrations manually any time (against whatever
`DATABASE_URL` resolves to - unset means the local SQLite file), e.g. to
check what a new migration would do before starting the app, or to apply
one without starting the app at all:

```bash
alembic upgrade head
```

Adopting Alembic against a database that **already has the current schema**
(e.g. an existing local SQLite file created before Alembic existed) needs
`stamp`, not `upgrade` - `upgrade head` would fail with "table already
exists" rather than do anything destructive:

```bash
alembic stamp head
```

Creating a new migration after changing a model:

```bash
alembic revision --autogenerate -m "describe the change"
```

See `PROJECT_CONTEXT.md`/`ARCHITECTURE.md` for the full reasoning, and
`alembic/versions/` for the baseline migration.

## Running tests

```bash
pytest
```

## Adding a new marketplace connector

Every real connector so far (Etsy, eBay, Reverb, Bonanza) was added the
same way, with **zero changes outside its own module and one line in the
registry** - see `ARCHITECTURE.md`'s "Why these choices" for why that's
the actual test of whether this works, not just a claim. To add one:

1. Create `marketplace_alert/connectors/<name>/connector.py`, a subclass
   of `MarketplaceConnector` (`core/connectors/base.py`) implementing
   `search()`/`normalize_listing()`/`health_check()`. Wrap your outbound
   HTTP call in `request_with_retry()`
   (`core/connectors/retry.py`, see `ARCHITECTURE.md` "Shared connector
   retry helper") so transient 429/502/503/504 responses get bounded,
   backed-off retries for free - your own code only needs to handle the
   final response, same as every existing connector does.
2. Map every field you can confidently identify onto `Listing`
   (`core/models/listing.py`) - leave anything you can't confirm as
   `null`, never a guess (see any existing connector's "Field mapping"
   section in `ARCHITECTURE.md` for the pattern).
3. Add `settings.<name>_...` fields for any credentials/limits to
   `config.py`, and matching placeholder entries (no real values) to
   `.env.example`. A missing credential must make `is_configured` False
   and `search()` raise a clear `MarketplaceConnectorError` **before**
   any network call - it must never crash app startup or affect any
   other connector.
4. Register it: one entry in `_CONNECTOR_FACTORIES`
   (`connectors/registry.py`), plus a display name in
   `display_name_for()`. This alone makes it selectable in saved
   searches, appear in the dashboard's checkboxes/status panel, and show
   up in `GET /api/v1/marketplaces` (and therefore the mobile app's
   marketplace picker) - none of those places should need any other
   change.
5. Write mocked-HTTP tests mirroring an existing connector's test file
   (request construction, field mapping including missing-field cases,
   pagination if applicable, every failure mode: missing credentials,
   401/403/429/5xx, timeout, malformed/empty responses, one malformed
   listing not failing the whole search) - see any of
   `tests/test_etsy_connector.py`/`test_ebay_connector.py`/
   `test_reverb_connector.py`/`test_bonanza_connector.py` as a template.
6. If the new connector introduces a product category the relevance
   engine (`core/relevance/`) doesn't know about yet (a new brand
   vocabulary, a new product-family synonym table), extend
   `brands.py`/`families.py`/`accessories.py` - see `ARCHITECTURE.md`
   "Relevance filtering". An unregistered category still works via the
   lenient token-overlap fallback, just less precisely.

Do not add scraping-based connectors - see PROJECT_CONTEXT.md decision #3
and #18 for why every marketplace evaluated without a real API was ruled
out rather than worked around with scraping.

## Project layout

```
marketplace_alert/
    main.py                    FastAPI app entry point (dashboard + legacy routes)
    dependencies.py            Shared singletons (notification service, run guard, ...)
    config.py                  Settings (env vars / .env)
    api/v1/                    Versioned Mobile API (status, marketplaces, saved-searches, listings)
    templates/, static/        The web dashboard (Jinja2 + vanilla JS)
    core/
        logging_config.py      Structured (JSON) logging setup
        connectors/
            base.py            MarketplaceConnector interface
            retry.py            Shared bounded-retry helper for connector HTTP calls
        models/
            listing.py         Normalized Listing model
        persistence/            SQLite/PostgreSQL, duplicate detection, historical cleanup
        relevance/               Deterministic relevance-scoring engine
        notifications/           NotificationProvider interface + dispatch service
        saved_searches/          Saved search CRUD, run logic
        scheduler/                Background scanning loop + overlap guard
    connectors/
        registry.py              get_connector / is_marketplace_supported / display_name_for
        mock/, etsy/, ebay/, reverb/, bonanza/   One package per marketplace
    notifications/telegram/     TelegramNotificationProvider
alembic/                        PostgreSQL schema migrations
tests/                          Tests, mirroring the package layout
mobile/                         React Native/Expo mobile app - see mobile/README.md
```

See `ARCHITECTURE.md` for the full picture and the reasoning behind it.
