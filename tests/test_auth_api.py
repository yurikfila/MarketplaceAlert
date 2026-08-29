"""HTTP tests for `/api/v1/auth*` (`marketplace_alert/api/v1/auth.py`).

Uses the same `client`/`db_session` fixtures as every other API test
(`tests/conftest.py`) - an isolated temp database, never the developer's
real one. `core/auth/service.py`'s own test suites
(`tests/test_auth_service.py` etc.) already cover the business logic
exhaustively; these tests are about the HTTP contract on top of it: status
codes, response shapes, headers, and that nothing sensitive ever reaches a
response body.
"""

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.auth.models import RefreshToken, User
from marketplace_alert.core.auth.security import create_access_token, hash_token


def _signup(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def _login(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


# =====================================================================
# Signup
# =====================================================================


def test_signup_returns_201_with_user_and_tokens(client) -> None:
    response = _signup(client)
    assert response.status_code == 201
    body = response.json()

    assert body["user"]["email"] == "person@example.com"
    assert isinstance(body["user"]["id"], int)
    assert "created_at" in body["user"]
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["tokens"]["token_type"] == "bearer"


def test_signup_normalizes_email(client) -> None:
    response = _signup(client, email="  Person@Example.COM  ")
    assert response.status_code == 201
    assert response.json()["user"]["email"] == "person@example.com"


def test_signup_persists_a_user_row(client, db_session) -> None:
    _signup(client)
    assert db_session.query(User).filter_by(email="person@example.com").count() == 1


def test_signup_rejects_a_too_short_password(client) -> None:
    response = client.post("/api/v1/auth/signup", json={"email": "person@example.com", "password": "short"})
    assert response.status_code == 422


def test_duplicate_signup_returns_409_without_leaking_db_internals(client) -> None:
    _signup(client, email="dup@example.com")

    response = _signup(client, email="DUP@example.com")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "IntegrityError" not in detail
    assert "UNIQUE constraint" not in detail
    assert "sqlite" not in detail.lower()
    assert "sql" not in detail.lower()


def test_duplicate_signup_does_not_create_a_second_user(client, db_session) -> None:
    _signup(client, email="dup@example.com")
    _signup(client, email="dup@example.com")

    assert db_session.query(User).filter_by(email="dup@example.com").count() == 1


# =====================================================================
# Login
# =====================================================================


def test_login_success_returns_user_and_tokens(client) -> None:
    _signup(client)
    response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "person@example.com"
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]


def test_login_with_wrong_password_returns_generic_401(client) -> None:
    _signup(client)
    response = _login(client, password="wrong-password")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_login_generic_failure_has_no_www_authenticate_header(client) -> None:
    """Unlike /me, /login's credential travels in the request body, not
    the Authorization header - a WWW-Authenticate challenge would be
    semantically wrong here."""
    response = _login(client, email="nobody@example.com", password="anything")
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_login_nonexistent_wrong_password_inactive_and_locked_are_indistinguishable_over_http(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core hardening requirement, proven at the HTTP layer: all four
    rejection reasons must produce byte-identical status/detail."""
    monkeypatch.setattr(settings, "max_failed_login_attempts", 1)

    _signup(client, email="wrongpw@example.com", password="correct-password")

    inactive_user = _signup(client, email="inactive@example.com", password="correct-password").json()
    user_row = db_session.query(User).filter_by(email="inactive@example.com").one()
    user_row.is_active = False
    db_session.commit()

    _signup(client, email="locked@example.com", password="correct-password")
    lock_trip = _login(client, email="locked@example.com", password="wrong")
    assert lock_trip.status_code == 401  # sanity: the lock-tripping attempt itself is rejected normally

    unknown_response = _login(client, email="nobody@example.com", password="anything")
    wrong_password_response = _login(client, email="wrongpw@example.com", password="not-the-password")
    inactive_response = _login(client, email="inactive@example.com", password="correct-password")
    locked_response = _login(client, email="locked@example.com", password="correct-password")

    responses = [unknown_response, wrong_password_response, inactive_response, locked_response]
    assert all(r.status_code == 401 for r in responses)
    details = {r.json()["detail"] for r in responses}
    assert details == {"Invalid email or password"}
    assert inactive_user["user"]["email"] == "inactive@example.com"  # sanity: signup itself worked


# =====================================================================
# Refresh
# =====================================================================


def test_refresh_returns_a_new_token_pair(client) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token"] != refresh_token


def test_refresh_rotation_revokes_the_old_token(client, db_session) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    stored = db_session.query(RefreshToken).filter_by(token_hash=hash_token(refresh_token)).one()
    assert stored.revoked_at is not None


def test_refresh_with_an_unrecognized_token_returns_401(client) -> None:
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "never-issued"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired refresh token"


def test_refresh_with_an_expired_token_returns_401(client, db_session) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    stored = db_session.query(RefreshToken).filter_by(token_hash=hash_token(refresh_token)).one()
    from datetime import datetime, timedelta, timezone

    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired refresh token"


def test_refresh_with_a_revoked_token_returns_401(client) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 401


def test_refresh_reuse_returns_401_and_revokes_every_active_token(client, db_session) -> None:
    signup_body = _signup(client).json()
    original_refresh_token = signup_body["tokens"]["refresh_token"]

    first_rotation = client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh_token}).json()

    reuse_response = client.post("/api/v1/auth/refresh", json={"refresh_token": original_refresh_token})

    assert reuse_response.status_code == 401
    assert reuse_response.json()["detail"] == "Invalid or expired refresh token"

    rotated_token_row = db_session.query(RefreshToken).filter_by(
        token_hash=hash_token(first_rotation["refresh_token"])
    ).one()
    assert rotated_token_row.revoked_at is not None  # killed by the reuse-detection side effect


# =====================================================================
# Logout
# =====================================================================


def test_logout_returns_204_and_revokes_the_token(client, db_session) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    response = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

    assert response.status_code == 204
    assert response.content == b""
    stored = db_session.query(RefreshToken).filter_by(token_hash=hash_token(refresh_token)).one()
    assert stored.revoked_at is not None


def test_logout_is_idempotent(client) -> None:
    signup_body = _signup(client).json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    first = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    second = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

    assert first.status_code == 204
    assert second.status_code == 204


def test_logout_with_an_unknown_token_still_returns_204(client) -> None:
    response = client.post("/api/v1/auth/logout", json={"refresh_token": "never-issued"})
    assert response.status_code == 204


# =====================================================================
# /me
# =====================================================================


def test_me_returns_the_authenticated_user(client) -> None:
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "person@example.com"
    assert body["id"] == signup_body["user"]["id"]


def test_me_without_authorization_header_returns_401_with_www_authenticate(client) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_wrong_auth_scheme_returns_401_with_www_authenticate(client) -> None:
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_an_expired_token_returns_401_with_www_authenticate(client) -> None:
    signup_body = _signup(client).json()
    user_id = signup_body["user"]["id"]
    expired_token = create_access_token(user_id, secret_key=settings.jwt_secret_key, expire_minutes=-1)

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_a_tampered_token_returns_401_with_www_authenticate(client) -> None:
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]
    header, payload, signature = access_token.split(".")
    tampered = f"{header}.{payload}Z.{signature}"

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tampered}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_a_malformed_token_returns_401_with_www_authenticate(client) -> None:
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt-at-all"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_a_token_naming_a_nonexistent_user_returns_401(client) -> None:
    token = create_access_token(999999, secret_key=settings.jwt_secret_key, expire_minutes=30)

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_an_inactive_users_token_returns_401(client, db_session) -> None:
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]
    user_row = db_session.query(User).filter_by(email="person@example.com").one()
    user_row.is_active = False
    db_session.commit()

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_401_reasons_that_involve_a_token_share_the_same_generic_detail(client) -> None:
    """A caller who *did* send some token should not be able to tell
    "expired" from "tampered" from "no such user" apart - same
    uniform-failure principle as login. A request with no token at all is
    a different situation (nothing about another account is at stake) and
    is allowed its own, equally generic, message - tested separately."""
    malformed = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    nonexistent_user_token = create_access_token(999999, secret_key=settings.jwt_secret_key, expire_minutes=30)
    nonexistent_user = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {nonexistent_user_token}"}
    )
    expired_token = create_access_token(1, secret_key=settings.jwt_secret_key, expire_minutes=-1)
    expired = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired_token}"})

    responses = [malformed, nonexistent_user, expired]
    assert all(r.status_code == 401 for r in responses)
    assert len({r.json()["detail"] for r in responses}) == 1


def test_me_missing_token_detail_reveals_nothing_account_specific(client) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


# =====================================================================
# Response schemas never leak sensitive fields
# =====================================================================


_FORBIDDEN_FIELD_NAMES = ("password", "password_hash", "token_hash", "failed_login_attempts", "locked_until")


def test_signup_response_contains_no_sensitive_fields(client) -> None:
    body = _signup(client).json()
    flattened = str(body).lower()
    for forbidden in _FORBIDDEN_FIELD_NAMES:
        assert forbidden not in flattened


def test_login_response_contains_no_sensitive_fields(client) -> None:
    _signup(client)
    body = _login(client).json()
    flattened = str(body).lower()
    for forbidden in _FORBIDDEN_FIELD_NAMES:
        assert forbidden not in flattened


def test_me_response_contains_no_sensitive_fields(client) -> None:
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    body = response.json()

    assert set(body.keys()) == {"id", "email", "created_at"}


def test_me_response_is_active_not_exposed(client) -> None:
    signup_body = _signup(client).json()
    access_token = signup_body["tokens"]["access_token"]

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})

    assert "is_active" not in response.json()
