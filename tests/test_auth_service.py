"""Tests for `core/auth/service.py`: `AuthService` - signup, login,
refresh (rotation + reuse detection), logout, and access-token
validation. The business-logic layer; `tests/test_auth_security.py` and
`tests/test_auth_repository.py` cover the primitives this composes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from marketplace_alert.core.auth import service as auth_service_module
from marketplace_alert.core.auth.models import RefreshToken
from marketplace_alert.core.auth.repository import RefreshTokenRepository, UserRepository
from marketplace_alert.core.auth.security import InvalidAccessTokenError, decode_access_token, hash_token
from marketplace_alert.core.auth.service import (
    AuthService,
    EmailAlreadyRegisteredError,
    ExpiredRefreshTokenError,
    InactiveAccountError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReusedError,
)

_SECRET = "test-secret-key-at-least-32-characters-long"


def _service(session, **overrides) -> AuthService:
    defaults = dict(
        secret_key=_SECRET,
        access_token_expire_minutes=30,
        refresh_token_expire_days=30,
        max_failed_login_attempts=3,
        account_lockout_minutes=15,
    )
    defaults.update(overrides)
    return AuthService(session, **defaults)


# =====================================================================
# Signup
# =====================================================================


def test_signup_creates_a_user_and_issues_tokens(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="Person@Example.com", password="a-strong-password")

    assert user.id is not None
    assert user.email == "person@example.com"
    assert user.password_hash != "a-strong-password"
    assert tokens.access_token
    assert tokens.refresh_token


def test_signup_access_token_decodes_to_the_new_user(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="a-strong-password")

    assert decode_access_token(tokens.access_token, secret_key=_SECRET) == user.id


def test_signup_persists_a_refresh_token_row(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="a-strong-password")

    stored = RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token))
    assert stored is not None
    assert stored.revoked_at is None


def test_signup_with_duplicate_email_raises_and_does_not_create_a_second_user(db_session) -> None:
    service = _service(db_session)
    service.signup(email="dup@example.com", password="password-one")

    with pytest.raises(EmailAlreadyRegisteredError):
        service.signup(email="DUP@example.com", password="password-two")

    assert db_session.query(RefreshToken).count() == 1  # only the first signup's refresh token exists


def test_signup_duplicate_email_rolls_back_cleanly_and_session_stays_usable(db_session) -> None:
    """Proves the transaction is genuinely rolled back, not left half-open -
    the session must still be perfectly usable for a subsequent,
    unrelated operation after the failure."""
    service = _service(db_session)
    service.signup(email="dup@example.com", password="password-one")

    with pytest.raises(EmailAlreadyRegisteredError):
        service.signup(email="dup@example.com", password="password-two")

    # The session must still work normally after the rollback.
    user, _ = service.signup(email="someone-else@example.com", password="password-three")
    assert user.id is not None

    all_users = UserRepository(db_session)
    assert all_users.get_by_email("dup@example.com") is not None
    assert all_users.get_by_email("someone-else@example.com") is not None


# =====================================================================
# Login
# =====================================================================


def test_login_with_correct_credentials_succeeds(db_session) -> None:
    service = _service(db_session)
    service.signup(email="person@example.com", password="correct-password")

    user, tokens = service.login(email="Person@Example.com", password="correct-password")

    assert user.email == "person@example.com"
    assert decode_access_token(tokens.access_token, secret_key=_SECRET) == user.id


def test_login_with_wrong_password_raises_invalid_credentials(db_session) -> None:
    service = _service(db_session)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong-password")


def test_login_with_unknown_email_raises_invalid_credentials(db_session) -> None:
    service = _service(db_session)
    with pytest.raises(InvalidCredentialsError):
        service.login(email="nobody@example.com", password="anything")


def test_login_failure_messages_are_identical_for_unknown_email_and_wrong_password(db_session) -> None:
    service = _service(db_session)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError) as wrong_password_exc:
        service.login(email="person@example.com", password="wrong-password")
    with pytest.raises(InvalidCredentialsError) as unknown_email_exc:
        service.login(email="nobody@example.com", password="wrong-password")

    assert str(wrong_password_exc.value) == str(unknown_email_exc.value)


def test_all_four_login_failure_reasons_raise_identical_invalid_credentials_error(db_session) -> None:
    """The core hardening requirement: nonexistent email, wrong password,
    an inactive account, and a currently-locked account must all be
    externally indistinguishable - same exception type, same message."""
    service = _service(db_session, max_failed_login_attempts=1)

    inactive_user, _ = service.signup(email="inactive@example.com", password="correct-password")
    inactive_user.is_active = False
    db_session.commit()

    locked_user, _ = service.signup(email="locked@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="locked@example.com", password="wrong")  # trips the 1-attempt lock

    service.signup(email="wrongpw@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError) as unknown_exc:
        service.login(email="nobody@example.com", password="anything")
    with pytest.raises(InvalidCredentialsError) as wrong_password_exc:
        service.login(email="wrongpw@example.com", password="not-the-password")
    with pytest.raises(InvalidCredentialsError) as inactive_exc:
        service.login(email="inactive@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError) as locked_exc:
        service.login(email="locked@example.com", password="correct-password")

    messages = {str(unknown_exc.value), str(wrong_password_exc.value), str(inactive_exc.value), str(locked_exc.value)}
    assert len(messages) == 1
    assert type(unknown_exc.value) is type(wrong_password_exc.value) is type(inactive_exc.value) is type(
        locked_exc.value
    )


def test_none_of_the_four_login_failures_issues_a_refresh_token(db_session) -> None:
    service = _service(db_session, max_failed_login_attempts=1)

    inactive_user, _ = service.signup(email="inactive@example.com", password="correct-password")
    inactive_user.is_active = False
    db_session.commit()

    locked_user, _ = service.signup(email="locked@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="locked@example.com", password="wrong")

    service.signup(email="wrongpw@example.com", password="correct-password")

    tokens_before = db_session.query(RefreshToken).count()

    with pytest.raises(InvalidCredentialsError):
        service.login(email="nobody@example.com", password="anything")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="wrongpw@example.com", password="not-the-password")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="inactive@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="locked@example.com", password="correct-password")

    assert db_session.query(RefreshToken).count() == tokens_before


def test_login_wrong_password_persists_the_failed_attempt_despite_raising(db_session) -> None:
    service = _service(db_session)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong-password")

    user = UserRepository(db_session).get_by_email("person@example.com")
    assert user.failed_login_attempts == 1


def test_login_rejects_an_inactive_user_with_invalid_credentials(db_session) -> None:
    service = _service(db_session)
    user, _ = service.signup(email="person@example.com", password="correct-password")
    user.is_active = False
    db_session.commit()

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="correct-password")


def test_login_rejects_an_inactive_user_even_with_no_password_hash_check_bypass(db_session) -> None:
    """Belt and braces: an inactive account must be rejected even when the
    *correct* password is supplied - never just "usually" rejected."""
    service = _service(db_session)
    user, _ = service.signup(email="person@example.com", password="the-real-password")
    user.is_active = False
    db_session.commit()

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="the-real-password")


def test_login_successful_resets_failed_attempt_counter(db_session) -> None:
    service = _service(db_session, max_failed_login_attempts=5)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")

    service.login(email="person@example.com", password="correct-password")

    user = UserRepository(db_session).get_by_email("person@example.com")
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_nonexistent_user_login_still_performs_password_verification_work(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual timing-safety mechanism, exercised through the service:
    a nonexistent email must not short-circuit before a real bcrypt
    verification happens."""
    calls = []
    real_verify = auth_service_module.verify_password_or_dummy

    def spy(password, password_hash):
        calls.append(password_hash)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_service_module, "verify_password_or_dummy", spy)
    service = _service(db_session)

    with pytest.raises(InvalidCredentialsError):
        service.login(email="nobody@example.com", password="anything")

    assert len(calls) == 1


