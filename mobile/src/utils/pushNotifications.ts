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
import type { NotificationChannelId } from '../api/types';
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

/**
 * Notification sound selection - the five approved, versioned Android
 * channels (see `NotificationChannelId`/backend `DeviceToken.
 * notification_channel_id` docstrings for why versioned ids, not a
 * mutable shared one: an Android channel's sound is locked in forever
 * once created, so changing a sound later means introducing a new
 * channel id, never editing an existing one). `sound: null` for the
 * default channel means "use Android's own default notification sound" -
 * every other channel names one of the four bundled `.wav` files
 * (`app.json`'s `expo-notifications` plugin config), referenced here by
 * filename without extension, matching how the plugin registers them as
 * native Android raw resources.
 */
export const NOTIFICATION_CHANNELS: ReadonlyArray<{
  id: NotificationChannelId;
  name: string;
  sound: string | null;
}> = [
  { id: 'listing-alerts-default-v1', name: 'New listing alerts', sound: null },
  { id: 'listing-alerts-ping-v1', name: 'New listing alerts (Clean Ping)', sound: 'clean_ping' },
  { id: 'listing-alerts-double-v1', name: 'New listing alerts (Double Ping)', sound: 'double_ping' },
  { id: 'listing-alerts-radar-v1', name: 'New listing alerts (Radar)', sound: 'radar' },
  { id: 'listing-alerts-premium-v1', name: 'New listing alerts (Premium Chime)', sound: 'premium_chime' },
];

/**
 * Creates all five notification channels, Android-only (channels are an
 * Android-only concept; iOS/web have no equivalent and `setNotification
 * ChannelAsync` doesn't exist there). Safe to call unconditionally, every
 * app start, regardless of auth status - creating a channel that already
 * exists on this device is a no-op, and channels are a device-level
 * setup concern, not a per-session one. Best-effort, like everything
 * else in this file: channel creation failing must never crash the app
 * or block anything else here.
 */
export async function ensureNotificationChannelsExist(): Promise<void> {
  if (Platform.OS !== 'android') {
    return;
  }
  try {
    for (const channel of NOTIFICATION_CHANNELS) {
      await Notifications.setNotificationChannelAsync(channel.id, {
        name: channel.name,
        importance: Notifications.AndroidImportance.HIGH,
        sound: channel.sound,
      });
    }
  } catch {
    // Best-effort - see this function's own docstring.
  }
}
void ensureNotificationChannelsExist();

// The currently-registered token for this app instance, if any - module-
// level (not React state) so `unregisterCurrentDeviceToken` can read it
// from outside any component, exactly the way `api/client.ts` tracks the
// current access token in a module-level variable for the same reason.
let registeredDeviceToken: string | null = null;

/**
 * This device's last server-confirmed registration state - `null` until
 * the first successful `registerDeviceToken` call this session. Updated
 * by both the automatic startup registration below AND
 * `updateNotificationChannelPreference` (never invented/duplicated
 * client-side - see `DeviceRegisterResponse`'s own backend docstring for
 * why: the backend is the only source of truth for this device's
 * `notification_channel_id`, and every registration call already
 * returns it for free). `NotificationSoundScreen` reads this via
 * `getLastKnownDeviceRegistration()` to know what to display as
 * currently selected, and `updateNotificationChannelPreference` reuses
 * `expoPushToken`/`platform` from here so the explicit save action never
 * has to re-derive them.
 */
interface DeviceRegistrationState {
  expoPushToken: string;
  platform: string;
  notificationChannelId: NotificationChannelId | null;
}
let lastKnownRegistration: DeviceRegistrationState | null = null;

