"""Tests for the authentication models (`core/auth/models.py`): `User`,
`RefreshToken`, `PasswordResetToken`, and the new `SavedSearch.user_id`
column - constraints, defaults, and cascade behavior.

Schema-level only, matching Phase 1's scope (see `core/auth/__init__.py`) -
no hashing, token issuance, or ownership-enforcement code exists yet to
test at a higher level.

**A note on foreign-key enforcement in these tests**: this project's
SQLite connection setup does not enable `PRAGMA foreign_keys` (a
pre-existing condition across the whole codebase, not introduced here -
`PendingNotification`'s existing `ON DELETE CASCADE`/`SET NULL`
constraints have the same property). That means the shared `db_session`/
`session_factory` fixtures (bound to a plain SQLite engine) will not
actually *execute* `ON DELETE CASCADE` at the database level, even though
the constraint is correctly defined - PostgreSQL (production) always
enforces foreign keys, so what actually matters is that the constraint
itself is correct. Tests that need to prove real cascade/FK-rejection
behavior build their own throwaway engine with `PRAGMA foreign_keys=ON`
explicitly enabled, rather than silently asserting something the default
local setup can't actually demonstrate.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.models import PasswordResetToken, RefreshToken, User
from marketplace_alert.core.persistence.database import Base, create_db_engine
from marketplace_alert.core.saved_searches.models import SavedSearch

# Imported for the side effect of registering every table on Base.metadata,
# same reasoning as alembic/env.py and tests/test_alembic_migrations.py.
import marketplace_alert.core.persistence.models  # noqa: F401


def _user(**overrides) -> User:
    defaults = {"email": "person@example.com", "password_hash": "not-a-real-hash"}
    defaults.update(overrides)
    return User(**defaults)


# =====================================================================
# User
# =====================================================================


def test_user_minimal_creation_has_expected_defaults(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    assert user.id is not None
    assert user.is_active is True
    assert isinstance(user.created_at, datetime)
    assert isinstance(user.updated_at, datetime)


def test_user_email_must_be_unique(db_session) -> None:
    db_session.add(_user(email="dup@example.com"))
    db_session.commit()

    db_session.add(_user(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_user_email_uniqueness_is_case_insensitive(db_session) -> None:
    """The database itself must reject this, not just Phase 2's future
    normalize-before-write repository logic - see `ix_users_email_lower`
    and `User`'s docstring for the full reasoning."""
    db_session.add(_user(email="user@example.com"))
    db_session.commit()

    db_session.add(_user(email="USER@example.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_user_email_uniqueness_is_case_insensitive_for_mixed_case(db_session) -> None:
    db_session.add(_user(email="Someone@Example.com"))
    db_session.commit()

    db_session.add(_user(email="someone@example.com"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_user_emails_differing_by_more_than_case_are_both_accepted(db_session) -> None:
    """Sanity check that the case-insensitive index isn't accidentally
    over-broad - two genuinely different emails must both be insertable."""
    db_session.add(_user(email="person-one@example.com"))
    db_session.add(_user(email="person-two@example.com"))
    db_session.commit()

    assert db_session.query(User).count() == 2


def test_user_email_is_required(db_session) -> None:
    db_session.add(User(email=None, password_hash="x"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_user_password_hash_is_required(db_session) -> None:
    db_session.add(User(email="nohash@example.com", password_hash=None))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# =====================================================================
# RefreshToken
# =====================================================================


def _refresh_token(user_id: int, **overrides) -> RefreshToken:
    defaults = {
        "user_id": user_id,
        "token_hash": "refresh-token-hash-1",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }
    defaults.update(overrides)
    return RefreshToken(**defaults)


def test_refresh_token_creation_has_expected_defaults(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    token = _refresh_token(user.id)
    db_session.add(token)
    db_session.commit()

    assert token.id is not None
    assert isinstance(token.issued_at, datetime)
    assert token.revoked_at is None


def test_refresh_token_hash_must_be_unique(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    db_session.add(_refresh_token(user.id, token_hash="dup-hash"))
    db_session.commit()

    db_session.add(_refresh_token(user.id, token_hash="dup-hash"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_refresh_token_can_be_marked_revoked(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    token = _refresh_token(user.id)
    db_session.add(token)
    db_session.commit()

    token.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    db_session.refresh(token)
    assert token.revoked_at is not None


# =====================================================================
# PasswordResetToken
# =====================================================================


def _password_reset_token(user_id: int, **overrides) -> PasswordResetToken:
    defaults = {
        "user_id": user_id,
        "token_hash": "reset-token-hash-1",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    defaults.update(overrides)
    return PasswordResetToken(**defaults)


def test_password_reset_token_creation_has_expected_defaults(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    token = _password_reset_token(user.id)
    db_session.add(token)
    db_session.commit()

    assert token.id is not None
    assert isinstance(token.created_at, datetime)
    assert token.used_at is None


def test_password_reset_token_hash_must_be_unique(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    db_session.add(_password_reset_token(user.id, token_hash="dup-reset-hash"))
    db_session.commit()

    db_session.add(_password_reset_token(user.id, token_hash="dup-reset-hash"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_password_reset_token_can_be_marked_used(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    token = _password_reset_token(user.id)
    db_session.add(token)
    db_session.commit()

    token.used_at = datetime.now(timezone.utc)
    db_session.commit()

    db_session.refresh(token)
    assert token.used_at is not None


# =====================================================================
# SavedSearch.user_id
# =====================================================================


def test_saved_search_user_id_is_nullable(db_session) -> None:
    """Phase 1's whole point for this column: every pre-existing saved
    search has no user to point at yet, and must remain creatable/valid
    without one until a later cutover backfills it."""
    saved_search = SavedSearch(query="Makita drill", scan_interval_seconds=300)
    db_session.add(saved_search)
    db_session.commit()

    assert saved_search.user_id is None


def test_saved_search_can_be_linked_to_a_user(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()

    saved_search = SavedSearch(query="Fender Stratocaster", scan_interval_seconds=300, user_id=user.id)
    db_session.add(saved_search)
    db_session.commit()

    db_session.refresh(saved_search)
    assert saved_search.user_id == user.id


# =====================================================================
# Real foreign-key enforcement (own throwaway engine - see module docstring)
# =====================================================================


def _fk_enforced_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'fk_enforced.db'}")
    event.listen(engine, "connect", lambda dbapi_connection, _: dbapi_connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(bind=engine)
    return engine


def test_refresh_token_insert_for_a_nonexistent_user_is_rejected_when_fk_enforcement_is_on(tmp_path) -> None:
    from sqlalchemy.orm import sessionmaker

    engine = _fk_enforced_engine(tmp_path)
    try:
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            session.add(_refresh_token(user_id=999999))
            with pytest.raises(IntegrityError):
                session.flush()
        finally:
            session.rollback()
            session.close()
    finally:
        engine.dispose()


def test_deleting_a_user_cascades_to_refresh_tokens_when_fk_enforcement_is_on(tmp_path) -> None:
    from sqlalchemy.orm import sessionmaker

    engine = _fk_enforced_engine(tmp_path)
    try:
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            user = _user()
            session.add(user)
            session.commit()
            session.add(_refresh_token(user.id))
            session.commit()

            session.delete(user)
            session.commit()

            assert session.query(RefreshToken).count() == 0
        finally:
            session.close()
    finally:
        engine.dispose()


def test_deleting_a_user_cascades_to_password_reset_tokens_when_fk_enforcement_is_on(tmp_path) -> None:
    from sqlalchemy.orm import sessionmaker

    engine = _fk_enforced_engine(tmp_path)
    try:
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            user = _user()
            session.add(user)
            session.commit()
            session.add(_password_reset_token(user.id))
            session.commit()

            session.delete(user)
            session.commit()

            assert session.query(PasswordResetToken).count() == 0
        finally:
            session.close()
    finally:
        engine.dispose()


def test_deleting_a_user_cascades_to_their_saved_searches_when_fk_enforcement_is_on(tmp_path) -> None:
    from sqlalchemy.orm import sessionmaker

    engine = _fk_enforced_engine(tmp_path)
    try:
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            user = _user()
            session.add(user)
            session.commit()
            session.add(SavedSearch(query="Bosch drill", scan_interval_seconds=300, user_id=user.id))
            session.commit()

            session.delete(user)
            session.commit()

            assert session.query(SavedSearch).count() == 0
        finally:
            session.close()
    finally:
        engine.dispose()


def test_deleting_a_user_does_not_affect_an_unlinked_saved_search_when_fk_enforcement_is_on(tmp_path) -> None:
    """A saved search with `user_id IS NULL` (the pre-cutover state every
    existing production row is in) has nothing to cascade from - deleting
    an unrelated user must never touch it."""
    from sqlalchemy.orm import sessionmaker

    engine = _fk_enforced_engine(tmp_path)
    try:
        session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            user = _user()
            session.add(user)
            session.commit()
            session.add(SavedSearch(query="Unowned search", scan_interval_seconds=300))
            session.commit()

            session.delete(user)
            session.commit()

            assert session.query(SavedSearch).count() == 1
        finally:
            session.close()
    finally:
        engine.dispose()
