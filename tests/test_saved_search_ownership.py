"""Tests for the ownership-scoped `SavedSearchRepository`/`SavedSearchService`
methods (`get_owned`/`list_owned`/`update_owned`/`delete_owned`/`run_owned`) -
Phase 4 groundwork for the later route-protection phase (see
PROJECT_CONTEXT.md's authentication design decision). Not called by any
route yet - the existing unscoped `get`/`list_all`/`update`/`delete` are
also verified here to still behave exactly as before, unaffected by
these additions.
"""

import pytest

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.schemas import SavedSearchUpdate
from marketplace_alert.core.saved_searches.service import SavedSearchService


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


class _SpyConnector:
    """Records every `search()` call it receives - never a real
    marketplace. Used to prove `run_owned` did (or, for a
    foreign/nonexistent id, deliberately did not) invoke the runner."""

    def __init__(self) -> None:
        self.search_calls: list[str] = []

    def search(self, query: str, filters: dict | None = None) -> list:
        self.search_calls.append(query)
        return []


def _service(session, *, resolve_connector=None) -> SavedSearchService:
    return SavedSearchService(
        session, is_marketplace_supported=lambda _name: True, resolve_connector=resolve_connector
    )


# =====================================================================
# Repository: get_owned / list_owned / delete_owned
# =====================================================================


def test_get_owned_returns_the_users_own_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)

    found = SavedSearchRepository(db_session).get_owned(saved_search.id, user_id=user.id)

    assert found is not None
    assert found.id == saved_search.id


def test_get_owned_returns_none_for_another_users_saved_search(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    saved_search = _saved_search(db_session, user_id=user_b.id)

    found = SavedSearchRepository(db_session).get_owned(saved_search.id, user_id=user_a.id)

    assert found is None


def test_get_owned_returns_none_for_an_unowned_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=None)

    found = SavedSearchRepository(db_session).get_owned(saved_search.id, user_id=user.id)

    assert found is None


def test_get_owned_returns_none_for_a_nonexistent_id_same_as_someone_elses(db_session) -> None:
    """The explicit "not found, not forbidden" requirement: a missing row
    and a row owned by someone else must be indistinguishable to the
    caller - both just `None`."""
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)

    missing = SavedSearchRepository(db_session).get_owned(999999, user_id=user_a.id)
    someone_elses = SavedSearchRepository(db_session).get_owned(owned_by_b.id, user_id=user_a.id)

    assert missing is None
    assert someone_elses is None


def test_list_owned_returns_only_this_users_saved_searches(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    a1 = _saved_search(db_session, query="A's drill search", user_id=user_a.id)
    a2 = _saved_search(db_session, query="A's guitar search", user_id=user_a.id)
    _saved_search(db_session, query="B's search", user_id=user_b.id)
    _saved_search(db_session, query="Unowned search", user_id=None)

    results = SavedSearchRepository(db_session).list_owned(user_id=user_a.id)

    assert {s.id for s in results} == {a1.id, a2.id}


def test_delete_owned_deletes_the_users_own_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)

    deleted = SavedSearchRepository(db_session).delete_owned(saved_search.id, user_id=user.id)

    assert deleted is True
    assert SavedSearchRepository(db_session).get(saved_search.id) is None


def test_delete_owned_does_not_delete_another_users_saved_search(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)

    deleted = SavedSearchRepository(db_session).delete_owned(owned_by_b.id, user_id=user_a.id)

    assert deleted is False
    assert SavedSearchRepository(db_session).get(owned_by_b.id) is not None


def test_delete_owned_does_not_delete_an_unowned_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    unowned = _saved_search(db_session, user_id=None)

    deleted = SavedSearchRepository(db_session).delete_owned(unowned.id, user_id=user.id)

    assert deleted is False
    assert SavedSearchRepository(db_session).get(unowned.id) is not None


# =====================================================================
# Service: get_owned / list_owned / update_owned / delete_owned
# =====================================================================


def test_service_get_owned_mirrors_the_repository(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_a = _saved_search(db_session, user_id=user_a.id)
    owned_by_b = _saved_search(db_session, user_id=user_b.id)

    service = _service(db_session)
    assert service.get_owned(owned_by_a.id, user_id=user_a.id) is not None
    assert service.get_owned(owned_by_b.id, user_id=user_a.id) is None


def test_service_update_owned_updates_the_users_own_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)

    updated = _service(db_session).update_owned(
        saved_search.id, user_id=user.id, data=SavedSearchUpdate(query="Updated query")
    )

    assert updated is not None
    assert updated.query == "Updated query"


def test_service_update_owned_cannot_update_another_users_saved_search(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, query="B's original query", user_id=user_b.id)

    result = _service(db_session).update_owned(
        owned_by_b.id, user_id=user_a.id, data=SavedSearchUpdate(query="Hijacked query")
    )

    assert result is None
    unchanged = SavedSearchRepository(db_session).get(owned_by_b.id)
    assert unchanged.query == "B's original query"


