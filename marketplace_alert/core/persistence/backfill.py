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
`NotificationService` - it only ever updates specific columns on an
*existing* row, found via a direct read, never through the
new-vs-already-seen classification path. There is no code path here that
can trigger a Telegram notification. `marketplace`, `external_listing_id`,
`first_discovered_at`, `last_seen_at`, and `discovered_by_saved_search_id`
are never touched - see `_apply_enrichment()`'s explicit field allowlist.

**Never overwrites a present value, and never writes a null over
anything.** For each candidate field, a freshly-fetched value is only
applied if the *existing* row's value for that field is currently
`None`. A stale-but-present value is left exactly as it was - this is a
"fill in what's missing" operation, not a "keep in sync" one.

**Persistent backfill state (`metadata_backfill_status`/
`metadata_backfill_attempted_at`, added to fix a real production bug -
see PROJECT_CONTEXT.md decision #23).** The original design relied
purely on "does this row still have a `NULL` enrichable field" for
resumability - which broke in production: a row missing only a field its
marketplace genuinely never provides (e.g. Etsy's permanently-`null`
`condition`) matched that query forever. Since candidates are always
selected newest-first up to `--limit`, a batch of these permanently-stuck
recent rows could occupy every candidate slot in every run, starving
older, genuinely-enrichable rows from ever being reached - exactly the
behavior observed in production (a `--limit 25` run repeatedly
re-selecting the same ~25 rows while later rows were never touched).

`metadata_backfill_status` fixes this by giving each row an explicit,
persisted outcome once it's genuinely been dealt with:

- **Terminal** (`core/persistence/models.py`'s `BACKFILL_TERMINAL_STATUSES`
  - a row in one of these states is never selected as a candidate again):
  - `enriched` - the lookup filled in at least one field. **This is
    terminal even if other fields remain `None`** because the
    marketplace itself doesn't provide them for this listing - a
    listing is "done" for this backfill generation the moment an
    authoritative lookup has been applied, not only once every field
    happens to be non-`None` (see `_apply_enrichment`'s docstring for
    why chasing 100%-non-null completeness is the wrong goal).
  - `no_data` - the lookup succeeded but had nothing new to add.
  - `not_found` - the marketplace confirmed the listing no longer exists.
  - `unsupported` - this marketplace has no connector lookup capability
    at all (a structural fact - `ListingLookupNotSupportedError`, or no
    registered connector).
- **Retryable** (stays eligible for a future run, same as a row that's
  never been attempted at all): `failed` - a transient failure (timeout,
  429, 5xx, malformed response). Persisted for observability
  (`metadata_backfill_attempted_at` records when), but deliberately
  *not* excluded from future candidate selection - a transient failure
  says nothing about whether a retry would succeed.
- **Never persisted at all** (row stays completely untouched, exactly as
  before this run): a marketplace that's merely *unconfigured* right now
  (missing credentials) - an operational condition, not a fact about the
  row, that can change without a data migration (an operator adding the
  missing credential). See `_STRUCTURAL_SKIP_REASONS` below for exactly
  which marketplace-level skip reasons *do* count as structural
  (persisted) vs. operational (not persisted).

**Authentication failures get a per-run circuit breaker, not per-row
retries.** A `MarketplaceAuthError` (401/403 - a *configured* credential
the marketplace itself rejected) almost certainly means every other
candidate row for that same marketplace would fail identically within
the same run - repeating the same doomed request for every remaining
candidate would be a real request-storm risk, not a legitimate retry
attempt. The first row that actually hits this is recorded as a genuine
(retryable) `failed` attempt; every other candidate for that marketplace
already queued in *this* run is skipped immediately, with **no request
and no persisted state change** - they're retried fresh (a real request)
on the next run, same as if this run had never touched them.

**Safety properties, all deliberate:**
- **Bounded**: every run processes at most `limit` rows.
- **Resumable / idempotent**: primarily via `metadata_backfill_status`
  (see above) - a terminal row never becomes a candidate again, and a
  fresh/retryable row is naturally picked up again on the next run.
- **Marketplace-isolated**: a marketplace with no connector support or
  missing credentials is detected once, up front, and every one of its
  candidate rows is skipped with a clear logged reason - never
  wastefully retried row by row.
- **Failure-isolated**: one row's connector failure (timeout, 429, 500,
  malformed response) is logged and skipped; it never stops the rest of
  the batch or affects other marketplaces.
- **Rate-limit aware**: a configurable delay is inserted between
  consecutive *actual* requests (never before the very first one, and
  never for a circuit-breaker-skipped row, since no request is made).
- **Committed incrementally, one row at a time** - never one large
  transaction for an entire run, so a crash partway through leaves
  whatever was already successfully processed intact rather than rolling
  it all back.