/**
 * Guards every write to `lastKnownRegistration` against a slow, stale
 * response overwriting a newer one - a confirmed race between the
 * automatic startup registration and an explicit
 * `updateNotificationChannelPreference` save (or two overlapping
 * automatic runs), since either can be in flight at once and nothing
 * previously stopped an older request's response from winning just
 * because it happened to resolve last.
 *
 * Every operation that's about to call `registerDeviceToken` captures
 * its own ticket via `++registrationWriteSequence` *before* the request
 * starts, and only applies its write afterward if its ticket still
 * equals the current `registrationWriteSequence` - i.e. no newer
 * operation has started since. This is a "newest start wins" rule, not
 * "newest resolution wins": a slow older request can never clobber a
 * newer one's result, regardless of which happens to resolve first.
 *
 * `unregisterCurrentDeviceToken` also bumps this on logout, specifically
 * so a registration request already in flight *at* logout time - which
 * hasn't written anything yet - is invalidated too, and can never
 * repopulate the cache for a session that's already gone once it
 * eventually resolves.
 */
let registrationWriteSequence = 0;

/** See `lastKnownRegistration`'s own docstring. */
export function getLastKnownDeviceRegistration(): DeviceRegistrationState | null {
  return lastKnownRegistration;
}

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
      const writeTicket = ++registrationWriteSequence;
      try {
        // Deliberately never sends `notification_channel_id` - see
        // `DeviceRegisterInput`'s own docstring for why an omitted value
        // must always mean "leave the existing preference as it is" to
        // the backend, never "clear it". The response still teaches
        // this app its current preference either way (see
        // `lastKnownRegistration`'s own docstring).
        const response = await registerDeviceToken({ expo_push_token: token, platform: Platform.OS });
        registeredDeviceToken = token;
        // Only apply if no newer write (an explicit save, a newer
        // automatic run, or a logout) has started since this one did -
        // see `registrationWriteSequence`'s own docstring.
        if (writeTicket === registrationWriteSequence) {
          lastKnownRegistration = {
            expoPushToken: token,
            platform: Platform.OS,
            notificationChannelId: response.notification_channel_id,
          };
        }
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
  // Unconditional, and before the early-return below: a registration
  // request can already be in flight (started, not yet resolved -
  // `registeredDeviceToken` isn't set until it resolves) at the exact
  // moment logout happens. Bumping the sequence here, regardless of
  // whether there's anything to actually unregister yet, guarantees
  // that request's eventual write is invalidated too - see
  // `registrationWriteSequence`'s own docstring.
  registrationWriteSequence += 1;
  lastKnownRegistration = null;

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

/**
 * Explicit, user-driven save from `NotificationSoundScreen` - the one
 * place `notification_channel_id` is ever actually sent. Reuses this
 * session's already-registered token/platform (see
 * `lastKnownRegistration`'s own docstring) rather than re-deriving them,
 * and updates the cache from the real response on success so the screen
 * reflects exactly what the backend now has stored.
 *
 * Deliberately NOT best-effort/silently-swallowed, unlike the automatic
 * registration above - this is a direct result of a user action, and
 * `NotificationSoundScreen` must be able to tell a failed save from a
 * successful one (never display a new selection as saved if the request
 * failed - see that screen's own docstring).
 *
 * @throws if no device has been registered yet this session, or if the
 * API call itself fails - the caller is expected to catch and display
 * this.
 */
export async function updateNotificationChannelPreference(channelId: NotificationChannelId): Promise<void> {
  if (!lastKnownRegistration) {
    throw new Error('No device is registered yet - open the app with notifications enabled first.');
  }
  const { expoPushToken, platform } = lastKnownRegistration;
  const writeTicket = ++registrationWriteSequence;
  const response = await registerDeviceToken({
    expo_push_token: expoPushToken,
    platform,
    notification_channel_id: channelId,
  });
  // Same "newest start wins" rule as the automatic registration effect -
  // see `registrationWriteSequence`'s own docstring. In practice this
  // save's own ticket is almost always still the newest by the time its
  // response arrives (it's the most recent explicit user action), but
  // the guard stays consistent and correct regardless.
  if (writeTicket === registrationWriteSequence) {
    lastKnownRegistration = {
      expoPushToken,
      platform,
      notificationChannelId: response.notification_channel_id,
    };
  }
}
