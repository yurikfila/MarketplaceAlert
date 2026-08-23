"""Raw persistence access for discovered listings.

Only this module (plus ``database.py`` and ``models.py``) knows any SQL/ORM
details. Everything above it - the service layer, routes - works with
``Listing`` and plain Python types only. See ``ARCHITECTURE.md``.
"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from marketplace_alert.core.models.listing import Listing
from marketplace_alert.core.persistence.models import BACKFILL_STATUS_FAILED, DiscoveredListing

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

        Backs `GET /api/v1/listings` - see that route's docstring for the
        exact meaning of each filter/sort mode, and for what's
        intentionally NOT offered (`only_new`) and why.
        """
        stmt = self._apply_filters(
            select(DiscoveredListing),
            marketplace=marketplace,
            marketplaces=marketplaces,
            saved_search_id=saved_search_id,
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
            saved_search_id=saved_search_id,
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

    def _apply_filters(
        self,
        stmt: Select,
        *,
        marketplace: str | None,
        marketplaces: list[str] | None,
        saved_search_id: int | None,
        min_price: float | None,
        max_price: float | None,
        currency: str | None,
        condition: str | None,
        location: str | None,
        discovered_after: datetime | None,
        discovered_before: datetime | None,
        new_since: datetime | None,
    ) -> Select:
        """The one place every `GET /api/v1/listings` filter becomes a
        SQL predicate - shared by `list_recent` and `count` so pagination
        metadata can never drift out of sync with what a page actually
        contains.

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
        if saved_search_id is not None:
            stmt = stmt.where(DiscoveredListing.discovered_by_saved_search_id == saved_search_id)
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
