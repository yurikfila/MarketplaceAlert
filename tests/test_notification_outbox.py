"""Tests for the notification outbox: enqueueing
(`core/persistence/notification_outbox.py`) and the claim -> resolve
destination -> deliver -> complete drain orchestration
(`core/notifications/outbox.py`).

See `core/notifications/outbox.py`'s module docstring for the full design
this file verifies: no database lock held during delivery, crash recovery
via a claim lease, at-least-once (not exactly-once) delivery semantics, a
bounded retry count, and - the per-user notification routing phase - that
a notification's destination is always resolved through the real
ownership chain (`PendingNotification -> DiscoveredListing ->
discovered_by_saved_search_id -> SavedSearch -> user_id ->
NotificationPreference -> telegram_chat_id`), never a global default.

Also covers the "no destination" small adjustment: Case A (owner
resolved, no Telegram destination configured yet - retried indefinitely,
throttled, never counts against `notification_max_attempts`) vs Case B
(ownership itself unresolvable - keeps the original bounded-retry-then-
`failed` behavior).

**A note on what this SQLite-backed suite can and cannot prove about
concurrency**: production runs on PostgreSQL, where `claim_batch`'s
`SELECT ... FOR UPDATE SKIP LOCKED` gives a genuine cross-process
guarantee that two concurrent drain runs can never claim the same row.
SQLite has no row-level locking at all (see that method's docstring), so
a real two-connections-racing-simultaneously test isn't meaningful here -
what the tests below prove instead is the query *contract* every claimer
actually relies on (a `processing` row within its lease is invisible to
another claim call) - the same contract PostgreSQL's row lock enforces
atomically, just exercised here sequentially rather than concurrently.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.auth.models import User
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.outbox import (
    DrainResult,
    claim_due_notifications,
    drain_pending_notifications,
    resolve_destination,
)
from marketplace_alert.core.notifications.preferences_repository import NotificationPreferenceRepository
from marketplace_alert.core.persistence.models import (
    NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG,
    NOTIFICATION_ERROR_OWNER_UNRESOLVED,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_SENT,
    DiscoveredListing,
    PendingNotification,
)
from marketplace_alert.core.persistence.notification_outbox import NotificationOutboxRepository
from marketplace_alert.core.persistence.repository import ListingRepository
from marketplace_alert.core.saved_searches.repository import SavedSearchRepository

# A large, effectively-"never" throttle - passed to `claim_due_notifications`/
# `drain_pending_notifications` by every test that isn't specifically
# exercising the Case-A throttle itself, so a freshly-completed Case-A row
# is never accidentally reclaimed within the same test by coincidence.
_NO_THROTTLE_TESTING = 10_000.0


def _listing(external_id: str) -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id=external_id,
        title=f"Listing {external_id}",
        listing_url=f"https://example.com/{external_id}",
    )


def _persist_and_enqueue(session_factory, external_id: str, *, saved_search_id: int | None = None) -> int:
    """Persists a `DiscoveredListing` and its outbox row in one committed
    transaction (matching how `ListingDiscoveryService.process_listings()`
    and `SavedSearchRunner` do it together in real use), and returns the
    notification id. `saved_search_id` defaults to `None` - a listing
    with no discovering search, exactly like a legacy `/scan` row -
    deliberately left unroutable unless a caller supplies one, so tests
    are explicit about which listings should resolve a destination."""
    session = session_factory()
    try:
        row = ListingRepository(session).save_new(_listing(external_id), saved_search_id=saved_search_id)
        notification = NotificationOutboxRepository(session).enqueue(row.id)
        session.commit()
        return notification.id
    finally:
        session.close()


def _create_user(session_factory, email: str) -> int:
    session = session_factory()
    try:
        user = User(email=email, password_hash="irrelevant-hash")
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def _create_owned_saved_search(session_factory, user_id: int, query: str = "Owned search") -> int:
    session = session_factory()
    try:
        saved_search = SavedSearchRepository(session).create(
            query=query, marketplaces=["mock"], scan_interval_seconds=300, is_active=True, user_id=user_id
        )
        session.commit()
        return saved_search.id
    finally:
        session.close()


def _set_telegram_preference(session_factory, user_id: int, telegram_chat_id: str | None) -> None:
    session = session_factory()
    try:
        NotificationPreferenceRepository(session).upsert_telegram_chat_id(user_id, telegram_chat_id)
        session.commit()
    finally:
        session.close()


def _routed_notification(session_factory, external_id: str, *, telegram_chat_id: str = "999") -> int:
    """Convenience for tests that just need "a notification that WILL
    resolve to some real destination", without caring about the specific
    user/search - builds the full ownership chain (a fresh user, one
    saved search they own, a preference with `telegram_chat_id` set) and
    one enqueued notification attributed to it."""
    user_id = _create_user(session_factory, f"user-{external_id}@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    _set_telegram_preference(session_factory, user_id, telegram_chat_id)
    return _persist_and_enqueue(session_factory, external_id, saved_search_id=saved_search_id)


def _drain(session_factory, provider, *, max_attempts: int = 5, no_destination_retry_seconds: float = _NO_THROTTLE_TESTING):
    return drain_pending_notifications(
        session_factory,
        provider,
        batch_size=10,
        lease_seconds=120,
        max_attempts=max_attempts,
        no_destination_retry_seconds=no_destination_retry_seconds,
    )


class RecordingProvider(NotificationProvider):
    """Always succeeds; records every (listing, destination) pair it was asked to send, in order."""

    def __init__(self) -> None:
        self.sent: list[Listing] = []
        self.destinations: list[str] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing, destination: str) -> None:
        self.sent.append(listing)
        self.destinations.append(destination)


class AlwaysFailingProvider(NotificationProvider):
    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing, destination: str) -> None:
        raise NotificationError("simulated permanent failure")


class NeverCalledProvider(NotificationProvider):
    """For Case A/B tests: fails the test outright if the provider is
    ever invoked - "no destination" must mean no Telegram call, not just
    "no successful Telegram call"."""

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing, destination: str) -> None:
        raise AssertionError("must never be called when there is no resolvable destination")


# =====================================================================
# Enqueue / dedup
# =====================================================================


def test_enqueue_is_not_committed_by_the_repository_itself(session_factory) -> None:
    session = session_factory()
    row = ListingRepository(session).save_new(_listing("flush-only-1"))
    NotificationOutboxRepository(session).enqueue(row.id)
    session.rollback()

    verify = session_factory()
    assert verify.query(PendingNotification).count() == 0
    verify.close()


def test_enqueue_twice_for_the_same_listing_violates_the_unique_constraint(session_factory) -> None:
    """The dedup guarantee - "never queue the same listing's notification
    twice" - is enforced by the database (`PendingNotification`'s
    `UNIQUE` constraint on `discovered_listing_id`), not just by callers
    happening to only enqueue once."""
    session = session_factory()
    row = ListingRepository(session).save_new(_listing("dedup-1"))
    session.commit()

    NotificationOutboxRepository(session).enqueue(row.id)
    session.commit()

    # enqueue() flushes (see its docstring), so the UNIQUE violation
    # surfaces immediately here, not at a later commit().
    with pytest.raises(IntegrityError):
        NotificationOutboxRepository(session).enqueue(row.id)
    session.rollback()
    session.close()


# =====================================================================
# Phase 2A: pending_notifications.user_id - schema-only groundwork,
# zero runtime behavior change (see PendingNotification's own docstring).
# =====================================================================


def test_enqueue_still_produces_a_row_with_user_id_null_by_default(session_factory) -> None:
    """Zero behavior change: `enqueue()`'s signature and behavior are
    completely untouched by Phase 2A - a freshly enqueued row still has
    `user_id` unset (`NULL`), since nothing writes it yet."""
    session = session_factory()
    row = ListingRepository(session).save_new(_listing("phase-2a-enqueue-1"))
    notification = NotificationOutboxRepository(session).enqueue(row.id)
    session.commit()

    assert notification.user_id is None
    session.close()


def test_pending_notification_user_id_can_be_set_directly(session_factory) -> None:
    """The column itself is usable and nullable - proven at the model
    level, independent of the fact that no current code path writes it
    yet (that's a later phase)."""
    user_id = _create_user(session_factory, "phase-2a-model@example.com")
    session = session_factory()
    row = ListingRepository(session).save_new(_listing("phase-2a-model-1"))
    notification = NotificationOutboxRepository(session).enqueue(row.id)
    notification.user_id = user_id
    session.commit()

    verify = session_factory()
    persisted = verify.get(PendingNotification, notification.id)
    assert persisted.user_id == user_id
    verify.close()


def test_deleting_the_user_cascades_to_a_pending_notification_that_references_them(session_factory) -> None:
    """SQLite ignores `ON DELETE CASCADE` (and every other FK constraint)
    unless `PRAGMA foreign_keys=ON` is issued per-connection - production
    runs Postgres, which always enforces it, so this pragma is only here
    to make SQLite behave like production for this one assertion (same
    approach already used for `NotificationPreference`'s and `Listing
    Attribution`'s equivalent cascade tests). Exercises the FK's declared
    `ondelete` behavior even though nothing writes `user_id` yet - the
    constraint itself must already behave correctly, ahead of any code
    relying on it."""
    session = session_factory()
    session.execute(text("PRAGMA foreign_keys=ON"))

    user = User(email="phase-2a-cascade@example.com", password_hash="irrelevant-hash")
    session.add(user)
    session.commit()

    row = ListingRepository(session).save_new(_listing("phase-2a-cascade-1"))
    notification = NotificationOutboxRepository(session).enqueue(row.id)
    notification.user_id = user.id
    session.commit()
    notification_id = notification.id

    session.delete(user)
    session.commit()

    assert session.query(PendingNotification).filter_by(id=notification_id).count() == 0
    # The canonical listing itself must survive - only the notification row is gone.
    assert session.query(DiscoveredListing).filter_by(id=row.id).count() == 1
    session.close()


# =====================================================================
# Claim phase: batching, ordering, and no double-claim of a live lease
# =====================================================================


def test_claim_batch_respects_limit_and_fifo_order(session_factory) -> None:
    ids_in_enqueue_order = [_persist_and_enqueue(session_factory, f"fifo-{i}") for i in range(3)]

    claimed = claim_due_notifications(
        session_factory, limit=2, lease_seconds=120, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )

    assert [c.notification_id for c in claimed] == ids_in_enqueue_order[:2]


def test_claim_batch_carries_the_discovering_saved_search_id(session_factory) -> None:
    """`ClaimedNotification.discovered_by_saved_search_id` is what
    `resolve_destination` uses to find the owning user - proven captured
    correctly straight from the claim query, no second lookup."""
    user_id = _create_user(session_factory, "carrier@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "carrier-1", saved_search_id=saved_search_id)

    claimed = claim_due_notifications(
        session_factory, limit=10, lease_seconds=120, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )

    assert len(claimed) == 1
    assert claimed[0].notification_id == notification_id
    assert claimed[0].discovered_by_saved_search_id == saved_search_id


def test_recently_claimed_row_is_not_claimed_again_within_its_lease(session_factory) -> None:
    notification_id = _persist_and_enqueue(session_factory, "concurrent-1")

    first_claim = claim_due_notifications(
        session_factory, limit=10, lease_seconds=120, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )
    assert [c.notification_id for c in first_claim] == [notification_id]

    second_claim = claim_due_notifications(
        session_factory, limit=10, lease_seconds=120, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )
    assert second_claim == []


# =====================================================================
# No database lock held during delivery
# =====================================================================


def test_no_database_lock_is_held_during_delivery(session_factory) -> None:
    """Phase 1 (claim) must commit and release its lock before phase 2
    (deliver) ever runs. Proven concretely: `send_listing_alert` itself
    opens a *second*, independent session and writes to the very row that
    was just claimed. If claim's transaction/lock were somehow still
    open, this second write would be the thing that could never happen
    without a lock-held bug being caught by SQLite's own single-writer
    behavior."""
    notification_id = _routed_notification(session_factory, "no-lock-1")

    probe_result = {"succeeded": False}

    class ProbingProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing, destination: str) -> None:
            probe_session = session_factory()
            try:
                row = probe_session.get(PendingNotification, notification_id)
                row.last_error = "probe-write"
                probe_session.commit()
                probe_result["succeeded"] = True
            finally:
                probe_session.close()

    result = _drain(session_factory, ProbingProvider())

    assert result.sent_count == 1
    assert probe_result["succeeded"] is True


# =====================================================================
# Crash recovery via lease expiry
# =====================================================================


def test_stale_processing_lease_is_reclaimed(session_factory) -> None:
    """An abandoned claim (the process that claimed it crashed before
    completing) becomes eligible again once its lease has expired -
    simulated here by directly backdating `claimed_at`, as if a previous
    drain run claimed this row and then died."""
    notification_id = _persist_and_enqueue(session_factory, "stale-1")

    session = session_factory()
    row = session.get(PendingNotification, notification_id)
    row.status = NOTIFICATION_STATUS_PROCESSING
    row.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=300)
    row.attempt_count = 1
    session.commit()
    session.close()

    claimed = claim_due_notifications(
        session_factory, limit=10, lease_seconds=120, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )

    assert [c.notification_id for c in claimed] == [notification_id]
    verify = session_factory()
    reclaimed = verify.get(PendingNotification, notification_id)
    assert reclaimed.status == NOTIFICATION_STATUS_PROCESSING
    assert reclaimed.attempt_count == 2
    verify.close()