def test_service_update_owned_cannot_update_an_unowned_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    unowned = _saved_search(db_session, query="Nobody's query yet", user_id=None)

    result = _service(db_session).update_owned(
        unowned.id, user_id=user.id, data=SavedSearchUpdate(query="Hijacked query")
    )

    assert result is None
    unchanged = SavedSearchRepository(db_session).get(unowned.id)
    assert unchanged.query == "Nobody's query yet"


def test_service_delete_owned_cannot_delete_another_users_saved_search(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)

    deleted = _service(db_session).delete_owned(owned_by_b.id, user_id=user_a.id)

    assert deleted is False
    assert SavedSearchRepository(db_session).get(owned_by_b.id) is not None


def test_service_list_owned_returns_only_this_users_saved_searches(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    a1 = _saved_search(db_session, query="A's search", user_id=user_a.id)
    _saved_search(db_session, query="B's search", user_id=user_b.id)

    results = _service(db_session).list_owned(user_id=user_a.id)

    assert [s.id for s in results] == [a1.id]


# =====================================================================
# run_owned
# =====================================================================


def test_run_owned_runs_the_users_own_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)
    connector = _SpyConnector()

    result = _service(db_session, resolve_connector=lambda _marketplace: connector).run_owned(
        saved_search.id, user_id=user.id
    )

    assert result is not None
    assert result.saved_search_id == saved_search.id
    assert connector.search_calls == ["Makita drill"]


def test_run_owned_cannot_run_another_users_saved_search(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)
    connector = _SpyConnector()

    result = _service(db_session, resolve_connector=lambda _marketplace: connector).run_owned(
        owned_by_b.id, user_id=user_a.id
    )

    assert result is None


def test_run_owned_never_invokes_the_runner_for_a_foreign_owned_id(db_session) -> None:
    """Not just "the result says None" - the connector (and therefore the
    runner underneath run_owned) must never even be called."""
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)
    connector = _SpyConnector()

    _service(db_session, resolve_connector=lambda _marketplace: connector).run_owned(
        owned_by_b.id, user_id=user_a.id
    )

    assert connector.search_calls == []


def test_run_owned_never_invokes_the_runner_for_a_nonexistent_id(db_session) -> None:
    user = _user(db_session, "a@example.com")
    connector = _SpyConnector()

    result = _service(db_session, resolve_connector=lambda _marketplace: connector).run_owned(
        999999, user_id=user.id
    )

    assert result is None
    assert connector.search_calls == []


def test_run_owned_never_invokes_the_runner_for_an_unowned_id(db_session) -> None:
    user = _user(db_session, "a@example.com")
    unowned = _saved_search(db_session, user_id=None)
    connector = _SpyConnector()

    result = _service(db_session, resolve_connector=lambda _marketplace: connector).run_owned(
        unowned.id, user_id=user.id
    )

    assert result is None
    assert connector.search_calls == []


def test_run_owned_nonexistent_and_foreign_owned_ids_are_indistinguishable(db_session) -> None:
    user_a = _user(db_session, "a@example.com")
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)
    connector = _SpyConnector()
    service = _service(db_session, resolve_connector=lambda _marketplace: connector)

    nonexistent_result = service.run_owned(999999, user_id=user_a.id)
    foreign_result = service.run_owned(owned_by_b.id, user_id=user_a.id)

    assert nonexistent_result is None
    assert foreign_result is None
    assert connector.search_calls == []


def test_run_owned_without_a_configured_runner_raises_clearly(db_session) -> None:
    """A service constructed the way every current caller constructs it
    today (no resolve_connector - see dependencies.py's
    get_saved_search_service) must fail loudly if run_owned is ever
    called on it, not silently do nothing."""
    user = _user(db_session, "a@example.com")
    saved_search = _saved_search(db_session, user_id=user.id)

    with pytest.raises(RuntimeError):
        _service(db_session).run_owned(saved_search.id, user_id=user.id)


# =====================================================================
# Existing unscoped behavior is unchanged
# =====================================================================


def test_unscoped_get_still_returns_any_saved_search_regardless_of_owner(db_session) -> None:
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)
    unowned = _saved_search(db_session, user_id=None)

    repo = SavedSearchRepository(db_session)
    assert repo.get(owned_by_b.id) is not None
    assert repo.get(unowned.id) is not None


def test_unscoped_list_all_still_returns_every_saved_search(db_session) -> None:
    user = _user(db_session, "a@example.com")
    _saved_search(db_session, query="Owned", user_id=user.id)
    _saved_search(db_session, query="Unowned", user_id=None)

    assert len(SavedSearchRepository(db_session).list_all()) == 2


def test_unscoped_delete_still_deletes_regardless_of_owner(db_session) -> None:
    user_b = _user(db_session, "b@example.com")
    owned_by_b = _saved_search(db_session, user_id=user_b.id)

    deleted = SavedSearchRepository(db_session).delete(owned_by_b.id)

    assert deleted is True
