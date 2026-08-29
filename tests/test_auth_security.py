"""Tests for `core/auth/security.py`: password hashing/verification, JWT
access tokens, and opaque refresh/reset-token generation/hashing.

Pure unit tests - no database, no service layer. `tests/test_auth_service.py`
covers how `AuthService` composes these; this file covers each primitive
in isolation, including the failure modes a hand-rolled JWT/hashing bug
could plausibly introduce (wrong algorithm, tampering, missing claims).
"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from marketplace_alert.core.auth import security
from marketplace_alert.core.auth.security import (
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
    verify_password_or_dummy,
)

_SECRET = "test-secret-key-at-least-32-characters-long"


# =====================================================================
# Password hashing
# =====================================================================


def test_hash_password_does_not_return_the_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert "correct horse battery staple" not in hashed


def test_verify_password_accepts_the_correct_password() -> None:
    hashed = hash_password("my-password-123")
    assert verify_password("my-password-123", hashed) is True


def test_verify_password_rejects_the_wrong_password() -> None:
    hashed = hash_password("my-password-123")
    assert verify_password("a-different-password", hashed) is False


def test_verify_password_rejects_a_malformed_hash_without_raising() -> None:
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_hash_password_is_salted_so_two_hashes_of_the_same_password_differ() -> None:
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


# =====================================================================
# verify_password_or_dummy: the account-enumeration timing guard
# =====================================================================


def test_verify_password_or_dummy_with_a_real_hash_behaves_like_verify_password() -> None:
    hashed = hash_password("real-password")
    assert verify_password_or_dummy("real-password", hashed) is True
    assert verify_password_or_dummy("wrong", hashed) is False


def test_verify_password_or_dummy_with_none_never_matches_anything() -> None:
    assert verify_password_or_dummy("anything at all", None) is False
    assert verify_password_or_dummy("", None) is False


def test_verify_password_or_dummy_with_none_still_performs_a_real_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: it must not shortcut/return early for a
    nonexistent account - it has to actually call into the same
    bcrypt-backed `verify_password` either way, so the two cases take
    roughly the same time."""
    calls = []
    real_verify = security.verify_password

    def spy(password, password_hash):
        calls.append((password, password_hash))
        return real_verify(password, password_hash)

    monkeypatch.setattr(security, "verify_password", spy)
    security.verify_password_or_dummy("anything", None)

    assert len(calls) == 1
    assert calls[0][1] is not None  # a real hash was passed, not skipped


def test_verify_password_or_dummy_caches_the_dummy_hash_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "_dummy_password_hash", None)

    security.verify_password_or_dummy("first call", None)
    first_cached = security._dummy_password_hash
    assert first_cached is not None

    security.verify_password_or_dummy("second call", None)
    assert security._dummy_password_hash == first_cached


# =====================================================================
# JWT access tokens
# =====================================================================


def test_create_and_decode_access_token_round_trips() -> None:
    token = create_access_token(42, secret_key=_SECRET, expire_minutes=30)
    assert decode_access_token(token, secret_key=_SECRET) == 42


def test_access_token_claims_are_minimal() -> None:
    token = create_access_token(1, secret_key=_SECRET, expire_minutes=30)
    payload = pyjwt.decode(token, _SECRET, algorithms=["HS256"])
    assert set(payload.keys()) == {"sub", "iat", "exp"}
    assert payload["sub"] == "1"


def test_expired_access_token_is_rejected() -> None:
    token = create_access_token(1, secret_key=_SECRET, expire_minutes=-1)
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


def test_tampered_access_token_is_rejected() -> None:
    token = create_access_token(1, secret_key=_SECRET, expire_minutes=30)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}Z.{signature}"

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(tampered, secret_key=_SECRET)


def test_access_token_signed_with_a_different_secret_is_rejected() -> None:
    token = create_access_token(1, secret_key="some-other-secret-key-32-chars-long", expire_minutes=30)
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


def test_malformed_access_token_is_rejected() -> None:
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token("not-a-jwt-at-all", secret_key=_SECRET)


def test_empty_access_token_is_rejected() -> None:
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token("", secret_key=_SECRET)


def test_access_token_with_a_different_algorithm_is_rejected() -> None:
    """Explicit algorithm allow-listing (`algorithms=["HS256"]` in
    `decode_access_token`) is what prevents an algorithm-confusion
    attack - proven here by signing with a different, still-symmetric
    algorithm using the same secret."""
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {"sub": "1", "iat": now, "exp": now + timedelta(minutes=30)}, _SECRET, algorithm="HS512"
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


def test_access_token_with_alg_none_is_rejected() -> None:
    """The classic JWT "alg: none" attack - a token that claims to need
    no signature verification at all. PyJWT refuses to decode this
    without an explicit opt-in `decode_access_token` never grants."""
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {"sub": "1", "iat": now, "exp": now + timedelta(minutes=30)}, key=None, algorithm="none"
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


def test_access_token_missing_subject_claim_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    token = pyjwt.encode({"iat": now, "exp": now + timedelta(minutes=30)}, _SECRET, algorithm="HS256")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


def test_access_token_with_a_non_numeric_subject_claim_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    token = pyjwt.encode(
        {"sub": "not-a-number", "iat": now, "exp": now + timedelta(minutes=30)}, _SECRET, algorithm="HS256"
    )
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, secret_key=_SECRET)


# =====================================================================
# Opaque tokens (refresh / password reset)
# =====================================================================


def test_generate_token_returns_a_sufficiently_long_random_string() -> None:
    token = generate_token()
    assert len(token) >= 32


def test_generate_token_is_random_across_calls() -> None:
    assert generate_token() != generate_token()


def test_hash_token_is_deterministic() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)


def test_hash_token_differs_for_different_inputs() -> None:
    assert hash_token(generate_token()) != hash_token(generate_token())


def test_hash_token_never_returns_the_raw_input() -> None:
    token = generate_token()
    assert hash_token(token) != token


def test_hash_token_produces_a_sha256_hex_digest() -> None:
    digest = hash_token("some-token-value")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
