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

/**
 * Make one JSON request to `${API_V1_BASE_URL}${path}`. Resolves with the
 * parsed response body on any 2xx status; rejects with `ApiError` for
 * every other outcome (timeout, no network, non-2xx, malformed body).
 */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const url = buildUrl(path, query);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers:
        body !== undefined
          ? { 'Content-Type': 'application/json', Accept: 'application/json' }
          : { Accept: 'application/json' },
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
