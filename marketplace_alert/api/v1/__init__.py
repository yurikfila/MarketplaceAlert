"""Versioned mobile API - `/api/v1`.

Aggregates every `/api/v1` sub-router into one `router` that `main.py`
mounts once (`app.include_router(router)`). Exists so a future Android/iOS
app has a stable, JSON-only contract that never depends on HTML/dashboard
behavior - see PROJECT_CONTEXT.md/ARCHITECTURE.md "Mobile API". Every
endpoint here is a thin adapter over the exact same services/repositories
the existing routes already use (`marketplace_alert.dependencies`,
`core/saved_searches/`, `core/persistence/`, `connectors/registry.py`) -
no business logic is duplicated for the mobile API.

Adding /api/v1/* is purely additive: nothing under `/`, `/health`,
`/search`, `/scan`, or the legacy `/saved-searches*` routes changes.
"""

from fastapi import APIRouter

from marketplace_alert.api.v1 import auth, listings, marketplaces, notification_preferences, saved_searches, status

router = APIRouter(prefix="/api/v1")
router.include_router(status.router)
router.include_router(marketplaces.router)
router.include_router(saved_searches.router)
router.include_router(listings.router)
router.include_router(auth.router)
router.include_router(notification_preferences.router)
