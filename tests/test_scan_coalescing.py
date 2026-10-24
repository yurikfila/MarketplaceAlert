"""MA-SCALE-02: scan coalescing and its feature flag.

Coalescing shares ONE marketplace request between saved searches that are due in the same
scanner pass and ask the same marketplace for the exact same query text. These tests pin
the contract: fewer requests, identical alerts.

- Disabled (the default) behaves exactly as before.
- Enabled and disabled runs of the same scenario end in the same database state
  (listings, attributions, notification rows, scan timestamps) - including a seeded
  randomized scenario - so nothing is lost, duplicated or cross-delivered.
- Failures are isolated and retried exactly as without coalescing.

No test touches a real marketplace; all time is simulated.
"""

import logging
import random
from datetime import datetime, timedelta, timezone

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.auth.models import User
from marketplace_alert.core.connectors.base import MarketplaceConnectorError
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing, ListingAttribution, PendingNotification
from marketplace_alert.core.saved_searches.coalescing import ScanFetchCache
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current


def _listing(marketplace: str, external_id: str, title: str) -> Listing:
    return Listing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title=title,
        listing_url=f"https://example.com/{marketplace}/{external_id}",
    )


class FakeMarketplace:
    """A connector-shaped fake. `catalog` maps an exact query text to what it returns."""

    def __init__(self, name: str, catalog: dict[str, list[Listing]], error: Exception | None = None) -> None:
        self.name = name
        self.catalog = catalog
        self.error = error
        self.calls: list[str] = []

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.catalog.get(query, []))


def _make_user(session, email: str) -> int:
    user = User(email=email, password_hash="irrelevant-hash")
    session.add(user)
    session.commit()
    return user.id


def _create(session_factory, query: str, marketplaces: list[str], *, user_id: int | None, interval: int = 300) -> int:
    session = session_factory()
    saved = SavedSearchRepository(session).create(
        query=query, marketplaces=marketplaces, scan_interval_seconds=interval, is_active=True, user_id=user_id
    )
    session.commit()
    identifier = saved.id
    session.close()
    return identifier


def _scanner(session_factory, markets: dict[str, FakeMarketplace], *, coalescing: bool, clock: Clock | None = None):
    clock = clock or Clock()
    runner = SavedSearchRunner(resolve_connector=lambda name: markets[name])
    return BackgroundScanner(
        session_factory=session_factory,
        runner=runner,
        run_guard=SavedSearchRunGuard(),
        clock=clock.now,
        coalescing_enabled=coalescing,
    )


def _state(session_factory) -> dict:
    """Everything a user could observe: stored listings, who each search is attributed, who gets
    notified about what, and when each search was last scanned."""
    session = session_factory()
    try:
        listing_key = {row.id: (row.marketplace, row.external_listing_id) for row in session.query(DiscoveredListing)}
        return {
            "listings": sorted(listing_key.values()),
            "attributions": sorted(
                (a.saved_search_id, listing_key[a.discovered_listing_id]) for a in session.query(ListingAttribution)
            ),
            "notifications": sorted(
                (n.user_id if n.user_id is not None else -1, listing_key[n.discovered_listing_id])
                for n in session.query(PendingNotification)
            ),
            "scanned": sorted(
                (s.id, s.last_scanned_at.replace(tzinfo=None) if s.last_scanned_at else None)
                for s in SavedSearchRepository(session).list_all()
            ),
        }
    finally:
        session.close()


# ---------------------------------------------------------------- fetch cache unit


def test_cache_fetches_once_per_exact_key_and_hands_out_independent_copies() -> None:
    cache = ScanFetchCache()
    calls = []
    original = [_listing("m", "1", "Bosch drill")]

    def fetch():
        calls.append(1)
        return original

    first = cache.fetch("m", "Bosch drill", fetch)
    first[0].title = "mutated by the first consumer"
    second = cache.fetch("m", "Bosch drill", fetch)

    assert len(calls) == 1 and (cache.fetches, cache.reused) == (1, 1)
    assert second[0].title == "Bosch drill"  # a private copy, untouched by the first consumer
    assert original[0].title == "Bosch drill"
    assert first[0] is not second[0]


def test_cache_keys_are_exact_marketplace_and_query_text() -> None:
    cache = ScanFetchCache()
    calls: list[tuple[str, str]] = []

    def fetcher(marketplace, query):
        def fetch():
            calls.append((marketplace, query))
            return []

        return fetch

    for marketplace, query in [("m", "Bosch drill"), ("m", "bosch drill"), ("m", "Bosch  drill"), ("m", "drill Bosch"),
                               ("n", "Bosch drill"), ("m", "Bosch drill")]:  # fmt: skip
        cache.fetch(marketplace, query, fetcher(marketplace, query))

    assert len(calls) == 5  # only the exact repeat was reused
    assert cache.reused == 1


