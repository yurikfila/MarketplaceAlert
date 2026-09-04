from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from marketplace_alert.connectors.bonanza.connector import BonanzaMarketplaceConnector
from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.connectors.reverb.connector import ReverbMarketplaceConnector
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing, PendingNotification
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner


def _pending_notification_listings(session) -> list[Listing]:
    """Every notification-outbox row's listing, in enqueue order.

    `SavedSearchRunner` never sends anything directly anymore - a new
    listing only ever gets a `pending_notifications` row (see that
    module's docstring). This is the outbox equivalent of what these
    tests used to check via a fake `NotificationProvider`'s recorded
    sends - "what would eventually be sent by a later drain run",
    not "what was sent just now".
    """
    stmt = (
        select(PendingNotification, DiscoveredListing)
        .join(DiscoveredListing, PendingNotification.discovered_listing_id == DiscoveredListing.id)
        .order_by(PendingNotification.created_at.asc(), PendingNotification.id.asc())
    )
    return [
        Listing(
            marketplace=row.marketplace,
            external_listing_id=row.external_listing_id,
            title=row.title,
            listing_url=row.listing_url,
        )
        for _, row in session.execute(stmt).all()
    ]


def _listing(marketplace: str = "good", external_id: str = "item-1") -> Listing:
    return Listing(
        marketplace=marketplace,
        external_listing_id=external_id,
        title="Pokemon Charizard",
        listing_url=f"https://example.com/{marketplace}/listing/{external_id}",
    )


class FakeConnector:
    """A connector-shaped fake returning a fixed list of listings - never
    MockMarketplaceConnector, so these tests prove the scanner/runner work
    with any connector reachable through the registry, not just the mock one."""

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        return self._listings


class BrokenConnector:
    """A connector-shaped fake that always fails, to test scanner/runner resilience."""

    def search(self, query: str, filters: dict | None = None) -> list[Listing]:
        raise RuntimeError("simulated connector failure")


