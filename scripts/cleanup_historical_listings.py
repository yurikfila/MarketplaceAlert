"""One-off maintenance script: re-evaluate every persisted `DiscoveredListing`
row against the current relevance engine and current saved searches, and
remove the ones that would now be rejected.

See `marketplace_alert/core/persistence/cleanup.py` for the full design
and the schema-limitation explanation (no `saved_search_id` on
`DiscoveredListing` - relevance is checked against every saved search
currently targeting a row's marketplace, not "the" search that found it).

**Deliberately a manual script, not wired into the app.** Never runs on
app startup, in the scheduler, or from an API endpoint - this deletes
data, so it only ever runs when a developer explicitly invokes it.

Usage (from the project root, with the project's virtualenv active):

    python scripts/cleanup_historical_listings.py            # dry run - reports what WOULD be removed, deletes nothing
    python scripts/cleanup_historical_listings.py --apply     # actually deletes, and commits

Always run without --apply first and read the report before re-running
with --apply. This script never runs against a database other than the
one `marketplace_alert.config.settings.database_url` (or its default
local SQLite file) already resolves to - the exact same database the app
itself uses; nothing here overrides that.
"""

import argparse
import sys

from marketplace_alert.core.persistence.cleanup import (
    HistoricalCleanupResult,
    preview_historical_cleanup,
    run_historical_cleanup,
)
from marketplace_alert.core.persistence.database import SessionLocal
from marketplace_alert.core.persistence.repository import ListingRepository


def _print_report(result: HistoricalCleanupResult, *, applied: bool) -> None:
    verb = "Removed" if applied else "Would remove"
    print(f"Total discovered_listings rows examined: {result.total_rows}")
    print(f"  Evaluated against a current saved search: {result.evaluated_count}")
    print(f"  Skipped (no current saved search targets that marketplace): {result.skipped_no_saved_search_count}")
    print(f"  Kept (relevant to at least one current saved search): {result.kept_count}")
    print(f"  {verb} (relevant to none): {result.removed_count}")
    if result.removed:
        print()
        print(f"{verb} listings:")
        for row in result.removed:
            print(f"  [{row.id}] {row.marketplace}/{row.external_listing_id}: {row.title}")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage (e.g. cp1255)
        # that can't encode every character a real marketplace listing
        # title contains (accents, curly quotes, non-Latin scripts) -
        # replace rather than crash mid-report.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the identified rows and commit. Without this flag, only a dry-run report is printed.",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        before_count = ListingRepository(session).count()
        print(f"discovered_listings row count BEFORE: {before_count}")
        print()

        if not args.apply:
            result = preview_historical_cleanup(session)
            _print_report(result, applied=False)
            print()
            print("Dry run only - nothing was deleted. Re-run with --apply to actually remove these rows.")
            return 0

        result = run_historical_cleanup(session)
        session.commit()
        after_count = ListingRepository(session).count()
        print()
        _print_report(result, applied=True)
        print()
        print(f"discovered_listings row count AFTER: {after_count}")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
