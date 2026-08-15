from datetime import datetime, timedelta, timezone

import httpx
import pytest

from marketplace_alert.connectors.ebay.connector import EbayMarketplaceConnector
from marketplace_alert.connectors.etsy.connector import EtsyMarketplaceConnector
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.service import NotificationService
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner


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


class RecordingProvider(NotificationProvider):
    def __init__(self) -> None:
        self.sent: list[Listing] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        self.sent.append(listing)


class AlwaysFailingNotificationProvider(NotificationProvider):
    """A notification provider that always raises, to test that the scheduler
    and saved-search runner survive a Telegram-side failure (not just a
    connector-side one)."""

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        raise NotificationError("simulated Telegram failure")


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

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    assert len(provider.sent) == 1

    verify_session = session_factory()
    saved_search = SavedSearchRepository(verify_session).list_all()[0]
    assert saved_search.last_scanned_at is not None
    verify_session.close()


def test_scanner_does_not_notify_twice_for_same_listing(session_factory) -> None:
    setup_session = session_factory()
    SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()
    assert len(provider.sent) == 1

    # The interval (60s) hasn't elapsed - an immediate second tick must not
    # find it due again, so the same listing must not be notified twice.
    scanner.run_due_searches()
    assert len(provider.sent) == 1


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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing()]) if marketplace == "good" else BrokenConnector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the "broken" saved search always fails.
    scanner.run_due_searches()

    assert len(provider.sent) == 1

    verify_session = session_factory()
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
        query="Makita", marketplaces=["good", "also-good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing(marketplace=marketplace, external_id="item-1")])

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    # Same external_listing_id but different marketplace = two distinct,
    # both-new listings - duplicate detection stays marketplace-specific.
    assert len(provider.sent) == 2
    assert {listing.marketplace for listing in provider.sent} == {"good", "also-good"}


def test_one_failing_marketplace_does_not_stop_another_in_the_same_search(session_factory) -> None:
    """Within ONE saved search targeting two marketplaces, one failing
    connector must not prevent the other marketplace from being searched,
    notified, and the saved search still completing (marked scanned)."""
    setup_session = session_factory()
    created = SavedSearchRepository(setup_session).create(
        query="Makita", marketplaces=["good", "broken"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    saved_search_id = created.id
    setup_session.close()

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        return FakeConnector([_listing(marketplace="good")]) if marketplace == "good" else BrokenConnector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    assert len(provider.sent) == 1
    assert provider.sent[0].marketplace == "good"

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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real Etsy connector's HTTP call fails.
    scanner.run_due_searches()

    assert len(provider.sent) == 1

    verify_session = session_factory()
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

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: EtsyMarketplaceConnector(
            api_key="fake-key", shared_secret="fake-secret"
        ),
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(provider.sent) == 1


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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        if marketplace == "good":
            return FakeConnector([_listing()])
        return _ebay_connector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though the real eBay connector's HTTP call fails.
    scanner.run_due_searches()

    assert len(provider.sent) == 1

    verify_session = session_factory()
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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    assert len(provider.sent) == 1
    assert provider.sent[0].marketplace == "etsy"

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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )

    run_session = session_factory()
    result = runner.run_by_id(run_session, saved_search_id)
    run_session.commit()
    run_session.close()

    assert len(provider.sent) == 1
    assert provider.sent[0].marketplace == "ebay"

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

    provider = RecordingProvider()
    runner = SavedSearchRunner(
        notification_service=NotificationService(provider),
        resolve_connector=lambda name: _ebay_connector(),
    )

    first = runner.run(db_session, saved_search)
    second = runner.run(db_session, saved_search)

    assert first.new_count == 1
    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert len(provider.sent) == 1


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

    provider = RecordingProvider()

    def resolve_connector(marketplace: str):
        if marketplace == "etsy":
            return EtsyMarketplaceConnector(api_key="fake-key", shared_secret="fake-secret")
        return _ebay_connector()

    runner = SavedSearchRunner(
        notification_service=NotificationService(provider), resolve_connector=resolve_connector
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    scanner.run_due_searches()

    assert len(provider.sent) == 2
    assert {listing.marketplace for listing in provider.sent} == {"etsy", "ebay"}
    assert all(listing.title == "Makita Cordless Drill" for listing in provider.sent)


# --- notification (Telegram) failures must not crash the run/scheduler -----


def test_notification_failure_does_not_crash_a_manual_run(session_factory) -> None:
    """A Telegram-side failure (not a connector failure) must not prevent
    `SavedSearchRunner.run` from completing and reporting the marketplace
    as successfully searched."""
    setup_session = session_factory()
    saved_search = SavedSearchRepository(setup_session).create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    runner = SavedSearchRunner(
        notification_service=NotificationService(AlwaysFailingNotificationProvider()),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )

    run_session = session_factory()
    # Must not raise, even though every notification send fails.
    result = runner.run_by_id(run_session, saved_search.id)
    run_session.commit()
    run_session.close()

    assert result is not None
    assert result.new_count == 1
    assert result.results[0].error is None  # the search itself succeeded


def test_notification_failure_does_not_crash_the_scheduler(session_factory) -> None:
    """Same as above, but through the full BackgroundScanner tick - and a
    second, healthy saved search must still run in the same tick."""
    setup_session = session_factory()
    repository = SavedSearchRepository(setup_session)
    failing_notify = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    other = repository.create(
        query="Rolex", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    setup_session.commit()
    setup_session.close()

    runner = SavedSearchRunner(
        notification_service=NotificationService(AlwaysFailingNotificationProvider()),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    scanner = BackgroundScanner(session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard())

    # Must not raise, even though every notification send fails for both.
    scanner.run_due_searches()

    verify_session = session_factory()
    verify_repository = SavedSearchRepository(verify_session)
    assert verify_repository.get(failing_notify.id).last_scanned_at is not None
    assert verify_repository.get(other.id).last_scanned_at is not None
    verify_session.close()


def test_listing_already_marked_seen_is_not_renotified_after_a_prior_notification_failure(
    db_session,
) -> None:
    """The database, not Telegram delivery success, is the source of truth
    for "already discovered". A listing that was persisted as new during a
    run where notification failed must NOT be treated as new again on a
    later run just because it was never successfully delivered."""
    repository = SavedSearchRepository(db_session)
    saved_search = repository.create(
        query="Charizard", marketplaces=["good"], scan_interval_seconds=60, is_active=True
    )
    db_session.commit()

    failing_runner = SavedSearchRunner(
        notification_service=NotificationService(AlwaysFailingNotificationProvider()),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    first = failing_runner.run(db_session, saved_search)
    assert first.new_count == 1  # persisted as discovered, even though the alert failed

    # A second run, now with a healthy provider and the connector still
    # returning the exact same listing - it must be reported already-seen,
    # not new, and must not be notified.
    working_provider = RecordingProvider()
    working_runner = SavedSearchRunner(
        notification_service=NotificationService(working_provider),
        resolve_connector=lambda name: FakeConnector([_listing()]),
    )
    second = working_runner.run(db_session, saved_search)

    assert second.new_count == 0
    assert second.already_seen_count == 1
    assert working_provider.sent == []
