"""MA-SCAN-01: saved-search scheduling against a cron that fires every 5 minutes.

The bug: `last_scanned_at` used to be stamped with the scan's *completion*
time while "due" is `last_scanned_at + interval <= now`. A cron pass starts
exactly one cron period after the previous pass *started*, i.e. a few seconds
before a completion-anchored 5-minute search became due - so it was skipped
and ran only on the pass after (~10 minutes). Every preset above 5 minutes was
late by one cron period the same way.

The fix: stamp the *pass start* time (one value for every search in that pass,
so a slow earlier search cannot delay a later one) and let the one-shot cron
entrypoint accept a small grace window for scheduler jitter. In-process
scanning keeps grace 0, so its behaviour is unchanged.

Time is fully simulated - no sleeps, no real clock, no network.
"""

from datetime import datetime, timedelta, timezone

import pytest

from marketplace_alert.config import settings
from marketplace_alert.core.persistence.models import PendingNotification
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard
from marketplace_alert.core.scheduler.scanner import BackgroundScanner

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
CRON = 300


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        self.current = value


class SlowConnector:
    """Returns nothing, but "takes" `duration` simulated seconds per search and records calls."""

    def __init__(self, clock: FakeClock, duration: float = 0.0, fail_with: Exception | None = None) -> None:
        self._clock = clock
        self._duration = duration
        self._fail_with = fail_with
        self.calls: list[tuple[str, datetime]] = []

    def search(self, query: str, filters: dict | None = None) -> list:
        self.calls.append((query, self._clock.now()))
        self._clock.advance(self._duration)
        if self._fail_with is not None:
            raise self._fail_with
        return []


def _create(session_factory, query: str, interval: int) -> int:
    session = session_factory()
    saved = SavedSearchRepository(session).create(
        query=query, marketplaces=["m"], scan_interval_seconds=interval, is_active=True
    )
    session.commit()
    identifier = saved.id
    session.close()
    return identifier


def _scanner(session_factory, connector, clock: FakeClock, *, grace: float = 30.0, guard=None) -> BackgroundScanner:
    runner = SavedSearchRunner(resolve_connector=lambda name: connector)
    return BackgroundScanner(
        session_factory=session_factory,
        runner=runner,
        run_guard=guard or SavedSearchRunGuard(),
        due_grace_seconds=grace,
        clock=clock.now,
    )


def _last_scanned(session_factory, saved_search_id: int) -> datetime | None:
    session = session_factory()
    try:
        value = SavedSearchRepository(session).get(saved_search_id).last_scanned_at
    finally:
        session.close()
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _run_cron(scanner: BackgroundScanner, clock: FakeClock, passes: int, jitter: list[float] | None = None) -> None:
    """`passes` cron executions, each starting CRON seconds after the previous *start* (plus jitter)."""
    for index in range(passes):
        offset = jitter[index] if jitter else 0.0
        clock.set(T0 + timedelta(seconds=CRON * index + offset))
        scanner.run_due_searches()


# --- 1. reproducing the defect at the repository level ------------------------


def test_defect_a_completion_anchored_stamp_misses_the_next_cron_tick(db_session) -> None:
    """The old behaviour, expressed as data: scan started at T0, finished 20 s later (stamp = T0+20 s).
    At the next tick (T0+300 s) a 300 s search is 20 s short of due, so it is skipped."""
    repository = SavedSearchRepository(db_session)
    saved = repository.create(query="x", marketplaces=["m"], scan_interval_seconds=300, is_active=True)
    saved.last_scanned_at = T0 + timedelta(seconds=20)
    db_session.commit()

    next_tick = T0 + timedelta(seconds=CRON)
    assert repository.list_due_for_scan(now=next_tick) == []  # strict behaviour: the skipped tick
    assert [s.id for s in repository.list_due_for_scan(now=next_tick, grace_seconds=30)] == [saved.id]


# --- 2. consecutive cron executions ------------------------------------------


def test_a_five_minute_search_runs_on_every_consecutive_five_minute_cron_pass(session_factory) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=20)  # a realistic 20 s scan
    search_id = _create(session_factory, "five", 300)
    scanner = _scanner(session_factory, connector, clock)

    _run_cron(scanner, clock, passes=6)

    starts = [started for _query, started in connector.calls]
    assert starts == [T0 + timedelta(seconds=CRON * i) for i in range(6)]
    assert _last_scanned(session_factory, search_id) == T0 + timedelta(seconds=CRON * 5)  # pass start, not completion


def test_consecutive_passes_tolerate_realistic_scheduler_jitter(session_factory) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=25)
    _create(session_factory, "five", 300)
    scanner = _scanner(session_factory, connector, clock)

    # cron start times drift by a few seconds either way (all within the grace window)
    _run_cron(scanner, clock, passes=6, jitter=[0, -8, 12, -5, 9, -11])

    assert len(connector.calls) == 6


