/**
 * Presentational marketplace display names - mirrors the backend's
 * `connectors/registry.py:display_name_for()` exactly (same ids, same
 * casing, same title-case fallback for anything not explicitly listed).
 * Kept as a small, separately-maintained mirror rather than a shared
 * import, since Python and TypeScript can't literally share a function -
 * this is the *one* place in this app that mapping is defined; every
 * screen showing a marketplace name must call `displayNameForMarketplace`
 * rather than rendering a raw id (`"ebay"`) or deriving its own casing.
 *
 * Where a live `GET /api/v1/marketplaces` response is already in hand
 * (e.g. CreateSearchScreen's marketplace picker), prefer its own `name`
 * field instead - that's the backend's live, authoritative value. This
 * module exists for the many places (ListingCard, SavedSearchCard, ...)
 * that only ever have a marketplace *id* on hand, not the full API
 * response, and would otherwise have no display name available at all.
 */
const MARKETPLACE_DISPLAY_NAMES: Record<string, string> = {
  ebay: 'eBay',
  etsy: 'Etsy',
  mock: 'Mock',
  reverb: 'Reverb',
  bonanza: 'Bonanza',
};

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

/** e.g. "ebay" -> "eBay". Falls back to title-casing an unrecognized id, same as the backend. */
export function displayNameForMarketplace(marketplaceId: string): string {
  return MARKETPLACE_DISPLAY_NAMES[marketplaceId] ?? titleCase(marketplaceId);
}
