"""Tests for `core/persistence/listing_attribution_backfill.py` - the
cutover core logic behind `scripts/backfill_listing_attributions.py`.

Uses the same `db_session` fixture as every other persistence test
(`tests/conftest.py`) - an isolated temp-file SQLite database, never the
developer's real one.
"""

from datetime import datetime, timezone

from marketplace_alert.core.persistence.listing_attribution_backfill import run_listing_attribution_backfill
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _saved_search(session, *, query: str = "Drill"):
    saved_search = SavedSearchRepository(session).create(
        query=query, marketplaces=["mock"], scan_interval_seconds=300, is_active=True
    )
    session.commit()
    return saved_search


def _listing(session, *, external_id: str, discovered_by_saved_search_id: int | None = None) -> DiscoveredListing:
    now = datetime.now(timezone.utc)
    row = DiscoveredListing(
        marketplace="mock",
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
# Dry run writes nothing
# =====================================================================


def test_dry_run_writes_no_attribution(db_session) -> None:
    search = _saved_search(db_session)
    _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)

    run_listing_attribution_backfill(db_session, apply=False)

    assert db_session.query(ListingAttribution).count() == 0


def test_dry_run_report_reflects_what_would_happen(db_session) -> None:
    search = _saved_search(db_session)
    _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    _listing(db_session, external_id="b", discovered_by_saved_search_id=None)

    report = run_listing_attribution_backfill(db_session, apply=False)

    assert report.applied is False
    assert report.total_discovered_listings == 2
    assert report.candidates_with_known_attribution == 1
    assert report.skipped_null_attribution_count == 1
    assert report.would_create_count == 1
    assert report.created_count == 0


# =====================================================================
# Apply copies only trustworthy existing facts
# =====================================================================


def test_apply_creates_attribution_for_every_known_discovering_search(db_session) -> None:
    search_a = _saved_search(db_session, query="A")
    search_b = _saved_search(db_session, query="B")
    listing_a = _listing(db_session, external_id="a", discovered_by_saved_search_id=search_a.id)
    listing_b = _listing(db_session, external_id="b", discovered_by_saved_search_id=search_b.id)

    report = run_listing_attribution_backfill(db_session, apply=True)

    assert report.applied is True
    assert report.created_count == 2
    repo = ListingAttributionRepository(db_session)
    assert repo.get(saved_search_id=search_a.id, discovered_listing_id=listing_a.id) is not None
    assert repo.get(saved_search_id=search_b.id, discovered_listing_id=listing_b.id) is not None


def test_apply_never_touches_rows_with_no_discovering_search(db_session) -> None:
    """discovered_by_saved_search_id IS NULL - never infer ownership, never
    guess based on title/query/relevance."""
    _listing(db_session, external_id="orphan", discovered_by_saved_search_id=None)

    report = run_listing_attribution_backfill(db_session, apply=True)

    assert report.skipped_null_attribution_count == 1
    assert report.created_count == 0
    assert db_session.query(ListingAttribution).count() == 0


def test_apply_with_nothing_to_migrate_writes_nothing(db_session) -> None:
    report = run_listing_attribution_backfill(db_session, apply=True)

    assert report.total_discovered_listings == 0
    assert report.applied is False
    assert db_session.query(ListingAttribution).count() == 0


# =====================================================================
# Idempotency
# =====================================================================


def test_rerun_is_idempotent_and_does_not_duplicate(db_session) -> None:
    search = _saved_search(db_session)
    _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)

    run_listing_attribution_backfill(db_session, apply=True)
    second_report = run_listing_attribution_backfill(db_session, apply=True)

    assert second_report.already_attributed_count == 1
    assert second_report.created_count == 0
    assert db_session.query(ListingAttribution).count() == 1


def test_rerun_does_not_disturb_an_attribution_created_by_a_live_scan_since(db_session) -> None:
    """Simulates the real-world sequence: the script runs once, then a
    live scan (or a second saved search) independently creates more
    attributions, then someone re-runs the script - it must only ever
    fill genuine gaps, never touch what's already there."""
    search = _saved_search(db_session)
    listing = _listing(db_session, external_id="a", discovered_by_saved_search_id=search.id)
    run_listing_attribution_backfill(db_session, apply=True)

    other_search = _saved_search(db_session, query="Other")
    ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=other_search.id, discovered_listing_id=listing.id
    )
    db_session.commit()

    run_listing_attribution_backfill(db_session, apply=True)

    assert db_session.query(ListingAttribution).filter_by(discovered_listing_id=listing.id).count() == 2
