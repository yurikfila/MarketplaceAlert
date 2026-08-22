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

/**
 * `null` when there's no price to show - callers must not render a
 * placeholder in its place (see api/types.ts's Listing - price is
 * genuinely absent for some listings, not just formatted as empty).
 *
 * Deliberately **not** `Intl.NumberFormat({style: 'currency', ...})`
 * (which renders a locale symbol like "$"/"€" and always pads to 2
 * decimals, e.g. "$1,250.00") - this app never converts currencies (see
 * ARCHITECTURE.md "Mobile API"), so showing the marketplace's own ISO
 * currency *code* is the honest, unambiguous choice: "$79.99" is
 * genuinely ambiguous across USD/CAD/AUD/etc., "USD 79.99" is not.
 * Fractional cents are only ever shown when the price actually has them
 * - "USD 1,250", not "USD 1,250.00" - mirrored exactly in the web
 * dashboard's `_format_price()` (marketplace_alert/main.py) so price
 * display is consistent between the mobile app and the dashboard.
 */
export function formatPrice(price: number | null, currency: string | null): string | null {
  if (price === null) {
    return null;
  }
  const hasFraction = !Number.isInteger(price);
  const formattedNumber = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: hasFraction ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(price);
  return currency ? `${currency.toUpperCase()} ${formattedNumber}` : formattedNumber;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** How long a listing counts as "recently discovered" for `isRecentlyDiscovered`'s "New" badge - independent of what `formatRelativeTime`'s text currently says (see that function's own "New" tier, which is much shorter-lived). */
export const NEW_LISTING_BADGE_THRESHOLD_MS = 30 * MINUTE_MS;

/**
 * One consistent relative-time strategy for "when was this discovered/
 * scanned" across the app - `first_discovered_at`/`last_scanned_at` are
 * always UTC ISO 8601 strings (the backend's `ListingOut`/`SavedSearchRead`
 * both guarantee this - see their `ensure_utc` validators), and `Date`
 * parses/compares those as absolute instants regardless of the device's
 * own timezone, so there's no timezone bug to work around here - only
 * `toLocaleDateString`'s own (correct) behavior of rendering the final
 * "older" tier in the user's local timezone.
 *
 * Tiers, in order: "Just now" (under a minute) -> "X min ago" -> "X hr
 * ago" -> "Yesterday" (by calendar day, only once 24h+ has passed - a
 * listing from 11pm yesterday discovered at 1am today still reads "X hr
 * ago" until a full day has passed, which is the least surprising
 * choice) -> a locale date for anything older.
 *
 * Deliberately "Just now", not "New" - `isRecentlyDiscovered()` below
 * already drives a separate "New" *badge* on the card; using the same
 * word here too would show "New" twice on the same card for the first
 * minute after discovery, which reads as a bug, not emphasis.
 */
export function formatRelativeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) {
    return 'Unknown';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'Unknown';
  }

  const diffMs = now.getTime() - date.getTime();
  if (diffMs < MINUTE_MS) {
    // Covers "now was actually earlier than `date`" (clock skew, a
    // just-received server timestamp a hair ahead of the device clock)
    // the same way as a genuine sub-minute gap - never show a negative time.
    return 'Just now';
  }
  if (diffMs < HOUR_MS) {
    return `${Math.floor(diffMs / MINUTE_MS)} min ago`;
  }
  if (diffMs < DAY_MS) {
    return `${Math.floor(diffMs / HOUR_MS)} hr ago`;
  }

  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday.getTime() - DAY_MS);
  if (date.getTime() >= startOfYesterday.getTime() && date.getTime() < startOfToday.getTime()) {
    return 'Yesterday';
  }
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Drives a card's "New" badge - a longer-lived signal than `formatRelativeTime`'s own "New" text tier (which only covers the first minute), so a listing still reads as freshly-found for a little while after the exact wording moves on to "12 min ago". */
export function isRecentlyDiscovered(
  iso: string | null,
  now: Date = new Date(),
  thresholdMs: number = NEW_LISTING_BADGE_THRESHOLD_MS,
): boolean {
  if (!iso) {
    return false;
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return false;
  }
  const diffMs = now.getTime() - date.getTime();
  return diffMs >= 0 && diffMs < thresholdMs;
}
