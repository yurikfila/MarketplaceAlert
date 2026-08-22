"""Tests for `marketplace_alert/core/persistence/cleanup.py` - re-evaluating
pre-existing `DiscoveredListing` rows against the current relevance engine
and the current saved searches.

Uses the isolated `db_session` fixture (temp SQLite, never the developer's
real `marketplace_alert.db`) - same as `tests/test_persistence.py`. Rows
are persisted directly via `ListingDiscoveryService`, simulating listings
that were discovered before relevance filtering existed (exactly the
scenario this module cleans up).
"""

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.cleanup import (
    preview_historical_cleanup,
    run_historical_cleanup,
)
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.persistence.service import ListingDiscoveryService
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _listing(title: str, *, marketplace: str = "etsy", external_id: str = "ext-1") -> Listing:
    return Listing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
    )


def _persist(session, *listings: Listing) -> None:
    """Simulate historical, pre-relevance-filtering persistence - directly
    through ListingDiscoveryService, bypassing relevance filtering
    entirely, same as rows written before this module existed."""
    ListingDiscoveryService(session).process_listings(list(listings))


def _create_saved_search(session, *, query: str, marketplaces: list[str], is_active: bool = True) -> None:
    SavedSearchRepository(session).create(
        query=query, marketplaces=marketplaces, scan_interval_seconds=60, is_active=is_active
    )
    session.commit()


# --- core behavior: relevant kept, irrelevant removed ----------------------