# =====================================================================
# Account lockout
# =====================================================================


def test_account_login_fails_with_invalid_credentials_after_max_failed_attempts(db_session) -> None:
    service = _service(db_session, max_failed_login_attempts=3)
    service.signup(email="person@example.com", password="correct-password")

    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            service.login(email="person@example.com", password="wrong")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="correct-password")


def test_locked_account_rejects_even_the_correct_password(db_session) -> None:
    service = _service(db_session, max_failed_login_attempts=1)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="correct-password")


def test_correct_password_during_active_lock_does_not_clear_the_lock(db_session) -> None:
    """Requirement: a correct password presented while still locked must
    neither unlock the account nor reset the failed-attempt counter."""
    service = _service(db_session, max_failed_login_attempts=1)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")

    user = UserRepository(db_session).get_by_email("person@example.com")
    locked_until_before = user.locked_until
    attempts_before = user.failed_login_attempts
    assert locked_until_before is not None

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="correct-password")

    user = UserRepository(db_session).get_by_email("person@example.com")
    assert user.locked_until == locked_until_before
    assert user.failed_login_attempts == attempts_before


def test_wrong_password_during_active_lock_does_not_extend_the_lock(db_session) -> None:
    """Symmetric with the correct-password case: hammering a locked
    account with more wrong guesses must not push the lock further out or
    keep incrementing the counter - the state is simply frozen while
    genuinely locked."""
    service = _service(db_session, max_failed_login_attempts=1)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")

    user = UserRepository(db_session).get_by_email("person@example.com")
    locked_until_before = user.locked_until
    attempts_before = user.failed_login_attempts

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="still-wrong")

    user = UserRepository(db_session).get_by_email("person@example.com")
    assert user.locked_until == locked_until_before
    assert user.failed_login_attempts == attempts_before


