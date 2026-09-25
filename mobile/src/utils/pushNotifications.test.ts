/**
 * Tests for the Phase 1 push notification utility - registration on
 * authentication, best-effort unregistration on logout, and the
 * notification-tap handler that opens the tapped listing.
 *
 * `waitFor` (not a fixed number of `act`/`Promise.resolve()` ticks) is
 * used throughout for anything that depends on the registration effect's
 * async chain (getPermissionsAsync -> [requestPermissionsAsync] ->
 * getExpoPushTokenAsync -> registerDeviceToken) - that chain is several
 * awaits deep, so polling for the expected call is the reliable way to
 * wait for it, rather than guessing a tick count.
 *
 * The very first test below (`unregisterCurrentDeviceToken` "no-op when
 * nothing has ever been registered") deliberately runs before any test
 * that causes a successful registration - `registeredDeviceToken` is
 * private module-level state (by design, see pushNotifications.ts's own
 * docstring), shared across every test in this file, and Jest runs a
 * single file's tests sequentially in declaration order. Every other
 * test that needs a registered token sets it up itself first, so nothing
 * else in this file depends on ordering.
 */
import { renderHook, waitFor } from '@testing-library/react-native';
import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';

import { registerDeviceToken, unregisterDeviceToken } from '../api/endpoints';
import { navigationRef } from '../navigation/navigationRef';
import { openListingUrl } from './linking';
import {
  NOTIFICATION_CHANNELS,
  ensureNotificationChannelsExist,
  getLastKnownDeviceRegistration,
  unregisterCurrentDeviceToken,
  updateNotificationChannelPreference,
  usePushNotificationSetup,
} from './pushNotifications';

jest.mock('expo-notifications', () => ({
  setNotificationHandler: jest.fn(),
  getPermissionsAsync: jest.fn(),
  requestPermissionsAsync: jest.fn(),
  getExpoPushTokenAsync: jest.fn(),
  setNotificationChannelAsync: jest.fn(),
  addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  AndroidImportance: { HIGH: 4 },
}));
jest.mock('../api/endpoints', () => ({
  registerDeviceToken: jest.fn(),
  unregisterDeviceToken: jest.fn(),
}));
jest.mock('../navigation/navigationRef', () => ({
  navigationRef: { isReady: jest.fn(), navigate: jest.fn() },
}));
jest.mock('./linking', () => ({
  openListingUrl: jest.fn(),
}));

const mockedGetPermissionsAsync = Notifications.getPermissionsAsync as jest.MockedFunction<typeof Notifications.getPermissionsAsync>;
const mockedRequestPermissionsAsync = Notifications.requestPermissionsAsync as jest.MockedFunction<typeof Notifications.requestPermissionsAsync>;
const mockedGetExpoPushTokenAsync = Notifications.getExpoPushTokenAsync as jest.MockedFunction<typeof Notifications.getExpoPushTokenAsync>;
const mockedSetNotificationChannelAsync = Notifications.setNotificationChannelAsync as jest.Mock;
const mockedAddListener = Notifications.addNotificationResponseReceivedListener as jest.Mock;
const mockedRegisterDeviceToken = registerDeviceToken as jest.MockedFunction<typeof registerDeviceToken>;
const mockedUnregisterDeviceToken = unregisterDeviceToken as jest.MockedFunction<typeof unregisterDeviceToken>;
const mockedNavigationRef = navigationRef as unknown as { isReady: jest.Mock; navigate: jest.Mock };
const mockedOpenListingUrl = openListingUrl as jest.MockedFunction<typeof openListingUrl>;

async function capturedTapHandler(): Promise<(response: unknown) => void> {
  await waitFor(() => expect(mockedAddListener).toHaveBeenCalled());
  const lastCall = mockedAddListener.mock.calls[mockedAddListener.mock.calls.length - 1];
  return lastCall[0];
}

/**
 * `renderHook`'s `Props` generic can only be inferred from the callback's
 * own parameter type (see its `NoInfer<Props>` signature), not from
 * `options.initialProps` - this explicit annotation is what makes that
 * inference resolve to `{ status: string }` instead of `unknown`.
 */
function renderPushSetup(initialStatus: string) {
  return renderHook(({ status }: { status: string }) => usePushNotificationSetup(status), {
    initialProps: { status: initialStatus },
  });
}

