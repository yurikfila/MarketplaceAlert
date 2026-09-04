"""HTTP-level tests for the `/api/v1` saved-search and listings
ownership-enforcement cutover: real signed-up users, real bearer tokens,
never a dependency override that would weaken auth for the test (matching
the requirement that existing/new tests authenticate properly rather than
bypass auth).

Complements the repository/service-level ownership tests
(`test_saved_search_ownership.py`, `test_listing_ownership.py`), which
prove the `*_owned` methods themselves are correct - these prove the
actual routes are wired to them correctly, end to end.
"""

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.saved_searches.models import SavedSearch


def _signup(client, email: str, password: str = "a-strong-password") -> dict:
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201
    return response.json()


def _auth_headers(signup_body: dict) -> dict:
    return {"Authorization": f"Bearer {signup_body['tokens']['access_token']}"}


def _create_search(client, headers, **overrides):
    body = {"query": "Pokemon", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True}
    body.update(overrides)
    return client.post("/api/v1/saved-searches", json=body, headers=headers)


class FakeConnector:
    """Connector-shaped fake returning a fixed list of listings, regardless
    of query - same pattern as tests/test_saved_search_scheduler.py."""

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        return self._listings


def _listing(external_id: str) -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id=external_id,
        title="Camera",
        listing_url=f"https://example.com/{external_id}",
    )


# =====================================================================
# 1-2: unauthenticated requests -> 401
# =====================================================================


def test_unauthenticated_create_saved_search_returns_401(client) -> None:
    response = client.post(
        "/api/v1/saved-searches",
        json={"query": "x", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True},
    )
    assert response.status_code == 401


def test_unauthenticated_list_saved_searches_returns_401(client) -> None:
    assert client.get("/api/v1/saved-searches").status_code == 401


def test_unauthenticated_get_saved_search_returns_401(client) -> None:
    assert client.get("/api/v1/saved-searches/1").status_code == 401


def test_unauthenticated_update_saved_search_returns_401(client) -> None:
    assert client.patch("/api/v1/saved-searches/1", json={"query": "y"}).status_code == 401


def test_unauthenticated_delete_saved_search_returns_401(client) -> None:
    assert client.delete("/api/v1/saved-searches/1").status_code == 401


def test_unauthenticated_run_saved_search_returns_401(client) -> None:
    assert client.post("/api/v1/saved-searches/1/run").status_code == 401


def test_unauthenticated_listings_returns_401(client) -> None:
    assert client.get("/api/v1/listings").status_code == 401


# =====================================================================
# 4-5: non-owner and nonexistent IDs both -> 404, indistinguishable
# =====================================================================


def test_get_someone_elses_saved_search_returns_404(client) -> None:
    owner = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")
    created = _create_search(client, _auth_headers(owner)).json()

    response = client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(other))
    assert response.status_code == 404
    assert response.json()["detail"] == "Saved search not found"


def test_update_someone_elses_saved_search_returns_404_and_leaves_it_unchanged(client) -> None:
    owner = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")
    created = _create_search(client, _auth_headers(owner), query="Original").json()

    response = client.patch(
        f"/api/v1/saved-searches/{created['id']}", json={"query": "Hijacked"}, headers=_auth_headers(other)
    )
    assert response.status_code == 404

    unchanged = client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(owner)).json()
    assert unchanged["query"] == "Original"


def test_delete_someone_elses_saved_search_returns_404_and_leaves_it_intact(client) -> None:
    owner = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")
    created = _create_search(client, _auth_headers(owner)).json()

    response = client.delete(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(other))
    assert response.status_code == 404

    still_there = client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(owner))
    assert still_there.status_code == 200


