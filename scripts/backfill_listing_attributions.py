"""One-off cutover script: create `ListingAttribution` rows for every
pre-existing (saved search, listing) relationship already known via
`discovered_listings.discovered_by_saved_search_id`.

See `marketplace_alert/core/persistence/listing_attribution_backfill.py`
for the full design and the exact rules governing it - this script is a
thin CLI wrapper around that module's `run_listing_attribution_backfill()`;
no business logic lives here.

**What this does:** for every `DiscoveredListing` row whose `discovered_
by_saved_search_id` is set, creates the corresponding `ListingAttribution`
if one doesn't already exist. Rows with no discovering search at all
(`discovered_by_saved_search_id IS NULL`) are left exactly as they are -
never guessed, never attributed based on title/query/relevance matching.
Idempotent - safe to re-run; a pair that's already attributed is left
untouched.

**Deliberately a manual script, not wired into the app** - same "reviewed
and run by a human, not automatic" posture as `scripts/create_bootstrap_
admin.py` and `scripts/backfill_notification_preference.py`.

Usage (from the project root, with the project's virtualenv active):

    python scripts/backfill_listing_attributions.py
        # dry run - reports what WOULD happen, writes nothing

    python scripts/backfill_listing_attributions.py --apply
        # actually writes the missing attribution rows

This script never runs against a database other than the one
`marketplace_alert.config.settings.database_url` (or its default local
SQLite file) already resolves to - the exact same database the app
itself uses.
"""

import argparse
import sys

from marketplace_alert.core.persistence.listing_attribution_backfill import (
    ListingAttributionBackfillReport,
    run_listing_attribution_backfill,
)
from marketplace_alert.core.persistence.database import SessionLocal


def _print_report(report: ListingAttributionBackfillReport, *, apply: bool) -> None:
    print(f"Mode: {'APPLIED' if apply else 'DRY RUN (nothing written)'}")
    print(f"Total discovered_listings rows: {report.total_discovered_listings}")
    print(f"  With a known discovering saved search: {report.candidates_with_known_attribution}")
    print(f"  With no discovering saved search (skipped, never guessed): {report.skipped_null_attribution_count}")
    print(f"  Already attributed (idempotent no-op): {report.already_attributed_count}")

    verb = "Created" if apply else "Would create"
    print(f"  {verb} attribution rows: {report.would_create_count if not apply else report.created_count}")
    if not apply and report.would_create_count:
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
        help="Actually write the missing attribution rows. Without this flag, always a dry run.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        report = run_listing_attribution_backfill(session, apply=args.apply)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _print_report(report, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
