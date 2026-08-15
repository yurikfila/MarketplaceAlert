"""SQLAlchemy models for saved searches.

Distinct from `marketplace_alert.core.persistence.models.DiscoveredListing`
(a listing we've seen): `SavedSearch` is a *search definition* - what to
search for, where, and how often - that the background scanner reads to
decide what to scan next.

One saved search can target multiple marketplaces (`SavedSearchMarketplace`,
a normalized join table - never a comma-separated string). See
ARCHITECTURE.md "Multi-marketplace saved searches".
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from marketplace_alert.core.persistence.database import Base


class SavedSearch(Base):
    """A recurring search definition: query + marketplaces + interval + on/off."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Bumped explicitly by the repository's update() - only on definition
    # edits (query/marketplaces/interval/is_active), never on a scan run.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Set the first time this saved search is scanned; None until then.
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Raw association rows - repository.py is the only place that touches
    # this list directly. Everything above it reads `.marketplaces` (a
    # plain list[str]) instead. `delete-orphan` means deleting a
    # SavedSearch, or replacing this list, safely cleans up the old rows.
    marketplace_links: Mapped[list["SavedSearchMarketplace"]] = relationship(
        back_populates="saved_search",
        cascade="all, delete-orphan",
        order_by="SavedSearchMarketplace.marketplace_name",
    )

    @property
    def marketplaces(self) -> list[str]:
        """Marketplace names this saved search targets, sorted for stable display."""
        return [link.marketplace_name for link in self.marketplace_links]


class SavedSearchMarketplace(Base):
    """One marketplace selection for a saved search - a normalized join row,
    never a comma-separated string on `SavedSearch`."""

    __tablename__ = "saved_search_marketplaces"
    __table_args__ = (
        UniqueConstraint("saved_search_id", "marketplace_name", name="uq_saved_search_marketplace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    saved_search_id: Mapped[int] = mapped_column(
        ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False
    )
    marketplace_name: Mapped[str] = mapped_column(String, nullable=False, index=True)

    saved_search: Mapped["SavedSearch"] = relationship(back_populates="marketplace_links")
