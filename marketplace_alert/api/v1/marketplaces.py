"""`GET /api/v1/marketplaces` - marketplace metadata for a mobile client.

Every marketplace listed comes from the connector registry
(`list_supported_marketplaces()`) - never a separately hard-coded list,
the same rule the dashboard's marketplace checkboxes already follow (see
ARCHITECTURE.md "The connector interface"). Display names come from the
same registry too (`display_name_for()`), so a brand's proper casing
(e.g. "eBay") is defined once, not duplicated between this route and the
dashboard.
"""

from fastapi import APIRouter

from marketplace_alert.api.v1.schemas import MarketplaceInfo
from marketplace_alert.connectors.registry import display_name_for, get_connector, list_supported_marketplaces

router = APIRouter(tags=["Mobile API - Marketplaces"])


@router.get(
    "/marketplaces",
    summary="List marketplace metadata",
    description=(
        "One entry per marketplace with a registered connector - driven "
        "entirely by the connector registry, never a separately "
        "maintained list. `configured` reflects whether real credentials "
        "are present (always true for a connector with nothing to "
        "configure, like the mock one); `available` is the connector's "
        "own cheap, side-effect-free `health_check()`."
    ),
)
def list_marketplaces() -> list[MarketplaceInfo]:
    infos = []
    for marketplace_id in list_supported_marketplaces():
        connector = get_connector(marketplace_id)
        infos.append(
            MarketplaceInfo(
                id=marketplace_id,
                name=display_name_for(marketplace_id),
                # Not every connector has a credentials concept (mock has
                # nothing to configure) - default to True rather than
                # assuming every connector defines is_configured.
                configured=getattr(connector, "is_configured", True),
                available=connector.health_check(),
            )
        )
    return infos
