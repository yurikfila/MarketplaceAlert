"""One-time (and safely re-runnable) cutover: create a `ListingAttribution`
row for every pre-existing (saved search, listing) relationship that's
already known via `DiscoveredListing.discovered_by_saved_search_id`, so
Phase 1 of multi-user listing attribution has correct historical data to
build on, not just data going forward.

Pure logic only - no `argparse`, no `print`. The CLI wrapper
(`scripts/backfill_listing_attributions.py`) owns all of that; this module
is a plain function a test can call directly against a session, matching
this codebase's established split (see `core/auth/bootstrap.py` and
`core/notifications/preference_backfill.py`, and their own CLI wrappers).

**Never infers ownership.** The only source of truth this reads is
`discovered_by_saved_search_id` - a value some earlier scan already wrote
with full confidence (it's the search that genuinely ran and genuinely
found this listing first). For every row where that column `IS NOT NULL`,
copying it into a `ListingAttribution` is not a guess - it's a faithful
restatement of an already-trusted fact. For every row where it `IS NULL`
(pre-cutover history, or a listing discovered via the legacy `/scan`
endpoint, never tied to any saved search), this backfill does **nothing**
- never guesses based on title, query text, or relevance, and never
invents an owner. Those rows simply remain unattributed, exactly as
correct today as they'll be after this backfill runs.

**Idempotent.** A (saved_search_id, discovered_listing_id) pair that
already has a `ListingAttribution` row - from a previous run of this
script, or because a live scan already created it going forward - is left
untouched; re-running finds nothing left to do for it.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from marketplace_alert.core.persistence.listing_attribution_repository import ListingAttributionRepository
from marketplace_alert.core.persistence.repository import ListingRepository


@dataclass
class ListingAttributionBackfillReport:
    """The complete result of one `run_listing_attribution_backfill` call -
    enough for the CLI wrapper to print a report in both dry-run and apply
    modes."""

    total_discovered_listings: int
    candidates_with_known_attribution: int
    already_attributed_count: int
    would_create_count: int
    created_count: int
    skipped_null_attribution_count: int
    applied: bool


def run_listing_attribution_backfill(session: Session, *, apply: bool) -> ListingAttributionBackfillReport:
    """For every `DiscoveredListing` row with a non-`NULL` `discovered_by_
    saved_search_id`, create the corresponding `ListingAttribution` if one
    doesn't already exist. Rows with `discovered_by_saved_search_id IS
    NULL` are counted (`skipped_null_attribution_count`) but never
    touched.

    Commits only when `apply=True` and at least one row was actually
    created - a dry run, or an apply that finds everything already
    attributed, never opens a write transaction at all.
    """
    all_listings = ListingRepository(session).list_all()
    attributions = ListingAttributionRepository(session)

    candidates = [row for row in all_listings if row.discovered_by_saved_search_id is not None]
    skipped_null = len(all_listings) - len(candidates)

    already_attributed = 0
    created = 0
    for row in candidates:
        existing = attributions.get(
            saved_search_id=row.discovered_by_saved_search_id, discovered_listing_id=row.id
        )
        if existing is not None:
            already_attributed += 1
            continue

        if apply:
            attributions.record_if_missing(
                saved_search_id=row.discovered_by_saved_search_id, discovered_listing_id=row.id
            )
            created += 1

    if apply and created:
        session.commit()

    would_create = len(candidates) - already_attributed
    return ListingAttributionBackfillReport(
        total_discovered_listings=len(all_listings),
        candidates_with_known_attribution=len(candidates),
        already_attributed_count=already_attributed,
        would_create_count=would_create,
        created_count=created if apply else 0,
        skipped_null_attribution_count=skipped_null,
        applied=apply and created > 0,
    )
