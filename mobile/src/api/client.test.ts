/**
 * Tests for the API client - the one place this app talks HTTP. Every test
 * mocks `global.fetch` directly; none of these ever make a real network
 * call (no real backend, and definitely no real Etsy/eBay/Telegram, which
 * this app never talks to directly anyway).
 */

import { apiRequest, ApiError } from './client';

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
