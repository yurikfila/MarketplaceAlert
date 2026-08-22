from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.database import create_db_engine, init_db
from marketplace_alert.core.persistence.models import DiscoveredListing
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
