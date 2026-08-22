import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';

export interface AsyncDataState<T> {
  data: T | null;
  /** True only for the very first load (no data to show yet at all). */
  loading: boolean;
  /** True for a background reload (pull-to-refresh) while stale data is still shown. */
  refreshing: boolean;
  error: string | null;
  isRetryable: boolean;
}

export interface UseAsyncDataResult<T> extends AsyncDataState<T> {
  /** Pull-to-refresh: reloads without clearing currently-shown data first. */
  refresh: () => void;
  /** Retry after an error, or a manual reload - clears currently-shown data first. */
  retry: () => void;
  /**
   * Automatic background reload (interval polling, app resume, screen
   * refocus - see `useAutoRefresh`): on success, updates `data` exactly
   * like `refresh` does; on failure, silently leaves the current `data`
   * and `error` untouched instead of surfacing the failure - a transient
   * blip (a Render cold start, a dropped connection) must never replace
   * already-displayed data with an error screen, and must never flash
   * `refreshing`/`loading` indicators the user didn't ask for. The next
   * interval tick (or the user's own manual refresh) simply tries again.
   */
  refreshQuietly: () => void;
}

/**
 * The one data-fetching hook every screen in this app uses for "load
 * something from the API, support pull-to-refresh and retry, never
 * crash on a network error" - deliberately small and dependency-free
 * (plain hooks, no Redux/React Query) since this first version has no
 * data shared across screens that would justify more machinery.
 */
export function useAsyncData<T>(fetcher: () => Promise<T>, deps: React.DependencyList): UseAsyncDataResult<T> {
  const [state, setState] = useState<AsyncDataState<T>>({
    data: null,
    loading: true,
    refreshing: false,
    error: null,
    isRetryable: false,
  });
  // Avoids a "set state after unmount" warning if a screen is left before
  // a slow request (e.g. a Render cold start) resolves.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  // Overlap protection: a poll tick, an app-resume refresh, a screen
  // refocus, and a manual pull-to-refresh can all ask to reload at nearly
  // the same moment. Only one request is ever in flight at a time for a
  // given useAsyncData instance - a call that arrives while one is still
  // pending is simply dropped, never queued or fired alongside it, which
  // is what keeps automatic refreshing from turning into a request storm.
  const inFlightRef = useRef(false);

  const load = useCallback((mode: 'initial' | 'refresh' | 'retry' | 'auto') => {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;

    if (mode !== 'auto') {
      // A quiet/automatic reload never touches loading/refreshing/error -
      // it must be invisible unless (or until) it actually has new data.
      setState((previous) => ({
        ...previous,
        loading: mode !== 'refresh' && previous.data === null,
        refreshing: mode === 'refresh',
        error: mode === 'retry' || mode === 'initial' ? null : previous.error,
      }));
    }

    fetcher().then(
      (data) => {
        inFlightRef.current = false;
        if (mountedRef.current) {
          setState({ data, loading: false, refreshing: false, error: null, isRetryable: false });
        }
      },
      (error: unknown) => {
        inFlightRef.current = false;
        if (!mountedRef.current) return;
        if (mode === 'auto') {
          // Preserve whatever is currently displayed - see
          // `refreshQuietly`'s doc comment.
          return;
        }
        const message = error instanceof ApiError ? error.message : 'Something went wrong.';
        const isRetryable = error instanceof ApiError ? error.isRetryable : true;
        setState((previous) => ({ ...previous, loading: false, refreshing: false, error: message, isRetryable }));
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    load('initial');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const refresh = useCallback(() => load('refresh'), [load]);
  const retry = useCallback(() => load('retry'), [load]);
  const refreshQuietly = useCallback(() => load('auto'), [load]);

  return { ...state, refresh, retry, refreshQuietly };
}
