"""One-time (and safely re-runnable) cutover: stamp `PendingNotification.
user_id` for existing rows, wherever ownership can be *proven* from the
exact same legacy chain delivery already resolves through today -
Phase 2C of the multi-user notification outbox redesign (see
`core/persistence/models.py:PendingNotification`'s own docstring, and
`core/notifications/outbox.py:resolve_destination`'s legacy-fallback path,
for the full design/reasoning).

Pure logic only - no `argparse`, no `print`. The CLI wrapper
(`scripts/backfill_pending_notification_users.py`) owns all of that; this
module is a plain function a test can call directly against a session,
matching this codebase's established split (see `core/auth/bootstrap.py`,
`core/notifications/preference_backfill.py`, and `core/persistence/
listing_attribution_backfill.py`, and their own CLI wrappers).

**Never infers ownership.** The only source of truth this reads is the
exact chain `resolve_destination()` already uses for delivery today:
`PendingNotification.discovered_listing_id -> DiscoveredListing.
discovered_by_saved_search_id -> SavedSearch.user_id`. If every hop
resolves, copying the result into `user_id` is not a guess - it's a
faithful restatement of a fact delivery already relies on right now, for
this exact row. If any hop fails - no discovering search, that search no
longer exists, or it exists with no owner yet - this backfill does
**nothing** for that row: never guesses from title/query/relevance,
never consults `ListingAttribution` (a *different*, and for this row
possibly *multiple*, set of candidate owners - see below), and never
creates a new `PendingNotification` row for any other user who might
have a `ListingAttribution` for the same listing. This backfill only
ever restates the single historical fact already true for the ONE
existing row - it does not, and must not, expand who gets notified.

**Idempotent, and never overwrites.** A row whose `user_id` is already
set - from a previous run of this script, or because it was stamped at
enqueue time by `SavedSearchRunner` (Phase 2B) - is left untouched,
regardless of what the legacy chain would otherwise resolve to for it.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

# Registers the `users` table on `Base.metadata` - needed because
# `PendingNotification.user_id`'s foreign key references it directly, and
# nothing else this module imports does. Without this, writing a resolved
# `user_id` here and committing raises `NoReferencedTableError` the
# moment SQLAlchemy needs to sort `pending_notifications`' own foreign
# keys for the flush - real only in a process that never otherwise
# imports `core.auth.models` (e.g. this module's own CLI wrapper, run
# standalone; the test suite is unaffected, since other test modules
# already import it first). Same reasoning `alembic/env.py` documents for
# its own equivalent imports.
import marketplace_alert.core.auth.models  # noqa: F401
from marketplace_alert.core.persistence.models import DiscoveredListing, PendingNotification
from marketplace_alert.core.persistence.notification_outbox import NotificationOutboxRepository
from marketplace_alert.core.saved_searches.models import SavedSearch

# Classification labels for a NULL-user_id row's legacy-chain resolution -
# see `_resolve_legacy_owner`'s docstring for exactly what each means.
_RESOLVED = "resolved"
_NO_DISCOVERING_SEARCH = "no_discovering_search"
_MISSING_SAVED_SEARCH = "missing_saved_search"
_UNOWNED_SAVED_SEARCH = "unowned_saved_search"


@dataclass
class NotificationUserBackfillReport:
    """The complete result of one `run_notification_user_backfill` call -
    enough for the CLI wrapper to print a report in both dry-run and
    apply modes."""

    total_pending_notifications: int
    already_has_user_id_count: int
    null_user_id_examined_count: int
    safely_resolvable_count: int
    unresolved_no_discovering_search_count: int
    unresolved_missing_saved_search_count: int
    unresolved_unowned_saved_search_count: int
    would_update_count: int
    updated_count: int
    applied: bool


def _resolve_legacy_owner(session: Session, discovered_listing_id: int) -> tuple[int | None, str]:
    """Walks the exact same legacy chain `resolve_destination()` uses for
    delivery today, but reports *which specific hop* failed - finer
    grained than that function's single undifferentiated "owner
    unresolved" outcome, since this backfill's reporting requirements
    distinguish the three ways ownership can fail to resolve.

    Returns `(user_id, _RESOLVED)` only when every hop succeeds;
    otherwise `(None, <one of the three unresolved labels>)`. Never
    guesses - a `None` result here means exactly "waiting will not
    change this," the same conclusion `resolve_destination` already
    reaches for the identical row today (Case B, `NOTIFICATION_ERROR_
    OWNER_UNRESOLVED`).
    """
    listing = session.get(DiscoveredListing, discovered_listing_id)
    if listing is None or listing.discovered_by_saved_search_id is None:
        return None, _NO_DISCOVERING_SEARCH

    saved_search = session.get(SavedSearch, listing.discovered_by_saved_search_id)
    if saved_search is None:
        return None, _MISSING_SAVED_SEARCH

    if saved_search.user_id is None:
        return None, _UNOWNED_SAVED_SEARCH

    return saved_search.user_id, _RESOLVED


def run_notification_user_backfill(session: Session, *, apply: bool) -> NotificationUserBackfillReport:
    """For every `PendingNotification` row with `user_id IS NULL`, stamps
    `user_id` with the legacy chain's resolved owner - but only when
    every hop of that chain actually resolves (see `_resolve_legacy_owner`).
    Rows that already have a `user_id` are left completely untouched and
    counted separately; unresolvable rows are left `NULL`, exactly as
    correct today as they'll be after this backfill runs (see this
    module's own docstring for why never guessing matters).

    Commits only when `apply=True` and at least one row was actually
    updated - a dry run, or an apply that finds nothing left to do,
    never opens a write transaction at all. All updates happen in one
    single transaction (one commit at the very end) - if anything
    unexpected fails partway through, nothing has been persisted yet;
    the caller (the CLI wrapper) is responsible for rolling back and
    reporting the failure, matching this codebase's established
    backfill-script convention.
    """
    all_notifications = NotificationOutboxRepository(session).list_all()
    null_user_id_rows = [row for row in all_notifications if row.user_id is None]
    already_has_user_id = len(all_notifications) - len(null_user_id_rows)

    safely_resolvable = 0
    unresolved_no_discovering_search = 0
    unresolved_missing_saved_search = 0
    unresolved_unowned_saved_search = 0
    updated = 0

    for notification in null_user_id_rows:
        owner_user_id, classification = _resolve_legacy_owner(session, notification.discovered_listing_id)

        if classification == _NO_DISCOVERING_SEARCH:
            unresolved_no_discovering_search += 1
        elif classification == _MISSING_SAVED_SEARCH:
            unresolved_missing_saved_search += 1
        elif classification == _UNOWNED_SAVED_SEARCH:
            unresolved_unowned_saved_search += 1
        else:
            safely_resolvable += 1
            if apply:
                notification.user_id = owner_user_id
                updated += 1

    if apply and updated:
        session.commit()

    return NotificationUserBackfillReport(
        total_pending_notifications=len(all_notifications),
        already_has_user_id_count=already_has_user_id,
        null_user_id_examined_count=len(null_user_id_rows),
        safely_resolvable_count=safely_resolvable,
        unresolved_no_discovering_search_count=unresolved_no_discovering_search,
        unresolved_missing_saved_search_count=unresolved_missing_saved_search,
        unresolved_unowned_saved_search_count=unresolved_unowned_saved_search,
        would_update_count=safely_resolvable,
        updated_count=updated if apply else 0,
        applied=apply and updated > 0,
    )