describe('unregisterCurrentDeviceToken - no-op case', () => {
  it('does nothing and makes no API call when no token has ever been registered', async () => {
    await unregisterCurrentDeviceToken();

    expect(mockedUnregisterDeviceToken).not.toHaveBeenCalled();
  });
});

describe('usePushNotificationSetup - registration', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('does not request permission or register while status is not authenticated', async () => {
    const { rerender } = await renderPushSetup('restoring');
    rerender({ status: 'unauthenticated' });

    // Nothing async to wait for here - give the effect queue a chance to
    // run, then assert the permission/registration calls never happened.
    await waitFor(() => expect(mockedAddListener).toHaveBeenCalled());
    expect(mockedGetPermissionsAsync).not.toHaveBeenCalled();
    expect(mockedRegisterDeviceToken).not.toHaveBeenCalled();
  });

  it('requests permission and registers the token once authenticated', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[abc]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });

    await renderPushSetup('authenticated');

    await waitFor(() =>
      expect(mockedRegisterDeviceToken).toHaveBeenCalledWith({
        expo_push_token: 'ExponentPushToken[abc]',
        platform: Platform.OS,
      }),
    );
  });

  it('asks for permission when not already granted, and registers once the user grants it', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'undetermined' } as never);
    mockedRequestPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[xyz]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });

    await renderPushSetup('authenticated');

    await waitFor(() =>
      expect(mockedRegisterDeviceToken).toHaveBeenCalledWith({
        expo_push_token: 'ExponentPushToken[xyz]',
        platform: Platform.OS,
      }),
    );
    expect(mockedRequestPermissionsAsync).toHaveBeenCalled();
  });

  it('never registers when permission is denied', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'undetermined' } as never);
    mockedRequestPermissionsAsync.mockResolvedValue({ status: 'denied' } as never);

    await renderPushSetup('authenticated');

    await waitFor(() => expect(mockedRequestPermissionsAsync).toHaveBeenCalled());
    expect(mockedGetExpoPushTokenAsync).not.toHaveBeenCalled();
    expect(mockedRegisterDeviceToken).not.toHaveBeenCalled();
  });

  it('never throws when getExpoPushTokenAsync rejects (e.g. no EAS project configured)', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockRejectedValue(new Error('no EAS project id'));

    await renderPushSetup('authenticated');

    await waitFor(() => expect(mockedGetExpoPushTokenAsync).toHaveBeenCalled());
    expect(mockedRegisterDeviceToken).not.toHaveBeenCalled();
  });

  it('never throws when registerDeviceToken itself fails - best-effort, silently swallowed', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[fails]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockRejectedValue(new Error('offline'));

    await renderPushSetup('authenticated');

    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));
  });
});

