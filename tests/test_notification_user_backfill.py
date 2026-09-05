"""Tests for `core/persistence/notification_user_backfill.py` - the
Phase 2C cutover core logic behind
`scripts/backfill_pending_notification_users.py`.

Uses the same `db_session` fixture as every other persistence test
(`tests/conftest.py`) - an isolated temp-file SQLite database, never the
developer's real one.
"""

import pytest

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.models import DiscoveredListing, PendingNotification
from marketplace_alert.core.persistence.notification_outbox import NotificationOutboxRepository
from marketplace_alert.core.persistence.notification_user_backfill import run_notification_user_backfill
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _user(session, email: str) -> User:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user


def _saved_search(session, *, query: str = "Drill", user_id: int | None = None):
    saved_search = SavedSearchRepository(session).create(
        query=query, marketplaces=["mock"], scan_interval_seconds=300, is_active=True, user_id=user_id
    )
    session.commit()
    return saved_search


def _listing(session, *, external_id: str, discovered_by_saved_search_id: int | None = None) -> DiscoveredListing:
    row, _ = ListingRepository(session).get_or_create(
        Listing(
            marketplace="mock",
            external_listing_id=external_id,
            title=f"Listing {external_id}",
            listing_url=f"https://example.com/{external_id}",
        ),
        saved_search_id=discovered_by_saved_search_id,
    )
    session.commit()
    return row


def _enqueue(session, discovered_listing_id: int, *, user_id: int | None = None) -> PendingNotification:
    notification = NotificationOutboxRepository(session).enqueue(discovered_listing_id, user_id=user_id)
    session.commit()
    return notification


# =====================================================================
# Dry run changes nothing
# =====================================================================


