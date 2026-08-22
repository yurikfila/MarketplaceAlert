import { formatIntervalSeconds, formatPrice, formatTimestamp } from './format';

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

  it('formats a price with a valid currency code', () => {
    const result = formatPrice(45, 'USD');
    expect(result).not.toBeNull();
    expect(result).toContain('45');
  });

  it('falls back to a plain number+currency when currency is present but not formattable', () => {
    expect(formatPrice(45, 'NOTACURRENCY')).toBe('45 NOTACURRENCY');
  });

  it('formats a bare number when no currency is given', () => {
    expect(formatPrice(45, null)).toBe('45');
  });
});
