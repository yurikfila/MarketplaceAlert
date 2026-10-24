"""Background scanner: periodically runs due saved searches.

One central loop (a single background thread) - not one thread or process
per saved search. Each tick: load active saved searches, work out which
are due (`SavedSearchRepository.list_due_for_scan`), and run each due one
through `SavedSearchRunner`, skipping any already in-flight
(`SavedSearchRunGuard`). A failure in one saved search is logged and never
stops the others or the loop itself.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from marketplace_alert.config import settings
from marketplace_alert.core.connectors.metrics import collect_request_metrics
from marketplace_alert.core.saved_searches.coalescing import ScanFetchCache

from marketplace_alert.core.saved_searches.repository import SavedSearchRepository
from marketplace_alert.core.saved_searches.runner import SavedSearchRunner
from marketplace_alert.core.scheduler.guard import SavedSearchRunGuard

logger = logging.getLogger(__name__)


class BackgroundScanner:
    """Runs due saved searches on a recurring tick, in one background thread."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        runner: SavedSearchRunner,
        run_guard: SavedSearchRunGuard,
        tick_seconds: float = 5.0,
        due_grace_seconds: float = 0.0,
        clock: Callable[[], datetime] | None = None,
        coalescing_enabled: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._run_guard = run_guard
        self._tick_seconds = tick_seconds
        # Only the one-shot cron entrypoint sets a grace window (see
        # `SavedSearchRepository.list_due_for_scan`); in-process scanning keeps 0.
        self._due_grace_seconds = due_grace_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # None = follow `settings.scan_coalescing_enabled` (read each pass); a bool pins it.
        self._coalescing_enabled = coalescing_enabled
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background thread. Safe to call more than once (no-op if running)."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="saved-search-scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait (briefly) for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_seconds + 5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due_searches()
            except Exception:
                logger.exception("Background scanner tick failed")
            self._stop_event.wait(self._tick_seconds)

    def run_due_searches(self) -> None:
        """One tick: find due saved searches and run each in its own session/transaction.

        Every search in this pass is stamped with the *pass start* time (taken once,
        before anything runs), so how long an earlier search took can neither delay
        a later one nor push the next pass's due check past its tick."""
        pass_started_at = self._clock()
        list_session = self._session_factory()
        try:
            due_ids = [
                s.id
                for s in SavedSearchRepository(list_session).list_due_for_scan(
                    now=pass_started_at, grace_seconds=self._due_grace_seconds
                )
            ]
        finally:
            list_session.close()

        if not due_ids:
            return

        coalescing = settings.scan_coalescing_enabled if self._coalescing_enabled is None else self._coalescing_enabled
        fetch_cache = ScanFetchCache() if coalescing else None
        with collect_request_metrics() as request_metrics:
            for saved_search_id in due_ids:
                self._run_one(saved_search_id, pass_started_at, fetch_cache)

        # Counts only - never a query, URL, token or credential.
        logger.info(
            "Scan pass requests: %s | searches=%d coalescing=%s fetches=%s reused=%s",
            request_metrics.summary(),
            len(due_ids),
            "on" if coalescing else "off",
            fetch_cache.fetches if fetch_cache else "-",
            fetch_cache.reused if fetch_cache else "-",
        )

    def _run_one(self, saved_search_id: int, scanned_at: datetime, fetch_cache: ScanFetchCache | None = None) -> None:
        with self._run_guard.acquire(saved_search_id) as acquired:
            if not acquired:
                logger.info("Saved search %s already running - skipping this tick", saved_search_id)
                return

            session = self._session_factory()
            try:
                if fetch_cache is None:
                    self._runner.run_by_id(session, saved_search_id, scanned_at=scanned_at)
                else:
                    self._runner.run_by_id(session, saved_search_id, scanned_at=scanned_at, fetch_cache=fetch_cache)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Saved search %s failed during scheduled run", saved_search_id)
            finally:
                session.close()
