"""FastAPI application entry point.

Run locally with:
    uvicorn marketplace_alert.main:app --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from marketplace_alert import __version__
from marketplace_alert.api.v1 import router as api_v1_router
from marketplace_alert.config import settings
from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.connectors.registry import (
    display_name_for,
    get_connector,
    is_marketplace_supported,
    list_supported_marketplaces,
)
# Imported purely for the side effect of registering `users` on
# Base.metadata before init_db()/Base.metadata.create_all() runs below -
# no code in this file uses it directly yet (Phase 1 is schema only), but
# saved_searches.models.SavedSearch.user_id's foreign key can't be
# resolved during local SQLite table creation unless this module has been
# imported first. Same reason alembic/env.py imports every model module
# explicitly.
import marketplace_alert.core.auth.models  # noqa: F401
from marketplace_alert.core.logging_config import configure_logging
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.database import SessionLocal, engine, get_db_session, init_db
from marketplace_alert.core.persistence.migrations import run_pending_migrations
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.persistence.service import ListingDiscoveryService
from marketplace_alert.core.relevance import filter_relevant_listings
from marketplace_alert.core.saved_searches.migration import migrate_legacy_marketplace_column
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.saved_searches.schemas import (
    MIN_SCAN_INTERVAL_SECONDS,
    MarketplaceRunResult,
    SavedSearchCreate,
    SavedSearchRead,
    SavedSearchRunResponse,
    SavedSearchUpdate,
)
from marketplace_alert.core.saved_searches.service import SavedSearchService, UnsupportedMarketplaceError
from marketplace_alert.core.scheduler.scanner import BackgroundScanner
from marketplace_alert.dependencies import get_notification_service, get_saved_search_service
from marketplace_alert.dependencies import saved_search_run_guard as _saved_search_run_guard
from marketplace_alert.dependencies import saved_search_runner as _saved_search_runner

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

# (seconds, human label) - the dashboard's scan-interval dropdown. All values
# already respect MIN_SCAN_INTERVAL_SECONDS; a saved search created outside
# the dashboard (curl, /docs) can still use any interval >= that minimum,
# which _format_interval() below renders sensibly even if it's not one of
# these presets.
SCAN_INTERVAL_OPTIONS: list[tuple[int, str]] = [
    (60, "1 minute"),
    (300, "5 minutes"),
    (900, "15 minutes"),
    (1800, "30 minutes"),
    (3600, "1 hour"),
]


def _format_interval(seconds: int) -> str:
    """Human-friendly label for a scan interval, for any value - not just the presets."""
    for value, label in SCAN_INTERVAL_OPTIONS:
        if value == seconds:
            return label
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" + ("s" if hours != 1 else "")
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{seconds} seconds"

# TEMPORARY: backs the /search and /scan endpoints below while we wait on
# eBay API approval. Remove once a real connector is wired up through a
# proper marketplace-selection mechanism.
_mock_connector = MockMarketplaceConnector()

# get_notification_service, get_saved_search_service, _saved_search_runner,
# and _saved_search_run_guard are imported above from
# marketplace_alert.dependencies - the one place these singletons are
# constructed, shared with the /api/v1 routers too. See that module's
# docstring.
_background_scanner = BackgroundScanner(
    session_factory=SessionLocal,
    runner=_saved_search_runner,
    run_guard=_saved_search_run_guard,
    tick_seconds=settings.scheduler_tick_seconds,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("marketplace_alert starting up", extra={"environment": settings.environment})
    # Applies any pending Alembic migrations - PostgreSQL only, a no-op
    # for local SQLite (see run_pending_migrations()'s own docstring).
    # Runs first, before init_db()/the legacy marketplace-column
    # migration/the scanner: the app must never start serving requests,
    # or begin background scanning, against a database whose schema
    # might not match what the running code expects. See
    # PROJECT_CONTEXT.md decision #20 (Render Free has no Pre-Deploy
    # Command, so this replaces what that would have done).
    run_pending_migrations(engine)
    # init_db() only actually does anything for SQLite (local dev/tests) -
    # it no-ops for PostgreSQL, whose schema is managed by the Alembic
    # migration above instead. See database.py's docstring.
    init_db()
    migrate_legacy_marketplace_column(engine)
    # RUN_SCANNER_IN_PROCESS gates the in-process background thread only -
    # see config.py's docstring. Default True (local dev, and any
    # deployment that hasn't yet cut over to the Render Cron Job
    # architecture); production sets it False once the scan Cron Job is
    # in place, since a Render Free web service can be spun down mid-scan
    # (see PROJECT_CONTEXT.md decision #25) and a thread
    # inside that same process is exactly as vulnerable to that as the
    # request-handling code around it.
    if settings.run_scanner_in_process:
        _background_scanner.start()
    try:
        yield
    finally:
        if settings.run_scanner_in_process:
            _background_scanner.stop()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Marketplace monitoring and alert platform.",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "Mobile API - Status",
            "description": "Lightweight, mobile-safe backend/database/notification status.",
        },
        {
            "name": "Mobile API - Marketplaces",
            "description": "Marketplace metadata, driven entirely by the connector registry.",
        },
        {
            "name": "Mobile API - Saved Searches",
            "description": "Saved-search CRUD and manual run, for a future Android/iOS client.",
        },
        {
            "name": "Mobile API - Listings",
            "description": "Browse recently discovered listings (see known limitations in the endpoint description).",
        },
    ],
)

# Native mobile apps don't need browser CORS at all - this exists only for
# future web/dev tooling (e.g. an Expo/React Native web preview) that might
# call this API from a browser context. Empty by default (settings.
# cors_allowed_origins, from CORS_ALLOWED_ORIGINS) - no cross-origin
# browser access until explicitly configured; never "*" - see config.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")
# Versioned mobile API - see marketplace_alert/api/v1/__init__.py. Additive
# only: nothing above/below this line changes for any existing route.
app.include_router(api_v1_router)


def _require_legacy_routes_enabled() -> None:
    """Gate for every route in this file's legacy, unauthenticated surface
    (the dashboard, its backing `/saved-searches*` CRUD/run routes, and the
    temporary `/search`/`/scan` endpoints) - see `settings.legacy_routes_
    enabled`'s docstring in config.py for why this exists. A plain 404,
    not a redirect or an explanatory error body - "disabled" reads to a
    caller identically to "never existed", never as "an admin surface is
    here but you're not allowed in"."""
    if not settings.legacy_routes_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(_require_legacy_routes_enabled)])
