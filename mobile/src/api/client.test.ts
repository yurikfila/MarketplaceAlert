/**
 * Tests for the API client - the one place this app talks HTTP. Every test
 * mocks `global.fetch` directly; none of these ever make a real network
 * call (no real backend, and definitely no real Etsy/eBay/Telegram, which
 * this app never talks to directly anyway).
 */

import { apiRequest, ApiError, setAccessToken, setUnauthorizedHandler } from './client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('apiRequest', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    jest.restoreAllMocks();
  });

  it('returns parsed JSON on a successful GET', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(200, { status: 'ok' }));

    const result = await apiRequest<{ status: string }>('/status');

    expect(result).toEqual({ status: 'ok' });
  });

  it('requests the correct URL under the /api/v1 prefix', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, []));
    global.fetch = fetchMock;

    await apiRequest('/marketplaces');

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('https://marketplacealert.onrender.com/api/v1/marketplaces');
  });

  it('appends query parameters, skipping undefined values', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { items: [] }));
    global.fetch = fetchMock;

    await apiRequest('/listings', { query: { limit: 20, offset: 0, marketplace: undefined } });

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('limit=20');
    expect(String(url)).toContain('offset=0');
    expect(String(url)).not.toContain('marketplace=');
  });

  it('repeats the query param once per array entry, matching FastAPI list[str] params', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { items: [] }));
    global.fetch = fetchMock;

    await apiRequest('/listings', { query: { marketplaces: ['ebay', 'etsy'] } });

    const [url] = fetchMock.mock.calls[0];
    const params = new URL(String(url)).searchParams.getAll('marketplaces');
    expect(params).toEqual(['ebay', 'etsy']);
  });

  it('sends no query param at all for an empty array', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { items: [] }));
    global.fetch = fetchMock;

    await apiRequest('/listings', { query: { marketplaces: [] } });

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).not.toContain('marketplaces');
  });

  it('sends a JSON body and Content-Type header on POST', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(201, { id: 1 }));
    global.fetch = fetchMock;

    await apiRequest('/saved-searches', { method: 'POST', body: { query: 'Makita' } });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.body).toBe(JSON.stringify({ query: 'Makita' }));
    expect(init.headers['Content-Type']).toBe('application/json');
  });

  it('returns undefined for a 204 No Content response without parsing a body', async () => {
    global.fetch = jest.fn().mockResolvedValue(new Response(null, { status: 204 }));

    const result = await apiRequest<undefined>('/saved-searches/1', { method: 'DELETE' });

    expect(result).toBeUndefined();
  });

  it('throws an ApiError with the server-provided string detail on a 404', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(404, { detail: 'Saved search not found' }));

    await expect(apiRequest('/saved-searches/999')).rejects.toMatchObject({
      message: 'Saved search not found',
      status: 404,
      kind: 'http',
    });
  });

  it('joins Pydantic-style array validation details into one message', async () => {
    global.fetch = jest.fn().mockResolvedValue(
      jsonResponse(422, {
        detail: [
          { loc: ['body', 'scan_interval_seconds'], msg: 'Input should be greater than or equal to 60', type: 'greater_than_equal' },
        ],
      }),
    );

    await expect(apiRequest('/saved-searches', { method: 'POST', body: {} })).rejects.toMatchObject({
      message: 'Input should be greater than or equal to 60',
      status: 422,
    });
  });

  it('falls back to a generic message when the error body has no usable detail', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(500, {}));

    await expect(apiRequest('/status')).rejects.toMatchObject({
      message: 'The server reported an error (HTTP 500).',
      status: 500,
    });
  });

  it('marks a 5xx failure as retryable', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(503, {}));

    try {
      await apiRequest('/status');
      throw new Error('expected apiRequest to reject');
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).isRetryable).toBe(true);
    }
  });

  it('does not mark a 4xx client error as retryable', async () => {
    global.fetch = jest.fn().mockResolvedValue(jsonResponse(422, { detail: 'bad input' }));

    try {
      await apiRequest('/status');
      throw new Error('expected apiRequest to reject');
    } catch (error) {
      expect((error as ApiError).isRetryable).toBe(false);
    }
  });

  it('throws a network ApiError when fetch itself fails (no connectivity)', async () => {
    global.fetch = jest.fn().mockRejectedValue(new TypeError('Network request failed'));

    await expect(apiRequest('/status')).rejects.toMatchObject({
      kind: 'network',
    });
  });

  it('throws a timeout ApiError when the request is aborted', async () => {
    global.fetch = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => {
          const abortError = new Error('Aborted');
          abortError.name = 'AbortError';
          reject(abortError);
        });
      });
    });

    await expect(apiRequest('/status', { timeoutMs: 5 })).rejects.toMatchObject({
      kind: 'timeout',
    });
  });

  it('throws a malformed_response ApiError when the body is not valid JSON', async () => {
    global.fetch = jest.fn().mockResolvedValue(new Response('<html>not json</html>', { status: 200 }));

    await expect(apiRequest('/status')).rejects.toMatchObject({
      kind: 'malformed_response',
    });
  });

  it('never contacts a real server - fetch is always the injected mock', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { status: 'ok' }));
    global.fetch = fetchMock;

    await apiRequest('/status');

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

