"""`/api/v1/auth*` - signup, login, refresh, logout, the current-user
check, and password-reset request/verification, over the existing
`AuthService` (`core/auth/service.py`).

Thin by design (see that module's own docstring for the full business/
security rules): every route here validates its request shape via the
schemas in `api/v1/schemas.py`, calls exactly one `AuthService` method,
and maps whatever it raises to an HTTP response - no business or security
logic is duplicated here. `GET /me` additionally depends on
`core/auth/dependencies.py`'s `get_current_user` for bearer-token
extraction/verification; `forgot_password`/`reset_password` deliberately
do NOT - both must work for a user who cannot currently log in at all.

**Nothing outside these seven routes is protected yet.** Saved-search and
listing routes remain exactly as open as before this router exists - see
PROJECT_CONTEXT.md's authentication design decision for the phased plan.

**Phase 4C of the approved 6-digit-code password-reset design: email
delivery is wired in, at this API/orchestration layer only.**
`forgot_password()` calls `AuthService.request_password_reset()`, then
`_deliver_password_reset_code()` (below) decides - internally,
privately - whether to actually call `PasswordResetEmailSender`. **This
route's own public response never changes based on any of that** -
`PasswordResetRequestResult` is internal-only (see its own docstring in
`core/auth/service.py`) and is never imported for any purpose other than
reading `.should_deliver`/`.raw_code` inside `_deliver_password_reset_code`
- never returned, logged, or otherwise exposed. `core/auth/` itself stays
completely free of Resend/httpx/email-provider imports, by design - see
`_deliver_password_reset_code`'s own docstring.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from marketplace_alert.api.v1.schemas import (
    AuthResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenPairOut,
    UserPublic,
)
from marketplace_alert.config import settings
from marketplace_alert.core.auth.dependencies import get_current_user
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.service import (
    AuthService,
    EmailAlreadyRegisteredError,
    ExpiredRefreshTokenError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidResetCodeError,
    PasswordResetRequestResult,
    RefreshTokenReusedError,
    TokenPair,
    WeakNewPasswordError,
)
from marketplace_alert.dependencies import get_auth_service, get_password_reset_email_sender
from marketplace_alert.notifications.email.provider import PasswordResetEmailError, PasswordResetEmailSender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Mobile API - Authentication"])

# Every refresh-side rejection reason - a token this database has never
# seen, one that's expired, one that's already been rotated/logged out
# (reuse), or one naming an account that's gone inactive - collapses to
# this one message. Same "don't expose which internal reason fired"
# principle as login's InvalidCredentialsError: a legitimate client's
# correct response to any of these is identical anyway (discard local
# tokens, prompt re-login), so there is no functional reason to
# distinguish them externally, only a reason not to.
_INVALID_REFRESH_TOKEN_DETAIL = "Invalid or expired refresh token"

# The one, fixed, generic response `/forgot-password` ever returns -
# never varies with whether the email is registered, active, on cooldown,
# or already at its hourly issuance limit. See `forgot_password()`'s own
# docstring for why.
_FORGOT_PASSWORD_GENERIC_MESSAGE = "If that email is registered, a verification code has been sent."


def _user_public(user: User) -> UserPublic:
    return UserPublic(id=user.id, email=user.email, created_at=user.created_at)


def _token_pair_out(tokens: TokenPair) -> TokenPairOut:
    return TokenPairOut(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    description=(
        "Creates a new account and immediately returns a fresh token pair "
        "(no separate login call needed). Email is normalized (stripped, "
        "lowercased) before storage and lookup. 409 if the normalized "
        "email is already registered - the response never includes "
        "database-internal detail, just that the email is taken."
    ),
)
def signup(data: SignupRequest, auth_service: AuthService = Depends(get_auth_service)) -> AuthResponse:
    try:
        user, tokens = auth_service.signup(email=data.email, password=data.password)
    except EmailAlreadyRegisteredError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from None
    return AuthResponse(user=_user_public(user), tokens=_token_pair_out(tokens))


@router.post(
    "/login",
    summary="Log in",
    description=(
        "Every rejection reason - unknown email, wrong password, an "
        "inactive account, or a currently-locked account - returns the "
        "exact same 401 with the exact same generic detail message; none "
        "of that distinction is ever exposed through this endpoint."
    ),
)
def login(data: LoginRequest, auth_service: AuthService = Depends(get_auth_service)) -> AuthResponse:
    try:
        user, tokens = auth_service.login(email=data.email, password=data.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None
    return AuthResponse(user=_user_public(user), tokens=_token_pair_out(tokens))


@router.post(
    "/refresh",
    summary="Rotate a refresh token for a fresh token pair",
    description=(
        "The presented refresh token is revoked and replaced atomically - "
        "there is no window in which both the old and new token are "
        "valid. Reusing an already-rotated-away (or logged-out) token is "
        "treated as a compromise signal: every refresh token this account "
        "currently has is revoked as a side effect. Any invalid, expired, "
        "or reused token - or one naming an inactive account - maps to "
        "the same 401."
    ),
)
def refresh(data: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenPairOut:
    try:
        tokens = auth_service.refresh(data.refresh_token)
    except (InvalidRefreshTokenError, ExpiredRefreshTokenError, RefreshTokenReusedError, InactiveAccountError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_REFRESH_TOKEN_DETAIL
        ) from None
    return _token_pair_out(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
    description=(
        "Idempotent - an already-revoked or never-issued token is treated "
        "the same as a freshly-revoked one. This never fails, and never "
        "reveals whether the token it was given was ever valid."
    ),
)
def logout(data: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> None:
    auth_service.logout(data.refresh_token)


@router.get(
    "/me",
    summary="The currently authenticated user",
    description=(
        "Requires `Authorization: Bearer <access_token>`. 401, with a "
        "`WWW-Authenticate: Bearer` challenge header, for a missing, "
        "malformed, tampered, wrong-algorithm, or expired token, or one "
        "naming an account that no longer exists or is no longer active - "
        "every one of those reasons produces the identical response."
    ),
)
def me(current_user: User = Depends(get_current_user)) -> UserPublic:
    return _user_public(current_user)


def _deliver_password_reset_code(
    result: PasswordResetRequestResult, *, email: str, sender: PasswordResetEmailSender
) -> None:
    """The one place `PasswordResetRequestResult`'s `should_deliver`/
    `raw_code` are ever read - deliberately kept out of `AuthService`
    (see `core/auth/service.py`'s own module docstring: it stays fully
    usable and fully tested with zero external I/O) and out of
    `core/auth/` entirely, so the auth/domain layer never has to know
    Resend exists. Called from `forgot_password()` below; never affects
    what that route returns, regardless of outcome.

    - `should_deliver=False`: nothing to send - return immediately.
      `result.raw_code` is never inspected beyond the dataclass's own
      already-enforced invariant.
    - `should_deliver=True` but `sender.is_enabled` is `False`: nothing
      configured to send with - a sanitized, non-identifying log line
      only, never the email/code.
    - `should_deliver=True` and enabled: call `send_password_reset_code`
      exactly once. That method already retries transient failures
      internally (Phase 4B) - this function adds no second retry loop.
    - `PasswordResetEmailError`: swallowed here, with a sanitized log
      line only (no email, no code, no exception text - see that
      exception's own docstring for why its message could still be
      provider-shaped rather than instance-specific). Deliberately the
      *only* exception type caught - an unexpected bug elsewhere must
      still surface normally as a 500, never silently swallowed.
    """
    if not result.should_deliver:
        return
    if not sender.is_enabled:
        logger.info("Password reset email skipped because sender is not configured")
        return
    try:
        sender.send_password_reset_code(
            email=email,
            code=result.raw_code,
            expires_minutes=settings.password_reset_token_expire_minutes,
        )
    except PasswordResetEmailError:
        logger.error("Password reset email delivery failed")


@router.post(
    "/forgot-password",
    summary="Request a password-reset verification code",
    description=(
        "Always returns the identical response whether or not the email "
        "is registered, active, on cooldown, or already at its hourly "
        "issuance limit, or whatever happens during email delivery - see "
        "AuthService.request_password_reset's own docstring. "
        "Unauthenticated by design."
    ),
)
def forgot_password(
    data: ForgotPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
    email_sender: PasswordResetEmailSender = Depends(get_password_reset_email_sender),
) -> ForgotPasswordResponse:
    result = auth_service.request_password_reset(email=data.email)
    _deliver_password_reset_code(result, email=data.email, sender=email_sender)
    return ForgotPasswordResponse(message=_FORGOT_PASSWORD_GENERIC_MESSAGE)


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Redeem a verification code and set a new password",
    description=(
        "Unauthenticated by design - the 6-digit code is the credential "
        "here, not a bearer token (a user who can't log in is exactly who "
        "needs this). Every rejection reason - wrong code, expired, "
        "already used, superseded by a newer code, attempts exhausted, "
        "unknown email, or an inactive account - maps to the identical "
        "generic 400 below; none of that distinction is ever exposed. "
        "Success revokes every refresh token this account has and issues "
        "no token pair - the user must log in again with the new "
        "password, exactly like any other device that's been logged out."
    ),
)
def reset_password(data: ResetPasswordRequest, auth_service: AuthService = Depends(get_auth_service)) -> None:
    try:
        auth_service.reset_password(email=data.email, code=data.code, new_password=data.new_password)
    except InvalidResetCodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except WeakNewPasswordError as exc:
        # Defense in depth only - ResetPasswordRequest.new_password's own
        # Field(min_length=8) already rejects this with a 422 before the
        # service is ever called (see that schema's docstring). Safe to
        # expose as-is: this message only states the length policy, never
        # anything about account/code state or the password's content.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