def dashboard(
    request: Request,
    service: SavedSearchService = Depends(get_saved_search_service),
    notification_service: NotificationService = Depends(get_notification_service),
) -> HTMLResponse:
    """The local management dashboard: create/list/run/enable/disable/delete
    saved searches from a browser. No business logic lives here - it's the
    same SavedSearchService (and, via the page's JS, the same
    /saved-searches* endpoints) everything else already uses.
    """
    saved_search_rows = [SavedSearchRead.model_validate(s) for s in service.list_all()]
    saved_searches = [
        {
            "id": s.id,
            "query": s.query,
            "marketplaces": s.marketplaces,
            "marketplaces_display": ", ".join(display_name_for(m) for m in s.marketplaces),
            "is_active": s.is_active,
            "scan_interval_display": _format_interval(s.scan_interval_seconds),
            "last_scanned_display": (
                s.last_scanned_at.strftime("%Y-%m-%d %H:%M UTC") if s.last_scanned_at else "Never"
            ),
        }
        for s in saved_search_rows
    ]
    marketplaces = list_supported_marketplaces()
    # Brand-cased display names for the create-search checkboxes
    # (dashboard.html) - the template must never derive one itself (e.g.
    # Python's `.title()` on "ebay" gives "Ebay", not "eBay") - the one
    # shared `display_name_for()` is the single source of truth, same as
    # `marketplace_status` below and `GET /api/v1/marketplaces`.
    marketplace_display_names = {name: display_name_for(name) for name in marketplaces}
    # One entry per registered connector, driven entirely by the registry -
    # never hard-code a marketplace's name here (see
    # `api/v1/marketplaces.py`, which the mobile client uses the same way,
    # and ARCHITECTURE.md "The connector interface"). Adding a marketplace
    # to the registry is enough for it to show up here automatically.
    marketplace_status = [
        {
            "name": name,
            "display_name": display_name_for(name),
            # Not every connector has a credentials concept (mock has
            # nothing to configure) - default to True rather than
            # assuming every connector defines is_configured, same
            # fallback `api/v1/marketplaces.py` uses.
            "configured": getattr(get_connector(name), "is_configured", True),
        }
        for name in marketplaces
    ]

    context = {
        "app_name": settings.app_name,
        "saved_searches": saved_searches,
        "marketplaces": marketplaces,
        "marketplace_display_names": marketplace_display_names,
        "scan_interval_options": SCAN_INTERVAL_OPTIONS,
        "min_scan_interval_seconds": MIN_SCAN_INTERVAL_SECONDS,
        "status": {
            "backend_running": True,
            "telegram_configured": notification_service.is_enabled,
            "marketplace_status": marketplace_status,
            "active_saved_search_count": sum(1 for s in saved_search_rows if s.is_active),
            "marketplace_count": len(marketplaces),
        },
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


_LISTINGS_PAGE_SIZE = 24
_VALID_LISTING_SORTS = {"newest", "oldest", "price_asc", "price_desc"}


def _format_price(price: float | None, currency: str | None) -> str | None:
    """Mirrors mobile's `src/utils/format.ts:formatPrice()` exactly (ISO
    currency-code prefix, no invented `.00` unless the price genuinely has
    cents, thousands-separated) - one consistent price format between the
    dashboard and the mobile app, see ARCHITECTURE.md "Why these choices"."""
    if price is None:
        return None
    has_fraction = price != int(price)
    formatted_number = f"{price:,.2f}" if has_fraction else f"{int(price):,}"
    return f"{currency.upper()} {formatted_number}" if currency else formatted_number


@app.get("/listings", response_class=HTMLResponse, dependencies=[Depends(_require_legacy_routes_enabled)])
def listings_page(
    request: Request,
    marketplace: str | None = None,
    saved_search_id: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    sort: str = "newest",
    offset: int = 0,
    session: Session = Depends(get_db_session),
    service: SavedSearchService = Depends(get_saved_search_service),
) -> HTMLResponse:
    """Browse recently discovered listings from a browser - the rest of
    Phase 7's stated dashboard scope ("...and viewing results"), on top of
    the saved-search *administration* `GET /` already covers. Reads
    through the exact same `ListingRepository` `GET /api/v1/listings`
    uses - no second implementation of listing filtering/sorting. A plain
    server-rendered `<form method="get">` (see `templates/listings.html`)
    submits filters as query-string params and reloads the page - no
    JavaScript needed, consistent with keeping this dashboard lightweight.
    """
    # A browser query string can be hand-edited to anything - degrade to
    # sensible defaults instead of a 422 error page for a page meant to be
    # browsed, not called as an API. Same reasoning for `offset`/prices
    # below (clamped, never trusted as already-valid).
    if marketplace is not None and not is_marketplace_supported(marketplace):
        marketplace = None
    if sort not in _VALID_LISTING_SORTS:
        sort = "newest"
    if min_price is not None and max_price is not None and min_price > max_price:
        min_price, max_price = None, None
    offset = max(offset, 0)

    repository = ListingRepository(session)
    filters = {
        "marketplace": marketplace,
        "saved_search_id": saved_search_id,
        "min_price": min_price,
        "max_price": max_price,
    }
    rows = repository.list_recent(limit=_LISTINGS_PAGE_SIZE, offset=offset, sort=sort, **filters)
    total_count = repository.count(**filters)

    listings = [
        {
            "marketplace": row.marketplace,
            "marketplace_display": display_name_for(row.marketplace),
            "title": row.title,
            "price_display": _format_price(row.price, row.currency),
            "condition": row.condition,
            "location": row.location,
            "seller": row.seller,
            "image_url": row.image_url,
            "listing_url": row.listing_url,
            "discovered_display": row.first_discovered_at.strftime("%Y-%m-%d %H:%M UTC"),
        }
        for row in rows
    ]

    marketplaces = list_supported_marketplaces()
    saved_search_options = [{"id": s.id, "query": s.query} for s in service.list_all()]

    context = {
        "app_name": settings.app_name,
        "listings": listings,
        "total_count": total_count,
        "marketplaces": marketplaces,
        "marketplace_display_names": {name: display_name_for(name) for name in marketplaces},
        "saved_search_options": saved_search_options,
        "sorts": [
            ("newest", "Newest first"),
            ("oldest", "Oldest first"),
            ("price_asc", "Price: low to high"),
            ("price_desc", "Price: high to low"),
        ],
        "filters": {
            "marketplace": marketplace or "",
            "saved_search_id": saved_search_id,
            "min_price": min_price,
            "max_price": max_price,
            "sort": sort,
        },
        "pagination": {
            "has_previous": offset > 0,
            "has_next": offset + _LISTINGS_PAGE_SIZE < total_count,
            "previous_url": str(request.url.include_query_params(offset=max(offset - _LISTINGS_PAGE_SIZE, 0))),
            "next_url": str(request.url.include_query_params(offset=offset + _LISTINGS_PAGE_SIZE)),
        },
    }
    return templates.TemplateResponse(request, "listings.html", context)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Liveness endpoint. Does not yet check downstream dependencies (DB, connectors)."""
    return {"status": "ok"}


@app.get("/search", dependencies=[Depends(_require_legacy_routes_enabled)])
def search(q: str) -> list[Listing]:
    """TEMPORARY endpoint: search the mock marketplace connector.

    Exists only so the mock connector can be exercised over HTTP while we
    wait for eBay API approval. Not the final search API design - no
    marketplace selection, no filters, no persistence, no auth. Stateless:
    running it twice never changes what it returns. For duplicate
    detection, see /scan.
    """
    return _mock_connector.search(q)


class ScanResult(BaseModel):
    query: str
    new_listings: list[Listing]
    new_count: int
    already_seen_count: int
    raw_count: int = 0
    rejected_count: int = 0


@app.get("/scan", dependencies=[Depends(_require_legacy_routes_enabled)])
def scan(
    q: str,
    session: Session = Depends(get_db_session),
) -> ScanResult:
    """TEMPORARY endpoint: stateful search + duplicate detection.

    Unlike /search, this persists every listing it sees and only returns
    the ones that are new since the last scan. Running the same query
    again immediately should return no new listings. Results are filtered
    for relevance (see `core/relevance/`) before duplicate detection, so
    an irrelevant listing is never persisted or counted - the same shared
    filtering path saved searches use.

    **No longer sends a Telegram alert.** This endpoint predates saved
    searches/ownership entirely - a listing it discovers has no owning
    user to resolve a per-user destination for, and per-user notification
    routing's security rule forbids falling back to the legacy global
    `TELEGRAM_CHAT_ID` at runtime (see `core/notifications/outbox.py`'s
    "SECURITY RULE"). Persistence/dedup behavior is unaffected.
    """
    listings = _mock_connector.search(q)
    filter_result = filter_relevant_listings(query=q, listings=listings, marketplace="mock")
    result = ListingDiscoveryService(session).process_listings(filter_result.relevant_listings)
    return ScanResult(
        query=q,
        new_listings=result.new_listings,
        new_count=len(result.new_listings),
        already_seen_count=result.already_seen_count,
        raw_count=filter_result.raw_count,
        rejected_count=filter_result.rejected_count,
    )


@app.post(
    "/saved-searches", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_require_legacy_routes_enabled)]
)
def create_saved_search(
    data: SavedSearchCreate, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    try:
        saved_search = service.create(data)
    except UnsupportedMarketplaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return SavedSearchRead.model_validate(saved_search)


@app.get("/saved-searches", dependencies=[Depends(_require_legacy_routes_enabled)])
def list_saved_searches(
    service: SavedSearchService = Depends(get_saved_search_service),
) -> list[SavedSearchRead]:
    return [SavedSearchRead.model_validate(s) for s in service.list_all()]


@app.get("/saved-searches/{saved_search_id}", dependencies=[Depends(_require_legacy_routes_enabled)])
def get_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    saved_search = service.get(saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return SavedSearchRead.model_validate(saved_search)


@app.patch("/saved-searches/{saved_search_id}", dependencies=[Depends(_require_legacy_routes_enabled)])
def update_saved_search(
    saved_search_id: int,
    data: SavedSearchUpdate,
    service: SavedSearchService = Depends(get_saved_search_service),
) -> SavedSearchRead:
    try:
        saved_search = service.update(saved_search_id, data)
    except UnsupportedMarketplaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return SavedSearchRead.model_validate(saved_search)


@app.delete(
    "/saved-searches/{saved_search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_legacy_routes_enabled)],
)
def delete_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> None:
    if not service.delete(saved_search_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")


@app.post("/saved-searches/{saved_search_id}/run", dependencies=[Depends(_require_legacy_routes_enabled)])
def run_saved_search_now(
    saved_search_id: int,
    session: Session = Depends(get_db_session),
) -> SavedSearchRunResponse:
    """Immediately run one saved search, through the same SavedSearchRunner the
    background scanner uses - no separate scan logic lives here."""
    saved_search = SavedSearchRepository(session).get(saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    if not saved_search.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Saved search is inactive")

    if not _saved_search_run_guard.try_acquire(saved_search_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Saved search is already running"
        )
    try:
        runner = SavedSearchRunner(resolve_connector=get_connector)
        result = runner.run(session, saved_search)
    finally:
        _saved_search_run_guard.release(saved_search_id)

    return SavedSearchRunResponse(
        saved_search_id=result.saved_search_id,
        results=[
            MarketplaceRunResult(
                marketplace=r.marketplace,
                new_count=r.new_count,
                already_seen_count=r.already_seen_count,
                error=r.error,
                raw_count=r.raw_count,
                rejected_count=r.rejected_count,
            )
            for r in result.results
        ],
        new_count=result.new_count,
        already_seen_count=result.already_seen_count,
    )
