/** Pure display-formatting helpers - no React, no network, easy to unit test. */

const SCAN_INTERVAL_PRESETS: Array<{ seconds: number; label: string }> = [
  { seconds: 60, label: '1 minute' },
  { seconds: 300, label: '5 minutes' },
  { seconds: 900, label: '15 minutes' },
  { seconds: 1800, label: '30 minutes' },
  { seconds: 3600, label: '1 hour' },
];

export function scanIntervalPresets(): ReadonlyArray<{ seconds: number; label: string }> {
  return SCAN_INTERVAL_PRESETS;
}

/** Mirrors the backend's own `_format_interval` (marketplace_alert/main.py) - any value, not just the presets. */
export function formatIntervalSeconds(totalSeconds: number): string {
  const preset = SCAN_INTERVAL_PRESETS.find((p) => p.seconds === totalSeconds);
  if (preset) {
    return preset.label;
  }
  if (totalSeconds % 3600 === 0) {
    const hours = totalSeconds / 3600;
    return `${hours} hour${hours === 1 ? '' : 's'}`;
  }
  if (totalSeconds % 60 === 0) {
    const minutes = totalSeconds / 60;
    return `${minutes} minute${minutes === 1 ? '' : 's'}`;
  }
  return `${totalSeconds} seconds`;
}

/** "Never" for null (never scanned yet), otherwise a locale-formatted date/time. */
export function formatTimestamp(iso: string | null): string {
  if (!iso) {
    return 'Never';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'Unknown';
  }
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function titleCase(value: string): string {
  return value.length === 0 ? value : value.charAt(0).toUpperCase() + value.slice(1);
}

/** e.g. ["ebay", "etsy"] -> "Ebay, Etsy" - mirrors the backend dashboard's own marketplaces_display. */
export function formatMarketplacesDisplay(marketplaces: string[]): string {
  return marketplaces.map(titleCase).join(', ');
}

/**
 * `null` when there's no price to show - callers must not render a
 * placeholder in its place (see api/types.ts's Listing - price is
 * genuinely absent from the backend today, not just formatted as empty).
 */
export function formatPrice(price: number | null, currency: string | null): string | null {
  if (price === null) {
    return null;
  }
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(price);
    } catch {
      return `${price} ${currency}`;
    }
  }
  return String(price);
}
