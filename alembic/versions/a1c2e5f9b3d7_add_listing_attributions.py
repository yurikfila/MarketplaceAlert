"""add listing attributions

Revision ID: a1c2e5f9b3d7
Revises: 5faab97d82e8
Create Date: 2026-09-04 00:00:00.000000

Multi-user listing attribution, Phase 1 (see `core/persistence/models.py`'s
`ListingAttribution` for the full design/reasoning). Adds one brand-new
table, `listing_attributions` - a many-to-many join between `saved_searches`
and `discovered_listings`, so every search that genuinely matches a
listing can be recorded independently, not just whichever search
discovered it first.

Purely additive - no existing table dropped, no existing column altered,
no data touched by this migration itself. `discovered_listings.discovered_
by_saved_search_id` is untouched and remains the historical "first
discovered by" fact (see that column's own docstring) - this migration
does not remove or repurpose it. Every existing (saved_search, listing)
pair that's already known via that column can be safely backfilled into
this new table afterwards (see `scripts/backfill_listing_attributions.py`)
- a separate, explicit, dry-run-by-default step, never run automatically
by this migration or at startup.

A brand-new table with inline foreign keys is a single, portable
`CREATE TABLE` on every backend this project targets (SQLite included) -
no batch mode needed, matching `5faab97d82e8`'s (`add notification
preferences`) exact precedent for its own new table.

Verified via the usual upgrade/downgrade/re-upgrade cycle against a
throwaway SQLite database before finalizing, and cross-checked against
`Base.metadata.create_all()` to confirm this produces exactly one
non-unique index on `discovered_listing_id` and one genuine multi-column
`UNIQUE` table constraint on `(saved_search_id, discovered_listing_id)` -
not a separate redundant index for either.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c2e5f9b3d7'
down_revision: Union[str, Sequence[str], None] = '5faab97d82e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('listing_attributions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('saved_search_id', sa.Integer(), nullable=False),
    sa.Column('discovered_listing_id', sa.Integer(), nullable=False),
    sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['discovered_listing_id'], ['discovered_listings.id'], name='fk_listing_attributions_discovered_listing_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['saved_search_id'], ['saved_searches.id'], name='fk_listing_attributions_saved_search_id', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('saved_search_id', 'discovered_listing_id', name='uq_listing_attribution_saved_search_listing')
    )
    op.create_index(op.f('ix_listing_attributions_discovered_listing_id'), 'listing_attributions', ['discovered_listing_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_listing_attributions_discovered_listing_id'), table_name='listing_attributions')
    op.drop_table('listing_attributions')
