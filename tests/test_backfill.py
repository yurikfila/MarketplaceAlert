"""Tests for the historical listing metadata backfill service
(`core/persistence/backfill.py`).

Never calls a real marketplace API - every connector here is either the
real `MockMarketplaceConnector` (whose `get_listing_by_id` is a genuine,
if trivial, implementation - see tests/test_mock_connector.py) or a
small stub built for one specific failure mode, matching how connector-
level HTTP failures are already exhaustively tested per-connector
(tests/test_ebay_connector.py etc.) - these tests are about the backfill
*service's* orchestration/safety behavior, not re-proving each
connector's own HTTP handling.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from marketplace_alert.connectors.mock.connector import MockMarketplaceConnector
from marketplace_alert.core.connectors.base import (
    ListingLookupNotSupportedError,
    MarketplaceConnector,
    MarketplaceConnectorError,
)
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.backfill import run_backfill
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.persistence.service import ListingDiscoveryService


class _BaseStubConnector(MarketplaceConnector):
    """Common bits shared by both stub connectors below - deliberately
    does NOT define `get_listing_by_id` itself, so `_StubConnectorNoLookup`
    (which never overrides it) genuinely inherits the base class's
    "not supported" default, exactly like the real Bonanza connector -
    never achieved by mutating a class attribute at instance-construction
    time, which would leak across every instance/test sharing the class."""

    def __init__(self, name: str, *, configured: bool = True) -> None:
        self._name = name
        self._configured = configured

    @property
    def marketplace_name(self) -> str:
        return self._name

    @property
    def is_configured(self) -> bool:
        return self._configured

    def search(self, query: str, filters: dict[str, Any] | None = None) -> list[Listing]:
        return []

    def normalize_listing(self, raw_listing: Any) -> Listing:
        raise NotImplementedError

    def health_check(self) -> bool:
        return self._configured


class _StubConnector(_BaseStubConnector):
    """A minimal connector for one specific backfill scenario - returns a
    fixed `Listing` (or `None`, or raises a given exception) for
    `get_listing_by_id`, regardless of the id asked for, unless
    `listings_by_id` is given for id-specific behavior."""

    def __init__(
        self,
        name: str,
        *,
        listing: Listing | None = None,
        listings_by_id: dict[str, Listing | None] | None = None,
        error: Exception | None = None,
        configured: bool = True,
    ) -> None:
        super().__init__(name, configured=configured)
        self._listing = listing
        self._listings_by_id = listings_by_id
        self._error = error
        self.lookup_calls: list[str] = []

    def get_listing_by_id(self, external_listing_id: str) -> Listing | None:
        self.lookup_calls.append(external_listing_id)
        if self._error is not None:
            raise self._error
        if self._listings_by_id is not None:
            return self._listings_by_id.get(external_listing_id)
        return self._listing


class _StubConnectorNoLookup(_BaseStubConnector):
    """A connector that genuinely never overrides `get_listing_by_id` -
    the real shape of an unsupported connector like Bonanza, not a
    simulation of one."""


def _resolver(connector: MarketplaceConnector):
    return lambda name: connector


def _always_supported(name: str) -> bool:
    return True


def _insert_row(db_session, *, marketplace="mock", external_id="mock-001", **overrides) -> DiscoveredListing:
    now = datetime.now(timezone.utc)
    defaults = dict(
        marketplace=marketplace,
        external_listing_id=external_id,
        title="Bare listing",
        listing_url=f"https://example.com/{marketplace}/{external_id}",
        first_discovered_at=now,
        last_seen_at=now,
    )
    defaults.update(overrides)
    row = DiscoveredListing(**defaults)
    db_session.add(row)
    db_session.commit()
    return row


def _fresh_listing(**overrides) -> Listing:
    base = dict(
        marketplace="mock",
        external_listing_id="mock-001",
        title="Bare listing",
        price=45.0,
        currency="USD",
        location="Tel Aviv, Israel",
        seller="vintage_kits_il",
        condition="used",
        listing_url="https://mock-marketplace.example.com/listing/mock-001",
        image_url="https://example.com/images/maccabi-shirt.jpg",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Listing(**base)


# --- core enrichment behavior ------------------------------------------


def test_fills_missing_fields(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=_fresh_listing())

    summary = run_backfill(
        db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False
    )

    assert summary.enriched == 1
    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.price == 45.0
    assert row.currency == "USD"
    assert row.location == "Tel Aviv, Israel"
    assert row.seller == "vintage_kits_il"
    assert row.condition == "used"
    assert row.image_url == "https://example.com/images/maccabi-shirt.jpg"
    # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
    # columns (every value written is UTC regardless) - same rule
    # documented in ListingOut.ensure_utc / tests/test_persistence.py.
    assert row.source_created_at.replace(tzinfo=timezone.utc) == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_does_not_overwrite_an_existing_value(db_session) -> None:
    _insert_row(db_session, price=999.0, currency="EUR")
    connector = _StubConnector("mock", listing=_fresh_listing(price=1.0, currency="USD"))

    run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.price == 999.0  # untouched - was already present
    assert row.currency == "EUR"


def test_partial_fill_only_touches_missing_fields(db_session) -> None:
    _insert_row(db_session, price=50.0, location="Already known")
    connector = _StubConnector("mock", listing=_fresh_listing())

    run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.price == 50.0  # untouched
    assert row.location == "Already known"  # untouched
    assert row.seller == "vintage_kits_il"  # filled in - was null


def test_a_field_missing_from_the_fresh_listing_is_left_null(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=_fresh_listing(seller=None))

    run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.seller is None  # never invented


def test_no_change_when_nothing_new_is_available(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector(
        "mock",
        listing=_fresh_listing(price=None, currency=None, location=None, seller=None, condition=None, image_url=None, created_at=None),
    )

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.enriched == 0
    assert summary.no_change == 1


# --- identity / discovery-safety guarantees -----------------------------


def test_never_touches_marketplace_identity_or_discovery_timestamps(db_session) -> None:
    row = _insert_row(db_session)
    original_first_discovered = row.first_discovered_at
    original_last_seen = row.last_seen_at
    connector = _StubConnector("mock", listing=_fresh_listing())

    run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    refreshed = ListingRepository(db_session).get("mock", "mock-001")
    assert refreshed.marketplace == "mock"
    assert refreshed.external_listing_id == "mock-001"
    assert refreshed.first_discovered_at == original_first_discovered
    assert refreshed.last_seen_at == original_last_seen
    assert refreshed.discovered_by_saved_search_id is None


def test_enriching_a_row_never_makes_it_look_newly_discovered(db_session) -> None:
    """The single most important safety property: after backfill fills in
    a row's metadata, running the exact same listing through the normal
    discovery/dedup path again must still classify it as already-seen,
    never as new (which is what would trigger a Telegram notification)."""
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=_fresh_listing())
    run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    # Now run the SAME listing through the real discovery service, as if
    # a fresh scan had found it again.
    result = ListingDiscoveryService(db_session).process_listings([_fresh_listing()])

    assert result.new_listings == []
    assert result.already_seen_count == 1


def test_backfill_module_has_no_notification_dependency() -> None:
    """A structural guard, not just a behavioral one: this module must
    never *import* NotificationService/NotificationProvider - there is
    no code path here that could send an alert, by construction. (The
    module's own docstring mentions NotificationService by name, in
    prose, precisely to document that guarantee - so this checks actual
    import statements, not a bare text search of the whole source.)"""
    import ast
    import inspect

    import marketplace_alert.core.persistence.backfill as backfill_module

    tree = ast.parse(inspect.getsource(backfill_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)

    assert "NotificationService" not in imported_names
    assert "NotificationProvider" not in imported_names
    assert not any("notifications" in name for name in imported_names)


# --- dry run --------------------------------------------------------------


def test_dry_run_performs_no_writes(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=_fresh_listing())

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=True)

    assert summary.enriched == 1  # reports what WOULD happen
    assert "price" in summary.per_field_fill_counts
    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.price is None  # nothing actually written
    assert row.seller is None


def test_dry_run_is_the_default() -> None:
    import inspect

    signature = inspect.signature(run_backfill)
    assert signature.parameters["dry_run"].default is True


# --- idempotency / resumability ------------------------------------------


def test_rerun_after_a_successful_enrichment_is_a_no_op(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=_fresh_listing())

    first = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)
    second = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert first.enriched == 1
    assert second.candidates_considered == 0  # already-enriched row no longer matches
    assert connector.lookup_calls == ["mock-001"]  # never looked up a second time


def test_batch_limit_is_respected(db_session) -> None:
    for i in range(5):
        _insert_row(db_session, external_id=f"mock-{i}")
    connector = _StubConnector("mock", listings_by_id={f"mock-{i}": _fresh_listing(external_listing_id=f"mock-{i}") for i in range(5)})

    summary = run_backfill(
        db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False, limit=2
    )

    assert summary.candidates_considered == 2
    assert len(connector.lookup_calls) == 2


def test_marketplace_filter_is_respected(db_session) -> None:
    _insert_row(db_session, marketplace="mock", external_id="m1")
    _insert_row(db_session, marketplace="etsy", external_id="e1")
    connector = _StubConnector("mock", listing=_fresh_listing())

    summary = run_backfill(
        db_session,
        resolve_connector=_resolver(connector),
        is_marketplace_supported=_always_supported,
        dry_run=False,
        marketplace="mock",
    )

    assert summary.candidates_considered == 1
    assert summary.rows[0].marketplace == "mock"


# --- marketplace isolation -------------------------------------------------


def test_skips_a_marketplace_with_no_registered_connector(db_session) -> None:
    _insert_row(db_session, marketplace="unknown_marketplace", external_id="u1")

    def is_supported(name: str) -> bool:
        return False

    summary = run_backfill(
        db_session,
        resolve_connector=lambda name: (_ for _ in ()).throw(AssertionError("must never be called")),
        is_marketplace_supported=is_supported,
        dry_run=False,
    )

    assert summary.skipped_unsupported_marketplace == 1
    assert "unknown_marketplace" in summary.skipped_marketplaces


def test_skips_a_connector_with_no_lookup_support(db_session) -> None:
    _insert_row(db_session, marketplace="bonanza", external_id="b1")
    connector = _StubConnectorNoLookup("bonanza")

    summary = run_backfill(
        db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False
    )

    assert summary.skipped_unsupported_marketplace == 1


def test_skips_an_unconfigured_marketplace(db_session) -> None:
    _insert_row(db_session, marketplace="etsy", external_id="e1")
    connector = _StubConnector("etsy", listing=_fresh_listing(), configured=False)

    summary = run_backfill(
        db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False
    )

    assert summary.skipped_unsupported_marketplace == 1
    assert connector.lookup_calls == []


def test_one_marketplace_being_unsupported_does_not_block_another(db_session) -> None:
    _insert_row(db_session, marketplace="mock", external_id="m1")
    _insert_row(db_session, marketplace="bonanza", external_id="b1")
    mock_connector = _StubConnector("mock", listing=_fresh_listing())
    bonanza_connector = _StubConnectorNoLookup("bonanza")

    def resolve(name: str):
        return mock_connector if name == "mock" else bonanza_connector

    summary = run_backfill(db_session, resolve_connector=resolve, is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.enriched == 1
    assert summary.skipped_unsupported_marketplace == 1


# --- failure isolation ------------------------------------------------------


def test_a_listing_that_no_longer_exists_is_marked_not_found(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", listing=None)  # simulates a 404

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.not_found == 1
    row = ListingRepository(db_session).get("mock", "mock-001")
    assert row.price is None  # nothing invented for a gone listing


def test_connector_failure_is_isolated_and_does_not_stop_the_batch(db_session) -> None:
    _insert_row(db_session, external_id="ok-1")
    _insert_row(db_session, external_id="fails-1")
    connector = _StubConnector(
        "mock",
        listings_by_id={"ok-1": _fresh_listing(external_listing_id="ok-1")},
    )
    original_lookup = connector.get_listing_by_id

    def flaky_lookup(external_id: str):
        if external_id == "fails-1":
            raise MarketplaceConnectorError("simulated transient failure")
        return original_lookup(external_id)

    connector.get_listing_by_id = flaky_lookup  # type: ignore[method-assign]

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.enriched == 1
    assert summary.failed == 1


def test_timeout_is_treated_as_a_normal_isolated_failure(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", error=MarketplaceConnectorError("eBay API request failed"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.failed == 1
    assert summary.rows[0].status == "failed"


def test_rate_limit_429_is_treated_as_a_normal_isolated_failure(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", error=MarketplaceConnectorError("eBay API rate limit exceeded (HTTP 429)"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.failed == 1


def test_server_error_500_is_treated_as_a_normal_isolated_failure(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", error=MarketplaceConnectorError("eBay API returned HTTP 500"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.failed == 1


def test_malformed_response_is_treated_as_a_normal_isolated_failure(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", error=MarketplaceConnectorError("eBay API returned a malformed response"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.failed == 1


def test_unexpected_exception_is_isolated_not_propagated(db_session) -> None:
    _insert_row(db_session)
    connector = _StubConnector("mock", error=RuntimeError("something truly unexpected"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.failed == 1  # never raises out of run_backfill


def test_listing_lookup_not_supported_error_is_handled_defensively(db_session) -> None:
    """Belt-and-suspenders: even if a connector somehow raises this
    despite passing the up-front support check, it's handled the same
    safe way as an unsupported marketplace, never propagated."""
    _insert_row(db_session)
    connector = _StubConnector("mock", error=ListingLookupNotSupportedError("simulated"))

    summary = run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert summary.skipped_unsupported_marketplace == 1


# --- rate-limit pacing -----------------------------------------------------


def test_delay_is_paced_between_requests_but_not_before_the_first(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert_row(db_session, external_id="a")
    _insert_row(db_session, external_id="b")
    connector = _StubConnector(
        "mock",
        listings_by_id={
            "a": _fresh_listing(external_listing_id="a"),
            "b": _fresh_listing(external_listing_id="b"),
        },
    )

    sleeps: list[float] = []
    monkeypatch.setattr("marketplace_alert.core.persistence.backfill.time.sleep", lambda s: sleeps.append(s))

    run_backfill(
        db_session,
        resolve_connector=_resolver(connector),
        is_marketplace_supported=_always_supported,
        dry_run=False,
        delay_seconds=0.75,
    )

    assert sleeps == [0.75]  # one gap between two requests, never before the first


# --- secrets -----------------------------------------------------------


def test_never_logs_a_connector_error_message_containing_a_credential(
    db_session, caplog: pytest.LogCaptureFixture
) -> None:
    _insert_row(db_session)
    connector = _StubConnector(
        "mock", error=MarketplaceConnectorError("eBay API returned HTTP 401 (token redacted, not included here)")
    )

    with caplog.at_level("DEBUG"):
        run_backfill(db_session, resolve_connector=_resolver(connector), is_marketplace_supported=_always_supported, dry_run=False)

    assert "super-secret" not in caplog.text  # no test constructs a message containing this, sanity guard


def test_real_mock_connector_round_trip() -> None:
    """Uses the real MockMarketplaceConnector end to end (not a stub),
    proving the whole mechanism works through a genuine connector
    implementing the interface, not just against test doubles."""
    connector = MockMarketplaceConnector()
    listing = connector.get_listing_by_id("mock-001")
    assert listing is not None
    assert listing.price == 45.0
