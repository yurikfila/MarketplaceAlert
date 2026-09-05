"""Tests for `ListingAttribution` (`core/persistence/models.py`) and its
repository (`core/persistence/listing_attribution_repository.py`) -
model/repository CRUD, `UNIQUE(saved_search_id, discovered_listing_id)`,
cascade delete on both foreign keys, and idempotent creation.

Uses the same `db_session` fixture as every other persistence test
(`tests/conftest.py`) - an isolated temp-file SQLite database, never the
developer's real one.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _user(session, email: str = "user@example.com") -> User:
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


def _listing(session, *, external_id: str = "item-1") -> DiscoveredListing:
    row, _ = ListingRepository(session).get_or_create(
        Listing(
            marketplace="mock",
            external_listing_id=external_id,
            title=f"Listing {external_id}",
            listing_url=f"https://example.com/{external_id}",
        )
    )
    session.commit()
    return row


# =====================================================================
# Repository CRUD
# =====================================================================


def test_get_returns_none_when_no_attribution_exists(db_session) -> None:
    search = _saved_search(db_session)
    listing = _listing(db_session)
    assert ListingAttributionRepository(db_session).get(
        saved_search_id=search.id, discovered_listing_id=listing.id
    ) is None


def test_record_if_missing_creates_a_new_row(db_session) -> None:
    search = _saved_search(db_session)
    listing = _listing(db_session)

    row, created = ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=search.id, discovered_listing_id=listing.id
    )
    db_session.commit()

    assert created is True
    assert row.saved_search_id == search.id
    assert row.discovered_listing_id == listing.id
    assert row.discovered_at is not None


def test_record_if_missing_is_idempotent(db_session) -> None:
    """Repeated scans of the same saved search must not create duplicate
    attribution rows."""
    search = _saved_search(db_session)
    listing = _listing(db_session)
    repo = ListingAttributionRepository(db_session)

    first, first_created = repo.record_if_missing(saved_search_id=search.id, discovered_listing_id=listing.id)
    db_session.commit()
    second, second_created = repo.record_if_missing(saved_search_id=search.id, discovered_listing_id=listing.id)
    db_session.commit()

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert db_session.query(ListingAttribution).count() == 1


# =====================================================================
# UNIQUE(saved_search_id, discovered_listing_id)
# =====================================================================


def test_unique_constraint_rejects_a_second_row_for_the_same_pair(db_session) -> None:
    """The repository's own `record_if_missing` never attempts this (it
    always checks first) - this proves the database itself enforces it
    too, not just application-level discipline."""
    search = _saved_search(db_session)
    listing = _listing(db_session)
    db_session.add(ListingAttribution(saved_search_id=search.id, discovered_listing_id=listing.id))
    db_session.commit()

    db_session.add(ListingAttribution(saved_search_id=search.id, discovered_listing_id=listing.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_same_search_can_attribute_two_different_listings(db_session) -> None:
    search = _saved_search(db_session)
    listing_a = _listing(db_session, external_id="a")
    listing_b = _listing(db_session, external_id="b")
    repo = ListingAttributionRepository(db_session)

    repo.record_if_missing(saved_search_id=search.id, discovered_listing_id=listing_a.id)
    db_session.commit()
    repo.record_if_missing(saved_search_id=search.id, discovered_listing_id=listing_b.id)
    db_session.commit()

    assert db_session.query(ListingAttribution).filter_by(saved_search_id=search.id).count() == 2


def test_two_different_searches_can_each_attribute_the_same_listing(db_session) -> None:
    search_a = _saved_search(db_session, query="A")
    search_b = _saved_search(db_session, query="B")
    listing = _listing(db_session)
    repo = ListingAttributionRepository(db_session)

    repo.record_if_missing(saved_search_id=search_a.id, discovered_listing_id=listing.id)
    db_session.commit()
    repo.record_if_missing(saved_search_id=search_b.id, discovered_listing_id=listing.id)
    db_session.commit()

    assert db_session.query(ListingAttribution).filter_by(discovered_listing_id=listing.id).count() == 2


# =====================================================================
# Cascade delete (both foreign keys)
# =====================================================================


def test_deleting_the_saved_search_cascades_to_its_attributions(db_session) -> None:
    """SQLite ignores `ON DELETE CASCADE` unless `PRAGMA foreign_keys=ON`
    is issued per-connection - production runs Postgres, which always
    enforces it, so this pragma is only here to make SQLite behave like
    production for this one assertion (same approach already used for
    `NotificationPreference`'s equivalent cascade test)."""
    db_session.execute(text("PRAGMA foreign_keys=ON"))

    search = _saved_search(db_session)
    listing = _listing(db_session)
    ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=search.id, discovered_listing_id=listing.id
    )
    db_session.commit()

    db_session.delete(search)
    db_session.commit()

    assert db_session.query(ListingAttribution).filter_by(discovered_listing_id=listing.id).count() == 0
    # The canonical listing itself must survive - only the attribution is gone.
    assert db_session.query(DiscoveredListing).filter_by(id=listing.id).count() == 1


