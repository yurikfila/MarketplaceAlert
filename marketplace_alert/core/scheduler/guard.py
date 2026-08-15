"""Prevents overlapping runs of the same saved search.

Shared by the manual `POST /saved-searches/{id}/run` endpoint and the
background scanner - both check in before running a saved search and check
out after, so the same search is never scanned by two paths (or two
overlapping scheduler ticks) at once.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class SavedSearchRunGuard:
    """Thread-safe "is this saved search currently running?" tracker."""

    def __init__(self) -> None:
        self._running_ids: set[int] = set()
        self._lock = threading.Lock()

    def try_acquire(self, saved_search_id: int) -> bool:
        """Mark as running and return True, unless it's already running (then False)."""
        with self._lock:
            if saved_search_id in self._running_ids:
                return False
            self._running_ids.add(saved_search_id)
            return True

    def release(self, saved_search_id: int) -> None:
        with self._lock:
            self._running_ids.discard(saved_search_id)

    def reset(self) -> None:
        """Clear all tracked in-flight runs. Test-only - production code should
        always release what it acquires instead of relying on this."""
        with self._lock:
            self._running_ids.clear()

    @contextmanager
    def acquire(self, saved_search_id: int) -> Iterator[bool]:
        """Context-manager form: yields whether the lock was actually acquired."""
        acquired = self.try_acquire(saved_search_id)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(saved_search_id)
