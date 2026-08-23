"""Historical listing metadata backfill - re-fetches individual,
already-persisted listings from their source marketplace (by id, via
each connector's optional `get_listing_by_id()`) to fill in fields that
predate rich metadata persistence. See PROJECT_CONTEXT.md's Listing
Enrichment decision and CHANGELOG.md for the full background: rich
fields (price/currency/image_url/condition/location/seller/
source_created_at) are only ever captured going forward, at first-
discovery time - a listing persisted before that existed has no other
way to ever get them.

**The one property this module exists to guarantee, above everything
else: enriching an existing row can NEVER make it look like a newly
discovered listing.** This module never calls
`ListingDiscoveryService`/`ListingRepository.save_new()`/
`NotificationService` - it only ever updates specific, currently-`NULL`
columns on an *existing* row, found via a direct read, never through the
new-vs-already-seen classification path. There is no code path here that
can trigger a Telegram notification. `marketplace`, `external_listing_id`,
`first_discovered_at`, `last_seen_at`, and `discovered_by_saved_search_id`
are never touched - see `_apply_enrichment()`'s explicit field allowlist.

**Never overwrites a present value, and never writes a null over
anything.** For each candidate field, a freshly-fetched value is only
applied if the *existing* row's value for that field is currently
`None`. A stale-but-present value is left exactly as it was - this is a
"fill in what's missing" operation, not a "keep in sync" one (see
`DiscoveredListing`'s own docstring for why product fields are captured
once, not refreshed on every re-scan either).

**Safety properties, all deliberate:**
- **Bounded**: every run processes at most `limit` rows.
- **Resumable / idempotent**: candidates are simply "rows still missing
  at least one field" (`ListingRepository.list_missing_metadata()`) -
  once a row is fully enriched, it naturally stops matching, so
  re-running (with the same or a fresh `limit`) never re-touches
  already-enriched rows and safely continues where the last run left
  off, with no separate progress-tracking column needed. **One accepted
  edge case**: a row missing a field the source marketplace genuinely
  never provides for that listing (e.g. no creation timestamp at all)
  can never become fully complete, so it keeps matching future candidate
  queries indefinitely - each re-run correctly does no harm (no field is
  ever overwritten, nothing is ever written twice), just one wasted-but-
  bounded lookup call per run for that row. Adding a separate "already
  attempted, stop retrying" column was considered and deliberately not
  added - it would need its own migration and bookkeeping for a cost
  that's already bounded by `limit` and paced by `delay_seconds`, not an
  unbounded or accelerating one.
- **Marketplace-isolated**: a marketplace with no connector support
  (`ListingLookupNotSupportedError`) or missing credentials is detected
  once, up front, and every one of its candidate rows is skipped with a
  clear logged reason - never wastefully retried row by row.
- **Failure-isolated**: one row's connector failure (timeout, 429, 500,
  malformed response) is logged and skipped; it never stops the rest of
  the batch or affects other marketplaces.
- **Rate-limit aware**: a configurable delay is inserted between
  consecutive marketplace requests (never before the very first one -
  same convention as `NotificationService`'s send pacing).
- **Committed incrementally, one row at a time** - never one large
  transaction for an entire run, so a crash partway through leaves
  whatever was already successfully enriched intact rather than rolling
  it all back.
- **`core/` stays connector-agnostic** (see PROJECT_CONTEXT.md decision
  #6): this module never imports `connectors/registry.py` - the caller
  (the CLI script) injects `resolve_connector`/`is_marketplace_supported`.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from marketplace_alert.core.connectors.base import (
    ListingLookupNotSupportedError,
    MarketplaceConnector,
    MarketplaceConnectorError,
)
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import DiscoveredListing
from marketplace_alert.core.persistence.repository import ListingRepository

# `DiscoveredListing.discovered_by_saved_search_id` is a lazy, string-form
# `ForeignKey("saved_searches.id", ...)`, resolved against SQLAlchemy's
# shared `Base.metadata` the first time a session actually flushes a
# change - not at class-definition time. The main app (`main.py`)
# transitively imports the saved-searches module chain already (it needs
# `SavedSearchRepository`/`SavedSearchService` etc.), so this never comes
# up there - but `scripts/backfill_listing_metadata.py` has a much
# narrower import surface and, without this line, would fail with
# `NoReferencedTableError` on its first real (non-dry-run) commit,
# despite every automated test passing (pytest's own test collection
# happens to import the full app elsewhere first, masking exactly this
# gap). Imported here, once, purely for the registration side effect -
# same pattern `alembic/env.py` already uses for the same reason.
import marketplace_alert.core.saved_searches.models  # noqa: F401,E402

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 25
DEFAULT_DELAY_SECONDS = 0.5

# The only columns backfill is ever allowed to write - deliberately not
# marketplace/external_listing_id/first_discovered_at/last_seen_at/
# discovered_by_saved_search_id (see module docstring).
_ENRICHABLE_FIELDS = (
    "price",
    "currency",
    "image_url",
    "condition",
    "location",
    "seller",
    "source_created_at",
)


@dataclass
class BackfillRowOutcome:
    row_id: int
    marketplace: str
    external_listing_id: str
    status: str  # "enriched" | "no_change" | "not_found" | "failed" | "skipped_unsupported"
    fields_filled: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class BackfillSummary:
    dry_run: bool
    candidates_considered: int = 0
    enriched: int = 0
    no_change: int = 0
    not_found: int = 0
    failed: int = 0
    skipped_unsupported_marketplace: int = 0
    per_field_fill_counts: dict[str, int] = field(default_factory=dict)
    skipped_marketplaces: dict[str, str] = field(default_factory=dict)
    rows: list[BackfillRowOutcome] = field(default_factory=list)


def _connector_supports_lookup(connector: MarketplaceConnector) -> bool:
    """True only if the connector's own class overrides
    `get_listing_by_id` - checked without calling it, so detecting "not
    supported" never costs a wasted API call or relies on catching the
    base class's exception as control flow."""
    return type(connector).get_listing_by_id is not MarketplaceConnector.get_listing_by_id


