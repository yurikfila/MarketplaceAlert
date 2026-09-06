"""Tests for `core/auth/repository.py`: `UserRepository`,
`RefreshTokenRepository`, `PasswordResetTokenRepository`, and
`normalize_email`.

Repository-level behavior only (email normalization, lockout counter
transitions, revocation, reset-code issuance/claim primitives) -
`tests/test_auth_service.py` covers the business logic built on top of
these.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.repository import (
    PasswordResetTokenRepository,
    RefreshTokenRepository,
    UserRepository,
    normalize_email,
)


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


def test_set_password_hash_overwrites_the_hash_and_bumps_updated_at(db_session) -> None:
    repo = UserRepository(db_session)
    user = _create_user(repo, password_hash="old-hash")
    db_session.commit()
    original_updated_at = user.updated_at

    repo.set_password_hash(user, "new-hash")

    assert user.password_hash == "new-hash"
    assert user.updated_at >= original_updated_at


def test_set_password_hash_does_not_touch_lockout_state(db_session) -> None:
    """A password reset and a login-lockout reset are independent
    concerns - this method must never silently clear/affect
    `failed_login_attempts`/`locked_until`."""
    repo = UserRepository(db_session)
    user = _create_user(repo)
    db_session.commit()
    repo.record_failed_login_attempt(user, max_attempts=3, lockout_minutes=15)

    repo.set_password_hash(user, "new-hash")

    assert user.failed_login_attempts == 1


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


# =====================================================================
# PasswordResetTokenRepository
# =====================================================================


def _reset_token(repo: PasswordResetTokenRepository, user_id: int, *, token_hash="hash-1", minutes=10):
    return repo.create(
        user_id=user_id, token_hash=token_hash, expires_at=datetime.now(timezone.utc) + timedelta(minutes=minutes)
    )


def test_create_persists_a_row_with_zero_failed_attempts_and_unused(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()

    assert token.id is not None
    assert token.failed_attempts == 0
    assert token.used_at is None


def test_get_live_for_user_finds_an_unused_unexpired_row(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    created = _reset_token(tokens, user.id)
    db_session.commit()

    found = tokens.get_live_for_user(user.id, now=datetime.now(timezone.utc))
    assert found is not None
    assert found.id == created.id


def test_get_live_for_user_returns_none_when_expired(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    _reset_token(tokens, user.id, minutes=-1)
    db_session.commit()

    assert tokens.get_live_for_user(user.id, now=datetime.now(timezone.utc)) is None


def test_get_live_for_user_returns_none_when_already_used(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()
    token.used_at = datetime.now(timezone.utc)
    db_session.commit()

    assert tokens.get_live_for_user(user.id, now=datetime.now(timezone.utc)) is None


def test_get_live_for_user_ignores_another_users_row(db_session) -> None:
    users = UserRepository(db_session)
    user_a = _create_user(users, email="a@example.com")
    user_b = _create_user(users, email="b@example.com")
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    _reset_token(tokens, user_a.id)
    db_session.commit()

    assert tokens.get_live_for_user(user_b.id, now=datetime.now(timezone.utc)) is None


def test_get_most_recent_for_user_finds_a_row_even_if_already_used(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()
    token.used_at = datetime.now(timezone.utc)
    db_session.commit()

    found = tokens.get_most_recent_for_user(user.id)
    assert found is not None
    assert found.id == token.id


def test_get_most_recent_for_user_returns_none_when_none_ever_issued(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    assert tokens.get_most_recent_for_user(user.id) is None


def test_get_most_recent_for_user_picks_the_latest_by_created_at(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    older = _reset_token(tokens, user.id, token_hash="older-hash")
    db_session.commit()
    older.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    newer = _reset_token(tokens, user.id, token_hash="newer-hash")
    db_session.commit()

    found = tokens.get_most_recent_for_user(user.id)
    assert found.id == newer.id


def test_count_issued_since_counts_rows_regardless_of_used_state(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    first = _reset_token(tokens, user.id, token_hash="hash-a")
    _reset_token(tokens, user.id, token_hash="hash-b")
    db_session.commit()
    first.used_at = datetime.now(timezone.utc)
    db_session.commit()

    count = tokens.count_issued_since(user.id, since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert count == 2


def test_count_issued_since_excludes_rows_before_the_window(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    old = _reset_token(tokens, user.id, token_hash="old-hash")
    db_session.commit()
    old.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()

    count = tokens.count_issued_since(user.id, since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert count == 0


def test_count_issued_since_does_not_count_another_users_rows(db_session) -> None:
    users = UserRepository(db_session)
    user_a = _create_user(users, email="count-a@example.com")
    user_b = _create_user(users, email="count-b@example.com")
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    _reset_token(tokens, user_b.id)
    db_session.commit()

    count = tokens.count_issued_since(user_a.id, since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert count == 0


def test_supersede_unused_for_user_marks_unused_rows_used(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()

    tokens.supersede_unused_for_user(user.id, now=datetime.now(timezone.utc))
    db_session.refresh(token)

    assert token.used_at is not None


def test_supersede_unused_for_user_does_not_touch_another_users_row(db_session) -> None:
    users = UserRepository(db_session)
    user_a = _create_user(users, email="supersede-a@example.com")
    user_b = _create_user(users, email="supersede-b@example.com")
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    other = _reset_token(tokens, user_b.id)
    db_session.commit()

    tokens.supersede_unused_for_user(user_a.id, now=datetime.now(timezone.utc))
    db_session.refresh(other)

    assert other.used_at is None


def test_supersede_unused_for_user_does_not_overwrite_an_already_used_timestamp(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()
    original_used_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    token.used_at = original_used_at
    db_session.commit()

    tokens.supersede_unused_for_user(user.id, now=datetime.now(timezone.utc))
    db_session.refresh(token)

    assert token.used_at.replace(tzinfo=None) == original_used_at.replace(tzinfo=None)


def test_increment_failed_attempts_increases_the_counter(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()

    tokens.increment_failed_attempts(token.id)
    db_session.refresh(token)
    assert token.failed_attempts == 1

    tokens.increment_failed_attempts(token.id)
    db_session.refresh(token)
    assert token.failed_attempts == 2


def test_claim_for_reset_succeeds_for_a_live_row_under_the_attempt_ceiling(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()

    claimed = tokens.claim_for_reset(token.id, now=datetime.now(timezone.utc), max_attempts=5)

    assert claimed is True
    db_session.refresh(token)
    assert token.used_at is not None


def test_claim_for_reset_fails_for_an_already_used_row(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()
    assert tokens.claim_for_reset(token.id, now=datetime.now(timezone.utc), max_attempts=5) is True

    claimed_again = tokens.claim_for_reset(token.id, now=datetime.now(timezone.utc), max_attempts=5)

    assert claimed_again is False


def test_claim_for_reset_fails_for_an_expired_row(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id, minutes=-1)
    db_session.commit()

    assert tokens.claim_for_reset(token.id, now=datetime.now(timezone.utc), max_attempts=5) is False


def test_claim_for_reset_fails_once_failed_attempts_reaches_the_maximum(db_session) -> None:
    users = UserRepository(db_session)
    user = _create_user(users)
    db_session.commit()

    tokens = PasswordResetTokenRepository(db_session)
    token = _reset_token(tokens, user.id)
    db_session.commit()
    for _ in range(5):
        tokens.increment_failed_attempts(token.id)
    db_session.commit()

    assert tokens.claim_for_reset(token.id, now=datetime.now(timezone.utc), max_attempts=5) is False
