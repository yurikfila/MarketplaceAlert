"""`/api/v1/devices` - the authenticated caller's own Expo push token
registration. Native mobile push, Phase 1.

Always "me" - see `api/v1/notification_preferences.py`'s identical
convention. No `user_id` field on either request schema, ever - ownership
is derived exclusively from the bearer token (`get_current_user`), never
accepted from the client. This is what makes the ownership-transfer
behavior in `DeviceTokenRepository.upsert` safe: a caller can only ever
register/unregister a token *as themselves*, never on behalf of another
user - see that repository's own docstring.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import DeviceRegisterRequest, DeviceUnregisterRequest
from marketplace_alert.core.auth.dependencies import get_current_user
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.notifications.device_repository import DeviceTokenRepository
from marketplace_alert.core.persistence.database import get_db_session

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