def test_cache_remembers_a_failure_and_reraises_it_for_every_consumer() -> None:
    cache = ScanFetchCache()
    attempts = []

    def failing():
        attempts.append(1)
        raise MarketplaceConnectorError("down")

    for _ in range(3):
        with pytest.raises(MarketplaceConnectorError):
            cache.fetch("m", "q", failing)

    assert len(attempts) == 1  # a marketplace that is down is asked once per pass, not once per search


# ---------------------------------------------------------------- scenarios


def _bosch_catalog(marketplace: str) -> dict[str, list[Listing]]:
    return {
        "Bosch drill": [_listing(marketplace, "d1", "Bosch Cordless Drill 18V"), _listing(marketplace, "f1", "Bosch Diesel Filter")],
        "Makita drill": [_listing(marketplace, "m1", "Makita Cordless Drill")],
    }


def test_two_users_same_query_cost_one_marketplace_request_and_each_gets_their_alert(session_factory) -> None:
    session = session_factory()
    alice, bob = _make_user(session, "alice@example.com"), _make_user(session, "bob@example.com")
    session.close()
    a = _create(session_factory, "Bosch drill", ["m"], user_id=alice)
    b = _create(session_factory, "Bosch drill", ["m"], user_id=bob)
    market = FakeMarketplace("m", _bosch_catalog("m"))

    _scanner(session_factory, {"m": market}, coalescing=True).run_due_searches()

    assert market.calls == ["Bosch drill"]  # ONE request for two searches
    state = _state(session_factory)
    assert state["listings"] == [("m", "d1")]  # the filter ran per search: the diesel filter is rejected for both
    assert state["attributions"] == [(a, ("m", "d1")), (b, ("m", "d1"))]
    assert state["notifications"] == [(alice, ("m", "d1")), (bob, ("m", "d1"))]  # independent rows, one per user


