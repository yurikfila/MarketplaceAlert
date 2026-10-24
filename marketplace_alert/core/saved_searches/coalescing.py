"""Per-scan-pass result sharing between saved searches with the same marketplace and query.

Two saved searches that ask the same marketplace for the exact same query text get the
same raw result list, so within one scanner pass the marketplace is asked once and every
search in the group is handed a copy. Everything after the fetch stays per saved search
and is untouched: relevance filtering, listing attribution, de-duplication and the
notification outbox all run once per search, so a user's alerts are exactly what they
would be without sharing.

Deliberately narrow:

- The key is the marketplace plus the *exact* stored query string. Queries that differ in
  case, spacing or word order are different keys and are never merged.
- The cache lives for one pass only (one `ScanFetchCache` per `run_due_searches`). A search
  that is not due in this pass cannot use, or feed, the cache - differing scan intervals
  are therefore never altered; only searches already due together share a request.
- A failed fetch is remembered for the pass too (so a down marketplace is asked once, not
  once per search) and re-raised to each search, which handles it exactly as it handles its
  own failure today: that marketplace is recorded as errored and its listings are picked up
  on the next scan - nothing is marked seen that was not fetched.
- Every search receives its own deep copy of the listings, so no search can see another's
  mutation of a shared object.
"""

from collections.abc import Callable
from dataclasses import dataclass

from marketplace_alert.core.models.listing import Listing


@dataclass
class _Entry:
    listings: list[Listing] | None = None
    error: Exception | None = None


class ScanFetchCache:
    """Results (or the failure) of each distinct (marketplace, query) fetched in this pass."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self.fetches = 0  # distinct marketplace searches actually made
        self.reused = 0  # searches served from an earlier fetch in the same pass

    def fetch(self, marketplace: str, query: str, do_fetch: Callable[[], list[Listing]]) -> list[Listing]:
        key = (marketplace, query)
        entry = self._entries.get(key)
        if entry is None:
            self.fetches += 1
            try:
                entry = _Entry(listings=do_fetch())
            except Exception as exc:  # noqa: BLE001 - re-raised below for every search in the group
                entry = _Entry(error=exc)
            self._entries[key] = entry
        else:
            self.reused += 1

        if entry.error is not None:
            raise entry.error
        assert entry.listings is not None
        return [listing.model_copy(deep=True) for listing in entry.listings]