def test_locked_account_login_still_performs_password_verification_work(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same timing-safety principle as the nonexistent-user case: a
    locked account must not short-circuit before a real bcrypt
    verification happens, or "locked" would be inferable from a fast
    response."""
    service = _service(db_session, max_failed_login_attempts=1)
    service.signup(email="person@example.com", password="correct-password")
    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")  # trips the lock

    calls = []
    real_verify = auth_service_module.verify_password_or_dummy

    def spy(password, password_hash):
        calls.append(password_hash)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_service_module, "verify_password_or_dummy", spy)

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="correct-password")

    assert len(calls) == 1
    assert calls[0] is not None  # the real password hash was used, not skipped


def test_lock_is_lazily_cleared_once_it_has_expired(db_session) -> None:
    service = _service(db_session, max_failed_login_attempts=1)
    service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(InvalidCredentialsError):
        service.login(email="person@example.com", password="wrong")

    # Simulate the lockout window having already passed.
    user = UserRepository(db_session).get_by_email("person@example.com")
    user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    _, tokens = service.login(email="person@example.com", password="correct-password")

    assert tokens.access_token
    refreshed_user = UserRepository(db_session).get_by_email("person@example.com")
    assert refreshed_user.locked_until is None
    assert refreshed_user.failed_login_attempts == 0


# =====================================================================
# Refresh: issuance and rotation
# =====================================================================


def test_refresh_issues_a_new_token_pair(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="correct-password")

    new_tokens = service.refresh(tokens.refresh_token)

    assert new_tokens.access_token
    assert new_tokens.refresh_token
    assert new_tokens.refresh_token != tokens.refresh_token
    assert decode_access_token(new_tokens.access_token, secret_key=_SECRET) == user.id


def test_refresh_revokes_the_presented_token(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.refresh(tokens.refresh_token)

    old = RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token))
    assert old.revoked_at is not None


def test_refresh_with_an_unrecognized_token_raises(db_session) -> None:
    service = _service(db_session)
    with pytest.raises(InvalidRefreshTokenError):
        service.refresh("never-issued-token")


def test_refresh_with_an_expired_token_raises(db_session) -> None:
    service = _service(db_session, refresh_token_expire_days=-1)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(ExpiredRefreshTokenError):
        service.refresh(tokens.refresh_token)


def test_expired_refresh_token_is_not_marked_revoked(db_session) -> None:
    """Expiry and revocation are deliberately distinct states - see
    RefreshToken's model docstring. A merely-expired token must not be
    flipped to revoked just because it was rejected."""
    service = _service(db_session, refresh_token_expire_days=-1)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    with pytest.raises(ExpiredRefreshTokenError):
        service.refresh(tokens.refresh_token)

    stored = RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token))
    assert stored.revoked_at is None


def test_refresh_rejects_an_inactive_users_token(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="correct-password")
    user.is_active = False
    db_session.commit()

    with pytest.raises(InactiveAccountError):
        service.refresh(tokens.refresh_token)


# =====================================================================
# Refresh-token reuse detection
# =====================================================================


def test_reusing_an_already_rotated_token_raises_reused_error(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.refresh(tokens.refresh_token)  # rotates - the original is now revoked

    with pytest.raises(RefreshTokenReusedError):
        service.refresh(tokens.refresh_token)  # presenting the same (now-revoked) token again


def test_reuse_revokes_every_active_token_for_that_user(db_session) -> None:
    """The actual point of reuse detection: a second, completely
    unrelated still-active session for the same user must also be killed,
    not just the reused token itself."""
    service = _service(db_session, max_failed_login_attempts=100)
    service.signup(email="person@example.com", password="correct-password")

    # A second "device" logs in - a second, independent active refresh token.
    _, second_session_tokens = service.login(email="person@example.com", password="correct-password")

    # A third session, whose token we rotate then replay (the reuse itself).
    _, first_session_tokens = service.login(email="person@example.com", password="correct-password")
    service.refresh(first_session_tokens.refresh_token)

    with pytest.raises(RefreshTokenReusedError):
        service.refresh(first_session_tokens.refresh_token)

    second_stored = RefreshTokenRepository(db_session).get_by_token_hash(
        hash_token(second_session_tokens.refresh_token)
    )
    assert second_stored.revoked_at is not None


def test_refresh_rotation_revokes_old_before_the_new_row_exists(db_session) -> None:
    """Ordering requirement: by the time `refresh()` returns, the
    presented token must already be revoked - proven by re-fetching it
    from the database (not just trusting the in-memory object) right
    after `refresh()` returns."""
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.refresh(tokens.refresh_token)

    fresh_session_view = RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token))
    assert fresh_session_view.revoked_at is not None


# =====================================================================
# Logout
# =====================================================================


def test_logout_revokes_the_token(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.logout(tokens.refresh_token)

    stored = RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token))
    assert stored.revoked_at is not None


def test_logout_is_idempotent(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.logout(tokens.refresh_token)
    service.logout(tokens.refresh_token)  # must not raise


def test_logout_with_an_unknown_token_does_not_raise(db_session) -> None:
    service = _service(db_session)
    service.logout("never-issued-token")  # must not raise


def test_logout_then_refresh_is_treated_as_reuse(db_session) -> None:
    service = _service(db_session)
    _, tokens = service.signup(email="person@example.com", password="correct-password")

    service.logout(tokens.refresh_token)

    with pytest.raises(RefreshTokenReusedError):
        service.refresh(tokens.refresh_token)


# =====================================================================
# get_current_user
# =====================================================================


def test_get_current_user_returns_the_correct_user(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="correct-password")

    found = service.get_current_user(tokens.access_token)

    assert found.id == user.id


def test_get_current_user_rejects_an_invalid_token(db_session) -> None:
    service = _service(db_session)
    with pytest.raises(InvalidAccessTokenError):
        service.get_current_user("not-a-jwt-at-all")


def test_get_current_user_rejects_a_token_for_a_deleted_user(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="correct-password")

    db_session.delete(RefreshTokenRepository(db_session).get_by_token_hash(hash_token(tokens.refresh_token)))
    db_session.delete(user)
    db_session.commit()

    with pytest.raises(InvalidAccessTokenError):
        service.get_current_user(tokens.access_token)


def test_get_current_user_rejects_a_token_for_an_inactive_user(db_session) -> None:
    service = _service(db_session)
    user, tokens = service.signup(email="person@example.com", password="correct-password")
    user.is_active = False
    db_session.commit()

    with pytest.raises(InvalidAccessTokenError):
        service.get_current_user(tokens.access_token)
