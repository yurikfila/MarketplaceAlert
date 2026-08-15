"""Raw persistence access for saved searches.

Only this module (plus `database.py` and `models.py`) issues SQL/ORM
queries for saved searches. The service layer, routes, and the background
scanner work with `SavedSearch` and plain Python types only.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketplace_alert.core.saved_searches.models import SavedSearch, SavedSearchMarketplace


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; every stored value is UTC regardless."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class SavedSearchRepository:
    """CRUD and due-search queries for `SavedSearch`, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, *, query: str, marketplaces: list[str], scan_interval_seconds: int, is_active: bool
    ) -> SavedSearch:
        saved_search = SavedSearch(
            query=query,
            scan_interval_seconds=scan_interval_seconds,
            is_active=is_active,
            marketplace_links=[SavedSearchMarketplace(marketplace_name=m) for m in marketplaces],
        )
        self._session.add(saved_search)
        self._session.flush()
        return saved_search

    def get(self, saved_search_id: int) -> SavedSearch | None:
        return self._session.get(SavedSearch, saved_search_id)

    def list_all(self) -> list[SavedSearch]:
        stmt = select(SavedSearch).order_by(SavedSearch.id)
        return list(self._session.execute(stmt).scalars().all())

    def update(
        self,
        saved_search: SavedSearch,
        *,
        query: str | None,
        marketplaces: list[str] | None,
        scan_interval_seconds: int | None,
        is_active: bool | None,
    ) -> SavedSearch:
        """Apply only the provided (non-None) fields, then bump updated_at.

        `marketplaces`, when provided, *replaces* the full set. Clearing the
        collection and flushing before extending it (rather than assigning
        a brand new list in one step) is deliberate: a one-step reassignment
        lets SQLAlchemy interleave the delete/insert statements, which trips
        the `(saved_search_id, marketplace_name)` unique constraint whenever
        a marketplace is unchanged across the update (e.g. `["mock"]` ->
        `["etsy", "mock"]`: re-adding "mock" would race the deletion of the
        old "mock" row).
        """
        if query is not None:
            saved_search.query = query
        if marketplaces is not None:
            saved_search.marketplace_links.clear()
            self._session.flush()
            saved_search.marketplace_links.extend(
                SavedSearchMarketplace(marketplace_name=m) for m in marketplaces
            )
        if scan_interval_seconds is not None:
            saved_search.scan_interval_seconds = scan_interval_seconds
        if is_active is not None:
            saved_search.is_active = is_active
        saved_search.updated_at = datetime.now(timezone.utc)
        self._session.add(saved_search)
        self._session.flush()
        return saved_search

    def delete(self, saved_search_id: int) -> bool:
        saved_search = self.get(saved_search_id)
        if saved_search is None:
            return False
        self._session.delete(saved_search)
        self._session.flush()
        return True

    def mark_scanned(self, saved_search: SavedSearch) -> None:
        """Record that a scan just ran - never touches updated_at (definition unchanged)."""
        saved_search.last_scanned_at = datetime.now(timezone.utc)
        self._session.add(saved_search)
        self._session.flush()

    def list_due_for_scan(self, now: datetime | None = None) -> list[SavedSearch]:
        """Active saved searches never scanned, or whose interval has elapsed."""
        now = now or datetime.now(timezone.utc)
        stmt = select(SavedSearch).where(SavedSearch.is_active.is_(True))
        due = []
        for saved_search in self._session.execute(stmt).scalars().all():
            if saved_search.last_scanned_at is None:
                due.append(saved_search)
                continue
            last_scanned_at = _as_aware_utc(saved_search.last_scanned_at)
            next_due_at = last_scanned_at + timedelta(seconds=saved_search.scan_interval_seconds)
            if next_due_at <= now:
                due.append(saved_search)
        return due
