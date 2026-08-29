"""Cryptographic primitives for authentication: password hashing, JWT
access tokens, and refresh-token generation/hashing.

Deliberately the only module in this package that imports `passlib` or
`jwt` directly - `core/auth/service.py` orchestrates business logic on top
of these functions, but never touches a hashing/signing library itself.
Same separation this codebase already uses for connectors/notification
providers: one place owns a third-party dependency, everything else
depends on a plain function/interface.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

# --- Passwords ---------------------------------------------------------
#
# bcrypt only, via passlib's CryptContext - `deprecated="auto"` means a
# hash produced by a scheme this context no longer prefers (irrelevant
# today, with only one scheme registered, but free future-proofing: a
# later scheme change - e.g. to argon2id - can be added here without a
# data migration, since CryptContext re-hashes transparently on next
# successful login, not something this module has to orchestrate itself).
_password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# A verify() call against a password that could never match, run whenever
# `verify_password` is asked to check a login attempt for an account that
# doesn't exist at all - see `verify_password_or_dummy`'s docstring. Hashed
# once, lazily, at first use (not at import time - keeps module import
# itself cheap, and avoids paying bcrypt's cost during e.g. `pytest`
# collection for tests that never call this at all).
_DUMMY_PASSWORD = "this-password-can-never-match-anything-3f8a1c"
_dummy_password_hash: str | None = None


def hash_password(password: str) -> str:
    """Hash a plaintext password. Never store or log the plaintext itself."""
    return _password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True if `password` matches `password_hash`. Never raises on a
    malformed/foreign hash - passlib itself treats that as "does not
    match", which is exactly the right behavior here (never let a
    corrupt stored hash turn into a 500 instead of a login failure)."""
    try:
        return _password_context.verify(password, password_hash)
    except ValueError:
        return False


def verify_password_or_dummy(password: str, password_hash: str | None) -> bool:
    """Like `verify_password`, but safe to call even when no account (and
    therefore no real `password_hash`) exists at all - always performs a
    real bcrypt verification either way, against a fixed dummy hash when
    `password_hash` is `None`.

    This is what keeps "email doesn't exist" and "email exists, wrong
    password" taking roughly the same amount of time: bcrypt is
    deliberately slow (~100ms+), so returning early for a nonexistent
    account - skipping the hash comparison entirely - would be a large,
    easily measurable timing signal an attacker could use to enumerate
    which emails have accounts, even though the response body itself
    never says so. `AuthService.login` always calls this rather than
    `verify_password` directly, precisely so no call site can accidentally
    skip it and reintroduce that gap.
    """
    global _dummy_password_hash
    if password_hash is None:
        if _dummy_password_hash is None:
            _dummy_password_hash = hash_password(_DUMMY_PASSWORD)
        password_hash = _dummy_password_hash
    return verify_password(password, password_hash)


# --- JWT access tokens ---------------------------------------------------

_JWT_ALGORITHM = "HS256"


class InvalidAccessTokenError(Exception):
    """Raised for a missing/malformed/tampered/wrong-algorithm/expired
    access token - callers never need to know which; all of them mean
    "this token cannot be trusted", never "please try to interpret it
    anyway"."""


def create_access_token(user_id: int, *, secret_key: str, expire_minutes: float) -> str:
    """Create a signed access token for `user_id`.

    Claims are deliberately minimal - `sub` (the user id, as a string per
    JWT's own convention - `sub` is always a string claim), `iat`, `exp`.
    Nothing else: no email, no role, no arbitrary payload that could grow
    stale relative to the database or leak more than a bearer credential
    needs to.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, secret_key, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, *, secret_key: str) -> int:
    """Verify and decode an access token, returning its `sub` as a user id.

    `algorithms=[_JWT_ALGORITHM]` is passed explicitly - PyJWT then
    rejects any token whose header names a different algorithm (including
    `none`), which is what actually prevents an algorithm-confusion
    attack; it is not merely a default. Every PyJWT failure mode
    (`ExpiredSignatureError`, a bad signature, a malformed token, a
    disallowed algorithm - all subclasses of `InvalidTokenError`) is
    caught here and re-raised as one project-level `InvalidAccessTokenError`
    - callers depend on this module's own exception type, never PyJWT's.
    """
    try:
        payload = jwt.decode(token, secret_key, algorithms=[_JWT_ALGORITHM])
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessTokenError(str(exc)) from exc

    sub = payload.get("sub")
    if sub is None:
        raise InvalidAccessTokenError("Token has no subject claim")
    try:
        return int(sub)
    except (TypeError, ValueError) as exc:
        raise InvalidAccessTokenError("Token subject claim is not a valid user id") from exc


# --- Refresh / password-reset tokens (opaque, hashed at rest) -----------


def generate_token() -> str:
    """A cryptographically random, URL-safe opaque token - what a client
    actually holds (a refresh token, or a password-reset link's token).
    32 random bytes (~256 bits) - comfortably beyond brute-force range,
    matching this project's JWT-secret minimum-strength reasoning."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Hash an opaque token for storage - SHA-256, not bcrypt.

    Deliberately a fast cryptographic hash, not a slow password-hashing
    one: bcrypt's slowness defends a *low-entropy, guessable* secret
    (a human password) against offline brute force. `raw_token` is
    already ~256 bits of real randomness (`generate_token`) - there is no
    dictionary to defend against, so a slow hash would only add cost with
    no security benefit. What still matters, and is still true here: the
    raw token is never stored, only this hash - see this package's
    `models.py` docstring.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