def test_crash_after_claim_before_delivery_is_recovered_by_a_later_drain(session_factory) -> None:
    """Simulates the process dying immediately after claim_due_notifications'
    own commit - the exact same state that function leaves behind either
    way, since there's nothing else phase 1 could observe about what
    happens next. A later drain, once the (very short, for this test)
    lease has expired, reclaims and successfully delivers it."""
    notification_id = _routed_notification(session_factory, "crash-claim-1")

    claimed = claim_due_notifications(
        session_factory, limit=10, lease_seconds=0.01, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )
    assert len(claimed) == 1
    time.sleep(0.05)

    provider = RecordingProvider()
    result = drain_pending_notifications(
        session_factory,
        provider,
        batch_size=10,
        lease_seconds=0.01,
        max_attempts=5,
        no_destination_retry_seconds=_NO_THROTTLE_TESTING,
    )

    assert result.sent_count == 1
    assert [listing.external_listing_id for listing in provider.sent] == ["crash-claim-1"]

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_SENT
    assert row.attempt_count == 2
    verify.close()


def test_crash_after_successful_send_before_completion_commit_may_redeliver(session_factory) -> None:
    """The documented, deliberately-not-eliminated at-least-once gap (see
    `core/notifications/outbox.py`'s module docstring): a crash between a
    provider accepting a message and `complete_notification`'s commit
    leaves the row still `processing` and still eligible for reclaim -
    so the next drain redelivers it. This test proves that gap is real,
    not just documented, and that it takes exactly this narrow a window
    to trigger - normal completion (every other test in this file) never
    leaves a sent row reclaimable."""
    notification_id = _routed_notification(session_factory, "crash-send-1")
    provider = RecordingProvider()

    claimed = claim_due_notifications(
        session_factory, limit=10, lease_seconds=0.01, no_destination_retry_seconds=_NO_THROTTLE_TESTING
    )
    assert len(claimed) == 1
    resolved = resolve_destination(
        session_factory, discovered_by_saved_search_id=claimed[0].discovered_by_saved_search_id
    )
    provider.send_listing_alert(claimed[0].listing, resolved.destination)  # "successfully" sent...
    # ...and then the process dies right here - complete_notification (phase 3) never runs.
    time.sleep(0.05)

    result = drain_pending_notifications(
        session_factory,
        provider,
        batch_size=10,
        lease_seconds=0.01,
        max_attempts=5,
        no_destination_retry_seconds=_NO_THROTTLE_TESTING,
    )

    assert result.sent_count == 1
    assert len(provider.sent) == 2
    assert [listing.external_listing_id for listing in provider.sent] == ["crash-send-1", "crash-send-1"]

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_SENT
    verify.close()


