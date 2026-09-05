"""Raw persistence access for discovered listings.

Only this module (plus ``database.py`` and ``models.py``) knows any SQL/ORM
details. Everything above it - the service layer, routes - works with
``Listing`` and plain Python types only. See ``ARCHITECTURE.md``.
"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import BACKFILL_STATUS_FAILED, DiscoveredListing, ListingAttribution
from marketplace_alert.core.saved_searches.models import SavedSearch

ListingSort = Literal["newest", "oldest", "price_asc", "price_desc"]


class ListingRepository:
    """Persistence operations for ``DiscoveredListing`` rows, scoped to one session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, marketplace: str, external_listing_id: str) -> DiscoveredListing | None:
        """Return the stored row for this listing, or None if never seen before."""
        stmt = select(DiscoveredListing).where(
            DiscoveredListing.marketplace == marketplace,
            DiscoveredListing.external_listing_id == external_listing_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def save_new(self, listing: Listing, *, saved_search_id: int | None = None) -> DiscoveredListing:
        """Persist a listing seen for the first time.

        Every product-display field a connector filled in on `listing`
        (price/currency/location/seller/condition/image_url/created_at)
        is captured now, once - see `DiscoveredListing`'s docstring for
        why `touch_last_seen` deliberately never refreshes these later.
        `saved_search_id` records which saved search's scan first found
        this row (`None` for the legacy `/scan` endpoint, which isn't
        tied to any saved search) - see `DiscoveredListing
        .discovered_by_saved_search_id`'s docstring for exactly what this
        does and doesn't mean.
        """
        now = datetime.now(timezone.utc)
        row = DiscoveredListing(
            marketplace=listing.marketplace,
            external_listing_id=listing.external_listing_id,
            title=listing.title,
            listing_url=str(listing.listing_url),
            price=listing.price,
            currency=listing.currency,
            location=listing.location,
            seller=listing.seller,
            condition=listing.condition,
            image_url=str(listing.image_url) if listing.image_url is not None else None,
            source_created_at=listing.created_at,
            discovered_by_saved_search_id=saved_search_id,
            first_discovered_at=now,
            last_seen_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_or_create(
        self, listing: Listing, *, saved_search_id: int | None = None
    ) -> tuple[DiscoveredListing, bool]:
        """Race-safe version of "check then `save_new`": returns
        `(row, created)`, where `created` is `True` only if this call is
        the one that actually inserted the row.

        **The concurrency fix.** Two different saved searches (owned by
        different users, or a scheduler tick racing a manual "run now"
        request - `SavedSearchRunGuard` only prevents the *same* saved
        search from overlapping itself, never two different ones) can
        both see "not found yet" for a brand-new listing and both attempt
        to insert it. Before this method existed, the second insert would
        raise an unhandled `IntegrityError` on `discovered_listings`'
        `UNIQUE(marketplace, external_listing_id)` constraint straight out
        of `ListingDiscoveryService.process_listings()`, crashing that
        scan.

        The fix is a standard SAVEPOINT-protected optimistic insert: the
        insert attempt runs inside `session.begin_nested()`, so if it
        collides with a concurrent committer, only that one SAVEPOINT is
        rolled back - never the caller's whole transaction (which, mid
        `process_listings()` loop, may already hold other successfully
        inserted rows from earlier in the same batch that must not be
        discarded). On `IntegrityError`, the losing side simply re-reads
        the row the winner just committed and returns that instead of
        raising - both callers converge on the exact same canonical row,
        and neither sees an error.
        """
        existing = self.get(listing.marketplace, listing.external_listing_id)
        if existing is not None:
            return existing, False

        try:
            with self._session.begin_nested():
                row = self.save_new(listing, saved_search_id=saved_search_id)
            return row, True
        except IntegrityError:
            existing = self.get(listing.marketplace, listing.external_listing_id)
            if existing is None:
                raise
            return existing, False

    def touch_last_seen(self, row: DiscoveredListing) -> None:
        """Update last_seen_at for a listing that showed up again."""
        row.last_seen_at = datetime.now(timezone.utc)
        self._session.add(row)

    def list_recent(
        self,
        *,
        limit: int,
        offset: int,
        marketplace: str | None = None,
        marketplaces: list[str] | None = None,
        saved_search_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        currency: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        discovered_after: datetime | None = None,
        discovered_before: datetime | None = None,
        new_since: datetime | None = None,
        sort: ListingSort = "newest",
    ) -> list[DiscoveredListing]:
        """Discovered listings matching every given filter, in `sort` order.

        Backs the legacy, unauthenticated dashboard `/listings` page only
        (`main.py`) - the authenticated `GET /api/v1/listings` route uses
        `list_recent_owned` below. `saved_search_id` here still means
        "first discovered by" (`discovered_by_saved_search_id`, unchanged) -
        this unscoped path was deliberately left alone by the Phase 1
        listing-attribution work; see `list_recent_owned`'s docstring for
        why that filter means something more precise there.
        """
        stmt = self._apply_filters(
            select(DiscoveredListing),
            marketplace=marketplace,
            marketplaces=marketplaces,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            condition=condition,
            location=location,
            discovered_after=discovered_after,
            discovered_before=discovered_before,
            new_since=new_since,
        )
        if saved_search_id is not None:
            stmt = stmt.where(DiscoveredListing.discovered_by_saved_search_id == saved_search_id)
        stmt = stmt.order_by(*self._sort_clause(sort)).limit(limit).offset(offset)
        return list(self._session.execute(stmt).scalars().all())

    def count(
        self,
        *,
        marketplace: str | None = None,
        marketplaces: list[str] | None = None,
        saved_search_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        currency: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        discovered_after: datetime | None = None,
        discovered_before: datetime | None = None,
        new_since: datetime | None = None,
    ) -> int:
        """Total rows matching the same filters `list_recent` would apply,
        ignoring limit/offset/sort - for pagination metadata that reflects
        the filtered set, not the whole table."""
        stmt = self._apply_filters(
            select(func.count()).select_from(DiscoveredListing),
            marketplace=marketplace,
            marketplaces=marketplaces,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            condition=condition,
            location=location,
            discovered_after=discovered_after,
            discovered_before=discovered_before,
            new_since=new_since,
        )
        if saved_search_id is not None:
            stmt = stmt.where(DiscoveredListing.discovered_by_saved_search_id == saved_search_id)
        return self._session.execute(stmt).scalar_one()

    def _apply_filters(
        self,
        stmt: Select,
        *,
        marketplace: str | None,
        marketplaces: list[str] | None,
        min_price: float | None,
        max_price: float | None,
        currency: str | None,
        condition: str | None,
        location: str | None,
        discovered_after: datetime | None,
        discovered_before: datetime | None,
        new_since: datetime | None,
    ) -> Select:
        """The one place every price/currency/condition/location/date/
        marketplace filter becomes a SQL predicate - shared by all four
        of `list_recent`/`count`/`list_recent_owned`/`count_owned` so
        none of them can drift out of sync with the others.

        **`saved_search_id` is deliberately NOT handled here** - unlike
        every other filter, what it means depends on which of the four
        callers is asking: `list_recent`/`count` (unscoped, legacy
        dashboard only) still filter on `discovered_by_saved_search_id`
        directly; `list_recent_owned`/`count_owned` filter through
        `ListingAttribution` instead (see that method's docstring for
        why - Phase 1 of multi-user listing attribution). Each caller
        applies its own `saved_search_id` predicate after calling this.

        `marketplace` (singular, exact match) and `marketplaces` (plural,
        "is one of") are two independent, both-optional filters on the
        same column - additive, backward-compatible: existing callers
        using `marketplace` alone are completely unaffected by
        `marketplaces` existing. A caller supplying both gets their
        intersection (AND, not a real scenario any current caller uses).

        `discovered_after` and `new_since` are two independent `>=`
        predicates on the same column, applied as separate `WHERE`
        clauses (SQL ANDs them together) rather than combined in Python -
        deliberately, so there's no naive-vs-timezone-aware datetime
        comparison to get wrong if a caller somehow supplies both.
        """
        if marketplace is not None:
            stmt = stmt.where(DiscoveredListing.marketplace == marketplace)
        if marketplaces:
            stmt = stmt.where(DiscoveredListing.marketplace.in_(marketplaces))
        if min_price is not None:
            stmt = stmt.where(DiscoveredListing.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(DiscoveredListing.price <= max_price)
        if currency is not None:
            stmt = stmt.where(func.upper(DiscoveredListing.currency) == currency.upper())
        if condition is not None:
            stmt = stmt.where(func.lower(DiscoveredListing.condition) == condition.lower())
        if location is not None:
            stmt = stmt.where(DiscoveredListing.location.ilike(f"%{location}%"))
        if discovered_after is not None:
            stmt = stmt.where(DiscoveredListing.first_discovered_at >= discovered_after)
        if discovered_before is not None:
            stmt = stmt.where(DiscoveredListing.first_discovered_at <= discovered_before)
        if new_since is not None:
            stmt = stmt.where(DiscoveredListing.first_discovered_at >= new_since)
        return stmt

    @staticmethod
    def _sort_clause(sort: ListingSort) -> tuple:
        """`id DESC`/`id ASC` is always the tiebreaker, for a stable order
        between rows that share an identical timestamp (or, for the price
        sorts, an identical price - including two NULLs).

        Price sorts put `NULL` prices last regardless of direction - a
        listing with an unknown price is neither the cheapest nor the
        most expensive result, so surfacing it at the *top* of either
        sort would be actively misleading; showing it last in both is the
        least surprising choice. Within each price tier, newest-first
        breaks ties instead of `id`, so "same price" still feels
        sensibly ordered rather than by an arbitrary internal id.
        """
        if sort == "oldest":
            return (DiscoveredListing.first_discovered_at.asc(), DiscoveredListing.id.asc())
        if sort == "price_asc":
            return (
                DiscoveredListing.price.asc().nulls_last(),
                DiscoveredListing.first_discovered_at.desc(),
                DiscoveredListing.id.desc(),
            )
        if sort == "price_desc":
            return (
                DiscoveredListing.price.desc().nulls_last(),
                DiscoveredListing.first_discovered_at.desc(),
                DiscoveredListing.id.desc(),
            )
        return (DiscoveredListing.first_discovered_at.desc(), DiscoveredListing.id.desc())  # "newest" (default)

    def list_missing_metadata(
        self, *, marketplace: str | None = None, limit: int
    ) -> list[DiscoveredListing]:
        """Backfill candidates: rows whose `metadata_backfill_status` is
        still pending (`NULL`) or retryable (`'failed'`), **and** that
        still have at least one enrichable field `NULL` - newest-
        discovered first, since a more recently found listing is more
        likely to still exist on the source marketplace than an older
        one, for the same total API budget.

        Backs the historical listing-metadata backfill service
        (`core/persistence/backfill.py`) only - never the normal scan/
        dedup path. **`metadata_backfill_status` is the primary gate**
        (a real, previously-shipped bug - see PROJECT_CONTEXT.md decision
        #23 - came from selecting on missing fields *alone*: a row
        missing only a field its marketplace never provides matched this
        query forever, permanently occupying candidate slots and
        starving genuinely-enrichable rows further back in the newest-
        first order). Status becomes terminal
        (`core/persistence/models.py`'s `BACKFILL_TERMINAL_STATUSES`) the
        moment a real backfill run determines there's nothing more to
        usefully attempt for a row - including a *partial* enrichment
        (some fields filled, others left `NULL` because the source
        genuinely doesn't provide them) - so this query naturally stops
        selecting a row once backfill has done everything it usefully
        can for it, not merely once every field happens to be non-`NULL`.
        The missing-field check stays as a secondary filter so a listing
        that already had every field populated at discovery time (e.g. a
        freshly-scanned eBay listing) is never selected at all - even
        though it's still nominally "pending" (never attempted) - since
        there would be nothing to gain from a lookup.
        """
        stmt = (
            select(DiscoveredListing)
            .where(
                or_(
                    DiscoveredListing.metadata_backfill_status.is_(None),
                    DiscoveredListing.metadata_backfill_status == BACKFILL_STATUS_FAILED,
                )
            )
            .where(
                or_(
                    DiscoveredListing.price.is_(None),
                    DiscoveredListing.currency.is_(None),
                    DiscoveredListing.image_url.is_(None),
                    DiscoveredListing.condition.is_(None),
                    DiscoveredListing.location.is_(None),
                    DiscoveredListing.seller.is_(None),
                    DiscoveredListing.source_created_at.is_(None),
                )
            )
        )
        if marketplace is not None:
            stmt = stmt.where(DiscoveredListing.marketplace == marketplace)
        stmt = stmt.order_by(
            DiscoveredListing.first_discovered_at.desc(), DiscoveredListing.id.desc()
        ).limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def reset_backfill_status(
        self, *, statuses: list[str], marketplace: str | None = None
    ) -> int:
        """Reset every row currently in one of `statuses` back to pending
        (`metadata_backfill_status` and `metadata_backfill_attempted_at`
        both set to `NULL`), so a future backfill run will reconsider
        them. Returns how many rows were reset.

        The explicit, deliberate retry mechanism for terminal rows (see
        `scripts/backfill_listing_metadata.py --reset-status`/
        `--retry-no-data`) - never called automatically by a normal
        backfill run. `statuses` is required (no "reset everything"
        default) so a caller must always say exactly which terminal
        state they intend to reopen.
        """
        stmt = select(DiscoveredListing).where(DiscoveredListing.metadata_backfill_status.in_(statuses))
        if marketplace is not None:
            stmt = stmt.where(DiscoveredListing.marketplace == marketplace)
        rows = list(self._session.execute(stmt).scalars().all())
        for row in rows:
            row.metadata_backfill_status = None
            row.metadata_backfill_attempted_at = None
        return len(rows)

    # --- Ownership-scoped variant - what the authenticated `GET
    # /api/v1/listings` route actually uses (see `api/v1/listings.py`).

    def _owned_exists_clause(self, *, user_id: int, saved_search_id: int | None):
        """`EXISTS(... ListingAttribution ... WHERE user owns it [and,
        if given, the attribution is specifically to `saved_search_id`])`
        - the ownership/filter predicate `list_recent_owned`/`count_owned`
        both apply to `DiscoveredListing`.

        `EXISTS`, not a `JOIN`, deliberately: a listing can now have more
        than one attribution (Phase 1 of multi-user listing attribution -
        the same user's two saved searches, or two different users',
        independently matching the same listing). A `JOIN` would return
        one result row per matching attribution, multiplying a listing
        with several attributions into several output rows; `EXISTS`
        never multiplies rows at all, so "does this user own at least one
        attribution for this listing" is answered without needing a
        `DISTINCT` (which would additionally need every `ORDER BY`
        expression to appear in the `SELECT` list on some backends) to
        undo it afterwards.

        When `saved_search_id` is given, a *second*, independent `EXISTS`
        re-verifies both "attributed to specifically this search" and
        "this search belongs to `user_id`" together - not just "attributed
        to this search" alone. This preserves the exact security property
        the old single-column `discovered_by_saved_search_id` join had:
        naming another user's `saved_search_id` must always yield zero
        rows for that listing via this clause, even if the current user
        separately owns a different, legitimate attribution for the same
        listing - the filter must narrow what's shown, never expand it to
        something the `saved_search_id` value itself doesn't actually
        authorize.
        """
        owns_any_attribution = (
            select(ListingAttribution.id)
            .join(SavedSearch, ListingAttribution.saved_search_id == SavedSearch.id)
            .where(
                ListingAttribution.discovered_listing_id == DiscoveredListing.id,
                SavedSearch.user_id == user_id,
            )
            .exists()
        )
        if saved_search_id is None:
            return owns_any_attribution

        owns_this_specific_search = (
            select(ListingAttribution.id)
            .join(SavedSearch, ListingAttribution.saved_search_id == SavedSearch.id)
            .where(
                ListingAttribution.discovered_listing_id == DiscoveredListing.id,
                ListingAttribution.saved_search_id == saved_search_id,
                SavedSearch.user_id == user_id,
            )
            .exists()
        )
        return owns_any_attribution & owns_this_specific_search

    def list_recent_owned(
        self,
        *,
        user_id: int,
        limit: int,
        offset: int,
        marketplace: str | None = None,
        marketplaces: list[str] | None = None,
        saved_search_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        currency: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        discovered_after: datetime | None = None,
        discovered_before: datetime | None = None,
        new_since: datetime | None = None,
        sort: ListingSort = "newest",
    ) -> list[DiscoveredListing]:
        """Listings this user owns at least one `ListingAttribution` for -
        see `_owned_exists_clause`'s docstring for the query shape and why
        it's `EXISTS`, not a `JOIN`. A listing this user's own two saved
        searches both independently matched appears exactly once (not
        twice) - `EXISTS` only ever asks "is there at least one", it never
        counts or multiplies.

        Takes the exact same optional filters as `list_recent` - reuses
        `_apply_filters()` for everything except `saved_search_id` (see
        that method's docstring), so a filter never has to be implemented
        twice or drift between the unscoped and owned query paths.
        """
        stmt = self._apply_filters(
            select(DiscoveredListing).where(self._owned_exists_clause(user_id=user_id, saved_search_id=saved_search_id)),
            marketplace=marketplace,
            marketplaces=marketplaces,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            condition=condition,
            location=location,
            discovered_after=discovered_after,
            discovered_before=discovered_before,
            new_since=new_since,
        )
        stmt = stmt.order_by(*self._sort_clause(sort)).limit(limit).offset(offset)
        return list(self._session.execute(stmt).scalars().all())

    def count_owned(
        self,
        *,
        user_id: int,
        marketplace: str | None = None,
        marketplaces: list[str] | None = None,
        saved_search_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        currency: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        discovered_after: datetime | None = None,
        discovered_before: datetime | None = None,
        new_since: datetime | None = None,
    ) -> int:
        """Total rows `list_recent_owned` would return across every page
        with the same filters applied, for pagination metadata - same
        `EXISTS`/filter, no limit/offset/sort. No `DISTINCT` needed here
        either, for the same reason `list_recent_owned` needs none - see
        `_owned_exists_clause`'s docstring."""
        stmt = self._apply_filters(
            select(func.count())
            .select_from(DiscoveredListing)
            .where(self._owned_exists_clause(user_id=user_id, saved_search_id=saved_search_id)),
            marketplace=marketplace,
            marketplaces=marketplaces,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            condition=condition,
            location=location,
            discovered_after=discovered_after,
            discovered_before=discovered_before,
            new_since=new_since,
        )
        return self._session.execute(stmt).scalar_one()

    def list_all(self) -> list[DiscoveredListing]:
        """Every discovered listing, unpaginated - for maintenance operations
        that need to inspect every row (e.g. `core/persistence/cleanup.py`'s
        historical relevance re-evaluation). Never for a normal request
        path - see `list_recent` for the paginated version those use."""
        stmt = select(DiscoveredListing).order_by(DiscoveredListing.id)
        return list(self._session.execute(stmt).scalars().all())

    def delete(self, row: DiscoveredListing) -> None:
        """Remove one discovered listing row. Used only by maintenance
        operations (e.g. historical relevance cleanup) - the normal scan/
        dedup path (`ListingDiscoveryService`) only ever adds or touches
        rows, never deletes one."""
        self._session.delete(row)
