/**
 * The one place this app makes HTTP calls to the MarketplaceAlert backend.
 * Screens/hooks must always go through `src/api/endpoints.ts` (which calls
 * `apiRequest` below), never call `fetch` directly - see mobile/README.md
 * "Architecture".
 *
 * Handles, deliberately, so no screen has to reimplement any of it:
 * - base URL + `/api/v1` prefix (src/api/config.ts)
 * - a request timeout, generous enough to survive a Render cold start
 *   (see DEFAULT_TIMEOUT_MS) without misreporting a slow backend as down
 * - JSON request/response bodies
 * - turning every failure mode (timeout, no network, non-2xx status,
 *   malformed/non-JSON response body) into one structured `ApiError` type,
 *   with a message that's actually safe and useful to show a user
 * - attaching `Authorization: Bearer <token>` when one is set, and
 *   attempting exactly one silent refresh-and-retry on a 401 - see
 *   "Authenticated requests" below
 *
 * **Authenticated requests.** `setAccessToken()` (called by
 * src/auth/AuthContext.tsx, the only caller) is how this file learns the
 * current access token - this file has no React/Context dependency of
 * its own. Every request attaches it as a Bearer token when present. On
 * a 401 response to a request that actually carried a token, `apiRequest`
 * calls the handler registered via `setUnauthorizedHandler()` - which
 * attempts a refresh and returns either a new access token or `null` -
 * and retries the *original* request exactly once with that new token.
 * If refresh also fails, the *original* 401 is reported as an `ApiError`,
 * same as any other failure.
 *
 * **Single-flight refresh, and why it matters here specifically.** The
 * backend's refresh tokens are single-use/rotating (presenting an
 * already-used one is treated as a compromise signal - see the backend's
 * `AuthService.refresh`). If two requests hit a 401 at nearly the same
 * moment (e.g. a screen firing several `useAsyncData` calls at once) and
 * each independently tried to refresh, the second attempt would present
 * a token the first attempt had already rotated away - a real, not
 * hypothetical, failure mode for this specific backend design. So at
 * most one refresh is ever in progress: every concurrent caller shares
 * the *same* in-flight promise (`getOrStartRefresh`) rather than
 * starting its own. This needs no lock/mutex - JS is single-threaded and
 * the check-and-set in `getOrStartRefresh` has no `await` in between it,
 * so two "concurrent" callers can never both see the guard as empty.
 *
 * **`skipAuthRefresh`** exists for exactly two callers
 * (`src/api/endpoints.ts`'s `refreshToken()` and `logout()`): a failed
 * refresh call must never try to refresh-and-retry *itself* (infinite
 * recursion), and logging out must never attempt a refresh at all just
 * to log out.
 */

import { API_V1_BASE_URL, DEFAULT_TIMEOUT_MS } from './config';

export type ApiErrorKind = 'timeout' | 'network' | 'http' | 'malformed_response';

/**
 * Every failure from `apiRequest` is a single `ApiError` type, regardless
 * of cause - screens only need to handle one type, and `error.message` is
 * always safe/reasonable to render directly in the UI.
 */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** HTTP status code, when the request actually reached the server. */
  readonly status: number | null;

  constructor(message: string, kind: ApiErrorKind, status: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
  }

  /** True for failures worth offering an immediate "Retry" for. */
  get isRetryable(): boolean {
    return this.kind === 'timeout' || this.kind === 'network' || (this.status !== null && this.status >= 500);
  }
}

type QueryValue = string | number | boolean | undefined | string[];

export interface ApiRequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, QueryValue>;
  timeoutMs?: number;
  /**
   * Internal use only - opts this request out of the 401-refresh-retry
   * mechanism entirely. Set by `src/api/endpoints.ts`'s `refreshToken()`
   * and `logout()` only; see this module's docstring for why both need it.
   */
  skipAuthRefresh?: boolean;
}

// --- Access token + single-flight refresh coordination ---------------
//
// Module-level, deliberately - this file has no React dependency.
// src/auth/AuthContext.tsx is the only caller of the two setters below.

let currentAccessToken: string | null = null;
let onUnauthorized: (() => Promise<string | null>) | null = null;
let inFlightRefresh: Promise<string | null> | null = null;

/** Called by AuthContext whenever the access token changes (login, refresh, logout). */
export function setAccessToken(token: string | null): void {
  currentAccessToken = token;
}

/**
 * Called once by AuthContext to register how a 401 should attempt to
 * recover. Must resolve to a new access token on success, or `null` on
 * any failure - this file does not distinguish *why* a refresh failed
 * (definitive vs. transient - that distinction, and what it implies for
 * locally stored session state, is entirely AuthContext's concern; this
 * file only cares whether there's a new token to retry with).
 */