def test_deleting_the_discovered_listing_cascades_to_its_attributions(db_session) -> None:
    db_session.execute(text("PRAGMA foreign_keys=ON"))

    search = _saved_search(db_session)
    listing = _listing(db_session)
    ListingAttributionRepository(db_session).record_if_missing(
        saved_search_id=search.id, discovered_listing_id=listing.id
    )
    db_session.commit()

    db_session.delete(listing)
    db_session.commit()

    assert db_session.query(ListingAttribution).filter_by(saved_search_id=search.id).count() == 0


def test_deleting_one_of_two_attributed_searches_leaves_the_others_attribution_intact(db_session) -> None:
    """Deleting search A's attribution to a shared listing must never
    affect search B's own, independent attribution to the same listing -
    proves cascade is scoped to the deleted row's own attributions only."""
    db_session.execute(text("PRAGMA foreign_keys=ON"))

    search_a = _saved_search(db_session, query="A")
    search_b = _saved_search(db_session, query="B")
    listing = _listing(db_session)
    repo = ListingAttributionRepository(db_session)
    repo.record_if_missing(saved_search_id=search_a.id, discovered_listing_id=listing.id)
    repo.record_if_missing(saved_search_id=search_b.id, discovered_listing_id=listing.id)
    db_session.commit()

    db_session.delete(search_a)
    db_session.commit()

    remaining = db_session.query(ListingAttribution).filter_by(discovered_listing_id=listing.id).all()
    assert len(remaining) == 1
    assert remaining[0].saved_search_id == search_b.id


# =====================================================================
# get_earliest_attribution_search_ids
# =====================================================================


def test_earliest_attribution_returns_none_for_a_listing_with_no_attribution(db_session) -> None:
    user = _user(db_session)
    listing = _listing(db_session)
    result = ListingAttributionRepository(db_session).get_earliest_attribution_search_ids(
        user_id=user.id, discovered_listing_ids=[listing.id]
    )
    assert listing.id not in result


def test_earliest_attribution_reports_this_users_own_search_only(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    search_a = _saved_search(db_session, query="A", user_id=user_a.id)
    search_b = _saved_search(db_session, query="B", user_id=user_b.id)
    listing = _listing(db_session)
    repo = ListingAttributionRepository(db_session)
    repo.record_if_missing(saved_search_id=search_a.id, discovered_listing_id=listing.id)
    repo.record_if_missing(saved_search_id=search_b.id, discovered_listing_id=listing.id)
    db_session.commit()

    result_a = repo.get_earliest_attribution_search_ids(user_id=user_a.id, discovered_listing_ids=[listing.id])
    result_b = repo.get_earliest_attribution_search_ids(user_id=user_b.id, discovered_listing_ids=[listing.id])

    assert result_a[listing.id] == search_a.id
    assert result_b[listing.id] == search_b.id


def test_earliest_attribution_picks_the_chronologically_first_of_the_users_own_searches(db_session) -> None:
    user = _user(db_session)
    early_search = _saved_search(db_session, query="Early", user_id=user.id)
    later_search = _saved_search(db_session, query="Later", user_id=user.id)
    listing = _listing(db_session)
    repo = ListingAttributionRepository(db_session)

    db_session.add(
        ListingAttribution(
            saved_search_id=early_search.id,
            discovered_listing_id=listing.id,
            discovered_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        ListingAttribution(
            saved_search_id=later_search.id,
            discovered_listing_id=listing.id,
            discovered_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    result = repo.get_earliest_attribution_search_ids(user_id=user.id, discovered_listing_ids=[listing.id])

    assert result[listing.id] == early_search.id


def test_earliest_attribution_with_no_listing_ids_returns_empty_without_querying(db_session) -> None:
    user = _user(db_session)
    assert ListingAttributionRepository(db_session).get_earliest_attribution_search_ids(
        user_id=user.id, discovered_listing_ids=[]
    ) == {}
