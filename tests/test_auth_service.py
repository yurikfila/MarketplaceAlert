"""Tests for `core/auth/service.py`: `AuthService` - signup, login,
refresh (rotation + reuse detection), logout, access-token validation,
and password-reset request/verification. The business-logic layer;
`tests/test_auth_security.py` and `tests/test_auth_repository.py` cover
the primitives this composes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from marketplace_alert.core.auth import service as auth_service_module
from marketplace_alert.core.auth.models import PasswordResetToken, RefreshToken, User
from marketplace_alert.core.auth.repository import RefreshTokenRepository, UserRepository
from marketplace_alert.core.auth.security import (
    InvalidAccessTokenError,
    decode_access_token,
    hash_password,
    hash_token,
    verify_password,
)
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
    WeakNewPasswordError,
)

_SECRET = "test-secret-key-at-least-32-characters-long"


def _service(session, **overrides) -> AuthService:
    defaults = dict(
        secret_key=_SECRET,
        access_token_expire_minutes=30,
        refresh_token_expire_days=30,
        max_failed_login_attempts=3,
        account_lockout_minutes=15,
        password_reset_token_expire_minutes=10,
        password_reset_max_attempts=5,
        password_reset_resend_cooldown_seconds=60,
        password_reset_max_per_hour=5,
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


# =====================================================================
# PasswordResetRequestResult - the should_deliver/raw_code contract
# =====================================================================


def test_password_reset_request_result_accepts_a_consistent_deliverable_result() -> None:
    result = PasswordResetRequestResult(should_deliver=True, raw_code="123456")
    assert result.should_deliver is True
    assert result.raw_code == "123456"


def test_password_reset_request_result_accepts_a_consistent_suppressed_result() -> None:
    result = PasswordResetRequestResult(should_deliver=False, raw_code=None)
    assert result.should_deliver is False
    assert result.raw_code is None


def test_password_reset_request_result_rejects_should_deliver_true_with_no_code() -> None:
    with pytest.raises(ValueError, match="should_deliver=True requires a real raw_code"):
        PasswordResetRequestResult(should_deliver=True, raw_code=None)


def test_password_reset_request_result_rejects_should_deliver_false_with_a_code() -> None:
    with pytest.raises(ValueError, match="should_deliver=False must never carry a raw_code"):
        PasswordResetRequestResult(should_deliver=False, raw_code="123456")


# =====================================================================
# request_password_reset - issuance
# =====================================================================


def _issue_code(service: AuthService, email: str) -> str:
    result = service.request_password_reset(email=email)
    assert result.should_deliver is True
    assert result.raw_code is not None
    return result.raw_code


def test_request_password_reset_issues_a_code_for_an_active_known_user(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="issue-active@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session)
    result = service.request_password_reset(email=user.email)

    assert result.should_deliver is True
    assert result.raw_code is not None
    assert len(result.raw_code) == 6
    assert result.raw_code.isdigit()

    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert row.token_hash == hash_token(result.raw_code)
    assert row.token_hash != result.raw_code  # raw code is never stored
    assert row.used_at is None
    assert row.failed_attempts == 0


def test_request_password_reset_does_nothing_for_an_unknown_email(db_session) -> None:
    service = _service(db_session)
    result = service.request_password_reset(email="nobody-at-all@example.com")

    assert result.should_deliver is False
    assert result.raw_code is None
    assert db_session.query(PasswordResetToken).count() == 0


def test_request_password_reset_does_nothing_for_an_inactive_user(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="inactive-issue@example.com", password_hash=hash_password("irrelevant"))
    user.is_active = False
    db_session.commit()

    service = _service(db_session)
    result = service.request_password_reset(email=user.email)

    assert result.should_deliver is False
    assert result.raw_code is None
    assert db_session.query(PasswordResetToken).count() == 0


def test_request_password_reset_is_suppressed_within_the_resend_cooldown(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="cooldown@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=60)
    first = service.request_password_reset(email=user.email)
    assert first.should_deliver is True
    assert first.raw_code is not None

    second = service.request_password_reset(email=user.email)

    assert second.should_deliver is False
    assert second.raw_code is None
    assert db_session.query(PasswordResetToken).count() == 1


def test_request_password_reset_can_issue_again_after_the_cooldown_elapses(db_session) -> None:
    """Time is controlled by backdating the persisted `created_at`
    directly - the same technique this codebase's notification-outbox
    tests already use for lease/throttle expiry - never a real sleep."""
    users = UserRepository(db_session)
    user = users.create(email="cooldown-elapsed@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=60)
    first = service.request_password_reset(email=user.email)
    assert first.should_deliver is True
    assert first.raw_code is not None

    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=61)
    db_session.commit()

    second = service.request_password_reset(email=user.email)

    assert second.should_deliver is True
    assert second.raw_code is not None
    assert db_session.query(PasswordResetToken).count() == 2


def test_request_password_reset_stops_after_the_hourly_cap(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="hourly-cap@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=0, password_reset_max_per_hour=5)
    for _ in range(5):
        result = service.request_password_reset(email=user.email)
        assert result.should_deliver is True
        assert result.raw_code is not None

    sixth = service.request_password_reset(email=user.email)

    assert sixth.should_deliver is False
    assert sixth.raw_code is None
    assert db_session.query(PasswordResetToken).filter_by(user_id=user.id).count() == 5


def test_request_password_reset_resumes_after_the_hourly_window_passes(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="hourly-resume@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=0, password_reset_max_per_hour=5)
    for _ in range(5):
        result = service.request_password_reset(email=user.email)
        assert result.should_deliver is True
        assert result.raw_code is not None

    blocked = service.request_password_reset(email=user.email)
    assert blocked.should_deliver is False
    assert blocked.raw_code is None

    db_session.query(PasswordResetToken).filter_by(user_id=user.id).update(
        {"created_at": datetime.now(timezone.utc) - timedelta(hours=2)}
    )
    db_session.commit()

    resumed = service.request_password_reset(email=user.email)
    assert resumed.should_deliver is True
    assert resumed.raw_code is not None


def test_new_issuance_supersedes_the_prior_unused_row(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="supersede@example.com", password_hash=hash_password("irrelevant"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=0)
    service.request_password_reset(email=user.email)
    first_row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    first_row_id = first_row.id

    service.request_password_reset(email=user.email)

    db_session.refresh(first_row)
    assert first_row.used_at is not None  # superseded, not deleted

    all_rows = db_session.query(PasswordResetToken).filter_by(user_id=user.id).all()
    assert len(all_rows) == 2  # both still present

    live_rows = [row for row in all_rows if row.used_at is None]
    assert len(live_rows) == 1  # at most one live row after issuance
    assert live_rows[0].id != first_row_id


# =====================================================================
# Collision safety - two different users sharing the exact same code
# =====================================================================


def test_two_different_users_can_receive_the_same_raw_code(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    users = UserRepository(db_session)
    user_a = users.create(email="collision-a@example.com", password_hash=hash_password("password-a"))
    user_b = users.create(email="collision-b@example.com", password_hash=hash_password("password-b"))
    db_session.commit()

    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: "555444")

    service = _service(db_session)
    result_a = service.request_password_reset(email=user_a.email)
    result_b = service.request_password_reset(email=user_b.email)

    assert result_a.should_deliver is True
    assert result_b.should_deliver is True
    assert result_a.raw_code == "555444"
    assert result_b.raw_code == "555444"

    rows = db_session.query(PasswordResetToken).filter_by(token_hash=hash_token("555444")).all()
    assert len(rows) == 2
    assert {row.user_id for row in rows} == {user_a.id, user_b.id}


def test_one_users_shared_code_cannot_reset_a_different_user(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The core safety property of the (user_id, token_hash)-scoped
    lookup: even though both users have the exact same raw code, a
    submission naming user A's email must only ever affect user A - never
    user B, regardless of the shared hash. B's own row remains live and
    independently redeemable with B's own email."""
    users = UserRepository(db_session)
    user_a = users.create(email="collision-reset-a@example.com", password_hash=hash_password("password-a"))
    user_b = users.create(email="collision-reset-b@example.com", password_hash=hash_password("password-b"))
    db_session.commit()

    monkeypatch.setattr(auth_service_module, "generate_reset_code", lambda: "111222")

    service = _service(db_session)
    service.request_password_reset(email=user_a.email)
    service.request_password_reset(email=user_b.email)

    service.reset_password(email=user_a.email, code="111222", new_password="new-password-for-a")

    db_session.refresh(user_a)
    db_session.refresh(user_b)
    assert verify_password("new-password-for-a", user_a.password_hash) is True
    assert verify_password("password-b", user_b.password_hash) is True  # untouched

    service.reset_password(email=user_b.email, code="111222", new_password="new-password-for-b")
    db_session.refresh(user_b)
    assert verify_password("new-password-for-b", user_b.password_hash) is True


