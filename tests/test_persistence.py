from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.database import create_db_engine, init_db
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.persistence.service import ListingDiscoveryService


def _listing(
    marketplace: str = "mock", external_id: str = "mock-001", title: str = "Maccabi Vintage Shirt"
) -> Listing:
    return Listing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://mock-marketplace.example.com/listing/{external_id}",
    )


def test_database_initializes_and_creates_tables(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'init_test.db'}")
    init_db(bind=engine)
    assert "discovered_listings" in inspect(engine).get_table_names()


def test_first_discovery_is_new(db_session) -> None:
    service = ListingDiscoveryService(db_session)
    result = service.process_listings([_listing()])
    assert len(result.new_listings) == 1
    assert result.new_listings[0].external_listing_id == "mock-001"
    assert result.already_seen_count == 0


def test_second_discovery_of_same_listing_is_already_seen(db_session) -> None:
    service = ListingDiscoveryService(db_session)
    service.process_listings([_listing()])
    result = service.process_listings([_listing()])
    assert result.new_listings == []
    assert result.already_seen_count == 1


def test_same_external_id_on_different_marketplaces_is_allowed(db_session) -> None:
    service = ListingDiscoveryService(db_session)
    mock_result = service.process_listings([_listing(marketplace="mock")])
    ebay_result = service.process_listings([_listing(marketplace="ebay")])
    assert len(mock_result.new_listings) == 1
    assert len(ebay_result.new_listings) == 1
    assert ebay_result.already_seen_count == 0


