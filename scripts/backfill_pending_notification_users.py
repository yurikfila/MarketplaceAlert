"""One-off cutover script: stamp `pending_notifications.user_id` for
existing rows, wherever ownership can be proven from the exact legacy
chain delivery already uses today.

See `marketplace_alert/core/persistence/notification_user_backfill.py`
for the full design and the exact rules governing it - this script is a
thin CLI wrapper around that module's `run_notification_user_backfill()`;
no business logic lives here.

**What this does:** for every `PendingNotification` row with `user_id IS
NULL`, walks `discovered_listing_id -> DiscoveredListing.discovered_by_
saved_search_id -> SavedSearch.user_id` and stamps `user_id` only if
every hop resolves. A row where that chain is broken (no discovering
search, a deleted search, or a search with no owner) is left exactly as
it is - never guessed, never attributed based on `ListingAttribution`,
title, query, or relevance matching, and no new `PendingNotification`
row is ever created for any other user. Idempotent - safe to re-run; a
row that already has a `user_id` (from a previous run of this script, or
stamped at enqueue time by `SavedSearchRunner`) is left untouched.

**Deliberately a manual script, not wired into the app** - same "reviewed
and run by a human, not automatic" posture as `scripts/create_bootstrap_
admin.py`, `scripts/backfill_notification_preference.py`, and `scripts/
backfill_listing_attributions.py`.

Usage (from the project root, with the project's virtualenv active):

    python scripts/backfill_pending_notification_users.py
        # dry run - reports what WOULD happen, writes nothing

    python scripts/backfill_pending_notification_users.py --apply
        # actually writes the resolvable user_id values

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app
itself uses.
"""

import argparse
import sys

from marketplace_alert.core.persistence.database import SessionLocal
from marketplace_alert.core.persistence.notification_user_backfill import (
    NotificationUserBackfillReport,
    run_notification_user_backfill,
)


def _print_report(report: NotificationUserBackfillReport, *, apply: bool) -> None:
    print(f"Mode: {'APPLIED' if apply else 'DRY RUN (nothing written)'}")
    print(f"Total pending_notifications rows: {report.total_pending_notifications}")
    print(f"  Already has user_id (untouched): {report.already_has_user_id_count}")
    print(f"  user_id NULL, examined: {report.null_user_id_examined_count}")
    print(f"    Safely resolvable via the legacy chain: {report.safely_resolvable_count}")
    print(f"    Unresolved - no discovering saved search: {report.unresolved_no_discovering_search_count}")
    print(f"    Unresolved - saved search no longer exists: {report.unresolved_missing_saved_search_count}")
    print(f"    Unresolved - saved search has no owner: {report.unresolved_unowned_saved_search_count}")

    verb = "Would update" if not apply else "Updated"
    count = report.would_update_count if not apply else report.updated_count
    print(f"  {verb} rows: {count}")
    if not apply and report.would_update_count:
        print("  Dry run only - nothing was written. Re-run with --apply to actually make this change.")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage - replace
        # rather than crash mid-report (same as create_bootstrap_admin.py).
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the resolvable user_id values. Without this flag, always a dry run.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        report = run_notification_user_backfill(session, apply=args.apply)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _print_report(report, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