def test_run_someone_elses_saved_search_returns_404_and_never_runs_it(client, monkeypatch) -> None:
    import marketplace_alert.api.v1.saved_searches as saved_searches_module

    calls: list[str] = []

    class _SpyConnector:
        def search(self, query, filters=None):
            calls.append(query)
            return []

    monkeypatch.setattr(saved_searches_module, "get_connector", lambda name: _SpyConnector())

    owner = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")
    created = _create_search(client, _auth_headers(owner)).json()

    response = client.post(f"/api/v1/saved-searches/{created['id']}/run", headers=_auth_headers(other))
    assert response.status_code == 404
    assert calls == []


def test_nonexistent_and_foreign_saved_search_ids_are_indistinguishable(client) -> None:
    owner = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")
    created = _create_search(client, _auth_headers(owner)).json()

    foreign = client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(other))
    nonexistent = client.get("/api/v1/saved-searches/999999", headers=_auth_headers(other))
    assert foreign.status_code == nonexistent.status_code == 404
    assert foreign.json() == nonexistent.json()


# =====================================================================
# 6-8: create assigns ownership, list is scoped, unowned rows invisible
# =====================================================================


def test_create_saved_search_is_owned_by_the_authenticated_user(client) -> None:
    user = _signup(client, "owner@example.com")
    other = _signup(client, "other@example.com")

    created = _create_search(client, _auth_headers(user)).json()

    assert client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(user)).status_code == 200
    assert client.get(f"/api/v1/saved-searches/{created['id']}", headers=_auth_headers(other)).status_code == 404


def test_list_saved_searches_returns_only_the_current_users_own(client) -> None:
    user_a = _signup(client, "a@example.com")
    user_b = _signup(client, "b@example.com")
    _create_search(client, _auth_headers(user_a), query="A's search")
    _create_search(client, _auth_headers(user_b), query="B's search")

    body = client.get("/api/v1/saved-searches", headers=_auth_headers(user_a)).json()
    assert {item["query"] for item in body} == {"A's search"}


def test_unowned_saved_search_is_invisible_via_authenticated_routes(client, db_session) -> None:
    """A `user_id IS NULL` row (pre-cutover data, or any row the
    production bootstrap backfill hasn't reached yet) must never appear
    for anyone via the now-authenticated routes - see PART 6."""
    user = _signup(client, "user@example.com")
    unowned = SavedSearch(query="Pre-cutover, unowned", scan_interval_seconds=60, is_active=True)
    db_session.add(unowned)
    db_session.commit()

    list_body = client.get("/api/v1/saved-searches", headers=_auth_headers(user)).json()
    assert "Pre-cutover, unowned" not in {item["query"] for item in list_body}

    get_response = client.get(f"/api/v1/saved-searches/{unowned.id}", headers=_auth_headers(user))
    assert get_response.status_code == 404


# =====================================================================
# 9-11: listings ownership scoping
# =====================================================================


def test_listings_are_scoped_to_the_authenticated_users_own_saved_searches(client, monkeypatch) -> None:
    import marketplace_alert.api.v1.saved_searches as saved_searches_module

    user_a = _signup(client, "a@example.com")
    user_b = _signup(client, "b@example.com")

    monkeypatch.setattr(saved_searches_module, "get_connector", lambda name: FakeConnector([_listing("a-item")]))
    search_a = _create_search(client, _auth_headers(user_a), query="Camera").json()
    run_a = client.post(f"/api/v1/saved-searches/{search_a['id']}/run", headers=_auth_headers(user_a))
    assert run_a.json()["total_new_count"] == 1

    monkeypatch.setattr(saved_searches_module, "get_connector", lambda name: FakeConnector([_listing("b-item")]))
    search_b = _create_search(client, _auth_headers(user_b), query="Camera").json()
    run_b = client.post(f"/api/v1/saved-searches/{search_b['id']}/run", headers=_auth_headers(user_b))
    assert run_b.json()["total_new_count"] == 1

    a_listings = client.get("/api/v1/listings", headers=_auth_headers(user_a)).json()
    b_listings = client.get("/api/v1/listings", headers=_auth_headers(user_b)).json()

    # 9: each sees only their own...
    assert {item["external_listing_id"] for item in a_listings["items"]} == {"a-item"}
    assert a_listings["total_count"] == 1
    # 10: ...and never the other's.
    assert {item["external_listing_id"] for item in b_listings["items"]} == {"b-item"}
    assert b_listings["total_count"] == 1