describe('usePushNotificationSetup - notification tap handling', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('registers a tap listener on mount and removes it on unmount', async () => {
    const remove = jest.fn();
    mockedAddListener.mockReturnValue({ remove });

    const { unmount } = await renderPushSetup('unauthenticated');
    await waitFor(() => expect(mockedAddListener).toHaveBeenCalledTimes(1));

    await unmount();
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it('opens the listing and navigates to the Listings tab when a push with a listing_url is tapped and navigation is ready', async () => {
    mockedNavigationRef.isReady.mockReturnValue(true);
    await renderPushSetup('unauthenticated');

    const handler = await capturedTapHandler();
    handler({
      notification: { request: { content: { data: { listing_url: 'https://example.com/item/1' } } } },
    });

    expect(mockedNavigationRef.navigate).toHaveBeenCalledWith('Tabs', { screen: 'Listings' });
    expect(mockedOpenListingUrl).toHaveBeenCalledWith('https://example.com/item/1');
  });

  it('still opens the listing when navigation is not ready yet, but does not attempt to navigate', async () => {
    mockedNavigationRef.isReady.mockReturnValue(false);
    await renderPushSetup('unauthenticated');

    const handler = await capturedTapHandler();
    handler({
      notification: { request: { content: { data: { listing_url: 'https://example.com/item/2' } } } },
    });

    expect(mockedNavigationRef.navigate).not.toHaveBeenCalled();
    expect(mockedOpenListingUrl).toHaveBeenCalledWith('https://example.com/item/2');
  });

  it('never calls openListingUrl when the notification carries no listing_url', async () => {
    mockedNavigationRef.isReady.mockReturnValue(true);
    await renderPushSetup('unauthenticated');

    const handler = await capturedTapHandler();
    handler({ notification: { request: { content: { data: {} } } } });

    expect(mockedOpenListingUrl).not.toHaveBeenCalled();
  });

  it('does not throw when the notification has no data payload at all', async () => {
    await renderPushSetup('unauthenticated');

    const handler = await capturedTapHandler();
    expect(() => handler({ notification: { request: { content: {} } } })).not.toThrow();
  });
});

describe('unregisterCurrentDeviceToken - after a successful registration', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('unregisters the previously-registered token, and becomes a no-op again afterward', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[logout-test]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });
    mockedUnregisterDeviceToken.mockResolvedValue(undefined);

    await renderPushSetup('authenticated');
    await waitFor(() =>
      expect(mockedRegisterDeviceToken).toHaveBeenCalledWith({
        expo_push_token: 'ExponentPushToken[logout-test]',
        platform: Platform.OS,
      }),
    );

    await unregisterCurrentDeviceToken();
    expect(mockedUnregisterDeviceToken).toHaveBeenCalledWith({ expo_push_token: 'ExponentPushToken[logout-test]' });

    await unregisterCurrentDeviceToken();
    expect(mockedUnregisterDeviceToken).toHaveBeenCalledTimes(1);
  });

  it('never throws when the unregister API call itself fails - best-effort logout', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[fails-on-logout]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });
    mockedUnregisterDeviceToken.mockRejectedValue(new Error('offline'));

    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    await expect(unregisterCurrentDeviceToken()).resolves.toBeUndefined();
  });
});

describe('usePushNotificationSetup - registration never sends notification_channel_id', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('the automatic startup registration call carries only expo_push_token and platform - no notification_channel_id key at all', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[startup-only]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: 'listing-alerts-radar-v1' });

    await renderPushSetup('authenticated');

    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));
    const requestBody = mockedRegisterDeviceToken.mock.calls[0][0];
    expect(Object.keys(requestBody).sort()).toEqual(['expo_push_token', 'platform']);
    expect('notification_channel_id' in requestBody).toBe(false);
  });
});

describe('ensureNotificationChannelsExist', () => {
  const originalPlatformOS = Platform.OS;

  afterEach(() => {
    jest.clearAllMocks();
    Object.defineProperty(Platform, 'OS', { value: originalPlatformOS, configurable: true, writable: true });
  });

  it('creates all five approved channels on Android, at HIGH importance', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true, writable: true });
    mockedSetNotificationChannelAsync.mockResolvedValue(null);

    await ensureNotificationChannelsExist();

    expect(mockedSetNotificationChannelAsync).toHaveBeenCalledTimes(5);
    const calledIds = mockedSetNotificationChannelAsync.mock.calls.map((call) => call[0]);
    expect(calledIds).toEqual([
      'listing-alerts-default-v1',
      'listing-alerts-ping-v1',
      'listing-alerts-double-v1',
      'listing-alerts-radar-v1',
      'listing-alerts-premium-v1',
    ]);
    for (const call of mockedSetNotificationChannelAsync.mock.calls) {
      expect(call[1].importance).toBe(Notifications.AndroidImportance.HIGH);
    }
  });

  it('assigns the correct sound resource to each custom channel', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true, writable: true });
    mockedSetNotificationChannelAsync.mockResolvedValue(null);

    await ensureNotificationChannelsExist();

    const soundById = new Map(
      mockedSetNotificationChannelAsync.mock.calls.map((call) => [call[0], call[1].sound]),
    );
    expect(soundById.get('listing-alerts-ping-v1')).toBe('clean_ping');
    expect(soundById.get('listing-alerts-double-v1')).toBe('double_ping');
    expect(soundById.get('listing-alerts-radar-v1')).toBe('radar');
    expect(soundById.get('listing-alerts-premium-v1')).toBe('premium_chime');
  });

  it('the default channel uses Android default sound behavior (sound: null, never a bundled file)', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true, writable: true });
    mockedSetNotificationChannelAsync.mockResolvedValue(null);

    await ensureNotificationChannelsExist();

    const defaultCall = mockedSetNotificationChannelAsync.mock.calls.find(
      (call) => call[0] === 'listing-alerts-default-v1',
    );
    expect(defaultCall?.[1].sound).toBeNull();
  });

  it('creates no channels at all on iOS - channels are an Android-only concept', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true, writable: true });

    await ensureNotificationChannelsExist();

    expect(mockedSetNotificationChannelAsync).not.toHaveBeenCalled();
  });

  it('never throws when channel creation itself fails - best-effort, like the rest of this module', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true, writable: true });
    mockedSetNotificationChannelAsync.mockRejectedValue(new Error('native module unavailable'));

    await expect(ensureNotificationChannelsExist()).resolves.toBeUndefined();
  });

  it('NOTIFICATION_CHANNELS lists exactly the five approved channel ids, in order', () => {
    expect(NOTIFICATION_CHANNELS.map((channel) => channel.id)).toEqual([
      'listing-alerts-default-v1',
      'listing-alerts-ping-v1',
      'listing-alerts-double-v1',
      'listing-alerts-radar-v1',
      'listing-alerts-premium-v1',
    ]);
  });
});

