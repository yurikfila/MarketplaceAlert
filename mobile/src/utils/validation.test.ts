import { MIN_SCAN_INTERVAL_SECONDS, validateCreateSearchForm } from './validation';

describe('validateCreateSearchForm', () => {
  it('passes for a well-formed submission', () => {
    const result = validateCreateSearchForm({
      query: 'Makita drill',
      marketplaces: ['etsy'],
      scanIntervalSeconds: MIN_SCAN_INTERVAL_SECONDS,
    });

    expect(result).toEqual({ valid: true, errors: {} });
  });

  it('rejects a blank query', () => {
    const result = validateCreateSearchForm({ query: '   ', marketplaces: ['etsy'], scanIntervalSeconds: 60 });

    expect(result.valid).toBe(false);
    expect(result.errors.query).toBeDefined();
  });

  it('rejects an empty marketplace selection', () => {
    const result = validateCreateSearchForm({ query: 'Makita', marketplaces: [], scanIntervalSeconds: 60 });

    expect(result.valid).toBe(false);
    expect(result.errors.marketplaces).toBeDefined();
  });

  it('rejects a scan interval below the minimum', () => {
    const result = validateCreateSearchForm({ query: 'Makita', marketplaces: ['etsy'], scanIntervalSeconds: 30 });

    expect(result.valid).toBe(false);
    expect(result.errors.scanIntervalSeconds).toContain(String(MIN_SCAN_INTERVAL_SECONDS));
  });

  it('accepts a scan interval exactly at the minimum', () => {
    const result = validateCreateSearchForm({
      query: 'Makita',
      marketplaces: ['etsy'],
      scanIntervalSeconds: MIN_SCAN_INTERVAL_SECONDS,
    });

    expect(result.errors.scanIntervalSeconds).toBeUndefined();
  });

  it('reports every failing field at once, not just the first', () => {
    const result = validateCreateSearchForm({ query: '', marketplaces: [], scanIntervalSeconds: 10 });

    expect(result.valid).toBe(false);
    expect(Object.keys(result.errors).sort()).toEqual(['marketplaces', 'query', 'scanIntervalSeconds']);
  });
});