def _resolve_backfill_connector(
    marketplace: str,
    *,
    resolve_connector: Callable[[str], MarketplaceConnector],
    is_marketplace_supported: Callable[[str], bool],
    summary: BackfillSummary,
) -> MarketplaceConnector | None:
    """Returns the connector to use for this marketplace's candidates, or
    `None` if the whole marketplace must be skipped - recording exactly
    one clear reason in `summary.skipped_marketplaces` either way, so
    every row that gets skipped this way is accounted for without
    needing its own per-row log line."""
    if not is_marketplace_supported(marketplace):
        summary.skipped_marketplaces[marketplace] = "no registered connector"
        return None

    connector = resolve_connector(marketplace)

    if not _connector_supports_lookup(connector):
        summary.skipped_marketplaces[marketplace] = (
            "connector has no documented single-item lookup endpoint"
        )
        return None

    if not getattr(connector, "is_configured", True):
        summary.skipped_marketplaces[marketplace] = "marketplace not configured (missing credentials)"
        return None

    return connector


def _apply_enrichment(row: DiscoveredListing, fresh: Listing, *, dry_run: bool) -> list[str]:
    """Fills only the candidate fields that are currently `None` on
    `row` with a non-`None` value from `fresh` - never overwrites a
    present value, never writes `None` over anything. In dry-run mode,
    `row` is never mutated; the return value alone describes what would
    have changed. Returns the list of field names actually (or, in
    dry-run mode, hypothetically) filled."""
    candidate_values = {
        "price": fresh.price,
        "currency": fresh.currency,
        "image_url": str(fresh.image_url) if fresh.image_url is not None else None,
        "condition": fresh.condition,
        "location": fresh.location,
        "seller": fresh.seller,
        "source_created_at": fresh.created_at,
    }
    filled: list[str] = []
    for field_name in _ENRICHABLE_FIELDS:
        new_value = candidate_values[field_name]
        if getattr(row, field_name) is None and new_value is not None:
            filled.append(field_name)
            if not dry_run:
                setattr(row, field_name, new_value)
    return filled


def run_backfill(
    session: Session,
    *,
    resolve_connector: Callable[[str], MarketplaceConnector],
    is_marketplace_supported: Callable[[str], bool],
    marketplace: str | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = True,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
) -> BackfillSummary:
    """Enrich up to `limit` existing listings still missing metadata.

    `dry_run=True` (the default - callers must opt into real writes)
    computes and reports exactly what would change without touching the
    database at all. See this module's docstring for every other safety
    property (bounded, resumable, idempotent, marketplace/failure
    isolation, rate-limit pacing, incremental commits).
    """
    summary = BackfillSummary(dry_run=dry_run)
    candidates = ListingRepository(session).list_missing_metadata(marketplace=marketplace, limit=limit)
    summary.candidates_considered = len(candidates)

    connector_cache: dict[str, MarketplaceConnector | None] = {}
    made_a_request = False

    for row in candidates:
        mp = row.marketplace

        if mp not in connector_cache:
            connector_cache[mp] = _resolve_backfill_connector(
                mp,
                resolve_connector=resolve_connector,
                is_marketplace_supported=is_marketplace_supported,
                summary=summary,
            )
        connector = connector_cache[mp]

        if connector is None:
            summary.skipped_unsupported_marketplace += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="skipped_unsupported",
                )
            )
            continue

        if made_a_request and delay_seconds > 0:
            time.sleep(delay_seconds)
        made_a_request = True

        try:
            fresh = connector.get_listing_by_id(row.external_listing_id)
        except ListingLookupNotSupportedError:
            # Shouldn't happen (already filtered by _connector_supports_lookup
            # above) - handled defensively the same way as a real failure,
            # never left to propagate and abort the whole run.
            summary.skipped_unsupported_marketplace += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="skipped_unsupported",
                )
            )
            continue
        except MarketplaceConnectorError as exc:
            logger.error("Backfill: %s/%s lookup failed: %s", mp, row.external_listing_id, exc)
            summary.failed += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="failed",
                    error=str(exc),
                )
            )
            continue
        except Exception:
            logger.exception("Backfill: %s/%s lookup failed unexpectedly", mp, row.external_listing_id)
            summary.failed += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="failed",
                    error="unexpected error",
                )
            )
            continue

        if fresh is None:
            logger.info("Backfill: %s/%s no longer exists on the marketplace", mp, row.external_listing_id)
            summary.not_found += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="not_found",
                )
            )
            continue

        fields_filled = _apply_enrichment(row, fresh, dry_run=dry_run)
        if fields_filled:
            if not dry_run:
                session.commit()
            summary.enriched += 1
            for field_name in fields_filled:
                summary.per_field_fill_counts[field_name] = (
                    summary.per_field_fill_counts.get(field_name, 0) + 1
                )
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="enriched",
                    fields_filled=fields_filled,
                )
            )
        else:
            summary.no_change += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="no_change",
                )
            )

    logger.info(
        "Backfill %s: %d candidate(s), %d enriched, %d unchanged, %d not found, "
        "%d failed, %d skipped (unsupported marketplace)",
        "dry-run" if dry_run else "run",
        summary.candidates_considered,
        summary.enriched,
        summary.no_change,
        summary.not_found,
        summary.failed,
        summary.skipped_unsupported_marketplace,
    )
    return summary
