from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.database import create_db_engine, init_db
from marketplace_alert.core.persistence.models import DiscoveredListing
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