describe('getLastKnownDeviceRegistration / updateNotificationChannelPreference', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('is null once reset, and a subsequent failed registration attempt does not populate it', async () => {
    // `lastKnownRegistration` is shared module state across this whole
    // file (same established convention as `registeredDeviceToken` -
    // see this file's own top-of-file docstring) - forced to a known
    // null baseline first via the already-exported reset path, exactly
    // the way every other test here that needs a clean starting point
    // does, rather than assuming nothing earlier in the file ran first.
    mockedUnregisterDeviceToken.mockResolvedValue(undefined);
    await unregisterCurrentDeviceToken();
    expect(getLastKnownDeviceRegistration()).toBeNull();

    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[cache-check]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockRejectedValue(new Error('offline'));

    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    expect(getLastKnownDeviceRegistration()).toBeNull();
  });

  it('caches the response from a successful automatic registration, including a null (never-chosen) preference', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[cache-fill]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });

    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    expect(getLastKnownDeviceRegistration()).toEqual({
      expoPushToken: 'ExponentPushToken[cache-fill]',
      platform: Platform.OS,
      notificationChannelId: null,
    });
  });

  it('updateNotificationChannelPreference sends the chosen channel id, reusing the cached token/platform', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[radar-select]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });
    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: 'listing-alerts-radar-v1' });
    await updateNotificationChannelPreference('listing-alerts-radar-v1');

    expect(mockedRegisterDeviceToken).toHaveBeenCalledWith({
      expo_push_token: 'ExponentPushToken[radar-select]',
      platform: Platform.OS,
      notification_channel_id: 'listing-alerts-radar-v1',
    });
    expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBe('listing-alerts-radar-v1');
  });

  it('updateNotificationChannelPreference throws without ever calling the API when no device is registered yet', async () => {
    // Force a clean null baseline first - see the identical reasoning in
    // this describe block's first test.
    mockedUnregisterDeviceToken.mockResolvedValue(undefined);
    await unregisterCurrentDeviceToken();

    await expect(updateNotificationChannelPreference('listing-alerts-radar-v1')).rejects.toThrow();
    expect(mockedRegisterDeviceToken).not.toHaveBeenCalled();
  });

  it('updateNotificationChannelPreference propagates a save failure, and leaves the cache unchanged', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[save-fails]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValue({ platform: Platform.OS, notification_channel_id: null });
    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    mockedRegisterDeviceToken.mockRejectedValue(new Error('server error'));
    await expect(updateNotificationChannelPreference('listing-alerts-premium-v1')).rejects.toThrow('server error');

    // The cache must still reflect the last *successful* state, never the
    // failed attempt - this is exactly what NotificationSoundScreen relies
    // on to never display an unsaved selection as if it were persisted.
    expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBeNull();
  });
});