# =====================================================================
# Bounded retries - genuine provider failure (unchanged by this phase)
# =====================================================================


def test_row_becomes_failed_after_max_attempts(session_factory) -> None:
    notification_id = _routed_notification(session_factory, "always-fails-1")
    provider = AlwaysFailingProvider()

    for _ in range(3):
        result = _drain(session_factory, provider, max_attempts=3)
        assert result.claimed_count == 1
        assert result.failed_count == 1
        assert result.awaiting_destination_config_count == 0
        assert result.unresolved_owner_count == 0

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_FAILED
    assert row.attempt_count == 3
    verify.close()

    # Terminal - a later drain must never pick a `failed` row up again.
    result_after = _drain(session_factory, provider, max_attempts=3)
    assert result_after.claimed_count == 0


# =====================================================================
# One bad row can't block the rest of a batch or crash the drain
# =====================================================================


def test_one_failing_notification_does_not_block_others_in_the_same_batch(session_factory) -> None:
    _routed_notification(session_factory, "batch-good-1")
    _routed_notification(session_factory, "batch-bad-1")

    class SelectivelyFailingProvider(NotificationProvider):
        def __init__(self) -> None:
            self.sent: list[Listing] = []

        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing, destination: str) -> None:
            if listing.external_listing_id == "batch-bad-1":
                raise NotificationError("simulated failure")
            self.sent.append(listing)

    provider = SelectivelyFailingProvider()
    result = _drain(session_factory, provider)

    assert result.claimed_count == 2
    assert result.sent_count == 1
    assert result.failed_count == 1
    assert [listing.external_listing_id for listing in provider.sent] == ["batch-good-1"]


