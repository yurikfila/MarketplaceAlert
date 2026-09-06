"""HTTP tests for `/api/v1/auth*` (`marketplace_alert/api/v1/auth.py`).

Uses the same `client`/`db_session` fixtures as every other API test
(`tests/conftest.py`) - an isolated temp database, never the developer's
real one. `core/auth/service.py`'s own test suites
(`tests/test_auth_service.py` etc.) already cover the business logic
exhaustively; these tests are about the HTTP contract on top of it: status
codes, response shapes, headers, and that nothing sensitive ever reaches a
response body.
"""

from datetime import datetime, timedelta, timezone

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.auth import service as auth_service_module
from marketplace_alert.core.auth.models import PasswordResetToken, RefreshToken, User
from marketplace_alert.core.auth.security import create_access_token, hash_token

_FORGOT_PASSWORD_GENERIC_BODY = {"message": "If that email is registered, a verification code has been sent."}


def _signup(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def _login(client, email="person@example.com", password="a-strong-password"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _forgot_password(client, email="person@example.com"):
    return client.post("/api/v1/auth/forgot-password", json={"email": email})


def _reset_password(client, email="person@example.com", code="123456", new_password="a-new-strong-password"):
    return client.post(
        "/api/v1/auth/reset-password", json={"email": email, "code": code, "new_password": new_password}
    )


def _issue_known_code(client, monkeypatch: pytest.MonkeyPatch, email: str, code: str) -> None:
    """Forces the /forgot-password endpoint's underlying `AuthService.
    request_password_reset` to issue a known, fixed code - the only way
    an HTTP-level test can complete a full reset-password round trip,
    since the real endpoint never reveals the raw code by design (see
    "Raw code non-exposure" below)."""
    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: code)
    response = _forgot_password(client, email=email)
    assert response.status_code == 200


def _live_reset_token(db_session, email: str) -> PasswordResetToken:
    user = db_session.query(User).filter_by(email=email.lower()).one()
    return (
        db_session.query(PasswordResetToken)
        .filter_by(user_id=user.id)
        .order_by(PasswordResetToken.created_at.desc())
        .first()
    )


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


# =====================================================================
# Forgot password - enumeration safety
# =====================================================================


def test_forgot_password_returns_the_generic_response_for_a_real_active_user(client) -> None:
    _signup(client, email="forgot-active@example.com")
    response = _forgot_password(client, email="forgot-active@example.com")

    assert response.status_code == 200
    assert response.json() == _FORGOT_PASSWORD_GENERIC_BODY


def test_forgot_password_returns_the_identical_response_for_an_unknown_email(client) -> None:
    response = _forgot_password(client, email="never-signed-up@example.com")

    assert response.status_code == 200
    assert response.json() == _FORGOT_PASSWORD_GENERIC_BODY


def test_forgot_password_returns_the_identical_response_for_an_inactive_user(client, db_session) -> None:
    _signup(client, email="forgot-inactive@example.com")
    user = db_session.query(User).filter_by(email="forgot-inactive@example.com").one()
    user.is_active = False
    db_session.commit()

    response = _forgot_password(client, email="forgot-inactive@example.com")

    assert response.status_code == 200
    assert response.json() == _FORGOT_PASSWORD_GENERIC_BODY


def test_forgot_password_returns_the_identical_response_during_the_resend_cooldown(client) -> None:
    _signup(client, email="forgot-cooldown@example.com")
    first = _forgot_password(client, email="forgot-cooldown@example.com")
    second = _forgot_password(client, email="forgot-cooldown@example.com")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == _FORGOT_PASSWORD_GENERIC_BODY


def test_forgot_password_returns_the_identical_response_once_the_hourly_cap_is_reached(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "password_reset_max_per_hour", 1)
    monkeypatch.setattr(settings, "password_reset_resend_cooldown_seconds", 0)
    _signup(client, email="forgot-hourly@example.com")

    first = _forgot_password(client, email="forgot-hourly@example.com")
    second = _forgot_password(client, email="forgot-hourly@example.com")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == _FORGOT_PASSWORD_GENERIC_BODY


def test_forgot_password_every_case_produces_byte_identical_responses(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core enumeration-safety requirement, proven directly across
    every suppression reason at once: status code, JSON body, and
    response schema (key set) are all identical regardless of account
    state - never compares wall-clock latency, only the response itself."""
    monkeypatch.setattr(settings, "password_reset_max_per_hour", 1)
    monkeypatch.setattr(settings, "password_reset_resend_cooldown_seconds", 0)

    _signup(client, email="enum-active@example.com")

    _signup(client, email="enum-inactive@example.com")
    inactive_user = db_session.query(User).filter_by(email="enum-inactive@example.com").one()
    inactive_user.is_active = False
    db_session.commit()

    _signup(client, email="enum-cooldown@example.com")
    _forgot_password(client, email="enum-cooldown@example.com")  # primes the cooldown

    _signup(client, email="enum-hourly@example.com")
    _forgot_password(client, email="enum-hourly@example.com")  # uses up the 1-per-hour cap

    responses = [
        _forgot_password(client, email="enum-active@example.com"),
        _forgot_password(client, email="never-registered-at-all@example.com"),
        _forgot_password(client, email="enum-inactive@example.com"),
        _forgot_password(client, email="enum-cooldown@example.com"),
        _forgot_password(client, email="enum-hourly@example.com"),
    ]

    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert all(body == _FORGOT_PASSWORD_GENERIC_BODY for body in bodies)
    assert all(set(body.keys()) == {"message"} for body in bodies)


# =====================================================================
# Raw code non-exposure - CRITICAL
# =====================================================================


def test_forgot_password_response_never_contains_the_raw_code(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: "483921")
    _signup(client, email="raw-code-leak@example.com")

    response = _forgot_password(client, email="raw-code-leak@example.com")

    assert "483921" not in response.text
    assert "raw_code" not in response.text
    assert "should_deliver" not in response.text
    assert set(response.json().keys()) == {"message"}


def test_forgot_password_response_headers_never_contain_the_raw_code(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: "111999")
    _signup(client, email="raw-code-headers@example.com")

    response = _forgot_password(client, email="raw-code-headers@example.com")

    assert "111999" not in str(response.headers)


def test_reset_password_error_response_never_contains_the_raw_code(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signup(client, email="raw-code-error@example.com")
    _issue_known_code(client, monkeypatch, "raw-code-error@example.com", code="246810")

    response = _reset_password(
        client, email="raw-code-error@example.com", code="999999", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert "246810" not in response.text


def test_forgot_password_never_logs_the_raw_code(
    client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: "135791")
    _signup(client, email="raw-code-log@example.com")

    with caplog.at_level("DEBUG"):
        _forgot_password(client, email="raw-code-log@example.com")

    assert "135791" not in caplog.text


# =====================================================================
# OpenAPI schema - only public fields exposed
# =====================================================================


def test_openapi_forgot_password_request_schema_contains_only_email(client) -> None:
    schema = client.get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["ForgotPasswordRequest"]
    assert set(request_schema["properties"].keys()) == {"email"}


def test_openapi_forgot_password_response_schema_contains_only_message(client) -> None:
    schema = client.get("/openapi.json").json()
    response_schema = schema["components"]["schemas"]["ForgotPasswordResponse"]
    assert set(response_schema["properties"].keys()) == {"message"}


def test_openapi_reset_password_request_schema_contains_only_expected_fields(client) -> None:
    schema = client.get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["ResetPasswordRequest"]
    assert set(request_schema["properties"].keys()) == {"email", "code", "new_password"}


def test_openapi_has_no_schema_named_after_the_internal_result_type(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "PasswordResetRequestResult" not in schema["components"]["schemas"]


def test_openapi_contains_no_raw_code_or_should_deliver_anywhere(client) -> None:
    """Blunt but effective - the entire generated OpenAPI document,
    flattened, must never mention either internal field name anywhere."""
    schema_text = str(client.get("/openapi.json").json())
    assert "raw_code" not in schema_text
    assert "should_deliver" not in schema_text


# =====================================================================
# Reset password - success
# =====================================================================


def test_reset_password_success_returns_204_with_empty_body(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client, email="reset-success@example.com", password="the-old-password")
    _issue_known_code(client, monkeypatch, "reset-success@example.com", code="111222")

    response = _reset_password(
        client, email="reset-success@example.com", code="111222", new_password="a-new-strong-password"
    )

    assert response.status_code == 204
    assert response.content == b""


def test_reset_password_old_password_no_longer_works(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client, email="reset-old-pw@example.com", password="the-old-password")
    _issue_known_code(client, monkeypatch, "reset-old-pw@example.com", code="222333")
    _reset_password(client, email="reset-old-pw@example.com", code="222333", new_password="a-new-strong-password")

    response = _login(client, email="reset-old-pw@example.com", password="the-old-password")
    assert response.status_code == 401


def test_reset_password_new_password_works(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client, email="reset-new-pw@example.com", password="the-old-password")
    _issue_known_code(client, monkeypatch, "reset-new-pw@example.com", code="333444")
    _reset_password(client, email="reset-new-pw@example.com", code="333444", new_password="a-new-strong-password")

    response = _login(client, email="reset-new-pw@example.com", password="a-new-strong-password")
    assert response.status_code == 200


def test_reset_password_revokes_refresh_tokens(client, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
    signup_body = _signup(client, email="reset-revoke@example.com", password="the-old-password").json()
    refresh_token = signup_body["tokens"]["refresh_token"]

    _issue_known_code(client, monkeypatch, "reset-revoke@example.com", code="444555")
    _reset_password(client, email="reset-revoke@example.com", code="444555", new_password="a-new-strong-password")

    stored = db_session.query(RefreshToken).filter_by(token_hash=hash_token(refresh_token)).one()
    assert stored.revoked_at is not None


def test_reset_password_does_not_return_a_token_pair_or_any_body(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No auto-login, no session restoration - the user must sign back in
    with the new password, exactly like this task requires."""
    _signup(client, email="reset-no-token@example.com", password="the-old-password")
    _issue_known_code(client, monkeypatch, "reset-no-token@example.com", code="555666")

    response = _reset_password(
        client, email="reset-no-token@example.com", code="555666", new_password="a-new-strong-password"
    )

    assert response.status_code == 204
    assert response.content == b""


# =====================================================================
# Reset password - generic failure mapping
# =====================================================================


def test_reset_password_wrong_code_returns_generic_400(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client, email="invalid-wrong@example.com")
    _issue_known_code(client, monkeypatch, "invalid-wrong@example.com", code="111111")

    response = _reset_password(
        client, email="invalid-wrong@example.com", code="999999", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_expired_code_returns_the_same_generic_400(
    client, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    _signup(client, email="invalid-expired@example.com")
    _issue_known_code(client, monkeypatch, "invalid-expired@example.com", code="222222")

    row = _live_reset_token(db_session, "invalid-expired@example.com")
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    response = _reset_password(
        client, email="invalid-expired@example.com", code="222222", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_already_used_code_returns_the_same_generic_400(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signup(client, email="invalid-used@example.com")
    _issue_known_code(client, monkeypatch, "invalid-used@example.com", code="333333")
    _reset_password(client, email="invalid-used@example.com", code="333333", new_password="first-new-password")

    response = _reset_password(
        client, email="invalid-used@example.com", code="333333", new_password="second-new-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_superseded_code_returns_the_same_generic_400(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "password_reset_resend_cooldown_seconds", 0)
    _signup(client, email="invalid-superseded@example.com")
    _issue_known_code(client, monkeypatch, "invalid-superseded@example.com", code="444444")
    _issue_known_code(client, monkeypatch, "invalid-superseded@example.com", code="555555")  # supersedes 444444

    response = _reset_password(
        client, email="invalid-superseded@example.com", code="444444", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_attempts_exhausted_returns_the_same_generic_400_even_with_the_correct_code(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "password_reset_max_attempts", 2)
    _signup(client, email="invalid-exhausted@example.com")
    _issue_known_code(client, monkeypatch, "invalid-exhausted@example.com", code="666666")

    _reset_password(client, email="invalid-exhausted@example.com", code="000000", new_password="a-new-strong-password")
    _reset_password(client, email="invalid-exhausted@example.com", code="000001", new_password="a-new-strong-password")

    response = _reset_password(
        client, email="invalid-exhausted@example.com", code="666666", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_unknown_email_returns_the_same_generic_400(client) -> None:
    response = _reset_password(
        client, email="never-registered-anywhere@example.com", code="123456", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


def test_reset_password_inactive_account_returns_the_same_generic_400(
    client, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    _signup(client, email="invalid-inactive@example.com")
    _issue_known_code(client, monkeypatch, "invalid-inactive@example.com", code="777777")
    user = db_session.query(User).filter_by(email="invalid-inactive@example.com").one()
    user.is_active = False
    db_session.commit()

    response = _reset_password(
        client, email="invalid-inactive@example.com", code="777777", new_password="a-new-strong-password"
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification code"


# =====================================================================
# Reset password - validation
# =====================================================================


def test_reset_password_code_shorter_than_six_is_rejected(client) -> None:
    response = _reset_password(client, code="12345")
    assert response.status_code == 422


def test_reset_password_code_longer_than_six_is_rejected(client) -> None:
    response = _reset_password(client, code="1234567")
    assert response.status_code == 422


def test_reset_password_code_with_letters_is_rejected(client) -> None:
    response = _reset_password(client, code="12345a")
    assert response.status_code == 422


def test_reset_password_code_with_a_leading_zero_is_structurally_accepted(client) -> None:
    """Structurally valid (passes schema validation, reaches the service)
    even with no matching account - proves a leading zero is never
    stripped/rejected just for being a leading zero. The 400 that follows
    is the ordinary "no such code" rejection, not a validation error -
    proving the request shape itself was accepted."""
    response = _reset_password(
        client, email="nobody-with-this-email@example.com", code="000483", new_password="a-new-strong-password"
    )
    assert response.status_code == 400


def test_reset_password_new_password_shorter_than_eight_is_rejected(client) -> None:
    response = _reset_password(client, new_password="short")
    assert response.status_code == 422


def test_reset_password_malformed_request_body_returns_422(client) -> None:
    response = client.post("/api/v1/auth/reset-password", json={"email": "person@example.com"})
    assert response.status_code == 422


# =====================================================================
# Auth requirements - unauthenticated by design
# =====================================================================


def test_forgot_password_requires_no_authorization_header(client) -> None:
    response = client.post("/api/v1/auth/forgot-password", json={"email": "no-auth-needed@example.com"})
    assert response.status_code == 200


def test_reset_password_requires_no_authorization_header(client) -> None:
    response = client.post(
        "/api/v1/auth/reset-password",
        json={"email": "no-auth-needed@example.com", "code": "123456", "new_password": "a-new-strong-password"},
    )
    # Unauthenticated and reachable - rejected for account/code reasons
    # (generic 400), never for a missing Authorization header (401/403).
    assert response.status_code == 400
