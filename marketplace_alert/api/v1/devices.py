"""`/api/v1/devices` - the authenticated caller's own Expo push token
registration. Native mobile push, Phase 1.

Always "me" - see `api/v1/notification_preferences.py`'s identical
convention. No `user_id` field on either request schema, ever - ownership
is derived exclusively from the bearer token (`get_current_user`), never
accepted from the client. This is what makes the ownership-transfer
behavior in `DeviceTokenRepository.upsert` safe: a caller can only ever
register/unregister a token *as themselves*, never on behalf of another
user - see that repository's own docstring.

`POST /test-push` (bottom of this file) is a **temporary** addition for
manual Phase 1 verification - not part of the normal notification
pipeline (which only ever sends via `scripts/drain_notification_outbox.py`
against real `pending_notifications` rows). Remove it once end-to-end
push delivery has been manually confirmed on a real device.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import DeviceRegisterRequest, DeviceUnregisterRequest, TestPushResponse
from marketplace_alert.core.auth.dependencies import get_current_user
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.device_repository import DeviceTokenRepository
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.dependencies import get_expo_push_provider

router = APIRouter(prefix="/devices", tags=["Mobile API - Devices"])


@router.post(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register/update my Expo push token",
    description=(
        "Registers the caller's Expo push token, or updates it if the exact same token "
        "was already registered - safe to call on every app launch (idempotent upsert). "
        "If this token was previously registered under a different user, ownership is "
        "transferred to the caller."
    ),
)
def register_device(
    data: DeviceRegisterRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> None:
    DeviceTokenRepository(session).upsert(
        user_id=current_user.id, expo_push_token=data.expo_push_token, platform=data.platform
    )


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister my Expo push token",
    description=(
        "Removes one of the caller's own registered tokens (e.g. on logout). Idempotent - "
        "removing a token that's already gone, or was never registered, is not an error."
    ),
)
def unregister_device(
    data: DeviceUnregisterRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> None:
    DeviceTokenRepository(session).delete_for_user(user_id=current_user.id, expo_push_token=data.expo_push_token)


def _build_test_listing() -> Listing:
    """An in-memory-only `Listing` for `send_test_push` below - never
    persisted, never written to any table. Its `title` becomes the push
    notification's body text (see `ExpoPushProvider.send_listing_alert`) -
    deliberately unmistakable as a manual test, never confusable with a
    real marketplace match."""
    return Listing(
        marketplace="test",
        external_listing_id="test-push",
        title="MarketplaceAlert Test: Push notifications are working.",
        listing_url="https://marketplacealert.onrender.com",
    )


@router.post(
    "/test-push",
    summary="[TEMPORARY - Phase 1 manual verification] Send myself one test push",
    description=(
        "**Temporary, Phase 1 manual-verification diagnostic - not part of the "
        "normal notification pipeline, remove once push has been verified "
        "end-to-end on a real device.** Sends one push notification with "
        "obviously-test content to every one of the caller's own registered "
        "devices, via the same ExpoPushProvider real listing alerts use. Reads "
        "the caller's existing device_tokens rows only - creates no database "
        "row of any kind (no listing, no pending_notifications row) - and never "
        "returns or logs a token value."
    ),
    response_model=TestPushResponse,
)
def send_test_push(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    provider: NotificationProvider = Depends(get_expo_push_provider),
) -> TestPushResponse:
    tokens = DeviceTokenRepository(session).list_tokens_for_user(current_user.id)
    if not tokens:
        return TestPushResponse(sent=False, device_count=0)

    listing = _build_test_listing()
    any_success = False
    for token in tokens:
        try:
            provider.send_listing_alert(listing, token)
            any_success = True
        except NotificationError:
            continue
        except Exception:  # noqa: BLE001 - one bad device can't block the rest, same reasoning as outbox.py's _deliver_push
            continue

    return TestPushResponse(sent=any_success, device_count=len(tokens))