def test_dry_run_writes_no_user_id(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    notification = _enqueue(db_session, listing.id)

    run_notification_user_backfill(db_session, apply=False)

    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id is None


def test_dry_run_report_reflects_a_safely_resolvable_row(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=False)

    assert report.applied is False
    assert report.total_pending_notifications == 1
    assert report.already_has_user_id_count == 0
    assert report.null_user_id_examined_count == 1
    assert report.safely_resolvable_count == 1
    assert report.would_update_count == 1
    assert report.updated_count == 0
    assert report.unresolved_no_discovering_search_count == 0
    assert report.unresolved_missing_saved_search_count == 0
    assert report.unresolved_unowned_saved_search_count == 0


# =====================================================================
# Apply sets the exact SavedSearch.user_id
# =====================================================================


def test_apply_sets_the_exact_saved_search_user_id(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.applied is True
    assert report.updated_count == 1
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id == user.id


def test_apply_never_overwrites_an_already_populated_user_id(db_session) -> None:
    """Even if the legacy chain points somewhere else entirely, an
    already-stamped row must never be touched - Phase 2C only fills
    genuine gaps, it never corrects or transfers existing ownership."""
    stamped_user = _user(db_session, "stamped@example.com")
    chain_user = _user(db_session, "chain-owner@example.com")
    search = _saved_search(db_session, user_id=chain_user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    notification = _enqueue(db_session, listing.id, user_id=stamped_user.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.already_has_user_id_count == 1
    assert report.null_user_id_examined_count == 0
    assert report.updated_count == 0
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id == stamped_user.id


# =====================================================================
# Unresolved cases - never guessed
# =====================================================================


def test_no_discovering_search_is_skipped(db_session) -> None:
    """discovered_by_saved_search_id IS NULL - e.g. a legacy `/scan` row."""
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=None)
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.unresolved_no_discovering_search_count == 1
    assert report.safely_resolvable_count == 0
    assert report.updated_count == 0
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id is None


def test_missing_saved_search_is_skipped(db_session) -> None:
    """discovered_by_saved_search_id points at a row that doesn't exist -
    a plain bogus int is enough here; SQLite doesn't enforce this FK in
    tests (same note as tests/test_backfill.py's equivalent case), and in
    production this FK is `ON DELETE SET NULL`, so this specific
    combination is a narrow, real race (search deleted between the
    listing being discovered and this backfill running), not the normal
    case - never guessed regardless of how it arises."""
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=999_999)
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.unresolved_missing_saved_search_count == 1
    assert report.safely_resolvable_count == 0
    assert report.updated_count == 0
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id is None


def test_unowned_saved_search_is_skipped(db_session) -> None:
    """The saved search exists but has no owner yet (pre-cutover, never
    backfilled) - never guessed."""
    search = _saved_search(db_session, user_id=None)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.unresolved_unowned_saved_search_count == 1
    assert report.safely_resolvable_count == 0
    assert report.updated_count == 0
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id is None


# =====================================================================
# Never creates rows; never infers from ListingAttribution
# =====================================================================


def test_apply_never_creates_a_new_pending_notification_row(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    _enqueue(db_session, listing.id)

    before = db_session.query(PendingNotification).count()
    run_notification_user_backfill(db_session, apply=True)
    after = db_session.query(PendingNotification).count()

    assert before == after == 1


def test_listing_attribution_is_never_consulted_for_an_unresolvable_row(db_session) -> None:
    """A listing with no discovering search (legacy chain unresolvable)
    may still have a real `ListingAttribution` pointing at some other
    user entirely (e.g. from Phase 1 backfill, or a later scan by a
    different saved search) - this must never be used as a substitute
    owner. The row must remain unresolved, exactly as the legacy chain
    alone says it is."""
    attributed_user = _user(db_session, "attributed@example.com")
    other_search = _saved_search(db_session, query="Other search", user_id=attributed_user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=None)
    ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=other_search.id, discovered_listing_id=listing.id
    )
    db_session.commit()
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.unresolved_no_discovering_search_count == 1
    assert report.updated_count == 0
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id is None


def test_listing_attribution_is_never_preferred_over_a_different_legacy_owner(db_session) -> None:
    """The legacy chain resolves to one user; a *different* user
    independently has a `ListingAttribution` for the same listing (a
    second saved search matched it too, Phase 1). The backfill must
    stamp the legacy chain's owner, never the attribution's."""
    legacy_owner = _user(db_session, "legacy-owner@example.com")
    legacy_search = _saved_search(db_session, query="Legacy search", user_id=legacy_owner.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=legacy_search.id)

    other_user = _user(db_session, "attributed-other@example.com")
    other_search = _saved_search(db_session, query="Other search", user_id=other_user.id)
    ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=other_search.id, discovered_listing_id=listing.id
    )
    db_session.commit()
    notification = _enqueue(db_session, listing.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.updated_count == 1
    refreshed = db_session.get(PendingNotification, notification.id)
    assert refreshed.user_id == legacy_owner.id
    assert refreshed.user_id != other_user.id


# =====================================================================
# Idempotency
# =====================================================================


def test_rerun_after_apply_shows_zero_remaining_safe_updates(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    _enqueue(db_session, listing.id)

    first_apply = run_notification_user_backfill(db_session, apply=True)
    assert first_apply.updated_count == 1

    second_dry_run = run_notification_user_backfill(db_session, apply=False)

    assert second_dry_run.would_update_count == 0
    assert second_dry_run.safely_resolvable_count == 0
    assert second_dry_run.already_has_user_id_count == 1
    assert second_dry_run.null_user_id_examined_count == 0


def test_rerun_leaves_genuinely_unresolvable_rows_unresolved(db_session) -> None:
    """Idempotency must not be confused with "eventually resolves" - a
    row that's genuinely unresolvable stays that way across reruns,
    never silently guessed just because it's been examined before."""
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=None)
    _enqueue(db_session, listing.id)

    run_notification_user_backfill(db_session, apply=True)
    second_report = run_notification_user_backfill(db_session, apply=True)

    assert second_report.unresolved_no_discovering_search_count == 1
    assert second_report.updated_count == 0


# =====================================================================
# Multiple rows / mixed classifications
# =====================================================================


def test_mixed_batch_produces_exact_counts_per_category(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    owned_search = _saved_search(db_session, query="Owned", user_id=user_a.id)
    unowned_search = _saved_search(db_session, query="Unowned", user_id=None)

    resolvable_listing = _listing(db_session, external_id="resolvable", discovered_by_saved_search_id=owned_search.id)
    no_search_listing = _listing(db_session, external_id="no-search", discovered_by_saved_search_id=None)
    missing_search_listing = _listing(
        db_session, external_id="missing-search", discovered_by_saved_search_id=888_888
    )
    unowned_search_listing = _listing(
        db_session, external_id="unowned-search", discovered_by_saved_search_id=unowned_search.id
    )
    already_stamped_user = _user(db_session, "already-stamped@example.com")
    already_stamped_listing = _listing(
        db_session, external_id="already-stamped", discovered_by_saved_search_id=owned_search.id
    )

    _enqueue(db_session, resolvable_listing.id)
    _enqueue(db_session, no_search_listing.id)
    _enqueue(db_session, missing_search_listing.id)
    _enqueue(db_session, unowned_search_listing.id)
    _enqueue(db_session, already_stamped_listing.id, user_id=already_stamped_user.id)

    report = run_notification_user_backfill(db_session, apply=True)

    assert report.total_pending_notifications == 5
    assert report.already_has_user_id_count == 1
    assert report.null_user_id_examined_count == 4
    assert report.safely_resolvable_count == 1
    assert report.unresolved_no_discovering_search_count == 1
    assert report.unresolved_missing_saved_search_count == 1
    assert report.unresolved_unowned_saved_search_count == 1
    assert report.updated_count == 1


# =====================================================================
# Transaction safety
# =====================================================================


def test_no_writes_are_persisted_if_an_unexpected_error_occurs_mid_apply(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """All updates happen in one single transaction (one commit at the
    very end) - if something unexpected fails partway through resolving
    rows, nothing should have been persisted yet. Simulated here by
    making the *second* row's resolution raise, after the first row was
    already (in-memory) marked resolved."""
    user = _user(db_session, "a@example.com")
    search = _saved_search(db_session, user_id=user.id)
    listing_1 = _listing(db_session, external_id="one", discovered_by_saved_search_id=search.id)
    listing_2 = _listing(db_session, external_id="two", discovered_by_saved_search_id=search.id)
    _enqueue(db_session, listing_1.id)
    _enqueue(db_session, listing_2.id)

    import marketplace_alert.core.persistence.notification_user_backfill as backfill_module

    real_resolve = backfill_module._resolve_legacy_owner
    calls = {"count": 0}

    def _flaky_resolve(session, discovered_listing_id):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated unexpected failure")
        return real_resolve(session, discovered_listing_id)

    monkeypatch.setattr(backfill_module, "_resolve_legacy_owner", _flaky_resolve)

    with pytest.raises(RuntimeError):
        run_notification_user_backfill(db_session, apply=True)
    db_session.rollback()

    verify = db_session.query(PendingNotification).filter(PendingNotification.user_id.isnot(None)).count()
    assert verify == 0
