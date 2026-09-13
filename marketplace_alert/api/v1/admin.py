"""`/api/v1/admin/*` - read-only user-management reporting for the
application owner only.

**Authorization.** Every route here depends on `core/auth/dependencies.
py:require_admin` - a valid `Authorization: Bearer` access token
(exactly what `GET /auth/me` already requires) belonging to an account
with `is_admin=True` in the database. `401` for missing/invalid/expired
credentials, `403` for a perfectly valid but non-admin account - never
anything looser (never an email comparison, never a client-supplied
`is_admin`/role field in a header or body, never something the mobile
app's own navigation hiding could substitute for). See that dependency's
own docstring for the full reasoning.

**Read-only, deliberately, in this phase.** No promote/demote/delete/ban/
disable endpoint exists here, or anywhere else in this API. The only way
an account ever becomes an admin is `scripts/set_admin.py`, run directly
by a server operator against the database - never reachable through any
authenticated request, admin or not. See that script's own docstring.

**Nothing here is ever built from a raw `User`/`SavedSearch` row via
`from_attributes`.** `core/admin/repository.py`'s `AdminRepository`
already selects only safe fields into `AdminUserRow`; `api/v1/schemas.py`'s
`AdminUserOut` is populated field-by-field from that, one more layer of
"a new, more sensitive column can't silently start being serialized."
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from marketplace_alert.api.v1.schemas import (
    AdminEmailDeliveryTestResponse,
    AdminStatsResponse,
    AdminUserListResponse,
    AdminUserOut,
)
from marketplace_alert.core.admin.repository import AdminRepository, AdminUserRow
from marketplace_alert.core.auth.dependencies import require_admin
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.persistence.database import get_db_session
from marketplace_alert.dependencies import get_password_reset_email_sender
from marketplace_alert.notifications.email.provider import PasswordResetEmailError, PasswordResetEmailSender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Mobile API - Admin"], dependencies=[Depends(require_admin)])

# ===== TEMPORARY DIAGNOSTIC - see email_delivery_test() below. Remove this
# constant together with that route once email deliverability has been
# confirmed and the diagnostic is no longer needed.
#
# Fixed, server-side only - never accepted from the request. The whole
# point of this endpoint is a controlled, one-recipient deliverability
# check; accepting an arbitrary recipient would turn an admin-only
# diagnostic into a general-purpose "send email to anyone" primitive.
_DIAGNOSTIC_RECIPIENT = "yurikfila@gmail.com"


def _user_out(row: AdminUserRow) -> AdminUserOut:
    return AdminUserOut(
        id=row.id,
        email=row.email,
        created_at=row.created_at,
        is_admin=row.is_admin,
        is_active=row.is_active,
        saved_search_count=row.saved_search_count,
    )


@router.get(
    "/users",
    summary="List registered users (admin only)",
    description=(
        "Every registered user with safe, non-secret fields only, plus "
        "each user's own saved-search count. Requires an authenticated "
        "account with is_admin=True - 401 unauthenticated, 403 for a "
        "valid but non-admin account."
    ),
)
def list_users(session: Session = Depends(get_db_session)) -> AdminUserListResponse:
    rows = AdminRepository(session).list_users()
    return AdminUserListResponse(total_users=len(rows), users=[_user_out(row) for row in rows])


@router.get(
    "/stats",
    summary="Aggregate counts (admin only)",
    description="Same authorization as GET /admin/users - aggregate counts only, no per-user data.",
)
def get_stats(session: Session = Depends(get_db_session)) -> AdminStatsResponse:
    repo = AdminRepository(session)
    return AdminStatsResponse(
        total_users=repo.count_users(),
        total_saved_searches=repo.count_saved_searches(),
    )


# ===== TEMPORARY DIAGNOSTIC - remove this route (and
# PasswordResetEmailSender.send_diagnostic_test_email/_DIAGNOSTIC_RECIPIENT/
# AdminEmailDeliveryTestResponse it depends on) once email deliverability
# has been confirmed and this diagnostic is no longer needed.
@router.post(
    "/email-delivery-test",
    summary="TEMPORARY: send one fixed diagnostic test email (admin only)",
    description=(
        "TEMPORARY diagnostic endpoint - sends exactly one fixed, plain-text "
        "test email to a hard-coded recipient, using the application's "
        "existing Resend configuration (PasswordResetEmailSender, the same "
        "sender /forgot-password uses). Accepts no request body - the "
        "recipient, subject, and body are never client-supplied, and this "
        "never touches password-reset behavior. Same authorization as every "
        "other /admin/* route (401 unauthenticated, 403 non-admin)."
    ),
)
def email_delivery_test(
    current_user: User = Depends(require_admin),
    email_sender: PasswordResetEmailSender = Depends(get_password_reset_email_sender),
) -> AdminEmailDeliveryTestResponse:
    # Logs the caller's id (not email - this codebase never logs a user's
    # email address anywhere, see PasswordResetEmailSender's own docstring)
    # and only the recipient's domain, never the full address.
    logger.info(
        "Admin email delivery diagnostic requested (user_id=%s, target_domain=gmail.com)",
        current_user.id,
    )
    if not email_sender.is_enabled:
        logger.info("Admin email delivery diagnostic skipped - sender is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Email delivery is not configured"
        )
    try:
        email_sender.send_diagnostic_test_email(to=_DIAGNOSTIC_RECIPIENT)
    except PasswordResetEmailError:
        logger.error("Admin email delivery diagnostic failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Email provider failed to accept the test email"
        ) from None
    logger.info("Admin email delivery diagnostic accepted by provider")
    return AdminEmailDeliveryTestResponse(status="accepted")


# ===== END TEMPORARY DIAGNOSTIC (email_delivery_test)
