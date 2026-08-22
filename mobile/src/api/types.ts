/**
 * TypeScript types mirroring the backend's `/api/v1` Pydantic schemas
 * (marketplace_alert/api/v1/schemas.py) exactly - field names and
 * optionality kept in sync by hand, since the two codebases don't share a
 * schema generator (yet). Timestamps are ISO 8601 strings (as sent over
 * JSON), not `Date` objects - format them for display with
 * src/utils/format.ts rather than parsing at the API layer.
 */

export interface MobileStatus {
  status: string;
  backend: boolean;
  database: boolean;
  telegram_configured: boolean;
  supported_marketplaces: string[];
}

export interface MarketplaceInfo {
  id: string;
  name: string;
  configured: boolean;
  available: boolean;
}

export interface SavedSearch {
  id: number;
  query: string;
  marketplaces: string[];
  is_active: boolean;
  scan_interval_seconds: number;
  created_at: string;
  updated_at: string;
  last_scanned_at: string | null;
}

/** POST /api/v1/saved-searches body. */
export interface SavedSearchCreateInput {
  query: string;
  marketplaces: string[];
  scan_interval_seconds: number;
  is_active: boolean;
}

/** PATCH /api/v1/saved-searches/{id} body - only send fields you want changed. */
export interface SavedSearchUpdateInput {
  query?: string;
  marketplaces?: string[];
  scan_interval_seconds?: number;
  is_active?: boolean;
}

export interface MarketplaceRunOutcome {
  new_count: number;
  already_seen_count: number;
  error: string | null;
}

/** POST /api/v1/saved-searches/{id}/run response. */
export interface SavedSearchRunResult {
  saved_search_id: number;
  query: string;
  marketplaces: Record<string, MarketplaceRunOutcome>;
  total_new_count: number;
  total_already_seen_count: number;
}

/**
 * One row from GET /api/v1/listings. Every field below reflects whatever
 * the connector that discovered it actually returned - genuinely `null`
 * when a marketplace/connector didn't provide it for that listing (never
 * invented). `saved_search_id` is the saved search whose scan *first*
 * discovered this row - `null` for a listing discovered before that
 * attribution existed, or found through a path not tied to any single
 * saved search - and is a "first discovered by" attribution, not an
 * exclusive-ownership relationship (the same listing may also match
 * other saved searches independently). See mobile/README.md and the
 * backend's ARCHITECTURE.md "Mobile API" for the full explanation.
 */
export interface Listing {
  id: number;
  marketplace: string;
  external_listing_id: string;
  title: string;
  price: number | null;
  currency: string | null;
  location: string | null;
  seller: string | null;
  condition: string | null;
  listing_url: string;
  image_url: string | null;
  /** The marketplace's own listing-creation time, if known - display only, never used for "new"/sort/filter semantics (see ListingsScreen). */
  source_created_at: string | null;
  first_discovered_at: string;
  last_seen_at: string;
  saved_search_id: number | null;
}

export interface ListingListResponse {
  items: Listing[];
  limit: number;
  offset: number;
  total_count: number;
}

/** Mirrors the backend's `ListingSort` (core/persistence/repository.py). */
export type ListingSort = 'newest' | 'oldest' | 'price_asc' | 'price_desc';

export interface ListListingsParams {
  limit?: number;
  offset?: number;
  marketplace?: string;
  /** Zero or more marketplace ids - sent as a repeated query param (see api/client.ts:buildUrl). An empty array is the same as omitting it (no restriction). */
  marketplaces?: string[];
  saved_search_id?: number;
  min_price?: number;
  max_price?: number;
  currency?: string;
  condition?: string;
  location?: string;
  discovered_after?: string;
  discovered_before?: string;
  new_since?: string;
  sort?: ListingSort;
}
