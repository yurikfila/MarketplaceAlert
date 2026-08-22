/**
 * Create-search form validation - pure functions, deliberately mirroring
 * the backend's own rules (marketplace_alert/core/saved_searches/schemas.py)
 * so a user sees the same problem client-side, immediately, instead of
 * only after a round trip. The backend remains the source of truth -
 * these are a fast pre-check, not a replacement for server-side errors.
 */

export const MIN_SCAN_INTERVAL_SECONDS = 60;

export interface CreateSearchFormValues {
  query: string;
  marketplaces: string[];
  scanIntervalSeconds: number;
}

export type CreateSearchFieldErrors = Partial<Record<'query' | 'marketplaces' | 'scanIntervalSeconds', string>>;

export interface CreateSearchValidationResult {
  valid: boolean;
  errors: CreateSearchFieldErrors;
}

export function validateCreateSearchForm(values: CreateSearchFormValues): CreateSearchValidationResult {
  const errors: CreateSearchFieldErrors = {};

  if (values.query.trim().length === 0) {
    errors.query = 'Enter a search keyword or phrase.';
  }

  if (values.marketplaces.length === 0) {
    errors.marketplaces = 'Select at least one marketplace.';
  }

  if (!Number.isFinite(values.scanIntervalSeconds) || values.scanIntervalSeconds < MIN_SCAN_INTERVAL_SECONDS) {
    errors.scanIntervalSeconds = `Scan interval must be at least ${MIN_SCAN_INTERVAL_SECONDS} seconds.`;
  }

  return { valid: Object.keys(errors).length === 0, errors };
}
