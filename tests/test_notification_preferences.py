"""Tests for `NotificationPreference`
(`core/notifications/models.py`) and its repository
(`core/notifications/preferences_repository.py`) - model/repository CRUD,
`UNIQUE(user_id)`, and cascade delete.

Uses the same `db_session` fixture as every other persistence test
(`tests/conftest.py`) - an isolated temp-file SQLite database, never the
developer's real one.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.notifications.models import NotificationPreference
from marketplace_alert.core.notifications.preferences_repository import NotificationPreferenceRepository


def _user(session, email: str = "user@example.com") -> User:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user


# =====================================================================
# Repository CRUD
# =====================================================================


def test_get_by_user_id_returns_none_when_no_preference_exists(db_session) -> None:
    user = _user(db_session)
    assert NotificationPreferenceRepository(db_session).get_by_user_id(user.id) is None


def test_upsert_creates_a_new_row_when_none_exists(db_session) -> None:
    user = _user(db_session)

    preference = NotificationPreferenceRepository(db_session).upsert_telegram_chat_id(user.id, "123456")
    db_session.commit()

    assert preference.user_id == user.id
    assert preference.telegram_chat_id == "123456"

    fetched = NotificationPreferenceRepository(db_session).get_by_user_id(user.id)
    assert fetched is not None
    assert fetched.telegram_chat_id == "123456"


def test_upsert_updates_the_existing_row_rather_than_creating_a_second_one(db_session) -> None:
    user = _user(db_session)
    repo = NotificationPreferenceRepository(db_session)

    repo.upsert_telegram_chat_id(user.id, "111111")
    db_session.commit()
    repo.upsert_telegram_chat_id(user.id, "222222")
    db_session.commit()

    assert db_session.query(NotificationPreference).filter_by(user_id=user.id).count() == 1
    assert repo.get_by_user_id(user.id).telegram_chat_id == "222222"


def test_upsert_can_clear_the_chat_id_back_to_none(db_session) -> None:
    user = _user(db_session)
    repo = NotificationPreferenceRepository(db_session)

    repo.upsert_telegram_chat_id(user.id, "123456")
    db_session.commit()
    repo.upsert_telegram_chat_id(user.id, None)
    db_session.commit()

    assert repo.get_by_user_id(user.id).telegram_chat_id is None


def test_upsert_bumps_updated_at_on_a_real_update(db_session) -> None:
    user = _user(db_session)
    repo = NotificationPreferenceRepository(db_session)

    first = repo.upsert_telegram_chat_id(user.id, "111111")
    db_session.commit()
    first_updated_at = first.updated_at

    second = repo.upsert_telegram_chat_id(user.id, "222222")
    db_session.commit()

    assert second.updated_at >= first_updated_at


# =====================================================================
# UNIQUE(user_id)
# =====================================================================


def test_unique_constraint_rejects_a_second_row_for_the_same_user(db_session) -> None:
    """The repository's own `upsert_telegram_chat_id` never attempts this
    (it always checks first) - this proves the database itself enforces
    it too, not just application-level discipline."""
    user = _user(db_session)
    db_session.add(NotificationPreference(user_id=user.id, telegram_chat_id="111111"))
    db_session.commit()

    db_session.add(NotificationPreference(user_id=user.id, telegram_chat_id="222222"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_two_different_users_can_each_have_their_own_preference(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    repo = NotificationPreferenceRepository(db_session)

    repo.upsert_telegram_chat_id(user_a.id, "111111")
    db_session.commit()
    repo.upsert_telegram_chat_id(user_b.id, "222222")
    db_session.commit()

    assert repo.get_by_user_id(user_a.id).telegram_chat_id == "111111"
    assert repo.get_by_user_id(user_b.id).telegram_chat_id == "222222"


# =====================================================================
# Cascade delete
# =====================================================================


def test_deleting_the_user_cascades_to_their_notification_preference(db_session) -> None:
    """SQLite ignores `ON DELETE CASCADE` (and every other FK constraint)
    unless `PRAGMA foreign_keys=ON` is issued per-connection - production
    runs Postgres, which always enforces it, so this pragma is only here to
    make SQLite behave like production for this one assertion; it is not
    enabled globally for the test suite."""
    db_session.execute(text("PRAGMA foreign_keys=ON"))

    user = _user(db_session)
    NotificationPreferenceRepository(db_session).upsert_telegram_chat_id(user.id, "123456")
    db_session.commit()

    db_session.delete(user)
    db_session.commit()

    assert db_session.query(NotificationPreference).filter_by(user_id=user.id).count() == 0