describe('registrationWriteSequence - stale-write race protection', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('a slow automatic registration response cannot overwrite a newer explicit selection', async () => {
    // 1. An initial, fast automatic registration succeeds normally.
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[race]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValueOnce({ platform: Platform.OS, notification_channel_id: null });
    const { rerender } = await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));
    expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBeNull();

    // 2. A second automatic run starts (e.g. a status flip) but is slow -
    // its own registerDeviceToken call is left pending, deliberately not
    // resolved yet.
    let resolveSlowRegistration!: (value: { platform: string; notification_channel_id: string | null }) => void;
    const slowRegistration = new Promise<{ platform: string; notification_channel_id: string | null }>((resolve) => {
      resolveSlowRegistration = resolve;
    });
    mockedRegisterDeviceToken.mockImplementationOnce(() => slowRegistration as never);
    await rerender({ status: 'unauthenticated' });
    await rerender({ status: 'authenticated' });
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(2));

    // 3. Before the slow run resolves, the user explicitly saves Radar -
    // this is a real, separate registerDeviceToken call that resolves
    // immediately.
    mockedRegisterDeviceToken.mockResolvedValueOnce({
      platform: Platform.OS,
      notification_channel_id: 'listing-alerts-radar-v1',
    });
    await updateNotificationChannelPreference('listing-alerts-radar-v1');
    expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(3);
    expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBe('listing-alerts-radar-v1');

    // 4. NOW the slow automatic run (started *before* the explicit save)
    // finally resolves. Its response must be discarded - it must never
    // overwrite the newer, already-applied explicit selection.
    resolveSlowRegistration({ platform: Platform.OS, notification_channel_id: null });
    await slowRegistration;
    await Promise.resolve();
    await Promise.resolve();

    expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBe('listing-alerts-radar-v1');
  });

  it('a newer automatic registration still populates the cache normally when no newer explicit save exists', async () => {
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[normal-update]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockResolvedValueOnce({ platform: Platform.OS, notification_channel_id: null });
    const { rerender } = await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    mockedRegisterDeviceToken.mockResolvedValueOnce({
      platform: Platform.OS,
      notification_channel_id: 'listing-alerts-premium-v1',
    });
    await rerender({ status: 'unauthenticated' });
    await rerender({ status: 'authenticated' });

    await waitFor(() =>
      expect(getLastKnownDeviceRegistration()?.notificationChannelId).toBe('listing-alerts-premium-v1'),
    );
  });

  it('logout invalidates a registration write already in flight, so its late response cannot repopulate the cache', async () => {
    // Force a clean null baseline first - `lastKnownRegistration` is
    // shared module state across this whole file (same established
    // convention documented at the top of this file and used by every
    // other test here that needs a known starting point).
    mockedUnregisterDeviceToken.mockResolvedValue(undefined);
    await unregisterCurrentDeviceToken();
    expect(getLastKnownDeviceRegistration()).toBeNull();
    // This reset call may itself have called the unregister API (if a
    // previous test left a token registered) - clear that call history so
    // the assertion below only reflects what happens from this point on.
    mockedUnregisterDeviceToken.mockClear();

    let resolveRegistration!: (value: { platform: string; notification_channel_id: string | null }) => void;
    const pendingRegistration = new Promise<{ platform: string; notification_channel_id: string | null }>(
      (resolve) => {
        resolveRegistration = resolve;
      },
    );
    mockedGetPermissionsAsync.mockResolvedValue({ status: 'granted' } as never);
    mockedGetExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[logout-race]', type: 'expo' } as never);
    mockedRegisterDeviceToken.mockReturnValue(pendingRegistration as never);

    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));
    // The registration hasn't resolved yet, so `registeredDeviceToken` was
    // never set - there is nothing for unregister to actually call the API
    // for, but it must still invalidate this in-flight write.
    expect(getLastKnownDeviceRegistration()).toBeNull();

    await unregisterCurrentDeviceToken();
    expect(mockedUnregisterDeviceToken).not.toHaveBeenCalled();

    // The registration finally resolves *after* logout.
    resolveRegistration({ platform: Platform.OS, notification_channel_id: 'listing-alerts-radar-v1' });
    await pendingRegistration;
    await Promise.resolve();
    await Promise.resolve();

    // Must still be null - a response arriving after logout must never
    // repopulate the cache for a session that's already gone.
    expect(getLastKnownDeviceRegistration()).toBeNull();
  });
});
