"""One-off maintenance script: re-fetch existing `DiscoveredListing` rows
still missing metadata (price/currency/image_url/condition/location/
seller/source_created_at) from their source marketplace, by id, and fill
in only what's missing.

See `marketplace_alert/core/persistence/backfill.py` for the full design
and every safety guarantee (never overwrites a present value, never
touches discovery timestamps or marketplace identity, never triggers a
"new listing" notification, marketplace/failure isolation, rate-limit
pacing, incremental commits). This script is the thin CLI wrapper around
that module - it contains no enrichment logic of its own.

**Defaults to a dry run - nothing is written unless you pass `--apply`.**
This matches `scripts/cleanup_historical_listings.py`'s own convention
(the other data-mutating maintenance script in this project) and this
feature's own explicit safety requirement: always see a report of what
WOULD change before actually changing production data.

Usage (from the project root, with the project's virtualenv active):

    # Dry run - reports what would be enriched, writes nothing.
    python scripts/backfill_listing_metadata.py
    python scripts/backfill_listing_metadata.py --dry-run

    # A small, bounded real run against one marketplace.
    python scripts/backfill_listing_metadata.py --marketplace reverb --limit 50 --apply
    python scripts/backfill_listing_metadata.py --marketplace ebay --limit 50 --apply

    # Every marketplace, the default batch size.
    python scripts/backfill_listing_metadata.py --apply

Always run without `--apply` first and read the report before re-running
with `--apply`. This script never runs against a database other than the
one `marketplace_alert.config.settings.database_url` (or its default
local SQLite file) already resolves to - the exact same database the app
itself uses; nothing here overrides that. Never logs credentials - see
`core/connectors/*` and `core/persistence/backfill.py` for the same rule
applied at every layer this script calls into.
"""

import argparse
import sys

from marketplace_alert.config import settings
from marketplace_alert.connectors.registry import get_connector, is_marketplace_supported
from marketplace_alert.core.persistence.backfill import BackfillSummary, run_backfill
from marketplace_alert.core.persistence.database import SessionLocal


def _print_report(summary: BackfillSummary) -> None:
    mode = "DRY RUN (nothing written)" if summary.dry_run else "APPLIED"
    print(f"Mode: {mode}")
    print(f"Candidates considered: {summary.candidates_considered}")
    print(f"  Enriched:                 {summary.enriched}")
    print(f"  No new data available:    {summary.no_change}")
    print(f"  No longer exists (404):   {summary.not_found}")
    print(f"  Failed (isolated):        {summary.failed}")
    print(f"  Skipped (unsupported):    {summary.skipped_unsupported_marketplace}")

    if summary.per_field_fill_counts:
        print()
        verb = "would be filled" if summary.dry_run else "were filled"
        print(f"Fields that {verb} in:")
        for field_name, count in sorted(summary.per_field_fill_counts.items()):
            print(f"  {field_name}: {count}")

    if summary.skipped_marketplaces:
        print()
        print("Marketplaces skipped entirely:")
        for marketplace, reason in sorted(summary.skipped_marketplaces.items()):
            print(f"  {marketplace}: {reason}")

    failed_rows = [row for row in summary.rows if row.status == "failed"]
    if failed_rows:
        print()
        print("Failed rows:")
        for row in failed_rows:
            print(f"  [{row.row_id}] {row.marketplace}/{row.external_listing_id}: {row.error}")


def main() -> int:
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        # Windows consoles often default to a legacy codepage that can't
        # encode every character a real marketplace listing title
        # contains - replace rather than crash mid-report.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--marketplace",
        default=None,
        help="Only backfill listings from this marketplace id (e.g. 'ebay'). Default: every marketplace.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=settings.listing_backfill_batch_size,
        help=f"Max rows to process in this run (default: {settings.listing_backfill_batch_size}).",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=settings.listing_backfill_delay_ms,
        help=f"Milliseconds to wait between consecutive marketplace requests (default: {settings.listing_backfill_delay_ms}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be enriched without writing anything. This is also the default with no flags at all.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the enriched fields and commit. Without this flag, the run is always a dry run.",
    )
    args = parser.parse_args()

    if args.marketplace is not None and not is_marketplace_supported(args.marketplace):
        print(f"Unknown marketplace {args.marketplace!r} - not a registered connector.", file=sys.stderr)
        return 2
    if args.limit <= 0:
        print("--limit must be a positive integer.", file=sys.stderr)
        return 2

    dry_run = not args.apply  # --apply is the only way to get a real run; --dry-run is accepted but redundant

    session = SessionLocal()
    try:
        summary = run_backfill(
            session,
            resolve_connector=get_connector,
            is_marketplace_supported=is_marketplace_supported,
            marketplace=args.marketplace,
            limit=args.limit,
            dry_run=dry_run,
            delay_seconds=args.delay_ms / 1000,
        )
        _print_report(summary)
        print()
        if dry_run:
            print("Dry run only - nothing was written. Re-run with --apply to actually enrich these rows.")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
