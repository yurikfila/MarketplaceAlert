"""`GET /api/v1/status` - safe, mobile-friendly system status.

Never includes secrets or credential values - only booleans and plain
marketplace identifiers, the same rule the dashboard's status section
already follows (see ARCHITECTURE.md "The management dashboard").
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import MobileStatus
from marketplace_alert.connectors.registry import list_supported_marketplaces
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.dependencies import get_notification_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Mobile API - Status"])


@router.get(
    "/status",
    summary="Mobile-friendly backend status",
    description=(
        "Lightweight status check for a mobile client: whether the backend "
        "responded, whether the database is reachable, whether Telegram "
        "notifications are configured, and which marketplaces are "
        "supported. Never exposes credentials, connection strings, or any "
        "other secret."
    ),
)
def mobile_status(
    session: Session = Depends(get_db_session),
    notification_service: NotificationService = Depends(get_notification_service),
) -> MobileStatus:
    database_ok = True
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        # Never log/return the raw exception - it could echo back
        # connection details (host, database name) from the driver.
        logger.error("Mobile status check: database connectivity check failed")
        database_ok = False

    return MobileStatus(
        status="ok",
        backend=True,
        database=database_ok,
        telegram_configured=notification_service.is_enabled,
        supported_marketplaces=list_supported_marketplaces(),
    )
