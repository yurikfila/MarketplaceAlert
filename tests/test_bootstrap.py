"""Tests for `core/auth/bootstrap.py` (the cutover core logic) and the
`scripts/create_bootstrap_admin.py` CLI wrapper's password-handling
safety specifically.

Uses the same `db_session`/`session_factory` fixtures as every other
persistence test (`tests/conftest.py`) - an isolated temp-file SQLite
database, never the developer's real one.
"""

from datetime import datetime, timezone

import pytest

from marketplace_alert.core.auth.bootstrap import (
    BootstrapPasswordTooShortError,
    compute_listing_attribution,
    create_bootstrap_user,
    find_bootstrap_user,
    list_unowned_saved_searches,
    run_cutover,
)
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.auth.security import verify_password
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository

_EMAIL = "admin@example.com"
_PASSWORD = "a-strong-bootstrap-password"


def _saved_search(session, *, query="Makita drill", marketplaces=("mock",), user_id=None):
    saved_search = SavedSearchRepository(session).create(
        query=query, marketplaces=list(marketplaces), scan_interval_seconds=300, is_active=True
    )
    if user_id is not None:
        saved_search.user_id = user_id
    session.commit()
    return saved_search


def _listing(session, *, marketplace="mock", external_id="l1", discovered_by_saved_search_id=None):
    now = datetime.now(timezone.utc)
    row = DiscoveredListing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=f"Listing {external_id}",
        listing_url=f"https://example.com/{external_id}",
        first_discovered_at=now,
        last_seen_at=now,
        discovered_by_saved_search_id=discovered_by_saved_search_id,
    )
    session.add(row)
    session.commit()
    return row


# =====================================================================
# Dry run changes nothing
# =====================================================================


def test_dry_run_creates_no_user(db_session) -> None:
    _saved_search(db_session)

    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=False)

    assert db_session.query(User).count() == 0


def test_dry_run_does_not_assign_saved_searches(db_session) -> None:
    saved_search = _saved_search(db_session)

    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=False)

    db_session.refresh(saved_search)
    assert saved_search.user_id is None


def test_dry_run_does_not_attribute_listings(db_session) -> None:
    _saved_search(db_session, marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock")

    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=False)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None


def test_dry_run_report_reflects_what_would_happen(db_session) -> None:
    _saved_search(db_session, marketplaces=("mock",))
    _listing(db_session, marketplace="mock")

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=False)

    assert report.applied is False
    assert report.user_already_existed is False
    assert report.user_id is None
    assert report.saved_search_backfill.unowned_before == 1
    assert report.saved_search_backfill.assigned_count == 0
    # Marketplace-uniqueness alone is never sufficient evidence - see
    # core/auth/bootstrap.py's _find_provenance_backed_saved_search_id.
    assert report.listing_attribution.safely_attributable_count == 0
    assert report.listing_attribution.no_provenance_count == 1


# =====================================================================
# Apply creates the user safely
# =====================================================================


def test_apply_creates_a_user_with_a_real_hashed_password(db_session) -> None:
    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    user = db_session.query(User).filter_by(email=_EMAIL).one()
    assert report.user_id == user.id
    assert user.password_hash != _PASSWORD
    assert verify_password(_PASSWORD, user.password_hash) is True


def test_apply_normalizes_the_bootstrap_email(db_session) -> None:
    run_cutover(db_session, email="  Admin@Example.COM  ", password=_PASSWORD, apply=True)

    assert db_session.query(User).filter_by(email="admin@example.com").count() == 1


def test_apply_without_a_password_for_a_new_account_raises(db_session) -> None:
    with pytest.raises(ValueError):
        run_cutover(db_session, email=_EMAIL, password=None, apply=True)
    assert db_session.query(User).count() == 0


def test_create_bootstrap_user_rejects_a_too_short_password(db_session) -> None:
    with pytest.raises(BootstrapPasswordTooShortError):
        create_bootstrap_user(db_session, email=_EMAIL, password="short")
    db_session.rollback()
    assert db_session.query(User).count() == 0


# =====================================================================
# Reruns are idempotent
# =====================================================================


def test_rerun_does_not_create_a_second_user(db_session) -> None:
    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)
    run_cutover(db_session, email=_EMAIL, password=None, apply=True)  # no password needed - already exists

    assert db_session.query(User).count() == 1


def test_rerun_reports_the_account_already_existed(db_session) -> None:
    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    second_report = run_cutover(db_session, email=_EMAIL, password=None, apply=True)

    assert second_report.user_already_existed is True


