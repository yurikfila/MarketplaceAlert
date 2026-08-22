/**
 * Tests for `useAsyncData` - specifically the behavior added for
 * automatic refresh: overlap protection (only one request in flight at a
 * time) and `refreshQuietly` (a background reload that never surfaces an
 * error or touches loading/refreshing, so a failed automatic refresh
 * always preserves whatever is currently displayed). The pre-existing
 * initial-load/manual-refresh/retry behavior is exercised indirectly by
 * every screen test that already uses this hook (e.g.
 * SavedSearchesScreen.test.tsx) and isn't re-derived here.
 */
import { act, renderHook } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import { useAsyncData } from './useAsyncData';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useAsyncData', () => {
  it('loads data on mount', async () => {
    const fetcher = jest.fn().mockResolvedValue('first');
    const { result } = await renderHook(() => useAsyncData(fetcher, []));

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.data).toBe('first');
    expect(result.current.loading).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('refreshQuietly updates data on success without ever setting loading or refreshing', async () => {
    const fetcher = jest.fn().mockResolvedValueOnce('first');
    const { result } = await renderHook(() => useAsyncData(fetcher, []));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.data).toBe('first');

    fetcher.mockResolvedValueOnce('second');
    await act(async () => {
      result.current.refreshQuietly();
      await Promise.resolve();
    });

    expect(result.current.data).toBe('second');
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('a failed refreshQuietly preserves the currently displayed data and does not surface an error', async () => {
    const fetcher = jest.fn().mockResolvedValueOnce('good data');
    const { result } = await renderHook(() => useAsyncData(fetcher, []));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.data).toBe('good data');

    fetcher.mockRejectedValueOnce(new ApiError('The server took too long to respond.', 'timeout'));
    await act(async () => {
      result.current.refreshQuietly();
      await Promise.resolve();
    });

    // Requirement: a failed automatic refresh must never clobber
    // already-displayed data or flash an error state - both are exactly
    // as they were before the failed attempt.
    expect(result.current.data).toBe('good data');
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(false);
  });

  it('a manual refresh() failure still surfaces a useful, retryable error', async () => {
    const fetcher = jest.fn().mockResolvedValueOnce('good data');
    const { result } = await renderHook(() => useAsyncData(fetcher, []));
    await act(async () => {
      await Promise.resolve();
    });

    fetcher.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));
    await act(async () => {
      result.current.refresh();
      await Promise.resolve();
    });

    expect(result.current.error).toBe('Could not reach the server.');
    expect(result.current.isRetryable).toBe(true);
    // Pull-to-refresh keeps showing the last good data alongside the error.
    expect(result.current.data).toBe('good data');
  });

  it('drops an overlapping request instead of starting a second one while one is already in flight', async () => {
    const first = deferred<string>();
    const fetcher = jest.fn().mockReturnValueOnce(first.promise);
    const { result } = await renderHook(() => useAsyncData(fetcher, []));
    // The initial load is now in flight (unresolved).
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      result.current.refresh();
      result.current.refreshQuietly();
      result.current.retry();
      await Promise.resolve();
    });

    // None of the three overlapping calls should have started a new
    // request while the initial one was still pending.
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => {
      first.resolve('resolved');
      await Promise.resolve();
    });
    expect(result.current.data).toBe('resolved');

    // Once the in-flight request has resolved, a new call is free to fire.
    fetcher.mockResolvedValueOnce('next');
    await act(async () => {
      result.current.refresh();
      await Promise.resolve();
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('manual pull-to-refresh (refresh()) still works and reflects a refreshing state while in flight', async () => {
    const fetcher = jest.fn().mockResolvedValueOnce('first');
    const { result } = await renderHook(() => useAsyncData(fetcher, []));
    await act(async () => {
      await Promise.resolve();
    });

    const second = deferred<string>();
    fetcher.mockReturnValueOnce(second.promise);
    await act(() => {
      result.current.refresh();
    });
    expect(result.current.refreshing).toBe(true);
    // The previous data stays visible while the manual refresh is pending.
    expect(result.current.data).toBe('first');

    await act(async () => {
      second.resolve('second');
      await Promise.resolve();
    });
    expect(result.current.refreshing).toBe(false);
    expect(result.current.data).toBe('second');
  });
});
