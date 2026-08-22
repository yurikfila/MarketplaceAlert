import {
  formatIntervalSeconds,
  formatPrice,
  formatRelativeTime,
  formatTimestamp,
  isRecentlyDiscovered,
} from './format';

describe('formatIntervalSeconds', () => {
  it.each([
    [60, '1 minute'],
    [300, '5 minutes'],
    [900, '15 minutes'],
    [1800, '30 minutes'],
    [3600, '1 hour'],
  ])('formats the %i-second preset as %s', (seconds, expected) => {
    expect(formatIntervalSeconds(seconds)).toBe(expected);
  });

  it('formats a non-preset multiple of an hour', () => {
    expect(formatIntervalSeconds(7200)).toBe('2 hours');
  });

  it('formats a non-preset multiple of a minute', () => {
    expect(formatIntervalSeconds(120)).toBe('2 minutes');
  });

  it('falls back to raw seconds when not a clean minute/hour multiple', () => {
    expect(formatIntervalSeconds(90)).toBe('90 seconds');
  });
});

describe('formatTimestamp', () => {
  it('returns "Never" for null', () => {
    expect(formatTimestamp(null)).toBe('Never');
  });

  it('returns "Unknown" for an unparsable string', () => {
    expect(formatTimestamp('not-a-date')).toBe('Unknown');
  });

  it('formats a real ISO timestamp into a non-empty string', () => {
    const result = formatTimestamp('2026-08-21T18:49:12.605814Z');
    expect(result.length).toBeGreaterThan(0);
    expect(result).not.toBe('Never');
    expect(result).not.toBe('Unknown');
  });
});

describe('formatPrice', () => {
  it('returns null when price is null - never fakes a value', () => {
    expect(formatPrice(null, 'USD')).toBeNull();
    expect(formatPrice(null, null)).toBeNull();
  });

  it('prefixes the ISO currency code, not a locale symbol', () => {
    expect(formatPrice(1250, 'USD')).toBe('USD 1,250');
  });

  it('omits fractional digits for a whole-number price', () => {
    expect(formatPrice(399, 'EUR')).toBe('EUR 399');
  });

  it('shows two decimals for a price that genuinely has cents', () => {
    expect(formatPrice(79.99, 'GBP')).toBe('GBP 79.99');
  });

  it('uppercases a lowercase currency code', () => {
    expect(formatPrice(45, 'usd')).toBe('USD 45');
  });

  it('never validates the currency string - an unrecognized code is still shown verbatim', () => {
    expect(formatPrice(45, 'NOTACURRENCY')).toBe('NOTACURRENCY 45');
  });

  it('formats a bare number (no leading/trailing text) when no currency is given', () => {
    expect(formatPrice(45, null)).toBe('45');
  });

  it('groups thousands for a very large price', () => {
    expect(formatPrice(1234567, 'USD')).toBe('USD 1,234,567');
  });
});

describe('formatRelativeTime', () => {
  const now = new Date('2026-08-22T12:00:00.000Z');

  it('returns "Unknown" for null', () => {
    expect(formatRelativeTime(null, now)).toBe('Unknown');
  });

  it('returns "Unknown" for an unparsable string', () => {
    expect(formatRelativeTime('not-a-date', now)).toBe('Unknown');
  });

  it('returns "Just now" for a sub-minute gap', () => {
    expect(formatRelativeTime('2026-08-22T11:59:59.500Z', now)).toBe('Just now');
  });

  it('returns "Just now" rather than a negative time for a timestamp slightly ahead of "now" (clock skew)', () => {
    expect(formatRelativeTime('2026-08-22T12:00:05.000Z', now)).toBe('Just now');
  });

  it('formats minutes ago', () => {
    expect(formatRelativeTime('2026-08-22T11:45:00.000Z', now)).toBe('15 min ago');
  });

  it('formats hours ago', () => {
    expect(formatRelativeTime('2026-08-22T09:00:00.000Z', now)).toBe('3 hr ago');
  });

  it('formats "Yesterday" for a timestamp from the previous calendar day, 24h+ ago', () => {
    // now is 2026-08-22 noon UTC; 2026-08-21 08:00 UTC is > 24h ago and falls on the previous calendar day.
    expect(formatRelativeTime('2026-08-21T08:00:00.000Z', now)).toBe('Yesterday');
  });

  it('formats a date for something older than yesterday', () => {
    const result = formatRelativeTime('2026-08-10T08:00:00.000Z', now);
    expect(result).not.toBe('Yesterday');
    expect(result.length).toBeGreaterThan(0);
  });
});

describe('isRecentlyDiscovered', () => {
  const now = new Date('2026-08-22T12:00:00.000Z');

  it('is false for null', () => {
    expect(isRecentlyDiscovered(null, now)).toBe(false);
  });

  it('is false for an unparsable string', () => {
    expect(isRecentlyDiscovered('not-a-date', now)).toBe(false);
  });

  it('is true within the default threshold', () => {
    expect(isRecentlyDiscovered('2026-08-22T11:45:00.000Z', now)).toBe(true);
  });

  it('is false beyond the default threshold', () => {
    expect(isRecentlyDiscovered('2026-08-22T11:00:00.000Z', now)).toBe(false);
  });

  it('respects a custom threshold', () => {
    expect(isRecentlyDiscovered('2026-08-22T11:58:00.000Z', now, 60_000)).toBe(false);
    expect(isRecentlyDiscovered('2026-08-22T11:59:30.000Z', now, 60_000)).toBe(true);
  });

  it('is false for a timestamp in the future beyond simple clock skew', () => {
    expect(isRecentlyDiscovered('2026-08-22T13:00:00.000Z', now)).toBe(false);
  });
});