def test_never_scanned_active_search_is_due(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    repository.create(query="Pokemon", marketplaces=["mock"], scan_interval_seconds=60, is_active=True)
    db_session.commit()

    assert len(repository.list_due_for_scan()) == 1


def test_recently_scanned_search_is_not_due(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Pokemon", marketplaces=["mock"], scan_interval_seconds=60, is_active=True
    )
    repository.mark_scanned(saved_search)
    db_session.commit()

    assert repository.list_due_for_scan() == []


def test_overdue_search_is_due(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Pokemon", marketplaces=["mock"], scan_interval_seconds=60, is_active=True
    )
    saved_search.last_scanned_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db_session.add(saved_search)
    db_session.commit()

    due = repository.list_due_for_scan()
    assert [s.id for s in due] == [saved_search.id]


def test_inactive_search_is_never_due(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    repository.create(query="Pokemon", marketplaces=["mock"], scan_interval_seconds=60, is_active=False)
    db_session.commit()

    assert repository.list_due_for_scan() == []


def test_scanner_runs_due_search_and_notifies_new_listing(session_factory) -> None:
    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    saved_search = SavedSearchRepository(verify_session).list_all()[0]
    assert saved_search.last_scanned_at is not None
    verify_session.close()


def test_scanner_runs_due_searches_belonging_to_multiple_different_users(session_factory) -> None:
    """The scheduler must remain fully unscoped - see the ownership-
    enforcement phase's scheduler requirement: it processes every due
    saved search regardless of which user (or no user at all) owns it,
    since `list_due_for_scan`/`run_by_id` never go through the HTTP layer
    or `get_current_user` at all."""
    from marketplace_alert.core.auth.models import User

    setup_session = session_factory()
    user_a = User(email="scheduler-a@example.com", password_hash="irrelevant-hash")
    user_b = User(email="scheduler-b@example.com", password_hash="irrelevant-hash")
    setup_session.add_all([user_a, user_b])
    setup_session.commit()

    repo = SavedSearchRepository(setup_session)
    search_a = repo.create(
        query="A's search", marketplaces=["good"], scan_interval_seconds=60, is_active=True, user_id=user_a.id
    )
    search_b = repo.create(
        query="B's search", marketplaces=["good"], scan_interval_seconds=60, is_active=True, user_id=user_b.id
    )
    search_unowned = repo.create(
        query="Unowned search", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    runner = SavedSearchRunner(resolve_connector=lambda name: FakeConnector([_listing()]))
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    scanned_ids = {
        s.id for s in SavedSearchRepository(verify_session).list_all() if s.last_scanned_at is not None
    }
    assert scanned_ids == {search_a.id, search_b.id, search_unowned.id}
    verify_session.close()


def test_scanner_does_not_notify_twice_for_same_listing(session_factory) -> None:
    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1

    # The interval (60s) hasn't elapsed - an immediate second tick must not
    # find it due again, so the same listing must not get a second outbox row.
    scanner.run_due_searches()
    verify_session.expire_all()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_session.close()


def test_one_failing_saved_search_does_not_stop_others(session_factory) -> None:
    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    good = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    broken = repository.create(
        query="Anything", marketplaces=["broken"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing()]) if marketplace == "good" else BrokenConnector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the "broken" saved search always fails.
    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(good.id).last_scanned_at is not None
    # A within-search connector failure is caught by the runner itself now
    # (see below), so even the "broken" saved search completes and gets
    # marked scanned - it just has an error recorded for that marketplace.
    assert verify_repository.get(broken.id).last_scanned_at is not None
    verify_session.close()


def test_run_guard_prevents_double_acquire() -> None:
    guard = SavedSearchRunGuard()
    assert guard.try_acquire(1) is True
    assert guard.try_acquire(1) is False

    guard.release(1)
    assert guard.try_acquire(1) is True


def test_run_guard_reset_clears_all_tracked_runs() -> None:
    guard = SavedSearchRunGuard()
    guard.try_acquire(1)
    guard.try_acquire(2)

    guard.reset()

    assert guard.try_acquire(1) is True
    assert guard.try_acquire(2) is True


# --- one saved search targeting multiple marketplaces -----------------


def test_scheduler_searches_every_marketplace_on_one_saved_search(session_factory) -> None:
    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good", "also-good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing(marketplace=marketplace, external_id="item-1")])

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    # Same external_listing_id but different marketplace = two distinct,
    # both-new listings - duplicate detection stays marketplace-specific.
    enqueued = _pending_notification_listings(verify_session)
    assert len(enqueued) == 2
    assert {listing.marketplace for listing in enqueued} == {"good", "also-good"}
    verify_session.close()


def test_one_failing_marketplace_does_not_stop_another_in_the_same_search(session_factory) -> None:
    """Within ONE saved search targeting two marketplaces, one failing
    connector must not prevent the other marketplace from being searched,
    enqueued for notification, and the saved search still completing
    (marked scanned)."""
    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good", "broken"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing(marketplace="good")]) if marketplace == "good" else BrokenConnector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()

    enqueued = _pending_notification_listings(run_session)
    assert len(enqueued) == 1
    assert enqueued[0].marketplace == "good"
    run_session.close()

    assert result is not None
    by_marketplace = {r.marketplace: r for r in result.results}
    assert by_marketplace["good"].new_count == 1
    assert by_marketplace["good"].error is None
    assert by_marketplace["broken"].new_count == 0
    assert by_marketplace["broken"].error is not None

    verify_session = session_factory()
    saved_search = SavedSearchRepository(verify_session).get(saved_search_id)
    assert saved_search.last_scanned_at is not None
    verify_session.close()


# --- against a real EtsyMarketplaceConnector (httpx mocked, no network) ----


def test_scheduler_survives_a_real_etsy_connector_failure(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved search using the real EtsyMarketplaceConnector (not a fake)
    whose HTTP call fails must not stop a healthy saved search's scan."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))

    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    good = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    etsy = repository.create(
        query="Maccabi", marketplaces=["etsy"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real Etsy connector's HTTP call fails.
    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(good.id).last_scanned_at is not None
    assert verify_repository.get(etsy.id).last_scanned_at is not None
    verify_session.close()


def test_etsy_listing_duplicate_is_not_notified_twice(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the same saved search twice against the real Etsy connector,
    with the API returning the same listing both times, must only notify once."""
    etsy_response = {
        "count": 1,
        "results": [
            {
                "listing_id": 555,
                "title": "Maccabi Tel Aviv Vintage Pennant",
                "url": "https://www.etsy.com/listing/555/maccabi-pennant",
                "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
            }
        ],
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=etsy_response))

    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Maccabi", marketplaces=["etsy"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: EtsyMarketplaceConnector(
            api_key="fake-key", shared_secret="fake-secret"
        ),
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(_pending_notification_listings(db_session)) == 1


# --- against a real EbayMarketplaceConnector (httpx mocked, no network) ----

_EBAY_TOKEN_BODY = {"access_token": "fake-access-token", "expires_in": 7200}


def _ebay_connector() -> EbayMarketplaceConnector:
    return EbayMarketplaceConnector(app_id="fake-app-id", cert_id="fake-cert-id")


def test_scheduler_survives_a_real_ebay_connector_failure(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved search using the real EbayMarketplaceConnector (not a fake)
    whose HTTP call fails must not stop a healthy saved search's scan."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))

    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    good = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    ebay = repository.create(
        query="Makita", marketplaces=["ebay"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return _ebay_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real eBay connector's HTTP call fails.
    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(good.id).last_scanned_at is not None
    assert verify_repository.get(ebay.id).last_scanned_at is not None
    verify_session.close()


def test_etsy_still_runs_if_ebay_fails_in_same_saved_search(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One saved search targeting both Etsy and eBay: eBay's HTTP call
    failing must not prevent Etsy from being searched and notified."""
    etsy_response = {
        "count": 1,
        "results": [
            {
                "listing_id": 111,
                "title": "Makita Cordless Drill",
                "url": "https://www.etsy.com/listing/111/makita-drill",
                "price": {"amount": 5000, "divisor": 100, "currency_code": "USD"},
            }
        ],
    }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))

    def fake_get(url, *args, **kwargs):
        if "etsy.com" in url:
            return httpx.Response(200, json=etsy_response)
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "get", fake_get)

    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Makita", marketplaces=["etsy", "ebay"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()

    enqueued = _pending_notification_listings(run_session)
    assert len(enqueued) == 1
    assert enqueued[0].marketplace == "etsy"
    run_session.close()

    by_marketplace = {r.marketplace: r for r in result.results}
    assert by_marketplace["etsy"].new_count == 1
    assert by_marketplace["etsy"].error is None
    assert by_marketplace["ebay"].new_count == 0
    assert by_marketplace["ebay"].error is not None


def test_ebay_still_runs_if_etsy_fails_in_same_saved_search(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above, reversed: Etsy's HTTP call failing must not prevent
    eBay from being searched and notified."""
    ebay_response = {
        "itemSummaries": [
            {
                "itemId": "v1|222|0",
                "title": "Makita Cordless Drill",
                "price": {"value": "60.00", "currency": "USD"},
                "itemWebUrl": "https://www.ebay.com/itm/222",
            }
        ]
    }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))

    def fake_get(url, *args, **kwargs):
        if "ebay.com" in url:
            return httpx.Response(200, json=ebay_response)
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "get", fake_get)

    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Makita", marketplaces=["etsy", "ebay"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()

    enqueued = _pending_notification_listings(run_session)
    assert len(enqueued) == 1
    assert enqueued[0].marketplace == "ebay"
    run_session.close()

    by_marketplace = {r.marketplace: r for r in result.results}
    assert by_marketplace["ebay"].new_count == 1
    assert by_marketplace["ebay"].error is None
    assert by_marketplace["etsy"].new_count == 0
    assert by_marketplace["etsy"].error is not None


def test_ebay_listing_duplicate_is_not_notified_twice(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the same saved search twice against the real eBay connector,
    with the API returning the same listing both times, must only notify once."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))
    ebay_response = {
        "itemSummaries": [
            {
                "itemId": "v1|333|0",
                "title": "Makita Cordless Drill",
                "price": {"value": "75.00", "currency": "USD"},
                "itemWebUrl": "https://www.ebay.com/itm/333",
            }
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=ebay_response))

    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Makita", marketplaces=["ebay"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: _ebay_connector(),
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(_pending_notification_listings(db_session)) == 1


def test_same_title_on_etsy_and_ebay_remains_two_separate_listings(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity is marketplace + external_listing_id, never title - an Etsy
    "Makita" listing and an eBay "Makita" listing are independent, both new."""
    etsy_response = {
        "count": 1,
        "results": [
            {
                "listing_id": 444,
                "title": "Makita Cordless Drill",
                "url": "https://www.etsy.com/listing/444/makita-drill",
                "price": {"amount": 4000, "divisor": 100, "currency_code": "USD"},
            }
        ],
    }
    ebay_response = {
        "itemSummaries": [
            {
                "itemId": "v1|444|0",
                "title": "Makita Cordless Drill",
                "price": {"value": "40.00", "currency": "USD"},
                "itemWebUrl": "https://www.ebay.com/itm/444",
            }
        ]
    }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))

    def fake_get(url, *args, **kwargs):
        if "etsy.com" in url:
            return httpx.Response(200, json=etsy_response)
        return httpx.Response(200, json=ebay_response)

    monkeypatch.setattr(httpx, "get", fake_get)

    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Makita", marketplaces=["etsy", "ebay"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    enqueued = _pending_notification_listings(verify_session)
    assert len(enqueued) == 2
    assert {listing.marketplace for listing in enqueued} == {"etsy", "ebay"}
    assert all(listing.title == "Makita Cordless Drill" for listing in enqueued)
    verify_session.close()


# --- outbox enqueueing: never re-enqueued, never blocks scanning -----------
#
# Notification *delivery* (Telegram) is no longer part of this runner at
# all - see `SavedSearchRunner`'s module docstring. The scheduler/runner
# tests below only ever check that the *outbox row* was (or wasn't)
# created; whether it eventually gets delivered, retried, or fails
# permanently is `core/notifications/outbox.py`'s concern, covered in
# `tests/test_notification_outbox.py`. This directly replaces two tests
# that used to simulate a failing `NotificationProvider` to prove a scan
# survives Telegram going down - that's now a structural guarantee (the
# runner has no code path that could call a notification provider at
# all), not something that needs an exception-catching test to prove.


def test_listing_already_marked_seen_does_not_get_a_second_outbox_row(db_session) -> None:
    """The database, not notification delivery, is the source of truth
    for "already discovered". A listing enqueued once must not get a
    second `pending_notifications` row just because the same saved search
    finds it again later."""
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    runner = SavedSearchRunner(resolve_connector=lambda name: FakeConnector([_listing()]))

    first = runner.run(db_session, saved_search)
    assert first.new_count == 1
    assert len(_pending_notification_listings(db_session)) == 1

    # A second run, connector still returning the exact same listing - it
    # must be reported already-seen, not new, and must not get a second
    # outbox row.
    second = runner.run(db_session, saved_search)

    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(_pending_notification_listings(db_session)) == 1


# --- against a real ReverbMarketplaceConnector (httpx mocked, no network) --


def _reverb_connector() -> ReverbMarketplaceConnector:
    return ReverbMarketplaceConnector(api_token="fake-reverb-token")


def _reverb_listings_response(*raw_listings: dict) -> dict:
    return {"listings": list(raw_listings), "_links": {}}


def _raw_reverb_listing(listing_id: int, title: str) -> dict:
    return {
        "id": listing_id,
        "title": title,
        "price": {"amount": "999.00", "currency": "USD"},
        "_links": {"web": {"href": f"https://reverb.com/item/{listing_id}"}},
    }


def test_scheduler_survives_a_real_reverb_connector_failure(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved search using the real ReverbMarketplaceConnector (not a
    fake) whose HTTP call fails must not stop a healthy saved search's
    scan - same resilience guarantee already proven for Etsy/eBay."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(500))

    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    good = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    reverb = repository.create(
        query="Fender Stratocaster", marketplaces=["reverb"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return _reverb_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real Reverb connector's HTTP call fails.
    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(good.id).last_scanned_at is not None
    assert verify_repository.get(reverb.id).last_scanned_at is not None
    verify_session.close()


def test_etsy_and_ebay_still_run_if_reverb_fails_in_same_saved_search(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One saved search targeting Etsy, eBay, and Reverb: Reverb's HTTP
    call failing must not prevent Etsy or eBay from being searched and
    notified."""
    etsy_response = {
        "count": 1,
        "results": [
            {
                "listing_id": 111,
                "title": "Fender Stratocaster",
                "url": "https://www.etsy.com/listing/111/fender-strat",
                "price": {"amount": 100000, "divisor": 100, "currency_code": "USD"},
            }
        ],
    }
    ebay_response = {
        "itemSummaries": [
            {
                "itemId": "v1|222|0",
                "title": "Fender Stratocaster",
                "price": {"value": "1000.00", "currency": "USD"},
                "itemWebUrl": "https://www.ebay.com/itm/222",
            }
        ]
    }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))

    def fake_get(url, *args, **kwargs):
        if "etsy.com" in url:
            return httpx.Response(200, json=etsy_response)
        if "ebay.com" in url:
            return httpx.Response(200, json=ebay_response)
        return httpx.Response(500)  # Reverb

    monkeypatch.setattr(httpx, "get", fake_get)

    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Fender Stratocaster",
        marketplaces=["etsy", "ebay", "reverb"],
        scan_interval_seconds=60,
        is_active=True,
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        if marketplace == "ebay":
            return EbayMarketplaceConnector(app_id="fake-app-id", cert_id="fake-cert-id")
        return _reverb_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()

    enqueued = _pending_notification_listings(run_session)
    assert {listing.marketplace for listing in enqueued} == {"etsy", "ebay"}
    run_session.close()

    by_marketplace = {r.marketplace: r for r in result.results}
    assert by_marketplace["etsy"].new_count == 1
    assert by_marketplace["etsy"].error is None
    assert by_marketplace["ebay"].new_count == 1
    assert by_marketplace["ebay"].error is None
    assert by_marketplace["reverb"].new_count == 0
    assert by_marketplace["reverb"].error is not None


def test_reverb_listing_duplicate_is_not_notified_twice(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the same saved search twice against the real Reverb
    connector, with the API returning the same listing both times, must
    only notify once - identical dedup guarantee already proven for
    Etsy/eBay, now also for Reverb (marketplace + external_listing_id)."""
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, json=_reverb_listings_response(_raw_reverb_listing(555, "Fender Stratocaster"))
        ),
    )

    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Fender Stratocaster", marketplaces=["reverb"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: _reverb_connector()
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(_pending_notification_listings(db_session)) == 1


def test_reverb_and_ebay_ids_are_independent_even_with_the_same_external_id(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity is marketplace + external_listing_id, never the id alone -
    a Reverb listing "444" and an eBay listing "444" are independent, both
    new. Same guarantee already proven for Etsy vs eBay, extended to
    Reverb."""
    ebay_response = {
        "itemSummaries": [
            {
                "itemId": "444",
                "title": "Fender Stratocaster",
                "price": {"value": "1000.00", "currency": "USD"},
                "itemWebUrl": "https://www.ebay.com/itm/444",
            }
        ]
    }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json=_EBAY_TOKEN_BODY))

    def fake_get(url, *args, **kwargs):
        if "ebay.com" in url:
            return httpx.Response(200, json=ebay_response)
        return httpx.Response(200, json=_reverb_listings_response(_raw_reverb_listing(444, "Fender Stratocaster")))

    monkeypatch.setattr(httpx, "get", fake_get)

    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Fender Stratocaster", marketplaces=["reverb", "ebay"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "ebay":
            return EbayMarketplaceConnector(app_id="fake-app-id", cert_id="fake-cert-id")
        return _reverb_connector()

    runner = SavedSearchRunner(resolve_connector=resolve_connector)
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    verify_session = session_factory()
    enqueued = _pending_notification_listings(verify_session)
    assert len(enqueued) == 2
    assert {listing.marketplace for listing in enqueued} == {"reverb", "ebay"}
    assert {listing.external_listing_id for listing in enqueued} == {"444"}
    verify_session.close()


# --- Reverb results go through the same relevance engine as every other
# marketplace ------------------------------------------------------------


def test_reverb_results_pass_through_the_relevance_engine(db_session) -> None:
    """A Reverb saved search returning both a relevant and an irrelevant
    listing must persist/notify only the relevant one - proves relevance
    filtering is applied to Reverb results via the shared runner path,
    not something ReverbMarketplaceConnector itself would need to do."""
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Makita drill", marketplaces=["reverb"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    relevant = Listing(
        marketplace="reverb",
        external_listing_id="rel-1",
        title="Makita Cordless Drill 18V",
        listing_url="https://reverb.com/item/rel-1",
    )
    irrelevant = Listing(
        marketplace="reverb",
        external_listing_id="irrel-1",
        title="Makita Battery Holder Wall Mount",
        listing_url="https://reverb.com/item/irrel-1",
    )

    runner = SavedSearchRunner(
        resolve_connector=lambda name: FakeConnector([relevant, irrelevant]),
    )

    result = runner.run(db_session, saved_search)

    assert result.results[0].marketplace == "reverb"
    assert result.results[0].new_count == 1
    assert result.results[0].rejected_count == 1
    enqueued = _pending_notification_listings(db_session)
    assert len(enqueued) == 1
    assert enqueued[0].external_listing_id == "rel-1"

    # The irrelevant listing was never persisted as discovered either -
    # relevance filtering happens before duplicate detection, not after.
    from marketplace_alert.core.persistence.models import DiscoveredListing

    assert (
        db_session.query(DiscoveredListing)
        .filter_by(marketplace="reverb", external_listing_id="irrel-1")
        .first()
        is None
    )
    assert (
        db_session.query(DiscoveredListing)
        .filter_by(marketplace="reverb", external_listing_id="rel-1")
        .first()
        is not None
    )


# --- against a real BonanzaMarketplaceConnector (httpx mocked, no network) -


def _bonanza_connector() -> BonanzaMarketplaceConnector:
    return BonanzaMarketplaceConnector(dev_name="fake-bonanza-dev-name")


def _bonanza_listings_response(*raw_listings: dict) -> dict:
    return {"findItemsByKeywordsResponse": {"ack": "Success", "searchResult": {"item": list(raw_listings)}}}


def _raw_bonanza_listing(item_id: str, title: str) -> dict:
    return {
        "itemId": item_id,
        "title": title,
        "viewItemURL": f"https://www.bonanza.com/listings/{item_id}",
        "sellingStatus": {"currentPrice": {"__value__": "899.99", "@currencyId": "USD"}},
    }


def test_scheduler_survives_a_real_bonanza_connector_failure(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved search using the real BonanzaMarketplaceConnector (not a
    fake) whose HTTP call fails must not stop a healthy saved search's
    scan - same resilience guarantee already proven for Etsy/eBay/Reverb."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(500))

    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    good = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    bonanza = repository.create(
        query="Fender Stratocaster", marketplaces=["bonanza"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return _bonanza_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real Bonanza connector's HTTP call fails.
    scanner.run_due_searches()

    verify_session = session_factory()
    assert len(_pending_notification_listings(verify_session)) == 1
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(good.id).last_scanned_at is not None
    assert verify_repository.get(bonanza.id).last_scanned_at is not None
    verify_session.close()


def test_other_marketplaces_still_run_if_bonanza_fails_in_the_same_saved_search(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One saved search targeting Reverb and Bonanza: Bonanza's HTTP call
    failing must not prevent Reverb from being searched and notified."""

    def fake_get(url, *args, **kwargs):
        # Reverb's connector uses httpx.get.
        return httpx.Response(
            200,
            json={
                "listings": [
                    {
                        "id": 111,
                        "title": "Fender Stratocaster",
                        "_links": {"web": {"href": "https://reverb.com/item/111"}},
                    }
                ],
                "_links": {},
            },
        )

    def fake_post(url, *args, **kwargs):
        # Bonanza's connector uses httpx.post - always fails.
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Fender Stratocaster", marketplaces=["reverb", "bonanza"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    def resolve_connector(marketplace: str):
        if marketplace == "reverb":
            return ReverbMarketplaceConnector(api_token="fake-reverb-token")
        return _bonanza_connector()

    runner = SavedSearchRunner(
        resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()

    enqueued = _pending_notification_listings(run_session)
    assert {listing.marketplace for listing in enqueued} == {"reverb"}
    run_session.close()

    by_marketplace = {r.marketplace: r for r in result.results}
    assert by_marketplace["reverb"].new_count == 1
    assert by_marketplace["reverb"].error is None
    assert by_marketplace["bonanza"].new_count == 0
    assert by_marketplace["bonanza"].error is not None


def test_bonanza_listing_duplicate_is_not_notified_twice(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the same saved search twice against the real Bonanza
    connector, with the API returning the same listing both times, must
    only notify once - identical dedup guarantee already proven for
    Etsy/eBay/Reverb, now also for Bonanza (marketplace + external_listing_id)."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200, json=_bonanza_listings_response(_raw_bonanza_listing("555", "Fender Stratocaster"))
        ),
    )

    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Fender Stratocaster", marketplaces=["bonanza"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    runner = SavedSearchRunner(
        resolve_connector=lambda name: _bonanza_connector()
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(_pending_notification_listings(db_session)) == 1


def test_bonanza_results_pass_through_the_relevance_engine(db_session) -> None:
    """A Bonanza saved search returning both a relevant and an irrelevant
    listing must persist/notify only the relevant one - proves relevance
    filtering is applied to Bonanza results via the shared runner path,
    not something BonanzaMarketplaceConnector itself would need to do."""
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Makita drill", marketplaces=["bonanza"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    relevant = Listing(
        marketplace="bonanza",
        external_listing_id="rel-1",
        title="Makita Cordless Drill 18V",
        listing_url="https://www.bonanza.com/listings/rel-1",
    )
    irrelevant = Listing(
        marketplace="bonanza",
        external_listing_id="irrel-1",
        title="Makita Battery Holder Wall Mount",
        listing_url="https://www.bonanza.com/listings/irrel-1",
    )

    runner = SavedSearchRunner(
        resolve_connector=lambda name: FakeConnector([relevant, irrelevant]),
    )

    result = runner.run(db_session, saved_search)

    assert result.results[0].marketplace == "bonanza"
    assert result.results[0].new_count == 1
    assert result.results[0].rejected_count == 1
    enqueued = _pending_notification_listings(db_session)
    assert len(enqueued) == 1
    assert enqueued[0].external_listing_id == "rel-1"

    assert (
        db_session.query(DiscoveredListing)
        .filter_by(marketplace="bonanza", external_listing_id="irrel-1")
        .first()
        is None
    )
    assert (
        db_session.query(DiscoveredListing)
        .filter_by(marketplace="bonanza", external_listing_id="rel-1")
        .first()
        is not None
    )