def test_an_unexpected_exception_during_delivery_does_not_crash_the_drain(session_factory) -> None:
    notification_id = _routed_notification(session_factory, "unexpected-1")

    class CrashingProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing, destination: str) -> None:
            raise ValueError("not a NotificationError at all")

    result = _drain(session_factory, CrashingProvider())

    assert result.claimed_count == 1
    assert result.failed_count == 1

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    assert row.last_error is not None and "not a NotificationError" in row.last_error
    verify.close()


# =====================================================================
# Disabled provider
# =====================================================================


def test_disabled_provider_claims_nothing(session_factory) -> None:
    """A disabled provider (no bot token configured) is a configuration
    state, not a delivery failure - rows are left untouched, ready to be
    delivered whenever a real provider is configured. See
    `drain_pending_notifications`'s docstring."""
    notification_id = _persist_and_enqueue(session_factory, "disabled-1")

    class DisabledProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return False

        def send_listing_alert(self, listing: Listing, destination: str) -> None:
            raise AssertionError("must never be called while disabled")

    result = _drain(session_factory, DisabledProvider())

    assert result == DrainResult()

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    assert row.attempt_count == 0
    verify.close()


# =====================================================================
# Per-user notification routing - the security rule
# =====================================================================


def test_resolve_destination_returns_owner_unresolved_when_no_discovering_search(session_factory) -> None:
    """A legacy `/scan`-discovered listing (or any listing whose
    `discovered_by_saved_search_id` is `None`) can never be routed -
    Case B, not something waiting will fix."""
    resolved = resolve_destination(session_factory, discovered_by_saved_search_id=None)
    assert resolved.destination is None
    assert resolved.unresolved_reason == NOTIFICATION_ERROR_OWNER_UNRESOLVED