- **`core/` stays connector-agnostic** (see PROJECT_CONTEXT.md decision
  #6): this module never imports `connectors/registry.py` - the caller
  (the CLI script) injects `resolve_connector`/`is_marketplace_supported`.

**Dry-run never writes anything** - not an enriched field, not
`metadata_backfill_status`, not `metadata_backfill_attempted_at`. See
`_persist_status()`, the single place every status write happens, for
where that's enforced.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from marketplace_alert.core.connectors.base import (
    ListingLookupNotSupportedError,
    MarketplaceAuthError,
    MarketplaceConnector,
    MarketplaceConnectorError,
)
from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import (
    BACKFILL_STATUS_ENRICHED,
    BACKFILL_STATUS_FAILED,
    BACKFILL_STATUS_NO_DATA,
    BACKFILL_STATUS_NOT_FOUND,
    BACKFILL_STATUS_UNSUPPORTED,
    DiscoveredListing,
)
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

# `_resolve_backfill_connector`'s two possible "skip this whole
# marketplace" reasons that describe a structural fact (this codebase
# has no way to look this marketplace up at all, full stop) rather than
# a temporary operational one - see `_STRUCTURAL_SKIP_REASONS` below for
# why that distinction controls whether a row's skip is persisted.
_SKIP_REASON_NO_CONNECTOR = "no registered connector"
_SKIP_REASON_NOT_SUPPORTED = "connector has no documented single-item lookup endpoint"
_SKIP_REASON_NOT_CONFIGURED = "marketplace not configured (missing credentials)"

# Structural skip reasons are persisted per-row as the terminal
# `unsupported` status (never selected as a candidate again - a code
# change would be needed to ever make this marketplace lookupable).
# `_SKIP_REASON_NOT_CONFIGURED` is deliberately excluded: missing
# credentials are an operational condition an operator can fix at any
# time without a data migration, so those rows are left completely
# untouched and simply retried (still "pending") whenever the
# marketplace becomes configured - see module docstring.
_STRUCTURAL_SKIP_REASONS = frozenset({_SKIP_REASON_NO_CONNECTOR, _SKIP_REASON_NOT_SUPPORTED})


@dataclass
class BackfillRowOutcome:
    row_id: int
    marketplace: str
    external_listing_id: str
    status: str  # "enriched" | "no_data" | "not_found" | "unsupported" | "skipped_unconfigured" | "failed"
    fields_filled: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class BackfillSummary:
    dry_run: bool
    candidates_considered: int = 0
    enriched: int = 0
    no_data: int = 0
    not_found: int = 0
    failed: int = 0
    skipped_unsupported: int = 0  # structural - persisted as a terminal "unsupported" status on apply
    skipped_unconfigured: int = 0  # operational - never persisted, row stays pending
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
        summary.skipped_marketplaces[marketplace] = _SKIP_REASON_NO_CONNECTOR
        return None

    connector = resolve_connector(marketplace)

    if not _connector_supports_lookup(connector):
        summary.skipped_marketplaces[marketplace] = _SKIP_REASON_NOT_SUPPORTED
        return None

    if not getattr(connector, "is_configured", True):
        summary.skipped_marketplaces[marketplace] = _SKIP_REASON_NOT_CONFIGURED
        return None

    return connector


def _apply_enrichment(row: DiscoveredListing, fresh: Listing, *, dry_run: bool) -> list[str]:
    """Fills only the candidate fields that are currently `None` on
    `row` with a non-`None` value from `fresh` - never overwrites a
    present value, never writes `None` over anything. In dry-run mode,
    `row` is never mutated; the return value alone describes what would
    have changed. Returns the list of field names actually (or, in
    dry-run mode, hypothetically) filled.

    An empty return does NOT mean nothing happened - it's the `no_data`
    outcome: the authoritative lookup succeeded, but every field it
    could offer was already present (or the marketplace itself has
    nothing further to give). A non-empty return is `enriched`, even if
    some `_ENRICHABLE_FIELDS` remain `None` afterward - see this
    module's docstring for why that's terminal rather than "still
    incomplete".
    """
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


