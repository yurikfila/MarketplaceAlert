"""`/api/v1/auth*` - signup, login, refresh, logout, and the current-user
check, over the existing `AuthService` (`core/auth/service.py`).

Thin by design (see that module's own docstring for the full business/
security rules): every route here validates its request shape via the
schemas in `api/v1/schemas.py`, calls exactly one `AuthService` method,
and maps whatever it raises to an HTTP response - no business or security
logic is duplicated here. `GET /me` additionally depends on
`core/auth/dependencies.py`'s `get_current_user` for bearer-token
extraction/verification.

**Nothing outside these five routes is protected yet.** Saved-search and
listing routes remain exactly as open as before this router exists - see
PROJECT_CONTEXT.md's authentication design decision for the phased plan.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from marketplace_alert.api.v1.schemas import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPairOut,
    UserPublic,
)
from marketplace_alert.core.auth.dependencies import get_current_user
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.service import (
    AuthService,
    EmailAlreadyRegisteredError,
    ExpiredRefreshTokenError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReusedError,
    TokenPair,
)
from marketplace_alert.dependencies import get_auth_service

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