def test_resolve_destination_returns_owner_unresolved_for_an_unowned_saved_search(session_factory) -> None:
    """The saved search exists but has no `user_id` yet (pre-cutover,
    never backfilled) - Case B."""
    session = session_factory()
    saved_search = SavedSearchRepository(session).create(
        query="Unowned", marketplaces=["mock"], scan_interval_seconds=300, is_active=True
    )
    session.commit()
    saved_search_id = saved_search.id
    session.close()

    resolved = resolve_destination(session_factory, discovered_by_saved_search_id=saved_search_id)
    assert resolved.destination is None
    assert resolved.unresolved_reason == NOTIFICATION_ERROR_OWNER_UNRESOLVED


def test_resolve_destination_returns_awaiting_config_when_owner_has_no_preference_row(session_factory) -> None:
    """The owner is fully resolvable - just hasn't configured a
    destination yet. Case A, not Case B."""
    user_id = _create_user(session_factory, "no-preference@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)

    resolved = resolve_destination(session_factory, discovered_by_saved_search_id=saved_search_id)
    assert resolved.destination is None
    assert resolved.unresolved_reason == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG


def test_resolve_destination_returns_awaiting_config_when_preference_has_no_chat_id(session_factory) -> None:
    user_id = _create_user(session_factory, "cleared-preference@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    _set_telegram_preference(session_factory, user_id, None)

    resolved = resolve_destination(session_factory, discovered_by_saved_search_id=saved_search_id)
    assert resolved.destination is None
    assert resolved.unresolved_reason == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG


def test_resolve_destination_returns_the_owners_chat_id(session_factory) -> None:
    user_id = _create_user(session_factory, "owner@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    _set_telegram_preference(session_factory, user_id, "111222")

    resolved = resolve_destination(session_factory, discovered_by_saved_search_id=saved_search_id)
    assert resolved.destination == "111222"
    assert resolved.unresolved_reason is None


def test_notification_for_user_a_routes_only_to_a(session_factory) -> None:
    user_a = _create_user(session_factory, "user-a@example.com")
    search_a = _create_owned_saved_search(session_factory, user_a, query="A's search")
    _set_telegram_preference(session_factory, user_a, "AAA111")
    _persist_and_enqueue(session_factory, "a-item", saved_search_id=search_a)

    provider = RecordingProvider()
    result = _drain(session_factory, provider)

    assert result.sent_count == 1
    assert provider.destinations == ["AAA111"]


def test_notification_for_user_b_routes_only_to_b(session_factory) -> None:
    user_b = _create_user(session_factory, "user-b@example.com")
    search_b = _create_owned_saved_search(session_factory, user_b, query="B's search")
    _set_telegram_preference(session_factory, user_b, "BBB222")
    _persist_and_enqueue(session_factory, "b-item", saved_search_id=search_b)

    provider = RecordingProvider()
    result = _drain(session_factory, provider)

    assert result.sent_count == 1
    assert provider.destinations == ["BBB222"]


def test_two_users_in_the_same_drain_batch_route_to_distinct_destinations(session_factory) -> None:
    """The core cross-user-leak regression: two users' notifications,
    claimed and delivered in the very same drain pass, must each reach
    only their own destination - never each other's, never a shared one."""
    user_a = _create_user(session_factory, "batch-a@example.com")
    search_a = _create_owned_saved_search(session_factory, user_a, query="A's search")
    _set_telegram_preference(session_factory, user_a, "AAA111")

    user_b = _create_user(session_factory, "batch-b@example.com")
    search_b = _create_owned_saved_search(session_factory, user_b, query="B's search")
    _set_telegram_preference(session_factory, user_b, "BBB222")

    _persist_and_enqueue(session_factory, "batch-a-item", saved_search_id=search_a)
    _persist_and_enqueue(session_factory, "batch-b-item", saved_search_id=search_b)

    provider = RecordingProvider()
    result = _drain(session_factory, provider)

    assert result.claimed_count == 2
    assert result.sent_count == 2
    by_listing = dict(zip((listing.external_listing_id for listing in provider.sent), provider.destinations))
    assert by_listing == {"batch-a-item": "AAA111", "batch-b-item": "BBB222"}


def test_drain_never_reads_the_global_telegram_chat_id_setting(session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a legacy global `TELEGRAM_CHAT_ID` configured in
    settings, an unroutable notification (Case B here) must never fall
    back to it - the outbox module has no code path that reads this
    setting at all."""
    from marketplace_alert.config import settings

    monkeypatch.setattr(settings, "telegram_chat_id", "GLOBAL-SHOULD-NEVER-BE-USED")
    _persist_and_enqueue(session_factory, "no-fallback-1")  # no saved_search_id - unroutable

    provider = RecordingProvider()
    result = _drain(session_factory, provider)

    assert result.sent_count == 0
    assert result.unresolved_owner_count == 1
    assert provider.destinations == []
    assert "GLOBAL-SHOULD-NEVER-BE-USED" not in provider.destinations


# =====================================================================
# Case A - owner resolvable, destination not configured yet
# =====================================================================


def test_case_a_never_calls_telegram(session_factory) -> None:
    user_id = _create_user(session_factory, "case-a-no-call@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    _persist_and_enqueue(session_factory, "case-a-no-call-1", saved_search_id=saved_search_id)

    result = _drain(session_factory, NeverCalledProvider())

    assert result.claimed_count == 1
    assert result.sent_count == 0
    assert result.awaiting_destination_config_count == 1
    assert result.unresolved_owner_count == 0
    assert result.failed_count == 0


def test_case_a_row_remains_pending_not_sent_not_falsely_successful(session_factory) -> None:
    user_id = _create_user(session_factory, "case-a-pending@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "case-a-pending-1", saved_search_id=saved_search_id)

    _drain(session_factory, NeverCalledProvider())

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    assert row.sent_at is None
    assert row.last_error == NOTIFICATION_ERROR_AWAITING_DESTINATION_CONFIG
    verify.close()


def test_case_a_does_not_consume_attempt_count(session_factory) -> None:
    """`claim_batch()` increments `attempt_count` before the outcome is
    known - `complete()` must undo that increment for Case A, so waiting
    for configuration leaves `attempt_count` exactly where it started."""
    user_id = _create_user(session_factory, "case-a-attempts@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "case-a-attempts-1", saved_search_id=saved_search_id)

    for _ in range(5):
        _drain(session_factory, NeverCalledProvider())

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.attempt_count == 0
    assert row.status == NOTIFICATION_STATUS_PENDING
    verify.close()


def test_case_a_survives_beyond_max_attempts_never_becomes_failed(session_factory) -> None:
    """The core reliability requirement: a user who simply hasn't
    configured Telegram yet must never have their notification
    permanently lost, no matter how many drain cycles pass."""
    user_id = _create_user(session_factory, "case-a-survives@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "case-a-survives-1", saved_search_id=saved_search_id)

    # Far more drain cycles than a tiny max_attempts would ever allow a
    # genuine failure to survive. `no_destination_retry_seconds=0` so
    # every cycle can reclaim immediately - this test is about
    # `max_attempts` never applying to Case A, not about the throttle
    # (covered separately below).
    for _ in range(8):
        result = _drain(session_factory, NeverCalledProvider(), max_attempts=3, no_destination_retry_seconds=0.0)
        assert result.awaiting_destination_config_count == 1

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    assert row.status != NOTIFICATION_STATUS_FAILED
    verify.close()


def test_case_a_row_cannot_be_reclaimed_before_the_throttle_interval_elapses(session_factory) -> None:
    user_id = _create_user(session_factory, "case-a-throttled@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    _persist_and_enqueue(session_factory, "case-a-throttled-1", saved_search_id=saved_search_id)

    first = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=120.0)
    assert first.claimed_count == 1
    assert first.awaiting_destination_config_count == 1

    # Immediately again, well within the 120s throttle window - must not
    # be reclaimed (the "no hot loop" requirement).
    second = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=120.0)
    assert second.claimed_count == 0


def test_case_a_row_can_be_reclaimed_after_the_throttle_interval_elapses(session_factory) -> None:
    user_id = _create_user(session_factory, "case-a-unthrottled@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "case-a-unthrottled-1", saved_search_id=saved_search_id)

    first = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=0.05)
    assert first.claimed_count == 1
    time.sleep(0.1)

    second = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=0.05)
    assert second.claimed_count == 1
    assert second.awaiting_destination_config_count == 1

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    verify.close()


def test_case_a_becomes_deliverable_once_a_destination_is_later_configured(session_factory) -> None:
    """The self-healing property this design deliberately preserves: a
    notification enqueued before a user configures their preference isn't
    lost - it just stays `pending`, throttled, until a later drain, after
    they configure it, delivers it successfully."""
    user_id = _create_user(session_factory, "configures-later@example.com")
    saved_search_id = _create_owned_saved_search(session_factory, user_id)
    notification_id = _persist_and_enqueue(session_factory, "configures-later-1", saved_search_id=saved_search_id)

    first = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=0.05)
    assert first.awaiting_destination_config_count == 1
    assert first.sent_count == 0
    time.sleep(0.1)

    # The user now configures their preference...
    _set_telegram_preference(session_factory, user_id, "LATE999")

    # ...and the next (throttle-elapsed) drain successfully delivers it.
    provider = RecordingProvider()
    second = _drain(session_factory, provider, no_destination_retry_seconds=0.05)
    assert second.sent_count == 1
    assert provider.destinations == ["LATE999"]

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_SENT
    verify.close()


# =====================================================================
# Case B - ownership/provenance genuinely unresolvable
# =====================================================================


def test_case_b_never_calls_telegram(session_factory) -> None:
    _persist_and_enqueue(session_factory, "case-b-no-call-1")  # no saved_search_id

    result = _drain(session_factory, NeverCalledProvider())

    assert result.claimed_count == 1
    assert result.sent_count == 0
    assert result.unresolved_owner_count == 1
    assert result.awaiting_destination_config_count == 0


def test_case_b_never_falls_back_to_the_global_chat_id(session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    from marketplace_alert.config import settings

    monkeypatch.setattr(settings, "telegram_chat_id", "GLOBAL-SHOULD-NEVER-BE-USED")
    _persist_and_enqueue(session_factory, "case-b-no-fallback-1")

    provider = RecordingProvider()
    result = _drain(session_factory, provider)

    assert result.sent_count == 0
    assert provider.destinations == []


def test_case_b_consumes_bounded_attempts_like_before(session_factory) -> None:
    notification_id = _persist_and_enqueue(session_factory, "case-b-attempts-1")  # no saved_search_id

    _drain(session_factory, NeverCalledProvider(), max_attempts=3)

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.attempt_count == 1
    assert row.status == NOTIFICATION_STATUS_PENDING
    verify.close()


def test_case_b_row_is_reclaimable_immediately_unlike_case_a(session_factory) -> None:
    """Case B intentionally keeps the original, un-throttled retry
    behavior - only Case A is throttled."""
    _persist_and_enqueue(session_factory, "case-b-immediate-1")

    first = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=10_000.0)
    assert first.claimed_count == 1

    second = _drain(session_factory, NeverCalledProvider(), no_destination_retry_seconds=10_000.0)
    assert second.claimed_count == 1


def test_case_b_eventually_becomes_failed_after_max_attempts(session_factory) -> None:
    """Reuses the exact same retry/expiry machinery as a genuine delivery
    failure (see core/notifications/outbox.py's module docstring) - after
    `max_attempts` reclaims with genuinely unresolvable ownership, the
    row settles into `failed`, never `sent`."""
    notification_id = _persist_and_enqueue(session_factory, "never-routed-1")  # no saved_search_id, ever
    provider = NeverCalledProvider()

    for _ in range(3):
        result = _drain(session_factory, provider, max_attempts=3)
        assert result.sent_count == 0
        assert result.unresolved_owner_count == 1

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_FAILED
    assert row.sent_at is None
    verify.close()

    # Terminal - a later drain must never pick a `failed` row up again.
    result_after = _drain(session_factory, provider, max_attempts=3)
    assert result_after.claimed_count == 0
