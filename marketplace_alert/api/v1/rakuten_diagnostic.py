"""TEMPORARY, ONE-TIME DIAGNOSTIC - `GET /api/v1/diagnostics/rakuten`.

Not a marketplace connector, not permanent infrastructure. Exists only to
prove that a request originating from the deployed Render web service (not
a developer's local machine) is accepted by Rakuten's IP allowlist for the
registered "API/Backend Service" application. Delete this file, and the two
lines it adds to `api/v1/__init__.py`, once that's confirmed - see this
module's own final paragraph for the exact removal steps.

**Authorization**: gated by the same `require_admin` dependency every
`/api/v1/admin/*` route already uses (see `core/auth/dependencies.py` and
`api/v1/admin.py`) - a valid bearer access token belonging to an account
with `is_admin=True`. No new auth mechanism, nothing looser.

**Credentials**: `RAKUTEN_APPLICATION_ID`/`RAKUTEN_ACCESS_KEY` are read
directly from the process environment via `os.environ.get()` - deliberately
NOT added to `Settings`/`config.py`, since this is not an approved
connector and should leave no permanent trace there. Both values, and any
URL/exception text that could contain them, are never logged, printed, or
included in the response body - only HTTP status, Rakuten's own
`error`/`error_description` fields (never the credential), and sanitized
product fields are ever surfaced.

**Endpoint verified against Rakuten's own current documentation**
(webservice.rakuten.co.jp, inspected 2026-09-28): the legacy
`app.rakuten.co.jp/services/api/...` path is retired; the current path is
versioned by date (`.../20260701`). `accessKey` is documented as usable
either as a query parameter or an HTTP header - sent here as a query
parameter (Rakuten's own supported, documented mechanism), with this
module never logging the constructed URL under any code path, success or
failure. `formatVersion=2` is used deliberately over the default
(`formatVersion=1`) - it returns a flat item object per array entry
(`items[].itemName`) instead of the legacy double-nested shape
(`items[].item.itemName`), which is simpler and less error-prone to parse
for a diagnostic that isn't meant to become a real integration.

**Removal steps, once Render's outbound IP is confirmed accepted**:
1. Delete this file (`marketplace_alert/api/v1/rakuten_diagnostic.py`).
2. In `marketplace_alert/api/v1/__init__.py`, remove the `rakuten_diagnostic`
   import and the `router.include_router(rakuten_diagnostic.router)` line.
Nothing else references this module - no config, no migration, no other
file to revert.
"""

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from marketplace_alert.core.auth.dependencies import require_admin
from marketplace_alert.core.connectors.retry import request_with_retry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diagnostics", tags=["Mobile API - Diagnostics"], dependencies=[Depends(require_admin)])

_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
_SEARCH_KEYWORD = "Bosch"
_SEARCH_HITS = 3


class RakutenDiagnosticItem(BaseModel):
    item_code: str | None = None
    item_name: str | None = None
    item_price: int | None = None
    item_url: str | None = None
    image_url: str | None = None
    shop_name: str | None = None


class RakutenDiagnosticResponse(BaseModel):
    """`configured=False` means one/both env vars are unset - no request
    was ever attempted. `configured=True, success=False` means a request
    was made and rejected (see `http_status`/`error`/`error_description`,
    e.g. the already-observed `403 CLIENT_IP_NOT_ALLOWED`). Never contains
    either credential under any outcome."""

    configured: bool
    http_status: int | None = None
    success: bool
    error: str | None = None
    error_description: str | None = None
    item_count: int = 0
    items: list[RakutenDiagnosticItem] = []


@router.get(
    "/rakuten",
    summary="TEMPORARY: verify Render's outbound IP is accepted by Rakuten (admin only)",
    description=(
        "One-time diagnostic, not a marketplace connector. Makes a single "
        "live request to the Rakuten Ichiba Item Search API "
        "(keyword='Bosch', hits=3) using RAKUTEN_APPLICATION_ID/"
        "RAKUTEN_ACCESS_KEY read from the process environment - never "
        "Settings, never logged, never returned. Delete once Render's "
        "outbound IP is confirmed accepted by Rakuten's IP allowlist."
    ),
)
def rakuten_diagnostic() -> RakutenDiagnosticResponse:
    application_id = os.environ.get("RAKUTEN_APPLICATION_ID")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY")

    if not application_id or not access_key:
        logger.error("Rakuten diagnostic: RAKUTEN_APPLICATION_ID and/or RAKUTEN_ACCESS_KEY are not set")
        return RakutenDiagnosticResponse(configured=False, success=False)

    params = {
        "applicationId": application_id,
        "accessKey": access_key,
        "keyword": _SEARCH_KEYWORD,
        "hits": _SEARCH_HITS,
        "formatVersion": 2,
    }

    try:
        response = request_with_retry(
            lambda: httpx.get(_SEARCH_URL, params=params, timeout=10.0),
            marketplace_name="Rakuten (diagnostic)",
        )
    except httpx.HTTPError as exc:
        # Never log str(exc)/repr(exc) - httpx's own exception messages can
        # echo the request URL, which would include the credential.
        logger.error("Rakuten diagnostic request failed (%s)", type(exc).__name__)
        return RakutenDiagnosticResponse(configured=True, success=False, error=type(exc).__name__)

    try:
        body = response.json()
    except ValueError:
        logger.error("Rakuten diagnostic: non-JSON response (HTTP %s)", response.status_code)
        return RakutenDiagnosticResponse(configured=True, http_status=response.status_code, success=False)

    if response.status_code != 200:
        error = body.get("error") if isinstance(body, dict) else None
        error_description = body.get("error_description") if isinstance(body, dict) else None
        logger.error("Rakuten diagnostic: HTTP %s (%s)", response.status_code, error)
        return RakutenDiagnosticResponse(
            configured=True,
            http_status=response.status_code,
            success=False,
            error=error,
            error_description=error_description,
        )

    raw_items = body.get("items") if isinstance(body, dict) else None
    items = [_sanitize_item(raw) for raw in raw_items] if isinstance(raw_items, list) else []

    logger.info("Rakuten diagnostic succeeded: %d item(s) returned", len(items))
    return RakutenDiagnosticResponse(
        configured=True,
        http_status=response.status_code,
        success=True,
        item_count=len(items),
        items=items,
    )


def _sanitize_item(raw: Any) -> RakutenDiagnosticItem:
    if not isinstance(raw, dict):
        return RakutenDiagnosticItem()

    # Defensive - Rakuten's documented `mediumImageUrls` shape has varied
    # across API versions (a list of plain strings vs. a list of
    # `{"imageUrl": ...}` objects); never guessed, both handled safely.
    image_url = None
    image_urls = raw.get("mediumImageUrls")
    if isinstance(image_urls, list) and image_urls:
        first = image_urls[0]
        if isinstance(first, dict):
            image_url = first.get("imageUrl")
        elif isinstance(first, str):
            image_url = first

    return RakutenDiagnosticItem(
        item_code=raw.get("itemCode"),
        item_name=raw.get("itemName"),
        item_price=raw.get("itemPrice"),
        item_url=raw.get("itemUrl"),
        image_url=image_url,
        shop_name=raw.get("shopName"),
    )
