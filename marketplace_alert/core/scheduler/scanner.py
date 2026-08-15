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

from sqlalchemy.orm import Session

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
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._run_guard = run_guard
        self._tick_seconds = tick_seconds
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
        """One tick: find due saved searches and run each in its own session/transaction."""
        list_session = self._session_factory()
        try:
            due_ids = [s.id for s in SavedSearchRepository(list_session).list_due_for_scan()]
        finally:
            list_session.close()

        for saved_search_id in due_ids:
            self._run_one(saved_search_id)

    def _run_one(self, saved_search_id: int) -> None:
        with self._run_guard.acquire(saved_search_id) as acquired:
            if not acquired:
                logger.info("Saved search %s already running - skipping this tick", saved_search_id)
                return

            session = self._session_factory()
            try:
                self._runner.run_by_id(session, saved_search_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Saved search %s failed during scheduled run", saved_search_id)
            finally:
                session.close()