def test_unowned_listing_is_invisible_to_every_authenticated_user(client, db_session) -> None:
    """11: a listing with no owning saved search at all
    (`discovered_by_saved_search_id IS NULL`) - e.g. from the legacy
    `/scan` endpoint - must never appear for anyone."""
    from datetime import datetime, timezone

    from marketplace_alert.core.persistence.models import DiscoveredListing

    user = _signup(client, "user@example.com")
    row = DiscoveredListing(
        marketplace="mock",
        external_listing_id="unowned-item",
        title="Unowned",
        listing_url="https://example.com/unowned-item",
        first_discovered_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        discovered_by_saved_search_id=None,
    )
    db_session.add(row)
    db_session.commit()

    body = client.get("/api/v1/listings", headers=_auth_headers(user)).json()
    assert body["total_count"] == 0
    assert body["items"] == []


# =====================================================================
# 13: two-user isolation regression
# =====================================================================


def test_two_user_isolation_regression(client) -> None:
    """End-to-end proof that two users' saved-search data never crosses:
    each can create, list, get, update, delete their own; neither can
    see, modify, or delete the other's; nonexistent and foreign ids are
    equally 404."""
    user_a = _signup(client, "regression-a@example.com")
    user_b = _signup(client, "regression-b@example.com")
    headers_a, headers_b = _auth_headers(user_a), _auth_headers(user_b)

    search_a = _create_search(client, headers_a, query="A's search").json()
    search_b = _create_search(client, headers_b, query="B's search").json()

    assert {s["query"] for s in client.get("/api/v1/saved-searches", headers=headers_a).json()} == {"A's search"}
    assert {s["query"] for s in client.get("/api/v1/saved-searches", headers=headers_b).json()} == {"B's search"}

    assert client.get(f"/api/v1/saved-searches/{search_b['id']}", headers=headers_a).status_code == 404
    assert client.get(f"/api/v1/saved-searches/{search_a['id']}", headers=headers_b).status_code == 404
    assert (
        client.patch(
            f"/api/v1/saved-searches/{search_b['id']}", json={"query": "hijacked"}, headers=headers_a
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/saved-searches/{search_a['id']}", headers=headers_b).status_code == 404

    assert (
        client.patch(
            f"/api/v1/saved-searches/{search_a['id']}", json={"query": "A updated"}, headers=headers_a
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/saved-searches/{search_b['id']}", headers=headers_b).status_code == 204

    assert client.get(f"/api/v1/saved-searches/{search_b['id']}", headers=headers_b).status_code == 404
    assert client.get(f"/api/v1/saved-searches/{search_a['id']}", headers=headers_a).status_code == 200


# =====================================================================
# 16: legacy production bypass is closed
# =====================================================================


def test_legacy_routes_return_404_when_disabled(client, monkeypatch) -> None:
    from marketplace_alert.config import settings

    monkeypatch.setattr(settings, "legacy_routes_enabled", False)

    assert client.get("/").status_code == 404
    assert client.get("/listings").status_code == 404
    assert client.get("/search", params={"q": "x"}).status_code == 404
    assert client.get("/scan", params={"q": "x"}).status_code == 404
    assert (
        client.post(
            "/saved-searches",
            json={"query": "x", "marketplaces": ["mock"], "scan_interval_seconds": 60, "is_active": True},
        ).status_code
        == 404
    )
    assert client.get("/saved-searches").status_code == 404
    assert client.get("/saved-searches/1").status_code == 404
    assert client.patch("/saved-searches/1", json={"query": "y"}).status_code == 404
    assert client.delete("/saved-searches/1").status_code == 404
    assert client.post("/saved-searches/1/run").status_code == 404

    # /health and /api/v1/* are never gated by this flag.
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/status").status_code == 200
