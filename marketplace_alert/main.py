"""FastAPI application entry point.

Run locally with:
    uvicorn marketplace_alert.main:app --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from marketplace_alert import __version__
from marketplace_alert.config import settings
from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.connectors.registry import (
    get_connector,
    is_marketplace_supported,
    list_supported_marketplaces,
)
from marketplace_alert.core.logging_config import configure_logging
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.database import SessionLocal, engine, get_db_session, init_db
from marketplace_alert.core.persistence.service import ListingDiscoveryService
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
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner
from marketplace_alert.notifications.telegram.provider import TelegramNotificationProvider

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

# The concrete provider (Telegram) is chosen here, once, at startup - the
# notification service and /scan route only ever depend on the
# NotificationProvider interface. Disabled automatically if credentials are
# missing (see TelegramNotificationProvider). The provider retries its own
# transient failures (429/5xx/timeout, bounded, with backoff); the service
# paces sends between separate listings - see both modules' docstrings.
_notification_service = NotificationService(
    TelegramNotificationProvider(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        max_retries=settings.telegram_max_retries,
        retry_base_seconds=settings.telegram_retry_base_seconds,
    ),
    send_delay_seconds=settings.telegram_send_delay_seconds,
)


def get_notification_service() -> NotificationService:
    """FastAPI dependency, overridden in tests with a fake provider.

    Never send real Telegram messages from automated tests - see
    tests/conftest.py.
    """
    return _notification_service


def get_saved_search_service(session: Session = Depends(get_db_session)) -> SavedSearchService:
    """FastAPI dependency: validated saved-search CRUD, wired to the real connector registry."""
    return SavedSearchService(session, is_marketplace_supported=is_marketplace_supported)


# The background scanner's own runner is bound to the real notification
# service, since a background thread has no per-request Depends to pull a
# (possibly test-faked) one from. It resolves connectors only through
# get_connector - it never imports MockMarketplaceConnector itself.
_saved_search_runner = SavedSearchRunner(
    notification_service=_notification_service,
    resolve_connector=get_connector,
)
# Shared between the scheduler and the manual /run endpoint so the same
# saved search can never be scanned by both at once.
_saved_search_run_guard = SavedSearchRunGuard()
_background_scanner = BackgroundScanner(
    session_factory=SessionLocal,
    runner=_saved_search_runner,
    run_guard=_saved_search_run_guard,
    tick_seconds=settings.scheduler_tick_seconds,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("marketplace_alert starting up", extra={"environment": settings.environment})
    init_db()
    migrate_legacy_marketplace_column(engine)
    _background_scanner.start()
    try:
        yield
    finally:
        _background_scanner.stop()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Marketplace monitoring and alert platform.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
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
            "marketplaces_display": ", ".join(m.title() for m in s.marketplaces),
            "is_active": s.is_active,
            "scan_interval_display": _format_interval(s.scan_interval_seconds),
            "last_scanned_display": (
                s.last_scanned_at.strftime("%Y-%m-%d %H:%M UTC") if s.last_scanned_at else "Never"
            ),
        }
        for s in saved_search_rows
    ]
    marketplaces = list_supported_marketplaces()
    etsy_configured = is_marketplace_supported("etsy") and get_connector("etsy").is_configured
    ebay_configured = is_marketplace_supported("ebay") and get_connector("ebay").is_configured

    context = {
        "app_name": settings.app_name,
        "saved_searches": saved_searches,
        "marketplaces": marketplaces,
        "scan_interval_options": SCAN_INTERVAL_OPTIONS,
        "min_scan_interval_seconds": MIN_SCAN_INTERVAL_SECONDS,
        "status": {
            "backend_running": True,
            "telegram_configured": notification_service.is_enabled,
            "etsy_configured": etsy_configured,
            "ebay_configured": ebay_configured,
            "active_saved_search_count": sum(1 for s in saved_search_rows if s.is_active),
            "marketplace_count": len(marketplaces),
        },
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Liveness endpoint. Does not yet check downstream dependencies (DB, connectors)."""
    return {"status": "ok"}


@app.get("/search")
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


@app.get("/scan")
def scan(
    q: str,
    session: Session = Depends(get_db_session),
    notification_service: NotificationService = Depends(get_notification_service),
) -> ScanResult:
    """TEMPORARY endpoint: stateful search + duplicate detection + alerts.

    Unlike /search, this persists every listing it sees and only returns
    the ones that are new since the last scan. Running the same query
    again immediately should return no new listings. A Telegram alert is
    sent for each new listing (never for already-seen ones); a failure to
    notify never fails the scan itself.
    """
    listings = _mock_connector.search(q)
    result = ListingDiscoveryService(session).process_listings(listings)
    notification_service.notify_new_listings(result.new_listings)
    return ScanResult(
        query=q,
        new_listings=result.new_listings,
        new_count=len(result.new_listings),
        already_seen_count=result.already_seen_count,
    )


@app.post("/saved-searches", status_code=status.HTTP_201_CREATED)
def create_saved_search(
    data: SavedSearchCreate, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    try:
        saved_search = service.create(data)
    except UnsupportedMarketplaceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return SavedSearchRead.model_validate(saved_search)


@app.get("/saved-searches")
def list_saved_searches(
    service: SavedSearchService = Depends(get_saved_search_service),
) -> list[SavedSearchRead]:
    return [SavedSearchRead.model_validate(s) for s in service.list_all()]


@app.get("/saved-searches/{saved_search_id}")
def get_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> SavedSearchRead:
    saved_search = service.get(saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return SavedSearchRead.model_validate(saved_search)


@app.patch("/saved-searches/{saved_search_id}")
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


@app.delete("/saved-searches/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_search(
    saved_search_id: int, service: SavedSearchService = Depends(get_saved_search_service)
) -> None:
    if not service.delete(saved_search_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")


@app.post("/saved-searches/{saved_search_id}/run")
def run_saved_search_now(
    saved_search_id: int,
    session: Session = Depends(get_db_session),
    notification_service: NotificationService = Depends(get_notification_service),
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
        runner = SavedSearchRunner(notification_service=notification_service, resolve_connector=get_connector)
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
            )
            for r in result.results
        ],
        new_count=result.new_count,
        already_seen_count=result.already_seen_count,
    )
