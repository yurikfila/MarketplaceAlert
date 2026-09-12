"""Tests for `core/auth/admin_promotion.py` - the business logic behind
`scripts/set_admin.py`. Session-level only (no HTTP, no CLI subprocess) -
mirrors `tests/test_auth_repository.py`'s own style for testing
persistence-adjacent logic directly against the `db_session` fixture.
"""

from marketplace_alert.core.auth.admin_promotion import promote_to_admin
from marketplace_alert.core.auth.repository import UserRepository


def _create_user(db_session, *, email="person@example.com", is_admin=False):
    user = UserRepository(db_session).create(email=email, password_hash="irrelevant-hash")
    if is_admin:
        user.is_admin = True
        db_session.commit()
    return user


def test_dry_run_reports_would_promote_but_writes_nothing(db_session) -> None:
    user = _create_user(db_session, email="owner@example.com")

    report = promote_to_admin(db_session, email="owner@example.com", apply=False)

    assert report.user_found is True
    assert report.already_admin is False
    assert report.applied is False
    db_session.refresh(user)
    assert user.is_admin is False


def test_apply_promotes_an_existing_user(db_session) -> None:
    user = _create_user(db_session, email="owner@example.com")

    report = promote_to_admin(db_session, email="owner@example.com", apply=True)

    assert report.applied is True
    db_session.refresh(user)
    assert user.is_admin is True


def test_apply_is_idempotent_against_an_already_admin_account(db_session) -> None:
    _create_user(db_session, email="owner@example.com", is_admin=True)

    report = promote_to_admin(db_session, email="owner@example.com", apply=True)

    assert report.user_found is True
    assert report.already_admin is True
    # already admin - this call did not need to (and did not) apply anything new
    assert report.applied is False


def test_unknown_email_reports_not_found_and_creates_no_account(db_session) -> None:
    report = promote_to_admin(db_session, email="nobody@example.com", apply=True)

    assert report.user_found is False
    assert report.already_admin is False
    assert report.applied is False
    assert UserRepository(db_session).get_by_email("nobody@example.com") is None


def test_email_lookup_is_normalized(db_session) -> None:
    _create_user(db_session, email="owner@example.com")

    report = promote_to_admin(db_session, email="  OWNER@EXAMPLE.COM  ", apply=True)

    assert report.user_found is True
    assert report.email == "owner@example.com"
    assert report.applied is True


def test_promotion_never_touches_password_hash_or_other_fields(db_session) -> None:
    user = _create_user(db_session, email="owner@example.com")
    original_password_hash = user.password_hash

    promote_to_admin(db_session, email="owner@example.com", apply=True)

    db_session.refresh(user)
    assert user.password_hash == original_password_hash
    assert user.is_active is True