def test_save_new_persists_every_product_field_the_connector_returned(db_session) -> None:
    """The real gap this pass closed: `save_new` used to only ever store
    marketplace/external_id/title/listing_url - everything else a
    connector returns was silently dropped. Confirms it now round-trips
    through the repository directly (not just via the API layer)."""
    listing = Listing(
        marketplace="mock",
        external_listing_id="rich-001",
        title="Full-fidelity listing",
        price=199.5,
        currency="EUR",
        location="Berlin, Germany",
        seller="berlin_seller",
        condition="new",
        listing_url="https://mock-marketplace.example.com/listing/rich-001",
        image_url="https://example.com/images/rich-001.jpg",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    service = ListingDiscoveryService(db_session)
    service.process_listings([listing])

    row = ListingRepository(db_session).get("mock", "rich-001")
    assert row is not None
    assert row.price == 199.5
    assert row.currency == "EUR"
    assert row.location == "Berlin, Germany"
    assert row.seller == "berlin_seller"
    assert row.condition == "new"
    assert row.image_url == "https://example.com/images/rich-001.jpg"
    # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
    # columns (every value written is UTC regardless) - compare naive, the
    # same rule ListingOut.ensure_utc documents for the API-layer version.
    assert row.source_created_at.replace(tzinfo=timezone.utc) == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_save_new_leaves_absent_fields_null_not_guessed(db_session) -> None:
    listing = _listing(external_id="bare-001")  # only the required fields are set
    service = ListingDiscoveryService(db_session)
    service.process_listings([listing])

    row = ListingRepository(db_session).get("mock", "bare-001")
    assert row is not None
    assert row.price is None
    assert row.currency is None
    assert row.location is None
    assert row.seller is None
    assert row.condition is None
    assert row.image_url is None
    assert row.source_created_at is None
    assert row.discovered_by_saved_search_id is None


def test_process_listings_records_the_saved_search_that_first_discovered_a_row(db_session) -> None:
    service = ListingDiscoveryService(db_session)
    service.process_listings([_listing(external_id="attributed-001")], saved_search_id=42)

    row = ListingRepository(db_session).get("mock", "attributed-001")
    assert row is not None
    assert row.discovered_by_saved_search_id == 42


def test_process_listings_without_a_saved_search_id_leaves_attribution_null(db_session) -> None:
    """The legacy /scan endpoint (and any other caller not tied to a
    saved search) must not fabricate an attribution."""
    service = ListingDiscoveryService(db_session)
    service.process_listings([_listing(external_id="unattributed-001")])

    row = ListingRepository(db_session).get("mock", "unattributed-001")
    assert row is not None
    assert row.discovered_by_saved_search_id is None


def test_two_different_searches_matching_the_same_listing_both_get_attribution(db_session) -> None:
    """The Phase 1 fix itself: whichever search discovers a listing first
    still owns `discovered_by_saved_search_id` (unchanged), but a second,
    different search matching the exact same already-existing listing
    must still get its own `ListingAttribution` row - not silently
    dropped the way it used to be."""
    service = ListingDiscoveryService(db_session)
    service.process_listings([_listing(external_id="shared-1")], saved_search_id=1)
    result = service.process_listings([_listing(external_id="shared-1")], saved_search_id=2)

    assert result.already_seen_count == 1  # canonical listing identity is still global

    row = ListingRepository(db_session).get("mock", "shared-1")
    assert row is not None
    assert row.discovered_by_saved_search_id == 1  # historical "first discovered by" - unchanged

    attribution_repo = ListingAttributionRepository(db_session)
    assert attribution_repo.get(saved_search_id=1, discovered_listing_id=row.id) is not None
    assert attribution_repo.get(saved_search_id=2, discovered_listing_id=row.id) is not None


def test_repeated_scan_of_the_same_search_does_not_duplicate_attribution(db_session) -> None:
    service = ListingDiscoveryService(db_session)
    service.process_listings([_listing(external_id="repeat-1")], saved_search_id=7)
    service.process_listings([_listing(external_id="repeat-1")], saved_search_id=7)
    service.process_listings([_listing(external_id="repeat-1")], saved_search_id=7)

    row = ListingRepository(db_session).get("mock", "repeat-1")
    assert db_session.query(ListingAttribution).filter_by(
        saved_search_id=7, discovered_listing_id=row.id
    ).count() == 1


def test_concurrent_discovery_of_a_brand_new_listing_does_not_raise_and_shares_one_canonical_row(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the race directly: two saved searches both check
    `ListingRepository.get()` and both see "not found yet" for the exact
    same brand-new listing, then both attempt to persist it. Session A
    genuinely wins and commits first; session B's own existence-check is
    forced to report the same stale "not found" it would have gotten had
    it truly run a moment earlier - proving `get_or_create` recovers via
    the UNIQUE constraint instead of raising an unhandled `IntegrityError`
    out of `process_listings()`."""
    session_a = session_factory()
    winner_row, winner_created = ListingRepository(session_a).get_or_create(_listing(external_id="race-1"))
    session_a.commit()
    session_a.close()
    assert winner_created is True

    session_b = session_factory()
    repository_b = ListingRepository(session_b)
    real_get = repository_b.get
    calls = {"count": 0}

    def _stale_get(marketplace: str, external_listing_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # the stale read - as if B's check ran before A's commit
        return real_get(marketplace, external_listing_id)

    monkeypatch.setattr(repository_b, "get", _stale_get)

    loser_row, loser_created = repository_b.get_or_create(_listing(external_id="race-1"))
    session_b.commit()
    session_b.close()

    assert loser_created is False
    assert loser_row.id == winner_row.id

    verify_session = session_factory()
    matching_rows = (
        verify_session.query(DiscoveredListing)
        .filter_by(marketplace="mock", external_listing_id="race-1")
        .all()
    )
    assert len(matching_rows) == 1
    verify_session.close()


def test_concurrent_discovery_still_records_attribution_for_each_matching_search(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race as above, but through the full `ListingDiscoveryService`
    path with two different `saved_search_id`s - proves no attribution is
    lost for the "losing" side of the race either."""
    session_a = session_factory()
    ListingDiscoveryService(session_a).process_listings(
        [_listing(external_id="race-attr-1")], saved_search_id=101
    )
    session_a.commit()
    session_a.close()

    session_b = session_factory()
    service_b = ListingDiscoveryService(session_b)
    real_get = service_b._repository.get
    calls = {"count": 0}

    def _stale_get(marketplace: str, external_listing_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return real_get(marketplace, external_listing_id)

    monkeypatch.setattr(service_b._repository, "get", _stale_get)

    service_b.process_listings([_listing(external_id="race-attr-1")], saved_search_id=102)
    session_b.commit()
    session_b.close()

    verify_session = session_factory()
    row = ListingRepository(verify_session).get("mock", "race-attr-1")
    attribution_repo = ListingAttributionRepository(verify_session)
    assert attribution_repo.get(saved_search_id=101, discovered_listing_id=row.id) is not None
    assert attribution_repo.get(saved_search_id=102, discovered_listing_id=row.id) is not None
    verify_session.close()


def test_touch_last_seen_does_not_refresh_product_fields(db_session) -> None:
    """Deliberate: product fields are captured once, at first discovery -
    a second scan seeing the same listing (even with a changed price on
    the source marketplace) must not silently overwrite what was
    originally recorded. See DiscoveredListing's docstring."""
    service = ListingDiscoveryService(db_session)
    first = Listing(
        marketplace="mock",
        external_listing_id="price-drop-001",
        title="Item",
        price=100.0,
        listing_url="https://mock-marketplace.example.com/listing/price-drop-001",
    )
    service.process_listings([first])

    second = Listing(
        marketplace="mock",
        external_listing_id="price-drop-001",
        title="Item",
        price=80.0,  # a price drop on a later scan
        listing_url="https://mock-marketplace.example.com/listing/price-drop-001",
    )
    result = service.process_listings([second])
    assert result.already_seen_count == 1

    row = ListingRepository(db_session).get("mock", "price-drop-001")
    assert row is not None
    assert row.price == 100.0  # unchanged - the original discovery value


def test_duplicate_marketplace_and_external_id_is_prevented_at_db_level(db_session) -> None:
    now = datetime.now(timezone.utc)
    db_session.add(
        DiscoveredListing(
            marketplace="mock",
            external_listing_id="mock-001",
            title="Maccabi Vintage Shirt",
            listing_url="https://mock-marketplace.example.com/listing/mock-001",
            first_discovered_at=now,
            last_seen_at=now,
        )
    )
    db_session.flush()

    db_session.add(
        DiscoveredListing(
            marketplace="mock",
            external_listing_id="mock-001",
            title="Maccabi Vintage Shirt (duplicate insert attempt)",
            listing_url="https://mock-marketplace.example.com/listing/mock-001",
            first_discovered_at=now,
            last_seen_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- list_missing_metadata (historical backfill candidate query) -----------


def _bare_row(db_session, *, marketplace="mock", external_id="mock-001", **overrides) -> DiscoveredListing:
    now = datetime.now(timezone.utc)
    defaults = dict(
        marketplace=marketplace,
        external_listing_id=external_id,
        title="Item",
        listing_url=f"https://example.com/{marketplace}/{external_id}",
        first_discovered_at=now,
        last_seen_at=now,
    )
    defaults.update(overrides)
    row = DiscoveredListing(**defaults)
    db_session.add(row)
    db_session.commit()
    return row


def test_list_missing_metadata_finds_a_row_missing_any_candidate_field(db_session) -> None:
    _bare_row(db_session, external_id="a", price=None, currency=None)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert len(candidates) == 1
    assert candidates[0].external_listing_id == "a"


def test_list_missing_metadata_excludes_a_fully_enriched_row(db_session) -> None:
    _bare_row(
        db_session,
        external_id="complete",
        price=10.0,
        currency="USD",
        image_url="https://example.com/i.jpg",
        condition="New",
        location="Somewhere",
        seller="someone",
        source_created_at=datetime.now(timezone.utc),
    )

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert candidates == []


def test_list_missing_metadata_respects_marketplace_filter(db_session) -> None:
    _bare_row(db_session, marketplace="mock", external_id="m1")
    _bare_row(db_session, marketplace="etsy", external_id="e1")

    candidates = ListingRepository(db_session).list_missing_metadata(marketplace="etsy", limit=10)

    assert len(candidates) == 1
    assert candidates[0].marketplace == "etsy"


def test_list_missing_metadata_respects_limit(db_session) -> None:
    for i in range(5):
        _bare_row(db_session, external_id=f"item-{i}")

    candidates = ListingRepository(db_session).list_missing_metadata(limit=2)

    assert len(candidates) == 2


def test_list_missing_metadata_orders_newest_discovered_first(db_session) -> None:
    from datetime import timedelta

    older = datetime.now(timezone.utc) - timedelta(days=2)
    newer = datetime.now(timezone.utc) - timedelta(minutes=1)
    _bare_row(db_session, external_id="old", first_discovered_at=older)
    _bare_row(db_session, external_id="new", first_discovered_at=newer)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert [c.external_listing_id for c in candidates] == ["new", "old"]


def test_list_missing_metadata_excludes_a_terminal_status_row_even_with_a_null_field(db_session) -> None:
    """The exact fix for the production bug: a row with a terminal
    metadata_backfill_status must never be selected again, regardless of
    whether it still has a null enrichable field (e.g. condition, which
    some marketplaces never provide)."""
    from marketplace_alert.core.persistence.models import BACKFILL_STATUS_NO_DATA

    _bare_row(db_session, external_id="terminal", condition=None, metadata_backfill_status=BACKFILL_STATUS_NO_DATA)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert candidates == []


def test_list_missing_metadata_includes_a_pending_row_with_null_status(db_session) -> None:
    _bare_row(db_session, external_id="pending", condition=None, metadata_backfill_status=None)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert len(candidates) == 1


def test_list_missing_metadata_includes_a_failed_status_row(db_session) -> None:
    """`failed` is retryable, not terminal - it must stay selectable."""
    from marketplace_alert.core.persistence.models import BACKFILL_STATUS_FAILED

    _bare_row(db_session, external_id="retryable", condition=None, metadata_backfill_status=BACKFILL_STATUS_FAILED)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert len(candidates) == 1


def test_list_missing_metadata_excludes_every_terminal_status(db_session) -> None:
    from marketplace_alert.core.persistence.models import BACKFILL_TERMINAL_STATUSES

    for status in BACKFILL_TERMINAL_STATUSES:
        _bare_row(db_session, external_id=f"terminal-{status}", condition=None, metadata_backfill_status=status)

    candidates = ListingRepository(db_session).list_missing_metadata(limit=10)

    assert candidates == []


# --- reset_backfill_status ------------------------------------------------


def test_reset_backfill_status_clears_status_and_attempted_at(db_session) -> None:
    from marketplace_alert.core.persistence.models import BACKFILL_STATUS_NO_DATA

    row = _bare_row(
        db_session,
        external_id="a",
        condition=None,
        metadata_backfill_status=BACKFILL_STATUS_NO_DATA,
        metadata_backfill_attempted_at=datetime.now(timezone.utc),
    )

    count = ListingRepository(db_session).reset_backfill_status(statuses=[BACKFILL_STATUS_NO_DATA])
    db_session.commit()

    assert count == 1
    db_session.refresh(row)
    assert row.metadata_backfill_status is None
    assert row.metadata_backfill_attempted_at is None


def test_reset_backfill_status_only_matches_given_statuses(db_session) -> None:
    from marketplace_alert.core.persistence.models import BACKFILL_STATUS_NO_DATA, BACKFILL_STATUS_NOT_FOUND

    _bare_row(db_session, external_id="a", condition=None, metadata_backfill_status=BACKFILL_STATUS_NO_DATA)
    _bare_row(db_session, external_id="b", condition=None, metadata_backfill_status=BACKFILL_STATUS_NOT_FOUND)

    count = ListingRepository(db_session).reset_backfill_status(statuses=[BACKFILL_STATUS_NO_DATA])
    db_session.commit()

    assert count == 1
    row_b = ListingRepository(db_session).get("mock", "b")
    assert row_b.metadata_backfill_status == BACKFILL_STATUS_NOT_FOUND


def test_reset_backfill_status_respects_marketplace_filter(db_session) -> None:
    from marketplace_alert.core.persistence.models import BACKFILL_STATUS_NO_DATA

    _bare_row(db_session, marketplace="mock", external_id="m1", condition=None, metadata_backfill_status=BACKFILL_STATUS_NO_DATA)
    _bare_row(db_session, marketplace="etsy", external_id="e1", condition=None, metadata_backfill_status=BACKFILL_STATUS_NO_DATA)

    count = ListingRepository(db_session).reset_backfill_status(statuses=[BACKFILL_STATUS_NO_DATA], marketplace="mock")
    db_session.commit()

    assert count == 1
    row_etsy = ListingRepository(db_session).get("etsy", "e1")
    assert row_etsy.metadata_backfill_status == BACKFILL_STATUS_NO_DATA
