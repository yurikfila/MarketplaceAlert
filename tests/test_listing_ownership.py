"""Tests for `ListingRepository.list_recent_owned`/`count_owned` - what the
authenticated `GET /api/v1/listings` route actually uses. Also covers
Phase 1 of multi-user listing attribution: `list_recent_owned` now
determines ownership via `ListingAttribution`, not the single scalar
`discovered_by_saved_search_id` column - see that repository method's
docstring. The existing unscoped `list_recent`/`count` (legacy dashboard
only) are also verified here to still behave exactly as before.
"""

from datetime import datetime, timezone

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository


def _user(session, email: str) -> User:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user


def _saved_search(session, *, query="Makita drill", user_id=None):
    saved_search = SavedSearchRepository(session).create(
        query=query, marketplaces=["mock"], scan_interval_seconds=300, is_active=True
    )
    if user_id is not None:
        saved_search.user_id = user_id
    session.commit()
    return saved_search


def _listing(session, *, external_id: str, discovered_by_saved_search_id=None, marketplace="mock"):
    """`discovered_by_saved_search_id`, when given, also creates the
    corresponding `ListingAttribution` row - that's what actually drives
    `list_recent_owned`'s visibility now (Phase 1 of multi-user listing
    attribution), not the column alone. Kept as the same parameter name/
    shape every existing test in this file already uses, so their intent
    (and assertions) stay unchanged - only the setup now matches what's
    actually authoritative."""
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
    if discovered_by_saved_search_id is not None:
        session.add(
            ListingAttribution(
                saved_search_id=discovered_by_saved_search_id, discovered_listing_id=row.id, discovered_at=now
            )
        )
        session.commit()
    return row


def _attribute(session, *, saved_search_id: int, listing: DiscoveredListing) -> None:
    """For tests that need a listing attributed to more than one saved
    search - `_listing()`'s own `discovered_by_saved_search_id` param
    only ever creates one attribution (matching the real "first
    discovered by" column it also sets), so a second/third attribution to
    the same listing is added explicitly via this helper instead."""
    ListingAttributionRepository(session).record_if_missing(
        saved_search_id=saved_search_id, discovered_listing_id=listing.id
    )
    session.commit()


# =====================================================================
# list_recent_owned / count_owned
# =====================================================================


def test_owner_sees_their_own_attributed_listings(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)
    listing = _listing(db_session, external_id="l1", discovered_by_saved_search_id=saved_search.id)

    results = ListingRepository(db_session).list_recent_owned(user_id=user.id, limit=10, offset=0)

    assert [r.id for r in results] == [listing.id]