# =====================================================================
# reset_password - verification
# =====================================================================


def test_reset_password_succeeds_with_the_correct_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-correct@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")

    db_session.refresh(user)
    assert verify_password("a-new-strong-password", user.password_hash) is True


def test_reset_password_fails_generically_for_a_wrong_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-wrong@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    wrong_code = "111111" if code != "111111" else "222222"

    with pytest.raises(InvalidResetCodeError, match="Invalid or expired verification code"):
        service.reset_password(email=user.email, code=wrong_code, new_password="a-new-strong-password")


def test_reset_password_wrong_code_increments_failed_attempts(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-attempts@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    wrong_code = "111111" if code != "111111" else "222222"

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=wrong_code, new_password="a-new-strong-password")

    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert row.failed_attempts == 1


def test_reset_password_fails_once_max_attempts_reached_even_with_the_correct_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-exhausted@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session, password_reset_max_attempts=3)
    code = _issue_code(service, user.email)
    wrong_code = "111111" if code != "111111" else "222222"

    for _ in range(3):
        with pytest.raises(InvalidResetCodeError):
            service.reset_password(email=user.email, code=wrong_code, new_password="a-new-strong-password")

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")

    db_session.refresh(user)
    assert verify_password("old-password", user.password_hash) is True


@pytest.mark.parametrize("malformed", ["12345", "1234567", "abcdef", "", "12 345", "123-45"])
def test_reset_password_rejects_a_malformed_code(db_session, malformed: str) -> None:
    users = UserRepository(db_session)
    user = users.create(email="malformed-code@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    _issue_code(service, user.email)

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=malformed, new_password="a-new-strong-password")


