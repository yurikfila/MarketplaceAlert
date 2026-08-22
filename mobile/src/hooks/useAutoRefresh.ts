import { useFocusEffect } from '@react-navigation/native';
import { useCallback, useEffect, useRef } from 'react';
import { AppState, type AppStateStatus } from 'react-native';

/**
 * How often a focused screen polls for fresh data while the app sits open
 * in the foreground. The backend scheduler keeps scanning saved searches
 * on its own schedule (as often as every 60 seconds) whether or not the
 * app is open, so this has to be frequent enough that "last scanned"/new
 * listings feel current, but not so frequent it meaningfully adds to
 * backend load - 20 seconds is the middle of the requested 15-30s range.
 */
export const DEFAULT_AUTO_REFRESH_INTERVAL_MS = 20_000;

export interface UseAutoRefreshOptions {
  /** How often to poll while this screen is focused. @default DEFAULT_AUTO_REFRESH_INTERVAL_MS */
  intervalMs?: number;
  /**
   * Set to `false` to suspend all automatic refreshing (interval, app
   * resume, and focus) without unmounting the screen - e.g. while a
   * screen has its own in-flight mutation (Run Now, Pause/Resume, Delete)
   * that a concurrent background refetch could race with.
   * @default true
   */
  enabled?: boolean;
}

/**
 * One reusable strategy for keeping a screen's data current without the
 * user having to manually pull-to-refresh or navigate away and back,
 * covering every trigger this app needs (see mobile/README.md "Automatic
 * refresh"):
 *
 * - **Foreground polling**: while this screen is focused, calls `refresh`
 *   every `intervalMs`.
 * - **App resume**: calls `refresh` immediately whenever the app comes
 *   back to `active` from the background - important because the
 *   backend's scheduler keeps scanning while the phone app is backgrounded,
 *   so data can go stale purely from time passing while the OS suspended
 *   this screen's own timer.
 * - **Screen focus**: calls `refresh` immediately when this screen
 *   *regains* focus (e.g. navigating back from a detail screen) - but
 *   deliberately not on the very first focus right after mount, since
 *   whatever hook loaded this screen's initial data already just fetched
 *   it; refetching again immediately would be a redundant request, not a
 *   refresh.
 *
 * All three are scoped to "this screen is currently focused" - the
 * interval timer and the AppState subscription are both torn down on
 * blur (e.g. switching tabs) and recreated on refocus, via
 * `useFocusEffect`'s own cleanup - so a screen sitting unfocused in a
 * background tab never polls, keeping backend load proportional to what
 * the user is actually looking at rather than every screen that has ever
 * been visited.
 *
 * `refresh` itself is expected to be safe to call repeatedly and to fail
 * quietly (see `useAsyncData`'s `refreshQuietly` / the hand-rolled
 * equivalent in `ListingsScreen`) - this hook only decides *when* to call
 * it, never how to interpret the result.
 */
export function useAutoRefresh(refresh: () => void, options: UseAutoRefreshOptions = {}): void {
  const { intervalMs = DEFAULT_AUTO_REFRESH_INTERVAL_MS, enabled = true } = options;

  // `refresh` is very often a fresh inline/useCallback closure on every
  // render (it usually closes over each screen's latest state). Reading it
  // through a ref - instead of putting it in the effect's dependency array
  // - means the interval/listener below are only ever torn down and
  // recreated when `intervalMs` actually changes, never just because the
  // caller re-rendered - which is what "no duplicate polling" depends on.
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  // `enabled` is read through a ref too, and deliberately kept OUT of the
  // focus effect's dependency array below. If it were a dependency,
  // toggling it (e.g. SavedSearchDetailScreen disabling this while a Run
  // Now is in flight, then re-enabling it once done) would tear down and
  // recreate the effect - which looks identical, from inside
  // useFocusEffect, to a genuine re-focus, and would incorrectly fire an
  // extra immediate refresh right as it re-enables (on top of whatever
  // explicit refresh the screen's own mutation handler already triggers).
  // Reading it at call time instead means enabling/disabling only ever
  // changes whether the *next* tick/event does anything - it never
  // disturbs the interval, the AppState subscription, or the
  // once-per-genuine-focus skip below.
  const enabledRef = useRef(enabled);
  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  const hasFocusedOnceRef = useRef(false);

  useFocusEffect(
    useCallback(() => {
      if (hasFocusedOnceRef.current && enabledRef.current) {
        refreshRef.current();
      }
      hasFocusedOnceRef.current = true;

      const intervalId = setInterval(() => {
        if (enabledRef.current) {
          refreshRef.current();
        }
      }, intervalMs);

      const appStateSubscription = AppState.addEventListener('change', (nextState: AppStateStatus) => {
        if (nextState === 'active' && enabledRef.current) {
          refreshRef.current();
        }
      });

      return () => {
        clearInterval(intervalId);
        appStateSubscription.remove();
      };
    }, [intervalMs]),
  );
}
