"""Tests for `core/auth/repository.py`: `UserRepository`,
`RefreshTokenRepository`, and `normalize_email`.

Repository-level behavior only (email normalization, lockout counter
transitions, revocation) - `tests/test_auth_service.py` covers the
business logic built on top of these.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.repository import RefreshTokenRepository, UserRepository, normalize_email


def _create_user(repo: UserRepository, *, email="person@example.com", password_hash="hash"):
    return repo.create(email=email, password_hash=password_hash)


# =====================================================================
# normalize_email
# =====================================================================


def test_normalize_email_lowercases() -> None:
    assert normalize_email("Person@Example.COM") == "person@example.com"


def test_normalize_email_strips_surrounding_whitespace() -> None:
    assert normalize_email("  person@example.com  ") == "person@example.com"


def test_normalize_email_handles_both_at_once() -> None:
    assert normalize_email("  Person@Example.COM  ") == "person@example.com"


# =====================================================================
# UserRepository
# =====================================================================


def test_create_normalizes_the_stored_email(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo, email="  Mixed.Case@Example.COM  ")
    db_session.commit()

    assert user.email == "mixed.case@example.com"


def test_get_by_email_is_case_and_whitespace_insensitive(db_session) -> None:
    repo = UserRepository(db_session)
    created = _create_user(repo, email="person@example.com")
    db_session.commit()

    for lookup in ("person@example.com", "PERSON@EXAMPLE.COM", "  Person@Example.com  "):
        found = repo.get_by_email(lookup)
        assert found is not None
        assert found.id == created.id


def test_get_by_email_returns_none_for_unknown_email(db_session) -> None:
    repo = UserRepository(db_session)
    assert repo.get_by_email("nobody@example.com") is None


def test_get_by_id_happy_path_and_missing(db_session) -> None:
    repo = UserRepository(db_session)
    created = _create_user(repo)
    db_session.commit()

    assert repo.get_by_id(created.id).id == created.id
    assert repo.get_by_id(999999) is None


def test_create_duplicate_email_raises_integrity_error_at_flush(db_session) -> None:
    repo = UserRepository(db_session)
    _create_user(repo, email="dup@example.com")
    db_session.commit()

    with pytest.raises(IntegrityError):
        _create_user(repo, email="DUP@example.com")  # different case, same normalized value
    db_session.rollback()


def test_record_failed_login_attempt_increments_counter(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo)
    db_session.commit()

    repo.record_failed_login_attempt(user, max_attempts=5, lockout_minutes=15)

    assert user.failed_login_attempts == 1
    assert user.locked_until is None


def test_record_failed_login_attempt_locks_once_max_attempts_reached(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo)
    db_session.commit()

    for _ in range(3):
        repo.record_failed_login_attempt(user, max_attempts=3, lockout_minutes=15)

    assert user.failed_login_attempts == 3
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(timezone.utc)


def test_record_failed_login_attempt_does_not_lock_before_threshold(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo)
    db_session.commit()

    for _ in range(2):
        repo.record_failed_login_attempt(user, max_attempts=3, lockout_minutes=15)

    assert user.failed_login_attempts == 2
    assert user.locked_until is None


def test_reset_failed_login_state_clears_counter_and_lock(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo)
    db_session.commit()

    for _ in range(3):
        repo.record_failed_login_attempt(user, max_attempts=3, lockout_minutes=15)
    assert user.locked_until is not None

    repo.reset_failed_login_state(user)

    assert user.failed_login_attempts == 0
    assert user.locked_until is None


# =====================================================================
# RefreshTokenRepository
# =====================================================================


def _issue_refresh_token(repo: RefreshTokenRepository, user_id: int, *, token_hash="hash-1", days=30):
    return repo.create(
        user_id=user_id, token_hash=token_hash, expires_at=datetime.now(timezone.utc) + timedelta(days=days)
    )


def test_refresh_token_create_and_get_by_token_hash(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    created = _issue_refresh_token(tokens, user.id)
    db_session.commit()

    found = tokens.get_by_token_hash("hash-1")
    assert found is not None
    assert found.id == created.id
    assert found.revoked_at is None


def test_get_by_token_hash_returns_none_for_unknown_hash(db_session) -> None:
    tokens = RefreshTokenRepository(db_session)
    assert tokens.get_by_token_hash("never-issued") is None


def test_revoke_sets_revoked_at(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    token = _issue_refresh_token(tokens, user.id)
    db_session.commit()

    tokens.revoke(token)

    assert token.revoked_at is not None


def test_revoke_is_idempotent_and_preserves_the_original_timestamp(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    token = _issue_refresh_token(tokens, user.id)
    db_session.commit()

    tokens.revoke(token)
    first_revoked_at = token.revoked_at

    tokens.revoke(token)

    assert token.revoked_at == first_revoked_at


def test_revoke_all_for_user_revokes_every_active_token(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    first = _issue_refresh_token(tokens, user.id, token_hash="hash-a")
    second = _issue_refresh_token(tokens, user.id, token_hash="hash-b")
    db_session.commit()

    tokens.revoke_all_for_user(user.id)
    db_session.refresh(first)
    db_session.refresh(second)

    assert first.revoked_at is not None
    assert second.revoked_at is not None


def test_revoke_all_for_user_does_not_touch_another_users_tokens(db_session) -> None:
    users = UserRepository(db_session)
    user_one = _create_user(users, email="one@example.com")
    user_two = _create_user(users, email="two@example.com")
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    other_users_token = _issue_refresh_token(tokens, user_two.id, token_hash="hash-other")
    db_session.commit()

    tokens.revoke_all_for_user(user_one.id)
    db_session.refresh(other_users_token)

    assert other_users_token.revoked_at is None


def test_revoke_all_for_user_preserves_an_already_revoked_tokens_timestamp(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = RefreshTokenRepository(db_session)
    token = _issue_refresh_token(tokens, user.id)
    db_session.commit()
    tokens.revoke(token)
    first_revoked_at = token.revoked_at

    tokens.revoke_all_for_user(user.id)
    db_session.refresh(token)

    # SQLite drops tzinfo on round-trip (a known, established quirk in this
    # codebase - see core/saved_searches/repository.py's _as_aware_utc) -
    # the reloaded value is naive even though the original in-memory one
    # (captured before the reload) is timezone-aware. Compare as naive;
    # the point of this test is that the *value* didn't change.
    assert token.revoked_at.replace(tzinfo=None) == first_revoked_at.replace(tzinfo=None)