def test_reset_password_fails_for_an_expired_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-expired@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")


def test_reset_password_fails_for_a_superseded_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-superseded@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session, password_reset_resend_cooldown_seconds=0)
    old_code = _issue_code(service, user.email)
    _issue_code(service, user.email)  # supersedes old_code

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=old_code, new_password="a-new-strong-password")


def test_reset_password_fails_generically_for_an_unknown_email(db_session) -> None:
    service = _service(db_session)

    with pytest.raises(InvalidResetCodeError, match="Invalid or expired verification code"):
        service.reset_password(
            email="never-signed-up@example.com", code="123456", new_password="a-new-strong-password"
        )


def test_reset_password_fails_generically_for_an_inactive_user(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="verify-inactive@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    user.is_active = False
    db_session.commit()

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")


def test_reset_password_rejects_a_too_short_new_password_without_consuming_the_code(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="weak-password@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)

    with pytest.raises(WeakNewPasswordError):
        service.reset_password(email=user.email, code=code, new_password="short")

    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert row.used_at is None
    assert row.failed_attempts == 0

    service.reset_password(email=user.email, code=code, new_password="a-sufficiently-long-password")
    db_session.refresh(user)
    assert verify_password("a-sufficiently-long-password", user.password_hash) is True


# =====================================================================
# Password change
# =====================================================================


def test_reset_password_old_password_no_longer_verifies(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="old-pw-check@example.com", password_hash=hash_password("the-old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="the-new-password")

    db_session.refresh(user)
    assert verify_password("the-old-password", user.password_hash) is False


def test_reset_password_new_password_verifies(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="new-pw-check@example.com", password_hash=hash_password("the-old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="the-new-password")

    db_session.refresh(user)
    assert verify_password("the-new-password", user.password_hash) is True


def test_reset_password_stores_a_hash_never_the_plaintext(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="hash-check@example.com", password_hash=hash_password("the-old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="the-new-plaintext-password")

    db_session.refresh(user)
    assert user.password_hash != "the-new-plaintext-password"
    assert "the-new-plaintext-password" not in user.password_hash


# =====================================================================
# Session revocation
# =====================================================================


def test_reset_password_revokes_all_refresh_tokens_for_the_user(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="revoke-mine@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    refresh_tokens = RefreshTokenRepository(db_session)
    token_1 = refresh_tokens.create(
        user_id=user.id, token_hash="mine-1", expires_at=datetime.now(timezone.utc) + timedelta(days=30)
    )
    token_2 = refresh_tokens.create(
        user_id=user.id, token_hash="mine-2", expires_at=datetime.now(timezone.utc) + timedelta(days=30)
    )
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")

    db_session.refresh(token_1)
    db_session.refresh(token_2)
    assert token_1.revoked_at is not None
    assert token_2.revoked_at is not None


def test_reset_password_does_not_revoke_another_users_refresh_tokens(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="revoke-target@example.com", password_hash=hash_password("old-password"))
    other_user = users.create(email="revoke-bystander@example.com", password_hash=hash_password("other-password"))
    db_session.commit()

    refresh_tokens = RefreshTokenRepository(db_session)
    other_token = refresh_tokens.create(
        user_id=other_user.id, token_hash="bystander-1", expires_at=datetime.now(timezone.utc) + timedelta(days=30)
    )
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")

    db_session.refresh(other_token)
    assert other_token.revoked_at is None


# =====================================================================
# Single use
# =====================================================================


def test_reset_password_code_cannot_be_reused_after_a_successful_reset(db_session) -> None:
    users = UserRepository(db_session)
    user = users.create(email="single-use@example.com", password_hash=hash_password("old-password"))
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)
    service.reset_password(email=user.email, code=code, new_password="first-new-password")

    with pytest.raises(InvalidResetCodeError):
        service.reset_password(email=user.email, code=code, new_password="second-new-password")

    db_session.refresh(user)
    assert verify_password("first-new-password", user.password_hash) is True
    assert verify_password("second-new-password", user.password_hash) is False


# =====================================================================
# Transaction safety
# =====================================================================


def test_reset_password_rolls_back_completely_if_a_step_after_the_claim_fails(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the real request-scoped session's own behavior
    (`core/persistence/database.py:get_db_session` rolls back on any
    unhandled exception before re-raising) - proves a failure after the
    atomic claim but before the final commit leaves the whole attempt as
    if it never happened: the code is still unconsumed, the password is
    unchanged, and no refresh token was revoked."""
    users = UserRepository(db_session)
    user = users.create(email="rollback-safety@example.com", password_hash=hash_password("the-original-password"))
    db_session.commit()

    refresh_tokens = RefreshTokenRepository(db_session)
    existing_token = refresh_tokens.create(
        user_id=user.id,
        token_hash="rollback-safety-token",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.commit()

    service = _service(db_session)
    code = _issue_code(service, user.email)

    def _boom(_password):
        raise RuntimeError("simulated failure after the claim")

    monkeypatch.setattr(auth_service_module, "hash_password", _boom)

    with pytest.raises(RuntimeError, match="simulated failure after the claim"):
        service.reset_password(email=user.email, code=code, new_password="a-new-strong-password")

    # The claim's UPDATE was flushed but never committed - roll back
    # exactly like the real get_db_session() dependency would on any
    # unhandled exception.
    db_session.rollback()

    row = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert row.used_at is None

    db_session.refresh(user)
    assert verify_password("the-original-password", user.password_hash) is True

    db_session.refresh(existing_token)
    assert existing_token.revoked_at is None


# =====================================================================
# Concurrency
# =====================================================================


def test_concurrent_reset_submissions_for_the_same_code_the_second_loses_the_claim_race(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the race directly - the same stale-read-monkeypatch
    technique already used for `ListingRepository.get_or_create`/
    `NotificationOutboxRepository.enqueue()` elsewhere in this codebase:
    session B's own pre-claim read is forced to report the same stale
    "still live" state it would have seen had it genuinely run a moment
    before session A committed its own claim. `claim_for_reset`'s
    conditional UPDATE still runs against the REAL, current database
    state regardless of what B's cached object claims - proving at most
    one of two concurrent successful-looking submissions can ever
    actually change the password."""
    setup_session = session_factory()
    user = User(email="race-reset@example.com", password_hash=hash_password("original-password"))
    setup_session.add(user)
    setup_session.commit()
    user_id = user.id
    email = user.email
    setup_session.close()

    setup_session = session_factory()
    setup_result = _service(setup_session).request_password_reset(email=email)
    assert setup_result.should_deliver is True
    raw_code = setup_result.raw_code
    assert raw_code is not None
    setup_session.close()

    session_b = session_factory()
    service_b = _service(session_b)
    # B's own "stale" read - captured while the row is still genuinely
    # live, as if B's own check had run a moment before A's claim below.
    stale_live_token = service_b._password_reset_tokens.get_live_for_user(
        user_id, now=datetime.now(timezone.utc)
    )
    assert stale_live_token is not None

    session_a = session_factory()
    service_a = _service(session_a)
    service_a.reset_password(email=email, code=raw_code, new_password="password-one")
    session_a.close()

    monkeypatch.setattr(service_b._password_reset_tokens, "get_live_for_user", lambda *a, **k: stale_live_token)

    with pytest.raises(InvalidResetCodeError):
        service_b.reset_password(email=email, code=raw_code, new_password="password-two")
    session_b.close()

    verify_session = session_factory()
    verify_user = UserRepository(verify_session).get_by_email(email)
    assert verify_password("password-one", verify_user.password_hash) is True
    assert verify_password("password-two", verify_user.password_hash) is False
    verify_session.close()