/**
 * Auth-header attachment and the single-flight refresh-and-retry mechanism.
 * Kept in its own describe block (own fetch/token/handler teardown) so it
 * can't leak `currentAccessToken`/`onUnauthorized` module state into the
 * unauthenticated tests above.
 */
describe('apiRequest - authenticated requests and refresh', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    setAccessToken(null);
    setUnauthorizedHandler(null);
    jest.restoreAllMocks();
  });

  it('attaches an Authorization header when an access token is set', async () => {
    setAccessToken('abc123');
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { status: 'ok' }));
    global.fetch = fetchMock;

    await apiRequest('/status');

    const [, init] = fetchMock.mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer abc123');
  });

  it('sends no Authorization header when no access token is set', async () => {
    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(200, { status: 'ok' }));
    global.fetch = fetchMock;

    await apiRequest('/status');

    const [, init] = fetchMock.mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it('concurrent 401s trigger exactly one refresh request', async () => {
    setAccessToken('expired-token');
    const unauthorizedHandler = jest.fn().mockImplementation(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      setAccessToken('new-token');
      return 'new-token';
    });
    setUnauthorizedHandler(unauthorizedHandler);

    const fetchMock = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
      const headers = init.headers as Record<string, string>;
      if (headers.Authorization === 'Bearer expired-token') {
        return Promise.resolve(jsonResponse(401, { detail: 'Invalid or expired token' }));
      }
      return Promise.resolve(jsonResponse(200, { status: 'ok' }));
    });
    global.fetch = fetchMock;

    const results = await Promise.all([
      apiRequest<{ status: string }>('/one'),
      apiRequest<{ status: string }>('/two'),
      apiRequest<{ status: string }>('/three'),
    ]);

    expect(results).toEqual([{ status: 'ok' }, { status: 'ok' }, { status: 'ok' }]);
    expect(unauthorizedHandler).toHaveBeenCalledTimes(1);
  });

  it('a failed refresh fails all waiters cleanly with their original 401, calling the refresh handler once', async () => {
    setAccessToken('expired-token');
    const unauthorizedHandler = jest.fn().mockResolvedValue(null);
    setUnauthorizedHandler(unauthorizedHandler);

    global.fetch = jest.fn().mockResolvedValue(jsonResponse(401, { detail: 'Invalid or expired token' }));

    const results = await Promise.allSettled([apiRequest('/one'), apiRequest('/two')]);

    expect(results[0].status).toBe('rejected');
    expect(results[1].status).toBe('rejected');
    if (results[0].status === 'rejected') {
      expect(results[0].reason).toBeInstanceOf(ApiError);
      expect((results[0].reason as ApiError).status).toBe(401);
    }
    expect(unauthorizedHandler).toHaveBeenCalledTimes(1);
  });

  it('a successful refresh retries the original request exactly once with the new token', async () => {
    setAccessToken('expired-token');
    setUnauthorizedHandler(
      jest.fn().mockImplementation(async () => {
        setAccessToken('new-token');
        return 'new-token';
      }),
    );

    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid or expired token' }))
      .mockResolvedValueOnce(jsonResponse(200, { status: 'ok' }));
    global.fetch = fetchMock;

    const result = await apiRequest<{ status: string }>('/status');

    expect(result).toEqual({ status: 'ok' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondCallHeaders = fetchMock.mock.calls[1][1].headers as Record<string, string>;
    expect(secondCallHeaders.Authorization).toBe('Bearer new-token');
  });

  it('never attempts a refresh for a request made without an access token', async () => {
    const unauthorizedHandler = jest.fn().mockResolvedValue('should-not-be-used');
    setUnauthorizedHandler(unauthorizedHandler);

    global.fetch = jest.fn().mockResolvedValue(jsonResponse(401, { detail: 'Incorrect email or password' }));

    await expect(apiRequest('/auth/login', { method: 'POST', body: {} })).rejects.toMatchObject({ status: 401 });
    expect(unauthorizedHandler).not.toHaveBeenCalled();
  });

  it('never attempts a refresh for a skipAuthRefresh request, even on 401 with a token set', async () => {
    setAccessToken('expired-token');
    const unauthorizedHandler = jest.fn().mockResolvedValue('new-token');
    setUnauthorizedHandler(unauthorizedHandler);

    global.fetch = jest.fn().mockResolvedValue(jsonResponse(401, { detail: 'Invalid or expired refresh token' }));

    await expect(
      apiRequest('/auth/refresh', { method: 'POST', body: { refresh_token: 'x' }, skipAuthRefresh: true }),
    ).rejects.toMatchObject({ status: 401 });
    expect(unauthorizedHandler).not.toHaveBeenCalled();
  });

  it('retries at most once - a still-401 response after refresh is surfaced, not retried again', async () => {
    setAccessToken('expired-token');
    setUnauthorizedHandler(
      jest.fn().mockImplementation(async () => {
        setAccessToken('new-token');
        return 'new-token';
      }),
    );

    const fetchMock = jest.fn().mockResolvedValue(jsonResponse(401, { detail: 'Invalid or expired token' }));
    global.fetch = fetchMock;

    await expect(apiRequest('/status')).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
