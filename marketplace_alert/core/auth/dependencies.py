"""`get_current_user`: the one FastAPI dependency that turns an
`Authorization: Bearer <access_token>` header into a `User`. `require_admin`
layers one further check on top: that resolved user must also have
`is_admin=True`, or the request is rejected outright.

Lives here (not `marketplace_alert/dependencies.py`) because it's real
security logic - header extraction, scheme validation, the
`WWW-Authenticate` challenge header - not just service wiring; everything
`marketplace_alert/dependencies.py` otherwise contains is a one-line
constructor call. `AuthService` itself (via `get_auth_service`) still
does all the actual token verification - this module's only job is
FastAPI-shaped request plumbing around that one call.

**Not used to protect anything beyond `/me` and the admin API.** Only
`GET /api/v1/auth/me` (`get_current_user`) and every `/api/v1/admin/*`
route (`require_admin`) depend on either of these in this phase. Route
protection for saved searches/listings is explicitly later work (see
PROJECT_CONTEXT.md's authentication design decision) - `get_current_user`
is ready for that, but nothing else wires it in yet.
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


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Everything `get_current_user` already guarantees (a valid, active
    account resolved from a genuine access token), plus `is_admin=True`
    on that same account - the one and only place this codebase checks
    that field. Rejects a non-admin (but otherwise perfectly authenticated)
    account with `403 Forbidden`, not `401` - the caller *is* who they say
    they are; they're simply not authorized for this resource, the
    RFC-correct distinction (`_unauthorized` above stays reserved for "we
    don't know who you are").

    Reads `is_admin` exclusively off the `User` row `get_current_user`
    already resolved server-side from a verified bearer token - never
    from a request header, query parameter, or request body field a
    client could supply, and never inferred from the account's email
    address. There is no request shape that can make this pass without
    that column already being `True` in the database - see
    `core/auth/models.py`'s `User.is_admin` docstring and
    `scripts/set_admin.py` for the only way it ever becomes `True`.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return current_user
