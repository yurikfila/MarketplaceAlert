/**
 * Centralized API base URL configuration - the ONLY place this app decides
 * which backend to talk to. Never hard-code the backend URL anywhere else;
 * import API_V1_BASE_URL from here instead.
 *
 * Reads `EXPO_PUBLIC_API_BASE_URL` (Expo automatically inlines any env var
 * prefixed `EXPO_PUBLIC_` at build time - see https://docs.expo.dev/guides/environment-variables/).
 * Falls back to the production MarketplaceAlert backend if unset, so the
 * app works out of the box after `npm install && npx expo start` with no
 * required setup step - see mobile/README.md "Environment variable".
 */

const PRODUCTION_API_BASE_URL = 'https://marketplacealert.onrender.com';

function resolveApiBaseUrl(): string {
  const configured = process.env.EXPO_PUBLIC_API_BASE_URL;
  const trimmed = configured?.trim();
  const value = trimmed && trimmed.length > 0 ? trimmed : PRODUCTION_API_BASE_URL;
  // Normalize away a trailing slash so callers can always do `${base}/path`.
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

/** The backend's root URL, e.g. "https://marketplacealert.onrender.com". */
export const API_BASE_URL = resolveApiBaseUrl();

/** Every mobile API call goes under this prefix - see api/v1 on the backend. */
export const API_V1_BASE_URL = `${API_BASE_URL}/api/v1`;

/**
 * Generous default timeout. Render's free tier "cold starts" an idle
 * backend on the first request, which can take several real seconds - a
 * short timeout here would misreport a slow-but-working backend as
 * unreachable. See api/client.ts's retry/timeout handling.
 */
export const DEFAULT_TIMEOUT_MS = 20000;