def test_user_a_cannot_see_user_bs_listings(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    search_b = _saved_search(db_session, query="B's search", user_id=user_b.id)
    _listing(db_session, external_id="b-listing", discovered_by_saved_search_id=search_b.id)

    results = ListingRepository(db_session).list_recent_owned(user_id=user_a.id, limit=10, offset=0)

    assert results == []


def test_unowned_listings_are_excluded_from_any_users_scoped_query(db_session) -> None:
    """discovered_by_saved_search_id IS NULL - the pre-cutover gap - must
    never surface for anyone via the scoped query, regardless of who
    asks."""
    user = _user(db_session, "a@example.com")
    _listing(db_session, external_id="unowned", discovered_by_saved_search_id=None)

    results = ListingRepository(db_session).list_recent_owned(user_id=user.id, limit=10, offset=0)

    assert results == []


def test_listings_attributed_to_an_unowned_saved_search_are_excluded(db_session) -> None:
    """A listing can point at a real saved search that itself has no
    owner yet (pre-cutover) - still must not appear for anyone until
    that search actually has an owner."""
    user = _user(db_session, "a@example.com")
    unowned_search = _saved_search(db_session, user_id=None)
    _listing(db_session, external_id="l1", discovered_by_saved_search_id=unowned_search.id)

    results = ListingRepository(db_session).list_recent_owned(user_id=user.id, limit=10, offset=0)

    assert results == []


def test_count_owned_matches_list_recent_owned(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)
    _listing(db_session, external_id="l1", discovered_by_saved_search_id=saved_search.id)
    _listing(db_session, external_id="l2", discovered_by_saved_search_id=saved_search.id)

    repo = ListingRepository(db_session)
    assert repo.count_owned(user_id=user.id) == 2
    assert len(repo.list_recent_owned(user_id=user.id, limit=10, offset=0)) == 2


def test_count_owned_excludes_unowned_and_other_users_listings(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    search_a = _saved_search(db_session, query="A's search", user_id=user_a.id)
    search_b = _saved_search(db_session, query="B's search", user_id=user_b.id)
    _listing(db_session, external_id="a1", discovered_by_saved_search_id=search_a.id)
    _listing(db_session, external_id="b1", discovered_by_saved_search_id=search_b.id)
    _listing(db_session, external_id="unowned")

    assert ListingRepository(db_session).count_owned(user_id=user_a.id) == 1


def test_list_recent_owned_respects_limit_and_offset(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)
    for i in range(3):
        _listing(db_session, external_id=f"l{i}", discovered_by_saved_search_id=saved_search.id)

    repo = ListingRepository(db_session)
    first_page = repo.list_recent_owned(user_id=user.id, limit=2, offset=0)
    second_page = repo.list_recent_owned(user_id=user.id, limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 1


# =====================================================================
# Existing unscoped behavior is unchanged
# =====================================================================


def test_unscoped_list_recent_still_returns_every_listing_regardless_of_owner(db_session) -> None:
    user_b = _user(db_session, "b@example.com")
    search_b = _saved_search(db_session, user_id=user_b.id)
    _listing(db_session, external_id="owned", discovered_by_saved_search_id=search_b.id)
    _listing(db_session, external_id="unowned")

    results = ListingRepository(db_session).list_recent(limit=10, offset=0)

    assert {r.external_listing_id for r in results} == {"owned", "unowned"}


def test_unscoped_count_still_counts_every_listing(db_session) -> None:
    _listing(db_session, external_id="l1")
    _listing(db_session, external_id="l2")

    assert ListingRepository(db_session).count() == 2


# =====================================================================
# Phase 1: multi-user listing attribution
# =====================================================================


def test_two_users_whose_searches_found_the_same_listing_can_both_see_it(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    search_a = _saved_search(db_session, query="A's search", user_id=user_a.id)
    search_b = _saved_search(db_session, query="B's search", user_id=user_b.id)
    listing = _listing(db_session, external_id="shared", discovered_by_saved_search_id=search_a.id)
    _attribute(db_session, saved_search_id=search_b.id, listing=listing)

    # Exactly one canonical row - never duplicated.
    assert db_session.query(DiscoveredListing).count() == 1

    repo = ListingRepository(db_session)
    assert [r.id for r in repo.list_recent_owned(user_id=user_a.id, limit=10, offset=0)] == [listing.id]
    assert [r.id for r in repo.list_recent_owned(user_id=user_b.id, limit=10, offset=0)] == [listing.id]
    assert repo.count_owned(user_id=user_a.id) == 1
    assert repo.count_owned(user_id=user_b.id) == 1


def test_same_user_with_two_matching_searches_sees_the_listing_only_once(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search_1 = _saved_search(db_session, query="Search 1", user_id=user.id)
    search_2 = _saved_search(db_session, query="Search 2", user_id=user.id)
    listing = _listing(db_session, external_id="double-matched", discovered_by_saved_search_id=search_1.id)
    _attribute(db_session, saved_search_id=search_2.id, listing=listing)

    repo = ListingRepository(db_session)
    results = repo.list_recent_owned(user_id=user.id, limit=10, offset=0)

    assert [r.id for r in results] == [listing.id]  # once, not twice
    assert repo.count_owned(user_id=user.id) == 1


def test_saved_search_id_filter_returns_listings_genuinely_attributed_to_that_search(db_session) -> None:
    """A listing attributed to search 1 (its "first discovered by") AND
    to search 2 (a second, independent attribution) must appear when
    filtering by *either* id - not just the first-discoverer's."""
    user = _user(db_session, "a@example.com")
    search_1 = _saved_search(db_session, query="Search 1", user_id=user.id)
    search_2 = _saved_search(db_session, query="Search 2", user_id=user.id)
    listing = _listing(db_session, external_id="multi-attributed", discovered_by_saved_search_id=search_1.id)
    _attribute(db_session, saved_search_id=search_2.id, listing=listing)
    other_listing = _listing(db_session, external_id="only-search-1", discovered_by_saved_search_id=search_1.id)

    repo = ListingRepository(db_session)
    by_search_1 = repo.list_recent_owned(user_id=user.id, saved_search_id=search_1.id, limit=10, offset=0)
    by_search_2 = repo.list_recent_owned(user_id=user.id, saved_search_id=search_2.id, limit=10, offset=0)

    assert {r.id for r in by_search_1} == {listing.id, other_listing.id}
    assert {r.id for r in by_search_2} == {listing.id}  # only the multi-attributed one


def test_saved_search_id_filter_never_leaks_via_another_users_search_id(db_session) -> None:
    """Naming another user's `saved_search_id` must never surface a
    listing this user happens to separately, legitimately own via their
    own attribution - the filter narrows what's shown, it never expands
    access beyond what the id itself actually authorizes."""
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    search_a = _saved_search(db_session, query="A's search", user_id=user_a.id)
    search_b = _saved_search(db_session, query="B's search", user_id=user_b.id)
    listing = _listing(db_session, external_id="shared", discovered_by_saved_search_id=search_a.id)
    _attribute(db_session, saved_search_id=search_b.id, listing=listing)

    # user_a legitimately owns an attribution to `listing` (via search_a),
    # but filtering by search_b's id (owned by user_b) must return nothing.
    results = ListingRepository(db_session).list_recent_owned(
        user_id=user_a.id, saved_search_id=search_b.id, limit=10, offset=0
    )
    assert results == []


def test_count_owned_matches_list_recent_owned_when_a_listing_has_multiple_attributions(db_session) -> None:
    user = _user(db_session, "a@example.com")
    search_1 = _saved_search(db_session, query="Search 1", user_id=user.id)
    search_2 = _saved_search(db_session, query="Search 2", user_id=user.id)
    listing = _listing(db_session, external_id="double", discovered_by_saved_search_id=search_1.id)
    _attribute(db_session, saved_search_id=search_2.id, listing=listing)

    repo = ListingRepository(db_session)
    assert repo.count_owned(user_id=user.id) == len(repo.list_recent_owned(user_id=user.id, limit=10, offset=0))