def test_rerun_picks_up_newly_created_unowned_saved_searches(db_session) -> None:
    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    new_search = _saved_search(db_session, query="Bosch drill", marketplaces=("mock",))
    second_report = run_cutover(db_session, email=_EMAIL, password=None, apply=True)

    db_session.refresh(new_search)
    assert new_search.user_id == second_report.user_id
    assert second_report.saved_search_backfill.assigned_count == 1


# =====================================================================
# Ownership assignment: unowned assigned, already-owned never touched
# =====================================================================


def test_existing_unowned_saved_searches_are_assigned(db_session) -> None:
    first = _saved_search(db_session, query="Makita drill")
    second = _saved_search(db_session, query="Bosch drill")

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.user_id == report.user_id
    assert second.user_id == report.user_id
    assert report.saved_search_backfill.assigned_count == 2


def test_already_owned_saved_searches_are_never_reassigned(db_session) -> None:
    other_user = User(email="someone-else@example.com", password_hash="irrelevant-hash")
    db_session.add(other_user)
    db_session.commit()

    already_owned = _saved_search(db_session, query="Someone else's search", user_id=other_user.id)

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(already_owned)
    assert already_owned.user_id == other_user.id  # untouched
    assert report.saved_search_backfill.unowned_before == 0
    assert report.saved_search_backfill.assigned_count == 0


# =====================================================================
# Historical listing attribution
# =====================================================================