export function setUnauthorizedHandler(handler: (() => Promise<string | null>) | null): void {
  onUnauthorized = handler;
}

function getOrStartRefresh(): Promise<string | null> {
  if (inFlightRefresh === null) {
    if (!onUnauthorized) {
      return Promise.resolve(null);
    }
    inFlightRefresh = onUnauthorized()
      .catch(() => null)
      .finally(() => {
        inFlightRefresh = null;
      });
  }
  return inFlightRefresh;
}

/**
 * An array value repeats the query param once per entry
 * (`?marketplaces=ebay&marketplaces=etsy`), matching FastAPI's own
 * convention for a `list[str]` query parameter (`GET /api/v1/listings`'s
 * `marketplaces` filter) - never a comma-joined single value, which
 * FastAPI would not parse as a list.
 */
function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(`${API_V1_BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined) {
        continue;
      }
      if (Array.isArray(value)) {
        for (const entry of value) {
          url.searchParams.append(key, entry);
        }
        continue;
      }
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * FastAPI error bodies are `{"detail": "..."}` for a hand-raised
 * HTTPException, or `{"detail": [{"msg": "...", ...}, ...]}` for an
 * automatic Pydantic validation error (a 422 on malformed input) - handle
 * both shapes so validation errors surface a genuinely useful message
 * instead of a generic "Request failed".
 */
function extractServerDetail(payload: unknown): string | null {
  if (payload === null || typeof payload !== 'object' || !('detail' in payload)) {
    return null;
  }
  const detail = (payload as { detail: unknown }).detail;
  if (typeof detail === 'string' && detail.length > 0) {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : null))
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length > 0) {
      return messages.join('; ');
    }
  }
  return null;
}

interface RawRequestOptions {
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, QueryValue>;
  timeoutMs: number;
}

/**
 * One raw HTTP attempt - the exact fetch/timeout/AbortController logic
 * this file has always had, now parameterized by which access token (if
 * any) to attach, so `apiRequest` can call it a second time with a
 * freshly-refreshed token after a 401, without duplicating any of this.
 */
async function attemptRequest(path: string, options: RawRequestOptions, accessToken: string | null): Promise<Response> {
  const { method, body, query, timeoutMs } = options;
  const url = buildUrl(path, query);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  const headers: Record<string, string> =
    body !== undefined ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: 'application/json' };
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }

  try {
    return await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new ApiError(
        'The server took too long to respond. The backend may be starting up after being idle - please try again in a moment.',
        'timeout',
      );
    }
    throw new ApiError('Could not reach the server. Check your connection and try again.', 'network');
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Turns a raw `Response` into the parsed body, or throws `ApiError` - the second half of what every request needs, shared by both the original attempt and a post-refresh retry. */
async function parseResponse<T>(response: Response): Promise<T> {
  // FastAPI returns 204 No Content for DELETE - no body to parse at all.
  if (response.status === 204) {
    return undefined as T;
  }

  const rawText = await response.text();
  let payload: unknown = null;
  if (rawText.length > 0) {
    try {
      payload = JSON.parse(rawText);
    } catch {
      throw new ApiError('The server returned a response this app could not understand.', 'malformed_response', response.status);
    }
  }

  if (!response.ok) {
    const detail = extractServerDetail(payload);
    throw new ApiError(detail ?? `The server reported an error (HTTP ${response.status}).`, 'http', response.status);
  }

  return payload as T;
}

/**
 * Make one JSON request to `${API_V1_BASE_URL}${path}`. Resolves with the
 * parsed response body on any 2xx status; rejects with `ApiError` for
 * every other outcome (timeout, no network, non-2xx, malformed body).
 */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, timeoutMs = DEFAULT_TIMEOUT_MS, skipAuthRefresh = false } = options;
  const rawOptions: RawRequestOptions = { method, body, query, timeoutMs };
  const tokenAtRequestTime = currentAccessToken;

  const response = await attemptRequest(path, rawOptions, tokenAtRequestTime);

  // Only a request that actually carried a token can plausibly be
  // failing *because that token expired* - a request made with no token
  // at all (e.g. login, signup) getting a 401 is a real credentials
  // failure, never something a refresh could fix.
  if (response.status === 401 && !skipAuthRefresh && tokenAtRequestTime !== null && onUnauthorized) {
    const newToken = await getOrStartRefresh();
    if (newToken) {
      const retryResponse = await attemptRequest(path, rawOptions, newToken);
      return parseResponse<T>(retryResponse); // at most one retry - this path never re-checks for 401
    }
    // Refresh failed (definitively or transiently - this file doesn't
    // know which, see the module docstring) - fall through and report
    // the ORIGINAL 401 below, exactly as if no refresh had been attempted.
  }

  return parseResponse<T>(response);
}
