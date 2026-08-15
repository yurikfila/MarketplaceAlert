# Marketplace Alert

A scalable marketplace monitoring and alert platform. Create keyword
searches (e.g. "Rolex Submariner", "Maccabi", "vintage Adidas"), pick which
marketplaces to search, and get alerted when a new matching listing
appears — with a link straight to the original listing.

> **Status:** early prototype (Phase 1 of `ROADMAP.md`). No marketplace
> connector, database, or alerting is implemented yet — see
> `PROJECT_CONTEXT.md` for exactly what exists today.

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
- http://127.0.0.1:8000/ — basic service info
- http://127.0.0.1:8000/health — health check
- http://127.0.0.1:8000/docs — interactive API docs (Swagger UI)

## Running tests

```bash
pytest
```

## Project layout

```
marketplace_alert/
    main.py                    FastAPI app entry point
    config.py                  Settings (env vars / .env)
    core/
        logging_config.py      Structured (JSON) logging setup
        connectors/
            base.py            MarketplaceConnector interface
        models/
            listing.py         Normalized Listing model
tests/                          Tests, mirroring the package layout
```

See `ARCHITECTURE.md` for why it's structured this way.
