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
import { unregisterCurrentDeviceToken, usePushNotificationSetup } from './pushNotifications';

jest.mock('expo-notifications', () => ({
  setNotificationHandler: jest.fn(),
  getPermissionsAsync: jest.fn(),
  requestPermissionsAsync: jest.fn(),
  getExpoPushTokenAsync: jest.fn(),
  addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
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
    mockedRegisterDeviceToken.mockResolvedValue(undefined);

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
    mockedRegisterDeviceToken.mockResolvedValue(undefined);

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
    mockedRegisterDeviceToken.mockResolvedValue(undefined);
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
    mockedRegisterDeviceToken.mockResolvedValue(undefined);
    mockedUnregisterDeviceToken.mockRejectedValue(new Error('offline'));

    await renderPushSetup('authenticated');
    await waitFor(() => expect(mockedRegisterDeviceToken).toHaveBeenCalledTimes(1));

    await expect(unregisterCurrentDeviceToken()).resolves.toBeUndefined();
  });
});
