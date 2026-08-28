"""Tests for the notification outbox: enqueueing
(`core/persistence/notification_outbox.py`) and the claim -> deliver ->
complete drain orchestration (`core/notifications/outbox.py`).

See `core/notifications/outbox.py`'s module docstring for the full design
this file verifies: no database lock held during delivery, crash recovery
via a claim lease, at-least-once (not exactly-once) delivery semantics,
and a bounded retry count.

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
from sqlalchemy.exc import IntegrityError

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.notifications.base import NotificationError, NotificationProvider
from marketplace_alert.core.notifications.outbox import (
    DrainResult,
    claim_due_notifications,
    drain_pending_notifications,
)
from marketplace_alert.core.persistence.models import (
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_SENT,
    DiscoveredListing,
    PendingNotification,
)
from marketplace_alert.core.persistence.notification_outbox import NotificationOutboxRepository
from marketplace_alert.core.persistence.repository import ListingRepository


def _listing(external_id: str) -> Listing:
    return Listing(
        marketplace="mock",
        external_listing_id=external_id,
        title=f"Listing {external_id}",
        listing_url=f"https://example.com/{external_id}",
    )


def _persist_and_enqueue(session_factory, external_id: str) -> int:
    """Persists a `DiscoveredListing` and its outbox row in one committed
    transaction (matching how `ListingDiscoveryService.process_listings()`
    and `SavedSearchRunner` do it together in real use), and returns the
    notification id."""
    session = session_factory()
    try:
        row = ListingRepository(session).save_new(_listing(external_id))
        notification = NotificationOutboxRepository(session).enqueue(row.id)
        session.commit()
        return notification.id
    finally:
        session.close()


class RecordingProvider(NotificationProvider):
    """Always succeeds; records every listing it was asked to send, in order."""

    def __init__(self) -> None:
        self.sent: list[Listing] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        self.sent.append(listing)


class AlwaysFailingProvider(NotificationProvider):
    @property
    def is_enabled(self) -> bool:
        return True

    def send_listing_alert(self, listing: Listing) -> None:
        raise NotificationError("simulated permanent failure")


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
# Claim phase: batching, ordering, and no double-claim of a live lease
# =====================================================================


def test_claim_batch_respects_limit_and_fifo_order(session_factory) -> None:
    ids_in_enqueue_order = [_persist_and_enqueue(session_factory, f"fifo-{i}") for i in range(3)]

    claimed = claim_due_notifications(session_factory, limit=2, lease_seconds=120)

    assert [c.notification_id for c in claimed] == ids_in_enqueue_order[:2]


def test_recently_claimed_row_is_not_claimed_again_within_its_lease(session_factory) -> None:
    notification_id = _persist_and_enqueue(session_factory, "concurrent-1")

    first_claim = claim_due_notifications(session_factory, limit=10, lease_seconds=120)
    assert [c.notification_id for c in first_claim] == [notification_id]

    second_claim = claim_due_notifications(session_factory, limit=10, lease_seconds=120)
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
    notification_id = _persist_and_enqueue(session_factory, "no-lock-1")

    probe_result = {"succeeded": False}

    class ProbingProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing) -> None:
            probe_session = session_factory()
            try:
                row = probe_session.get(PendingNotification, notification_id)
                row.last_error = "probe-write"
                probe_session.commit()
                probe_result["succeeded"] = True
            finally:
                probe_session.close()

    result = drain_pending_notifications(
        session_factory, ProbingProvider(), batch_size=10, lease_seconds=120, max_attempts=5
    )

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

    claimed = claim_due_notifications(session_factory, limit=10, lease_seconds=120)

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
    notification_id = _persist_and_enqueue(session_factory, "crash-claim-1")

    claimed = claim_due_notifications(session_factory, limit=10, lease_seconds=0.01)
    assert len(claimed) == 1
    time.sleep(0.05)

    provider = RecordingProvider()
    result = drain_pending_notifications(
        session_factory, provider, batch_size=10, lease_seconds=0.01, max_attempts=5
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
    notification_id = _persist_and_enqueue(session_factory, "crash-send-1")
    provider = RecordingProvider()

    claimed = claim_due_notifications(session_factory, limit=10, lease_seconds=0.01)
    assert len(claimed) == 1
    provider.send_listing_alert(claimed[0].listing)  # "successfully" sent...
    # ...and then the process dies right here - complete_notification (phase 3) never runs.
    time.sleep(0.05)

    result = drain_pending_notifications(
        session_factory, provider, batch_size=10, lease_seconds=0.01, max_attempts=5
    )

    assert result.sent_count == 1
    assert len(provider.sent) == 2
    assert [listing.external_listing_id for listing in provider.sent] == ["crash-send-1", "crash-send-1"]

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_SENT
    verify.close()


# =====================================================================
# Bounded retries
# =====================================================================


def test_row_becomes_failed_after_max_attempts(session_factory) -> None:
    notification_id = _persist_and_enqueue(session_factory, "always-fails-1")
    provider = AlwaysFailingProvider()

    for _ in range(3):
        result = drain_pending_notifications(
            session_factory, provider, batch_size=10, lease_seconds=120, max_attempts=3
        )
        assert result.claimed_count == 1
        assert result.failed_count == 1

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_FAILED
    assert row.attempt_count == 3
    verify.close()

    # Terminal - a later drain must never pick a `failed` row up again.
    result_after = drain_pending_notifications(
        session_factory, provider, batch_size=10, lease_seconds=120, max_attempts=3
    )
    assert result_after.claimed_count == 0


# =====================================================================
# One bad row can't block the rest of a batch or crash the drain
# =====================================================================


def test_one_failing_notification_does_not_block_others_in_the_same_batch(session_factory) -> None:
    _persist_and_enqueue(session_factory, "batch-good-1")
    _persist_and_enqueue(session_factory, "batch-bad-1")

    class SelectivelyFailingProvider(NotificationProvider):
        def __init__(self) -> None:
            self.sent: list[Listing] = []

        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing) -> None:
            if listing.external_listing_id == "batch-bad-1":
                raise NotificationError("simulated failure")
            self.sent.append(listing)

    provider = SelectivelyFailingProvider()
    result = drain_pending_notifications(
        session_factory, provider, batch_size=10, lease_seconds=120, max_attempts=5
    )

    assert result.claimed_count == 2
    assert result.sent_count == 1
    assert result.failed_count == 1
    assert [listing.external_listing_id for listing in provider.sent] == ["batch-good-1"]


def test_an_unexpected_exception_during_delivery_does_not_crash_the_drain(session_factory) -> None:
    notification_id = _persist_and_enqueue(session_factory, "unexpected-1")

    class CrashingProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return True

        def send_listing_alert(self, listing: Listing) -> None:
            raise ValueError("not a NotificationError at all")

    result = drain_pending_notifications(
        session_factory, CrashingProvider(), batch_size=10, lease_seconds=120, max_attempts=5
    )

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
    """A disabled provider (no credentials configured) is a configuration
    state, not a delivery failure - rows are left untouched, ready to be
    delivered whenever a real provider is configured. See
    `drain_pending_notifications`'s docstring."""
    notification_id = _persist_and_enqueue(session_factory, "disabled-1")

    class DisabledProvider(NotificationProvider):
        @property
        def is_enabled(self) -> bool:
            return False

        def send_listing_alert(self, listing: Listing) -> None:
            raise AssertionError("must never be called while disabled")

    result = drain_pending_notifications(
        session_factory, DisabledProvider(), batch_size=10, lease_seconds=120, max_attempts=5
    )

    assert result == DrainResult()

    verify = session_factory()
    row = verify.get(PendingNotification, notification_id)
    assert row.status == NOTIFICATION_STATUS_PENDING
    assert row.attempt_count == 0
    verify.close()