def test_without_grace_scheduler_jitter_alone_still_skips_ticks(session_factory) -> None:
    """Start anchoring is not enough on its own: a pass that begins a few seconds before the previous
    pass's anchor + 300 s finds the search not yet due. That is why the cron entrypoint needs the grace window."""
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=20)
    _create(session_factory, "five", 300)
    scanner = _scanner(session_factory, connector, clock, grace=0)

    _run_cron(scanner, clock, passes=6, jitter=[0, -8, 12, -5, 9, -11])

    assert 0 < len(connector.calls) < 6


@pytest.mark.parametrize(("interval", "expected_pass_indexes"), [(900, [0, 3, 6, 9]), (1800, [0, 6]), (3600, [0])])
def test_longer_intervals_run_at_their_true_period_not_one_cron_period_late(
    session_factory, interval, expected_pass_indexes
) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=15)
    _create(session_factory, "long", interval)
    scanner = _scanner(session_factory, connector, clock)

    _run_cron(scanner, clock, passes=10, jitter=[0, 4, -6, 7, -3, 9, -9, 2, 5, -4])

    ran = [i for i in range(10) if any(abs((started - (T0 + timedelta(seconds=CRON * i))).total_seconds()) < 20 for _q, started in connector.calls)]
    assert ran == expected_pass_indexes


def test_a_one_minute_search_cannot_run_more_than_once_per_cron_pass(session_factory) -> None:
    """The scheduler never promises an interval shorter than the cron period."""
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=5)
    _create(session_factory, "fast", 60)
    scanner = _scanner(session_factory, connector, clock)

    _run_cron(scanner, clock, passes=4)

    assert len(connector.calls) == 4  # one per pass, never two


# --- 3. slow and failed scans -------------------------------------------------


def test_a_slow_earlier_search_does_not_delay_a_later_one(session_factory) -> None:
    clock = FakeClock(T0)
    slow = SlowConnector(clock, duration=200)  # the first search eats 200 s of the pass
    first = _create(session_factory, "first", 300)
    second = _create(session_factory, "second", 300)
    scanner = _scanner(session_factory, slow, clock)

    scanner.run_due_searches()
    # both stamped with the PASS start, regardless of when each actually ran
    assert _last_scanned(session_factory, first) == T0
    assert _last_scanned(session_factory, second) == T0

    clock.set(T0 + timedelta(seconds=CRON))
    scanner.run_due_searches()
    assert len(slow.calls) == 4  # both searches ran again on the next pass


def test_a_crash_inside_the_runner_keeps_the_search_due_for_the_next_pass(session_factory) -> None:
    clock = FakeClock(T0)
    boom = SlowConnector(clock, duration=3)
    ok_id = _create(session_factory, "ok", 300)
    runner = SavedSearchRunner(resolve_connector=lambda name: boom)
    scanner = BackgroundScanner(
        session_factory=session_factory, runner=runner, run_guard=SavedSearchRunGuard(), due_grace_seconds=30, clock=clock.now
    )
    scanner.run_due_searches()
    assert _last_scanned(session_factory, ok_id) == T0

    # a crash outside the per-marketplace handling: the runner itself blows up
    class ExplodingRunner:
        def run_by_id(self, session, saved_search_id, scanned_at=None):
            raise RuntimeError("runner crashed")

    crashing = BackgroundScanner(
        session_factory=session_factory, runner=ExplodingRunner(), run_guard=SavedSearchRunGuard(), due_grace_seconds=30, clock=clock.now
    )
    clock.set(T0 + timedelta(seconds=CRON))
    crashing.run_due_searches()  # must not raise
    assert _last_scanned(session_factory, ok_id) == T0  # unchanged: still due, will be retried
    clock.set(T0 + timedelta(seconds=2 * CRON))
    scanner.run_due_searches()
    assert _last_scanned(session_factory, ok_id) == T0 + timedelta(seconds=2 * CRON)


def test_a_marketplace_error_still_advances_the_schedule_as_before(session_factory) -> None:
    """Preserved behaviour: a per-marketplace failure is recorded in the run result and the search
    is not hammered every pass."""
    from marketplace_alert.core.connectors.base import MarketplaceConnectorError

    clock = FakeClock(T0)
    failing = SlowConnector(clock, duration=1, fail_with=MarketplaceConnectorError("down"))
    search_id = _create(session_factory, "down", 300)
    scanner = _scanner(session_factory, failing, clock)

    scanner.run_due_searches()

    assert _last_scanned(session_factory, search_id) == T0


# --- 4. overlap ---------------------------------------------------------------


def test_a_search_already_running_is_skipped_and_stays_due(session_factory) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock)
    search_id = _create(session_factory, "busy", 300)
    guard = SavedSearchRunGuard()
    assert guard.try_acquire(search_id)  # another path (e.g. "Run now") is mid-scan
    scanner = _scanner(session_factory, connector, clock, guard=guard)

    scanner.run_due_searches()

    assert connector.calls == []
    assert _last_scanned(session_factory, search_id) is None  # untouched, so the next pass retries it
    guard.release(search_id)
    clock.set(T0 + timedelta(seconds=CRON))
    scanner.run_due_searches()
    assert len(connector.calls) == 1


# --- 5. compatibility and boundaries -----------------------------------------


