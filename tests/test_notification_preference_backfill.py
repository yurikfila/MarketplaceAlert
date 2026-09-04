"""Tests for `core/notifications/preference_backfill.py` - the cutover
core logic behind `scripts/backfill_notification_preference.py`.

Uses the same `db_session` fixture as every other persistence test
(`tests/conftest.py`) - an isolated temp-file SQLite database, never the
developer's real one.
"""

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.notifications.preference_backfill import run_preference_backfill
from marketplace_alert.core.notifications.preferences_repository import NotificationPreferenceRepository

_EMAIL = "existing-user@example.com"
_GLOBAL_CHAT_ID = "999888777"


def _existing_user(session, email: str = _EMAIL) -> User:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user


# =====================================================================
# Dry run changes nothing
# =====================================================================


def test_dry_run_writes_no_preference(db_session) -> None:
    user = _existing_user(db_session)

    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=False)

    assert NotificationPreferenceRepository(db_session).get_by_user_id(user.id) is None


def test_dry_run_report_reflects_what_would_happen(db_session) -> None:
    user = _existing_user(db_session)

    report = run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=False)

    assert report.applied is False
    assert report.user_found is True
    assert report.user_id == user.id
    assert report.already_had_a_preference is False


def test_dry_run_for_a_nonexistent_email_reports_not_found_and_writes_nothing(db_session) -> None:
    report = run_preference_backfill(
        db_session, email="nobody@example.com", telegram_chat_id=_GLOBAL_CHAT_ID, apply=False
    )

    assert report.user_found is False
    assert report.applied is False
    assert db_session.query(User).count() == 0


# =====================================================================
# Apply targets only the specified, existing account
# =====================================================================


def test_apply_sets_the_named_users_preference_from_the_given_chat_id(db_session) -> None:
    user = _existing_user(db_session)

    report = run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    assert report.applied is True
    preference = NotificationPreferenceRepository(db_session).get_by_user_id(user.id)
    assert preference is not None
    assert preference.telegram_chat_id == _GLOBAL_CHAT_ID


def test_apply_for_a_nonexistent_email_creates_no_user_and_writes_nothing(db_session) -> None:
    """Unlike core/auth/bootstrap.py, this script has no legitimate reason
    to create an account - a nonexistent email is always reported as
    not-found, never silently created."""
    report = run_preference_backfill(
        db_session, email="nobody@example.com", telegram_chat_id=_GLOBAL_CHAT_ID, apply=True
    )

    assert report.user_found is False
    assert report.applied is False
    assert db_session.query(User).count() == 0


def test_apply_does_not_touch_a_different_users_preference(db_session) -> None:
    target = _existing_user(db_session, _EMAIL)
    other = _existing_user(db_session, "someone-else@example.com")
    NotificationPreferenceRepository(db_session).upsert_telegram_chat_id(other.id, "should-not-move")
    db_session.commit()

    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    other_preference = NotificationPreferenceRepository(db_session).get_by_user_id(other.id)
    assert other_preference.telegram_chat_id == "should-not-move"  # untouched
    target_preference = NotificationPreferenceRepository(db_session).get_by_user_id(target.id)
    assert target_preference.telegram_chat_id == _GLOBAL_CHAT_ID


# =====================================================================
# Idempotency - never overwrites an existing row
# =====================================================================


def test_rerun_is_idempotent_and_does_not_duplicate(db_session) -> None:
    user = _existing_user(db_session)

    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)
    second_report = run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    assert second_report.already_had_a_preference is True
    assert second_report.applied is False
    preference = NotificationPreferenceRepository(db_session).get_by_user_id(user.id)
    assert preference.telegram_chat_id == _GLOBAL_CHAT_ID


def test_rerun_never_overwrites_a_value_the_user_set_themselves(db_session) -> None:
    """Simulates the real-world sequence: the script runs once, then the
    user changes their own preference via PUT /api/v1/notification-
    preferences/me, then someone accidentally re-runs the script - their
    own choice must survive untouched."""
    user = _existing_user(db_session)
    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    # The user changes it themselves afterwards.
    NotificationPreferenceRepository(db_session).upsert_telegram_chat_id(user.id, "user-chosen-value")
    db_session.commit()

    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    preference = NotificationPreferenceRepository(db_session).get_by_user_id(user.id)
    assert preference.telegram_chat_id == "user-chosen-value"


def test_rerun_never_overwrites_even_an_explicitly_cleared_preference(db_session) -> None:
    """A user who explicitly cleared their preference (telegram_chat_id =
    None) still counts as "already had a preference row" - the backfill
    must not resurrect the old global value over their explicit choice."""
    user = _existing_user(db_session)
    run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    NotificationPreferenceRepository(db_session).upsert_telegram_chat_id(user.id, None)
    db_session.commit()

    report = run_preference_backfill(db_session, email=_EMAIL, telegram_chat_id=_GLOBAL_CHAT_ID, apply=True)

    assert report.already_had_a_preference is True
    assert report.applied is False
    preference = NotificationPreferenceRepository(db_session).get_by_user_id(user.id)
    assert preference.telegram_chat_id is None