def test_different_query_texts_are_separate_marketplace_searches(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    _create(session_factory, "Makita drill", ["m"], user_id=uid)
    _create(session_factory, "bosch drill", ["m"], user_id=uid)  # same words, different text: never merged
    market = FakeMarketplace("m", _bosch_catalog("m"))

    _scanner(session_factory, {"m": market}, coalescing=True).run_due_searches()

    assert sorted(market.calls) == ["Bosch drill", "Makita drill", "bosch drill"]


def test_same_user_two_identical_searches_get_one_alert_per_listing(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    market = FakeMarketplace("m", _bosch_catalog("m"))

    _scanner(session_factory, {"m": market}, coalescing=True).run_due_searches()

    assert len(market.calls) == 1
    assert _state(session_factory)["notifications"] == [(uid, ("m", "d1"))]  # never two alerts for one listing


def test_relevance_filtering_still_runs_once_per_saved_search(session_factory, monkeypatch) -> None:
    from marketplace_alert.core.saved_searches import runner as runner_module

    seen: list[tuple[str, list[str]]] = []
    real = runner_module.filter_relevant_listings

    def spy(**kwargs):
        seen.append((kwargs["query"], [x.title for x in kwargs["listings"]]))
        if len(seen) == 1:
            for x in kwargs["listings"]:
                x.title = "MUTATED BY THE FIRST SEARCH"  # must not leak to the second search
        return real(**kwargs)

    monkeypatch.setattr(runner_module, "filter_relevant_listings", spy)
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)

    _scanner(session_factory, {"m": FakeMarketplace("m", _bosch_catalog("m"))}, coalescing=True).run_due_searches()

    assert len(seen) == 2
    assert seen[1][1] == ["Bosch Cordless Drill 18V", "Bosch Diesel Filter"]  # second search got pristine objects


@pytest.mark.parametrize("engine", ["legacy", "shadow", "v2"])
def test_relevance_engines_decide_identically_with_and_without_coalescing(session_factory, engine, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "relevance_engine", engine)
    results = []
    for coalescing in (False, True):
        from marketplace_alert.core.persistence.database import Base, create_db_engine
        from sqlalchemy.orm import sessionmaker

        db = create_db_engine(f"sqlite:///{tmp_path / f'{engine}-{coalescing}.db'}")
        Base.metadata.create_all(bind=db)
        factory = sessionmaker(bind=db, autoflush=False, expire_on_commit=False)
        s = factory()
        u1, u2 = _make_user(s, "a@example.com"), _make_user(s, "b@example.com")
        s.close()
        _create(factory, "Bosch drill", ["m"], user_id=u1)
        _create(factory, "Bosch drill", ["m"], user_id=u2)
        _scanner(factory, {"m": FakeMarketplace("m", _bosch_catalog("m"))}, coalescing=coalescing).run_due_searches()
        results.append(_state(factory))
        db.dispose()
    assert results[0] == results[1]


# ---------------------------------------------------------------- failures


def test_a_failed_fetch_is_asked_once_and_every_search_retries_normally_next_pass(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    first = _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    second = _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    market = FakeMarketplace("m", _bosch_catalog("m"), error=MarketplaceConnectorError("marketplace down"))
    clock = Clock()
    scanner = _scanner(session_factory, {"m": market}, coalescing=True, clock=clock)

    scanner.run_due_searches()

    assert market.calls == ["Bosch drill"]  # one failed request, not one per search
    state = _state(session_factory)
    assert state["listings"] == [] and state["notifications"] == []  # nothing was marked seen that was never fetched
    # same as without coalescing: a marketplace error still advances the schedule
    assert [t for _id, t in state["scanned"]] == [T0.replace(tzinfo=None)] * 2

    market.error = None  # the marketplace recovers; the next due pass finds the listing for both searches
    clock.current = T0 + timedelta(seconds=300)
    scanner.run_due_searches()
    assert _state(session_factory)["notifications"] == [(uid, ("m", "d1"))]
    assert len(market.calls) == 2
    del first, second


def test_an_unexpected_exception_in_one_group_does_not_affect_another_group(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    _create(session_factory, "Makita drill", ["m"], user_id=uid)

    class Flaky(FakeMarketplace):
        def search(self, query, filters=None):
            if query == "Bosch drill":
                self.calls.append(query)
                raise RuntimeError("boom")
            return super().search(query, filters)

    market = Flaky("m", _bosch_catalog("m"))
    _scanner(session_factory, {"m": market}, coalescing=True).run_due_searches()

    assert _state(session_factory)["listings"] == [("m", "m1")]  # Makita's group delivered; Bosch's failed alone


def test_one_marketplace_failing_does_not_stop_the_other_marketplace_of_the_same_search(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["bad", "good"], user_id=uid)
    markets = {
        "bad": FakeMarketplace("bad", {}, error=MarketplaceConnectorError("down")),
        "good": FakeMarketplace("good", _bosch_catalog("good")),
    }

    _scanner(session_factory, markets, coalescing=True).run_due_searches()

    assert _state(session_factory)["listings"] == [("good", "d1")]


# ---------------------------------------------------------------- flag, equivalence, semantics


def test_disabled_flag_keeps_one_request_per_search(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    for _ in range(3):
        _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    market = FakeMarketplace("m", _bosch_catalog("m"))

    _scanner(session_factory, {"m": market}, coalescing=False).run_due_searches()

    assert market.calls == ["Bosch drill"] * 3  # legacy behaviour


def test_flag_defaults_to_off_and_follows_the_setting(session_factory, monkeypatch) -> None:
    assert settings.scan_coalescing_enabled is False
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    market = FakeMarketplace("m", _bosch_catalog("m"))
    runner = SavedSearchRunner(resolve_connector=lambda name: market)
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()
    assert len(market.calls) == 2  # default: off

    monkeypatch.setattr(settings, "scan_coalescing_enabled", True)
    market.calls.clear()
    from marketplace_alert.core.saved_searches.repository import SavedSearchRepository as Repo

    s = session_factory()
    for saved in Repo(s).list_all():
        saved.last_scanned_at = None
    s.commit()
    s.close()
    scanner.run_due_searches()
    assert len(market.calls) == 1  # setting honoured without rebuilding the scanner


def test_runner_called_without_a_cache_is_unchanged(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    search_id = _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    market = FakeMarketplace("m", _bosch_catalog("m"))
    session = session_factory()
    SavedSearchRunner(resolve_connector=lambda name: market).run_by_id(session, search_id)  # the manual "Run now" path
    session.commit()
    session.close()
    assert market.calls == ["Bosch drill"]


def test_searches_not_due_in_this_pass_are_not_touched_and_intervals_are_unchanged(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    fast = _create(session_factory, "Bosch drill", ["m"], user_id=uid, interval=300)
    slow = _create(session_factory, "Bosch drill", ["m"], user_id=uid, interval=900)
    market = FakeMarketplace("m", _bosch_catalog("m"))
    clock = Clock()
    scanner = _scanner(session_factory, {"m": market}, coalescing=True, clock=clock)

    scanner.run_due_searches()  # both never scanned: one shared request
    assert len(market.calls) == 1

    clock.current = T0 + timedelta(seconds=300)
    scanner.run_due_searches()  # only the 5-minute search is due: it pays its own request (no cross-pass cache)
    assert len(market.calls) == 2
    scanned = dict(_state(session_factory)["scanned"])
    assert scanned[slow] == T0.replace(tzinfo=None)  # the 15-minute search was not touched
    assert scanned[fast] == (T0 + timedelta(seconds=300)).replace(tzinfo=None)


def test_different_marketplace_selections_share_only_the_marketplaces_they_have_in_common(session_factory) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Bosch drill", ["m", "n"], user_id=uid)
    _create(session_factory, "Bosch drill", ["m"], user_id=uid)
    markets = {"m": FakeMarketplace("m", _bosch_catalog("m")), "n": FakeMarketplace("n", _bosch_catalog("n"))}

    _scanner(session_factory, markets, coalescing=True).run_due_searches()

    assert len(markets["m"].calls) == 1 and len(markets["n"].calls) == 1


def test_enabled_and_disabled_runs_end_in_identical_database_state_for_a_random_scenario(tmp_path) -> None:
    """Seeded property check over many shapes: users, repeated/different queries, overlapping
    marketplace sets, intervals, two passes. Coalescing must never change what anyone is told."""
    from sqlalchemy.orm import sessionmaker

    from marketplace_alert.core.persistence.database import Base, create_db_engine

    queries = ["Bosch drill", "bosch drill", "Makita drill", "Bosch", "Fender Stratocaster", "Acme widget"]
    markets_names = ["m", "n", "o"]

    def catalog(marketplace):
        return {
            "Bosch drill": [_listing(marketplace, "b1", "Bosch Cordless Drill"), _listing(marketplace, "b2", "Bosch Filter")],
            "bosch drill": [_listing(marketplace, "b1", "Bosch Cordless Drill")],
            "Makita drill": [_listing(marketplace, "k1", "Makita Drill 18V")],
            "Bosch": [_listing(marketplace, "b2", "Bosch Filter"), _listing(marketplace, "b3", "Bosch Hammer")],
            "Fender Stratocaster": [_listing(marketplace, "f1", "Fender Stratocaster Guitar")],
            "Acme widget": [_listing(marketplace, "a1", "Acme Widget Pro")],
        }

    for seed in range(12):
        outcomes = []
        for coalescing in (False, True):
            rng = random.Random(seed)
            engine = create_db_engine(f"sqlite:///{tmp_path / f'r{seed}-{coalescing}.db'}")
            Base.metadata.create_all(bind=engine)
            factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            s = factory()
            users = [_make_user(s, f"u{i}@example.com") for i in range(3)]
            s.close()
            for _ in range(rng.randint(3, 9)):
                _create(
                    factory,
                    rng.choice(queries),
                    rng.sample(markets_names, rng.randint(1, 3)),
                    user_id=rng.choice(users),
                    interval=rng.choice([300, 900]),
                )
            fakes = {name: FakeMarketplace(name, catalog(name)) for name in markets_names}
            clock = Clock()
            scanner = _scanner(factory, fakes, coalescing=coalescing, clock=clock)
            for step in range(3):  # three passes: 0, +5 min, +10 min
                clock.current = T0 + timedelta(seconds=300 * step)
                scanner.run_due_searches()
            outcomes.append((_state(factory), sum(len(f.calls) for f in fakes.values())))
            engine.dispose()
        (plain_state, plain_calls), (shared_state, shared_calls) = outcomes
        assert plain_state == shared_state, f"seed {seed}: state differs"
        assert shared_calls <= plain_calls, f"seed {seed}: coalescing made more requests"


# ---------------------------------------------------------------- metrics


def test_pass_summary_reports_counts_only(session_factory, caplog) -> None:
    uid = _make_user(session_factory(), "u@example.com")
    _create(session_factory, "Secret Query Words", ["m"], user_id=uid)
    _create(session_factory, "Secret Query Words", ["m"], user_id=uid)
    market = FakeMarketplace("m", {"Secret Query Words": [_listing("m", "1", "Secret Query Words item")]})

    with caplog.at_level(logging.INFO):
        _scanner(session_factory, {"m": market}, coalescing=True).run_due_searches()

    lines = [r.getMessage() for r in caplog.records if "Scan pass requests" in r.getMessage()]
    assert len(lines) == 1
    assert "searches=2 coalescing=on fetches=1 reused=1" in lines[0]
    assert "secret" not in caplog.text.lower().replace("secret_", "")  # no query text anywhere in the logs


def test_an_idle_pass_logs_nothing(session_factory, caplog) -> None:
    with caplog.at_level(logging.INFO):
        _scanner(session_factory, {}, coalescing=True).run_due_searches()
    assert not [r for r in caplog.records if "Scan pass requests" in r.getMessage()]
