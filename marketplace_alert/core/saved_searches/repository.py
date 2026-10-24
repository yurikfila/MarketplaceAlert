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
        self,
        *,
        query: str,
        marketplaces: list[str],
        scan_interval_seconds: int,
        is_active: bool,
        user_id: int | None = None,
    ) -> SavedSearch:
        """`user_id` defaults to `None` (unowned) so every existing caller
        that predates ownership enforcement - tests, the legacy dashboard
        route - is unaffected. The protected `/api/v1` route is the one
        caller that now always passes the authenticated user's id."""
        saved_search = SavedSearch(
            query=query,
            scan_interval_seconds=scan_interval_seconds,
            is_active=is_active,
            user_id=user_id,
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

    # --- Ownership-scoped variants (groundwork for the route-protection
    # phase - see PROJECT_CONTEXT.md's authentication design decision).
    # Not yet called by any route; `get`/`list_all`/`update`/`delete`
    # above are unchanged and remain what every current route uses.

    def get_owned(self, saved_search_id: int, *, user_id: int) -> SavedSearch | None:
        """Like `get`, but returns `None` for a saved search that exists
        but belongs to a different user - not a distinguishable "exists
        but forbidden" result, deliberately: a future route calling this
        must 404, never 403, for someone else's saved search (matching
        this project's approved authentication design - see the "not
        found, not forbidden" requirement)."""
        stmt = select(SavedSearch).where(SavedSearch.id == saved_search_id, SavedSearch.user_id == user_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_owned(self, *, user_id: int) -> list[SavedSearch]:
        stmt = select(SavedSearch).where(SavedSearch.user_id == user_id).order_by(SavedSearch.id)
        return list(self._session.execute(stmt).scalars().all())

    def delete_owned(self, saved_search_id: int, *, user_id: int) -> bool:
        """Like `delete`, but only deletes a row this user actually owns -
        a nonexistent id, or one owned by someone else, both return
        `False` without touching anything, indistinguishably."""
        saved_search = self.get_owned(saved_search_id, user_id=user_id)
        if saved_search is None:
            return False
        self._session.delete(saved_search)
        self._session.flush()
        return True

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

    def mark_scanned(self, saved_search: SavedSearch, scanned_at: datetime | None = None) -> None:
        """Record that a scan ran - never touches updated_at (definition unchanged).

        `scanned_at` is the time the scan was *scheduled/started*, not when it
        finished: "due" is `last_scanned_at + interval`, so stamping the
        completion time made every search run later than its interval by its own
        scan duration, and skipped whole cron ticks (a 5-minute search ran every
        ~10 minutes). Defaults to now for callers that have no better anchor.
        """
        saved_search.last_scanned_at = scanned_at or datetime.now(timezone.utc)
        self._session.add(saved_search)
        self._session.flush()

    def list_due_for_scan(self, now: datetime | None = None, grace_seconds: float = 0.0) -> list[SavedSearch]:
        """Active saved searches never scanned, or whose interval has elapsed.

        `grace_seconds` lets a search count as due slightly *before* its interval
        has fully elapsed. It exists for the one-shot cron entrypoint, whose passes
        start on a fixed period but with a few seconds of scheduler jitter: without
        it a search whose interval equals the cron period is skipped whenever a pass
        starts a moment early. The grace is capped at half the search's interval, so
        it can never make a short-interval search due again right after it ran. The
        default (0) keeps the strict behaviour used by in-process scanning.
        """
        now = now or datetime.now(timezone.utc)
        grace = max(0.0, grace_seconds)
        stmt = select(SavedSearch).where(SavedSearch.is_active.is_(True))
        due = []
        for saved_search in self._session.execute(stmt).scalars().all():
            if saved_search.last_scanned_at is None:
                due.append(saved_search)
                continue
            last_scanned_at = _as_aware_utc(saved_search.last_scanned_at)
            effective_grace = min(grace, saved_search.scan_interval_seconds / 2)
            next_due_at = last_scanned_at + timedelta(seconds=saved_search.scan_interval_seconds - effective_grace)
            if next_due_at <= now:
                due.append(saved_search)
        return due
