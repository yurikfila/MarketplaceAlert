/**
 * Native mobile push notifications (Expo Notifications) - Phase 1.
 *
 * **Deliberately has no import of `AuthContext.tsx`** - `usePushNotification
 * Setup` below takes the current auth `status` as a plain parameter
 * instead of calling `useAuth()` itself, and `unregisterCurrentDeviceToken`
 * is a plain async function with no React/Context dependency at all. This
 * mirrors `api/client.ts`'s own established rule ("this file has no
 * React/Context dependency of its own") for exactly the same reason:
 * `AuthContext.tsx`'s `logout()` needs to call `unregisterCurrentDeviceToken`
 * (see that function's own docstring for why it must run *before* the
 * access token is cleared) - if this module imported `AuthContext.tsx` in
 * turn, that would be a real circular import. `RootNavigator.tsx`'s
 * `RootNavigatorContent` (which already calls `useAuth()`) is the one
 * place that calls `usePushNotificationSetup(status)`, passing the value
 * straight through.
 */

import { useEffect, useRef } from 'react';
import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';

import { registerDeviceToken, unregisterDeviceToken } from '../api/endpoints';
import { navigationRef } from '../navigation/navigationRef';
import { openListingUrl } from './linking';

/**
 * Required for a push notification to actually display (a banner/alert)
 * while the app is in the foreground - Expo Notifications does not show
 * one by default. Registered once, at module load time (matches Expo's
 * own documented usage - this call is meant to run before any screen
 * mounts, not inside a component).
 */
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

// The currently-registered token for this app instance, if any - module-
// level (not React state) so `unregisterCurrentDeviceToken` can read it
// from outside any component, exactly the way `api/client.ts` tracks the
// current access token in a module-level variable for the same reason.
let registeredDeviceToken: string | null = null;

/**
 * Requests notification permission (if not already granted/denied) and
 * returns this device's Expo push token, or `null` if permission was
 * denied or the token couldn't be obtained (e.g. no EAS project
 * configured yet - this project has no `eas.json`/project id set up as
 * of Phase 1, so `getExpoPushTokenAsync()` may fail in some build types;
 * failing closed here, never throwing, is deliberate - a push-setup
 * problem must never crash or block the rest of the app).
 */
async function requestPermissionAndGetToken(): Promise<string | null> {
  try {
    const existing = await Notifications.getPermissionsAsync();
    let permissionStatus = existing.status;
    if (permissionStatus !== 'granted') {
      const requested = await Notifications.requestPermissionsAsync();
      permissionStatus = requested.status;
    }
    if (permissionStatus !== 'granted') {
      return null;
    }
    const tokenResponse = await Notifications.getExpoPushTokenAsync();
    return tokenResponse.data;
  } catch {
    return null;
  }
}

/**
 * Handles a tap on a push notification (app backgrounded or killed at
 * the time) - opens the listing exactly the way tapping a `ListingCard`
 * already does (`openListingUrl`, see that module's own docstring), per
 * the Phase 1 design decision: there is no in-app listing-detail screen
 * yet, so "open the relevant listing" means launching the listing's own
 * real page, not a new screen. Also brings the Listings tab into view,
 * guarded by `navigationRef.isReady()` - a tap arriving before the
 * authenticated stack has mounted (e.g. the app was killed and is only
 * now cold-starting) is not actionable for navigation yet, but the
 * listing itself can still be opened regardless.
 */
function handleNotificationTap(response: Notifications.NotificationResponse): void {
  const data = response.notification.request.content.data as { listing_url?: unknown } | undefined;
  const listingUrl = typeof data?.listing_url === 'string' ? data.listing_url : null;

  if (navigationRef.isReady()) {
    navigationRef.navigate('Tabs', { screen: 'Listings' });
  }
  if (listingUrl) {
    void openListingUrl(listingUrl);
  }
}

/**
 * Mounted once, from `RootNavigator.tsx`'s `RootNavigatorContent` (which
 * already calls `useAuth()` and can pass `status` straight through - see
 * this module's own docstring for why this hook doesn't call `useAuth()`
 * itself).
 *
 * Registers this device's Expo push token with the backend whenever
 * `status` is `'authenticated'` - covers login, signup, and a silent
 * session restore uniformly, without needing three separate call sites.
 * Registration is best-effort: a failure (offline, backend hiccup, no
 * EAS project configured) is silently swallowed - the next time `status`
 * becomes `'authenticated'` again (e.g. after the user reopens the app)
 * simply tries again.
 *
 * Also sets up the notification-tap listener once, for the lifetime of
 * the app - independent of auth status, since a tap can arrive for an
 * app that was killed and is only now cold-starting back into an
 * authenticated session.
 */
export function usePushNotificationSetup(status: string): void {
  const registrationRunIdRef = useRef(0);

  useEffect(() => {
    if (status !== 'authenticated') {
      return;
    }
    const runId = ++registrationRunIdRef.current;
    (async () => {
      const token = await requestPermissionAndGetToken();
      if (!token || registrationRunIdRef.current !== runId) {
        return;
      }
      try {
        await registerDeviceToken({ expo_push_token: token, platform: Platform.OS });
        registeredDeviceToken = token;
      } catch {
        // Best-effort - see this function's own docstring.
      }
    })();
  }, [status]);

  useEffect(() => {
    const subscription = Notifications.addNotificationResponseReceivedListener(handleNotificationTap);
    return () => subscription.remove();
  }, []);
}

/**
 * Best-effort unregister of this device's currently-registered push
 * token - called from `AuthContext.tsx`'s `logout()`, deliberately
 * *before* the access token is cleared (`DELETE /api/v1/devices`
 * requires a valid bearer token, same as every other authenticated
 * call - calling this after logout has already cleared the session
 * would always fail with 401). Mirrors the exact same "best-effort,
 * never blocks or fails logout" convention `AuthContext.logout()`
 * already applies to revoking the refresh token.
 *
 * A no-op if no token was ever successfully registered this session -
 * never makes a network call it has nothing to ask for.
 */
export async function unregisterCurrentDeviceToken(): Promise<void> {
  const token = registeredDeviceToken;
  if (!token) {
    return;
  }
  registeredDeviceToken = null;
  try {
    await unregisterDeviceToken({ expo_push_token: token });
  } catch {
    // Best-effort - a failed unregister (offline, backend hiccup) must
    // never block or fail logout. A stale server-side registration left
    // behind this way is harmless: the next real notification attempt
    // to it will simply fail delivery to that one device, the same as
    // any other unreachable/uninstalled device.
  }
}