def test_never_scanned_and_legacy_completion_stamped_rows_are_handled(session_factory, db_session) -> None:
    repository = SavedSearchRepository(db_session)
    never = repository.create(query="never", marketplaces=["m"], scan_interval_seconds=300, is_active=True)
    legacy = repository.create(query="legacy", marketplaces=["m"], scan_interval_seconds=300, is_active=True)
    legacy.last_scanned_at = (T0 - timedelta(seconds=290)).replace(tzinfo=None)  # naive, as SQLite returns it
    recent = repository.create(query="recent", marketplaces=["m"], scan_interval_seconds=300, is_active=True)
    recent.last_scanned_at = T0 - timedelta(seconds=100)
    db_session.commit()

    due = {s.query for s in repository.list_due_for_scan(now=T0, grace_seconds=30)}

    assert due == {"never", "legacy"}  # 290 s + 30 s grace >= 300 s; 100 s is not due
    assert never.id != legacy.id


def test_grace_is_capped_at_half_the_interval_so_it_cannot_double_scan(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    saved = repository.create(query="short", marketplaces=["m"], scan_interval_seconds=60, is_active=True)
    saved.last_scanned_at = T0
    db_session.commit()

    assert repository.list_due_for_scan(now=T0 + timedelta(seconds=29), grace_seconds=50) == []
    assert len(repository.list_due_for_scan(now=T0 + timedelta(seconds=30), grace_seconds=50)) == 1


def test_default_grace_is_zero_so_in_process_scanning_is_unchanged(db_session) -> None:
    repository = SavedSearchRepository(db_session)
    saved = repository.create(query="x", marketplaces=["m"], scan_interval_seconds=300, is_active=True)
    saved.last_scanned_at = T0
    db_session.commit()

    assert repository.list_due_for_scan(now=T0 + timedelta(seconds=299)) == []
    assert len(repository.list_due_for_scan(now=T0 + timedelta(seconds=300))) == 1


def test_inprocess_scanner_default_grace_is_zero(session_factory) -> None:
    clock = FakeClock(T0)
    scanner = BackgroundScanner(
        session_factory=session_factory,
        runner=SavedSearchRunner(resolve_connector=lambda n: SlowConnector(clock)),
        run_guard=SavedSearchRunGuard(),
        clock=clock.now,
    )
    search_id = _create(session_factory, "x", 300)
    scanner.run_due_searches()
    clock.set(T0 + timedelta(seconds=299))
    scanner.run_due_searches()
    assert _last_scanned(session_factory, search_id) == T0  # not re-scanned at 299 s


def test_manual_run_stamps_the_run_start_not_its_completion(session_factory) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock, duration=40)
    search_id = _create(session_factory, "manual", 300)
    runner = SavedSearchRunner(resolve_connector=lambda name: connector)

    session = session_factory()
    runner.run_by_id(session, search_id, scanned_at=T0)
    session.commit()
    session.close()

    assert _last_scanned(session_factory, search_id) == T0


def test_run_by_id_without_scanned_at_stamps_a_time_not_after_completion(session_factory) -> None:
    clock = FakeClock(T0)
    connector = SlowConnector(clock)
    search_id = _create(session_factory, "manual2", 300)
    runner = SavedSearchRunner(resolve_connector=lambda name: connector)

    before = datetime.now(timezone.utc)
    session = session_factory()
    runner.run_by_id(session, search_id)
    session.commit()
    session.close()
    after = datetime.now(timezone.utc)

    stamped = _last_scanned(session_factory, search_id)
    assert before <= stamped <= after


# --- 6. the cron entrypoint and configuration ---------------------------------


def test_cron_grace_setting_defaults_to_thirty_seconds() -> None:
    assert settings.scan_due_grace_seconds == 30.0


def test_run_due_scans_script_passes_the_grace_to_the_scanner(monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "run_due_scans_under_test", Path(__file__).resolve().parent.parent / "scripts" / "run_due_scans.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: dict = {}

    class FakeScanner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def run_due_searches(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(module, "BackgroundScanner", FakeScanner)
    monkeypatch.setattr(module, "configure_logging", lambda level: None)
    monkeypatch.setattr(settings, "scan_due_grace_seconds", 17.0)

    assert module.main() == 0
    assert captured["due_grace_seconds"] == 17.0 and captured["ran"] is True


# --- 7. notifications and relevance are untouched ------------------------------


def test_scheduling_change_does_not_change_notifications(session_factory) -> None:
    from marketplace_alert.core.models.listing import Listing

    clock = FakeClock(T0)

    class OneListing(SlowConnector):
        def search(self, query, filters=None):
            super().search(query, filters)
            return [
                Listing(
                    marketplace="m",
                    external_listing_id="item-1",
                    title="Bosch Cordless Drill",
                    listing_url="https://example.com/item-1",
                )
            ]

    _create(session_factory, "Bosch drill", 300)
    scanner = _scanner(session_factory, OneListing(clock), clock)

    _run_cron(scanner, clock, passes=3)  # the same listing seen on three consecutive passes

    verify = session_factory()
    assert verify.query(PendingNotification).count() == 1  # one alert, never repeated by rescheduling
    verify.close()