def test_a_single_candidate_saved_search_for_the_marketplace_is_not_enough_to_attribute(db_session) -> None:
    """The core correction: marketplace-uniqueness alone is a
    correlation, never provenance. Exactly one saved search targeting a
    listing's marketplace must NOT be treated as sufficient evidence."""
    _saved_search(db_session, marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock")

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None
    assert report.listing_attribution.safely_attributable_count == 0
    assert report.listing_attribution.no_provenance_count == 1


def test_multiple_candidate_saved_searches_remain_unowned(db_session) -> None:
    _saved_search(db_session, query="Makita drill", marketplaces=("mock",))
    _saved_search(db_session, query="Bosch drill", marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock")

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None
    assert report.listing_attribution.ambiguous_count == 1
    assert report.listing_attribution.safely_attributable_count == 0


def test_zero_candidate_saved_searches_remain_unowned(db_session) -> None:
    _saved_search(db_session, marketplaces=("mock",))
    listing = _listing(db_session, marketplace="etsy")  # no saved search targets etsy

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None
    assert report.listing_attribution.no_provenance_count == 1
    assert report.listing_attribution.safely_attributable_count == 0


def test_already_attributed_listing_is_never_touched(db_session) -> None:
    """A row that already has SOME discovering-search attribution (a
    normal scan, not the pre-cutover gap) must never be reconsidered by
    this heuristic at all - it already has a real, non-guessed answer."""
    original_search = _saved_search(db_session, query="Original finder", marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock", discovered_by_saved_search_id=original_search.id)

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id == original_search.id
    assert report.listing_attribution.already_attributed_count == 1
    assert report.listing_attribution.total_unattributed == 0


def test_never_fabricates_ownership_regardless_of_candidate_count(db_session) -> None:
    """Sanity check on the whole heuristic's restraint: three unowned
    listings across three different marketplace-candidate-count outcomes
    must all land unowned - none of them "helpfully" attributed, since
    none of them carry genuine provenance evidence."""
    _saved_search(db_session, query="Makita drill", marketplaces=("mock", "etsy"))
    _saved_search(db_session, query="Bosch drill", marketplaces=("etsy",))
    single_candidate = _listing(db_session, marketplace="mock", external_id="single-candidate")
    two_candidates = _listing(db_session, marketplace="etsy", external_id="two-candidates")
    no_candidate = _listing(db_session, marketplace="reverb", external_id="no-candidate")

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(single_candidate)
    db_session.refresh(two_candidates)
    db_session.refresh(no_candidate)
    assert single_candidate.discovered_by_saved_search_id is None
    assert two_candidates.discovered_by_saved_search_id is None
    assert no_candidate.discovered_by_saved_search_id is None
    assert report.listing_attribution.total_unattributed == 3
    assert report.listing_attribution.safely_attributable_count == 0
    assert report.listing_attribution.ambiguous_count == 1  # two_candidates only
    assert report.listing_attribution.no_provenance_count == 2  # single_candidate + no_candidate


def test_rerun_does_not_attribute_a_listing_even_after_a_conflicting_search_is_deleted(db_session) -> None:
    """Confirms the fix directly: reducing "ambiguous" down to a single
    marketplace candidate (by deleting the conflict) must NOT cause
    attribution on a later run - marketplace-candidate-count was never a
    legitimate basis for the decision, at any count."""
    first = _saved_search(db_session, query="Makita drill", marketplaces=("mock",))
    second = _saved_search(db_session, query="Bosch drill", marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock")

    run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)
    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None

    SavedSearchRepository(db_session).delete(second.id)
    db_session.commit()

    second_report = run_cutover(db_session, email=_EMAIL, password=None, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None
    assert second_report.listing_attribution.safely_attributable_count == 0
    assert first.id  # sanity: the surviving search is still there, just never used to attribute


def test_rerun_is_idempotent_for_attribution_too(db_session) -> None:
    _saved_search(db_session, marketplaces=("mock",))
    listing = _listing(db_session, marketplace="mock")

    first_report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)
    second_report = run_cutover(db_session, email=_EMAIL, password=None, apply=True)

    db_session.refresh(listing)
    assert listing.discovered_by_saved_search_id is None
    assert first_report.listing_attribution.no_provenance_count == 1
    assert second_report.listing_attribution.no_provenance_count == 1


def test_attribution_mechanism_works_when_genuine_provenance_is_available(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the attribution *mechanism* is real and correctly wired -
    not just permanently dead code - by simulating a hypothetical future
    schema that DOES provide genuine provenance for one specific row,
    without changing marketplace-based reasoning at all. Everything else
    (multiple candidates, no candidates) must still be left unowned."""
    import marketplace_alert.core.auth.bootstrap as bootstrap_module

    saved_search = _saved_search(db_session, marketplaces=("mock",))
    provable = _listing(db_session, marketplace="mock", external_id="provable")
    unprovable = _listing(db_session, marketplace="mock", external_id="unprovable")

    real_finder = bootstrap_module._find_provenance_backed_saved_search_id

    def fake_finder(row, candidate_ids_by_marketplace):
        if row.id == provable.id:
            return saved_search.id
        return real_finder(row, candidate_ids_by_marketplace)

    monkeypatch.setattr(bootstrap_module, "_find_provenance_backed_saved_search_id", fake_finder)

    report = run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)

    db_session.refresh(provable)
    db_session.refresh(unprovable)
    assert provable.discovered_by_saved_search_id == saved_search.id
    assert unprovable.discovered_by_saved_search_id is None
    assert report.listing_attribution.safely_attributable_count == 1


# =====================================================================
# compute_listing_attribution in isolation (no user needed at all)
# =====================================================================


def test_compute_listing_attribution_with_no_candidates_marks_everything_no_provenance(db_session) -> None:
    _listing(db_session, marketplace="mock")

    result = compute_listing_attribution(db_session, candidate_saved_search_ids=[])

    assert result.no_provenance_count == 1
    assert result.safely_attributable_count == 0
    assert result.ambiguous_count == 0


# =====================================================================
# Transaction rollback on failure
# =====================================================================


def test_failure_during_backfill_rolls_back_without_committing(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a failure partway through the (single, atomic) backfill
    transaction - nothing from that half must be committed."""
    saved_search = _saved_search(db_session, marketplaces=("mock",))

    import marketplace_alert.core.auth.bootstrap as bootstrap_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure during listing attribution")

    monkeypatch.setattr(bootstrap_module, "apply_listing_attribution", _boom)

    with pytest.raises(RuntimeError):
        run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)
    db_session.rollback()

    # The user WAS committed (stage 1, independent - see bootstrap.py's
    # docstring) - but the saved-search assignment (stage 2, same
    # transaction as the failed listing attribution) must not have been.
    assert db_session.query(User).count() == 1
    db_session.refresh(saved_search)
    assert saved_search.user_id is None


def test_failure_before_any_commit_leaves_no_user_created(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marketplace_alert.core.auth.bootstrap as bootstrap_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure during user creation")

    monkeypatch.setattr(bootstrap_module, "create_bootstrap_user", _boom)

    with pytest.raises(RuntimeError):
        run_cutover(db_session, email=_EMAIL, password=_PASSWORD, apply=True)
    db_session.rollback()

    assert db_session.query(User).count() == 0


# =====================================================================
# The bootstrap script never leaks the password
# =====================================================================


def test_script_stdout_never_contains_the_password(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import sys

    from sqlalchemy.orm import sessionmaker

    import scripts.create_bootstrap_admin as script_module
    from marketplace_alert.core.persistence.database import Base, create_db_engine

    # Isolated engine - never the developer's real database.
    engine = create_db_engine(f"sqlite:///{tmp_path / 'bootstrap_script_test.db'}")
    Base.metadata.create_all(bind=engine)
    test_session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(script_module, "SessionLocal", test_session_local)

    secret_password = "unmistakable-sentinel-password-xyz789"
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", secret_password)
    monkeypatch.setattr(sys, "argv", ["create_bootstrap_admin.py", "--email", _EMAIL, "--apply"])

    exit_code = script_module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert secret_password not in captured.out
    assert secret_password not in captured.err

    engine.dispose()
