"""`get_current_user`: the one FastAPI dependency that turns an
`Authorization: Bearer <access_token>` header into a `User`.

Lives here (not `marketplace_alert/dependencies.py`) because it's real
security logic - header extraction, scheme validation, the
`WWW-Authenticate` challenge header - not just service wiring; everything
`marketplace_alert/dependencies.py` otherwise contains is a one-line
constructor call. `AuthService` itself (via `get_auth_service`) still
does all the actual token verification - this module's only job is
FastAPI-shaped request plumbing around that one call.

**Not used to protect anything yet** - only `GET /api/v1/auth/me`
(`api/v1/auth.py`) depends on this in this phase. Route protection for
saved searches/listings is explicitly later work (see PROJECT_CONTEXT.md's
authentication design decision) - this dependency is ready for that, but
nothing wires it in yet.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.security import InvalidAccessTokenError
from marketplace_alert.core.auth.service import AuthService
from marketplace_alert.dependencies import get_auth_service

# auto_error=False: FastAPI's own default (auto_error=True) raises 403 for
# a missing/malformed Authorization header, which is the wrong status for
# "you need to authenticate" (403 means "authenticated but not allowed" -
# 401 is the RFC-correct response here, with a WWW-Authenticate challenge
# telling the client how). Disabling FastAPI's built-in error means this
# module raises its own 401 uniformly, for every failure reason, below.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """Every rejection reason (missing header, wrong scheme, malformed
    token, tampered signature, expired, or a token naming a gone/inactive
    account) raises this same shape - one status code, one header, never
    a reason-specific hint about *why* the token didn't work."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    """Resolve the calling user from the request's bearer access token.

    `credentials is None` covers a missing header entirely, and a header
    present but not using the `Bearer` scheme (`HTTPBearer` itself treats
    both as "no credentials" when `auto_error=False`). Everything else -
    malformed, tampered, wrong algorithm, expired, or naming a user that
    no longer exists or is no longer active - is `AuthService.
    get_current_user`'s job (`core/auth/security.py`'s `decode_access_token`
    underneath it), which raises exactly one exception type
    (`InvalidAccessTokenError`) for all of them - never trusting anything
    about the token beyond the `sub`/`iat`/`exp` claims that function
    itself validates.
    """
    if credentials is None:
        raise _unauthorized("Not authenticated")

    try:
        return auth_service.get_current_user(credentials.credentials)
    except InvalidAccessTokenError:
        raise _unauthorized("Invalid or expired access token") from None