def test_relevant_historical_listing_is_kept(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(db_session, _listing("Makita Cordless Drill 18V", external_id="good-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.removed_count == 0
    assert result.kept_count == 1
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="good-1").first() is not None


def test_irrelevant_historical_listing_is_removed(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(db_session, _listing("Makita Battery Holder Wall Mount", external_id="bad-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.removed_count == 1
    assert result.kept_count == 0
    assert result.removed[0].external_listing_id == "bad-1"
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="bad-1").first() is None


def test_brand_conflicting_historical_listing_is_removed(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(db_session, _listing("DeWalt 20V Drill DCD771C2", external_id="dewalt-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.removed_count == 1
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="dewalt-1").first() is None


def test_mixed_relevant_and_irrelevant_listings_are_separated(db_session) -> None:
    _create_saved_search(db_session, query="Bosch drill", marketplaces=["etsy"])
    _persist(
        db_session,
        _listing("Bosch Rotary Hammer Drill GBH", external_id="keep-1"),
        _listing("Bosch Drill Bit Holder Organizer", external_id="remove-1"),
        _listing("Makita Cordless Drill 18V", external_id="remove-2"),
    )

    result = run_historical_cleanup(db_session)
    db_session.commit()

    remaining_ids = {row.external_listing_id for row in db_session.query(DiscoveredListing).all()}
    assert remaining_ids == {"keep-1"}
    assert result.kept_count == 1
    assert result.removed_count == 2
    assert {r.external_listing_id for r in result.removed} == {"remove-1", "remove-2"}


# --- listing relevant to ANY current saved search targeting its marketplace


def test_listing_kept_if_relevant_to_at_least_one_of_several_saved_searches(db_session) -> None:
    """A listing that fails one saved search's query but matches another's
    (both targeting the same marketplace) must be kept - some current
    interest still wants it."""
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _create_saved_search(db_session, query="Makita battery holder", marketplaces=["etsy"])
    _persist(db_session, _listing("Makita Battery Holder Wall Mount", external_id="holder-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.kept_count == 1
    assert result.removed_count == 0
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="holder-1").first() is not None


# --- the schema-limitation policy: no saved search to evaluate against -----


def test_listing_with_no_current_saved_search_for_its_marketplace_is_preserved(db_session) -> None:
    """If every saved search that ever targeted this marketplace has since
    been deleted, there is no query left to evaluate this listing against -
    the schema doesn't record which search originally found it, so it must
    be left untouched rather than guessed at."""
    _persist(db_session, _listing("Totally Unrelated Item", marketplace="ebay", external_id="orphan-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.skipped_no_saved_search_count == 1
    assert result.removed_count == 0
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="orphan-1").first() is not None


def test_only_matching_marketplace_saved_searches_are_considered(db_session) -> None:
    """A saved search for "etsy" must not make an "ebay" listing evaluable -
    marketplace scoping must be respected, not just query text."""
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(db_session, _listing("Makita Cordless Drill 18V", marketplace="ebay", external_id="ebay-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    # No saved search targets "ebay" - unevaluable, so preserved untouched.
    assert result.skipped_no_saved_search_count == 1
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="ebay-1").first() is not None


# --- paused (inactive) saved searches still count as current interest ------


def test_paused_saved_search_still_counts_as_a_current_interest(db_session) -> None:
    """Pausing a saved search is not the same as deleting it - a listing it
    would still find relevant must not be removed just because the search
    is currently inactive."""
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"], is_active=False)
    _persist(db_session, _listing("Makita Cordless Drill 18V", external_id="paused-1"))

    result = run_historical_cleanup(db_session)
    db_session.commit()

    assert result.kept_count == 1
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="paused-1").first() is not None


# --- dry run never mutates anything -----------------------------------------


def test_preview_does_not_delete_anything(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(db_session, _listing("Makita Battery Holder Wall Mount", external_id="bad-1"))

    result = preview_historical_cleanup(db_session)

    assert result.removed_count == 1
    # Still there - preview must never delete.
    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="bad-1").first() is not None


def test_preview_and_run_agree_on_what_would_be_removed(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(
        db_session,
        _listing("Makita Cordless Drill 18V", external_id="good-1"),
        _listing("Makita Battery Holder Wall Mount", external_id="bad-1"),
    )

    preview = preview_historical_cleanup(db_session)
    actual = run_historical_cleanup(db_session)
    db_session.commit()

    assert {r.external_listing_id for r in preview.removed} == {r.external_listing_id for r in actual.removed}
    assert preview.kept_count == actual.kept_count


# --- safety: saved searches themselves are never touched --------------------


def test_cleanup_never_modifies_or_deletes_saved_searches(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _create_saved_search(db_session, query="Bosch drill", marketplaces=["etsy"], is_active=False)
    _persist(db_session, _listing("Makita Battery Holder Wall Mount", external_id="bad-1"))

    before = {s.id: (s.query, s.is_active, s.marketplaces) for s in SavedSearchRepository(db_session).list_all()}
    run_historical_cleanup(db_session)
    db_session.commit()
    after = {s.id: (s.query, s.is_active, s.marketplaces) for s in SavedSearchRepository(db_session).list_all()}

    assert before == after


# --- idempotency: running twice in a row is a clean no-op the second time --


def test_running_cleanup_twice_is_idempotent(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(
        db_session,
        _listing("Makita Cordless Drill 18V", external_id="good-1"),
        _listing("Makita Battery Holder Wall Mount", external_id="bad-1"),
    )

    first = run_historical_cleanup(db_session)
    db_session.commit()
    assert first.removed_count == 1

    second = run_historical_cleanup(db_session)
    db_session.commit()
    assert second.removed_count == 0
    assert second.kept_count == 1


# --- row count accounting ---------------------------------------------------


def test_result_counts_add_up_to_total_rows(db_session) -> None:
    _create_saved_search(db_session, query="Makita drill", marketplaces=["etsy"])
    _persist(
        db_session,
        _listing("Makita Cordless Drill 18V", external_id="a"),
        _listing("Makita Battery Holder Wall Mount", external_id="b"),
        _listing("Totally Unrelated Item", marketplace="ebay", external_id="c"),
    )

    result = preview_historical_cleanup(db_session)

    assert result.total_rows == 3
    assert result.evaluated_count == 2  # both "etsy" rows
    assert result.skipped_no_saved_search_count == 1  # the "ebay" row - no saved search targets ebay
    assert result.kept_count + result.removed_count == result.evaluated_count


# --- repository additions used by this module -------------------------------


def test_listing_repository_list_all_returns_every_row(db_session) -> None:
    _persist(
        db_session,
        _listing("Item One", external_id="one"),
        _listing("Item Two", external_id="two"),
    )

    rows = ListingRepository(db_session).list_all()

    assert {row.external_listing_id for row in rows} == {"one", "two"}


def test_listing_repository_delete_removes_the_row(db_session) -> None:
    _persist(db_session, _listing("Item One", external_id="one"))
    row = db_session.query(DiscoveredListing).filter_by(external_listing_id="one").one()

    ListingRepository(db_session).delete(row)
    db_session.commit()

    assert db_session.query(DiscoveredListing).filter_by(external_listing_id="one").first() is None