def _persist_status(session: Session, row: DiscoveredListing, status: str, *, dry_run: bool) -> None:
    """Sets `metadata_backfill_status`/`metadata_backfill_attempted_at`
    and commits immediately - one row at a time, the same incremental-
    commit safety property field enrichment already relies on. A
    complete no-op in dry-run mode: never mutates `row`, never commits -
    the one place this module's "dry-run writes nothing" guarantee is
    enforced for status, so every call site can call this unconditionally
    rather than repeating an `if not dry_run` check."""
    if dry_run:
        return
    row.metadata_backfill_status = status
    row.metadata_backfill_attempted_at = datetime.now(timezone.utc)
    session.commit()


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
    database at all - not an enriched field, not a status, nothing. See
    this module's docstring for every other safety property (bounded,
    resumable, idempotent, marketplace/failure isolation, the
    authentication circuit breaker, rate-limit pacing, incremental
    commits).
    """
    summary = BackfillSummary(dry_run=dry_run)
    candidates = ListingRepository(session).list_missing_metadata(marketplace=marketplace, limit=limit)
    summary.candidates_considered = len(candidates)

    connector_cache: dict[str, MarketplaceConnector | None] = {}
    # Marketplaces where a real request already came back as an auth
    # failure earlier in *this* run - see module docstring. Reset on
    # every call (function-local), never persisted across runs.
    auth_failed_marketplaces: set[str] = set()
    made_a_request = False

    for row in candidates:
        mp = row.marketplace

        if mp in auth_failed_marketplaces:
            # Circuit breaker: skip without a request. Deliberately NOT
            # persisted (row stays exactly as it was) - this specific
            # row was never actually attempted, so it's retried with a
            # real request on the next run rather than being recorded as
            # having failed itself.
            summary.failed += 1
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id,
                    marketplace=mp,
                    external_listing_id=row.external_listing_id,
                    status="failed",
                    error="skipped: marketplace authentication failed earlier in this run",
                )
            )
            continue

        if mp not in connector_cache:
            connector_cache[mp] = _resolve_backfill_connector(
                mp,
                resolve_connector=resolve_connector,
                is_marketplace_supported=is_marketplace_supported,
                summary=summary,
            )
        connector = connector_cache[mp]

        if connector is None:
            reason = summary.skipped_marketplaces[mp]
            if reason in _STRUCTURAL_SKIP_REASONS:
                summary.skipped_unsupported += 1
                _persist_status(session, row, BACKFILL_STATUS_UNSUPPORTED, dry_run=dry_run)
                summary.rows.append(
                    BackfillRowOutcome(
                        row_id=row.id, marketplace=mp, external_listing_id=row.external_listing_id, status="unsupported"
                    )
                )
            else:
                summary.skipped_unconfigured += 1
                summary.rows.append(
                    BackfillRowOutcome(
                        row_id=row.id,
                        marketplace=mp,
                        external_listing_id=row.external_listing_id,
                        status="skipped_unconfigured",
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
            # above) - handled defensively the same way as a structural
            # "unsupported" skip, never left to propagate and abort the
            # whole run.
            summary.skipped_unsupported += 1
            _persist_status(session, row, BACKFILL_STATUS_UNSUPPORTED, dry_run=dry_run)
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id, marketplace=mp, external_listing_id=row.external_listing_id, status="unsupported"
                )
            )
            continue
        except MarketplaceAuthError as exc:
            logger.error(
                "Backfill: %s authentication failed - skipping remaining %s candidates this run",
                mp,
                mp,
            )
            auth_failed_marketplaces.add(mp)
            summary.failed += 1
            # This row itself WAS actually attempted (a real request was
            # made and failed) - persisted for observability, same as
            # any other retryable failure below.
            _persist_status(session, row, BACKFILL_STATUS_FAILED, dry_run=dry_run)
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
        except MarketplaceConnectorError as exc:
            logger.error("Backfill: %s/%s lookup failed: %s", mp, row.external_listing_id, exc)
            summary.failed += 1
            _persist_status(session, row, BACKFILL_STATUS_FAILED, dry_run=dry_run)
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
            _persist_status(session, row, BACKFILL_STATUS_FAILED, dry_run=dry_run)
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
            _persist_status(session, row, BACKFILL_STATUS_NOT_FOUND, dry_run=dry_run)
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id, marketplace=mp, external_listing_id=row.external_listing_id, status="not_found"
                )
            )
            continue

        fields_filled = _apply_enrichment(row, fresh, dry_run=dry_run)
        if fields_filled:
            summary.enriched += 1
            for field_name in fields_filled:
                summary.per_field_fill_counts[field_name] = (
                    summary.per_field_fill_counts.get(field_name, 0) + 1
                )
            _persist_status(session, row, BACKFILL_STATUS_ENRICHED, dry_run=dry_run)
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
            summary.no_data += 1
            _persist_status(session, row, BACKFILL_STATUS_NO_DATA, dry_run=dry_run)
            summary.rows.append(
                BackfillRowOutcome(
                    row_id=row.id, marketplace=mp, external_listing_id=row.external_listing_id, status="no_data"
                )
            )

    logger.info(
        "Backfill %s: %d candidate(s), %d enriched, %d no data, %d not found, "
        "%d failed, %d skipped (unsupported), %d skipped (unconfigured)",
        "dry-run" if dry_run else "run",
        summary.candidates_considered,
        summary.enriched,
        summary.no_data,
        summary.not_found,
        summary.failed,
        summary.skipped_unsupported,
        summary.skipped_unconfigured,
    )
    return summary


def reset_backfill_status(
    session: Session, *, statuses: list[str], marketplace: str | None = None, dry_run: bool = True
) -> int:
    """The explicit, deliberate way to make terminal rows backfill
    candidates again (e.g. after improving a connector's extraction, or
    to retry `no_data` rows once a marketplace adds a field it didn't
    used to expose) - never called automatically by `run_backfill`
    itself. Returns how many rows are/were reset.

    `dry_run=True` (the default) only counts matching rows, without
    writing anything - same "always see a report before mutating
    production data" rule as `run_backfill`.
    """
    repo = ListingRepository(session)
    if dry_run:
        return sum(
            1
            for row in repo.list_all()
            if row.metadata_backfill_status in statuses and (marketplace is None or row.marketplace == marketplace)
        )
    count = repo.reset_backfill_status(statuses=statuses, marketplace=marketplace)
    session.commit()
    return count
