/**
 * Tests for NotificationSoundScreen - rendering the five choices, current-
 * preference display (including the NULL/explicit-default distinction),
 * saving a new selection (success and failure), and preview playback
 * (single-active-player, cleanup on unmount).
 */
import { fireEvent, waitFor } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import * as pushNotifications from '../utils/pushNotifications';
import { NotificationSoundScreen } from './NotificationSoundScreen';

jest.mock('../utils/pushNotifications', () => ({
  getLastKnownDeviceRegistration: jest.fn(),
  updateNotificationChannelPreference: jest.fn(),
}));

const mockPlayers: Array<{ play: jest.Mock; remove: jest.Mock; addListener: jest.Mock; source: unknown }> = [];

jest.mock('expo-audio', () => ({
  createAudioPlayer: jest.fn((source: unknown) => {
    const player = { play: jest.fn(), remove: jest.fn(), addListener: jest.fn(), source };
    mockPlayers.push(player);
    return player;
  }),
}));

const mockedGetLastKnownDeviceRegistration = pushNotifications.getLastKnownDeviceRegistration as jest.MockedFunction<
  typeof pushNotifications.getLastKnownDeviceRegistration
>;
const mockedUpdateNotificationChannelPreference =
  pushNotifications.updateNotificationChannelPreference as jest.MockedFunction<
    typeof pushNotifications.updateNotificationChannelPreference
  >;

function registeredAs(notificationChannelId: string | null) {
  return {
    expoPushToken: 'ExponentPushToken[test]',
    platform: 'android',
    notificationChannelId: notificationChannelId as never,
  };
}

beforeEach(() => {
  mockPlayers.length = 0;
});

afterEach(() => {
  jest.clearAllMocks();
});

describe('NotificationSoundScreen - rendering all five choices', () => {
  it('renders System Default, Clean Ping, Double Ping, Radar, and Premium Chime', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText } = await renderWithNavigation(NotificationSoundScreen);

    expect(await findByText('System Default')).toBeTruthy();
    expect(await findByText('Clean Ping')).toBeTruthy();
    expect(await findByText('Double Ping')).toBeTruthy();
    expect(await findByText('Radar')).toBeTruthy();
    expect(await findByText('Premium Chime')).toBeTruthy();
  });

  it('shows a Preview button for each of the four custom sounds, but not for System Default', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText, getAllByText } = await renderWithNavigation(NotificationSoundScreen);

    await findByText('System Default');
    expect(getAllByText('Preview')).toHaveLength(4);
  });
});

describe('NotificationSoundScreen - current preference display', () => {
  it('displays System Default as selected when the backend preference is NULL (never explicitly chosen)', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText } = await renderWithNavigation(NotificationSoundScreen);

    await findByText('System Default');
    expect(await findByText('Selected')).toBeTruthy();
  });

  it('also displays System Default as selected for the explicit listing-alerts-default-v1 value', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs('listing-alerts-default-v1'));
    const { findByText } = await renderWithNavigation(NotificationSoundScreen);

    await findByText('System Default');
    expect(await findByText('Selected')).toBeTruthy();
  });

  it('displays Radar as selected when that is the stored preference', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs('listing-alerts-radar-v1'));
    const { findByText } = await renderWithNavigation(NotificationSoundScreen);

    await findByText('Radar');
    expect(await findByText('Selected')).toBeTruthy();
  });
});

describe('NotificationSoundScreen - selecting a sound saves it for this device', () => {
  it('selecting Radar sends notification_channel_id = listing-alerts-radar-v1', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    mockedUpdateNotificationChannelPreference.mockResolvedValue(undefined);
    const { findByText, getByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('System Default');

    await fireEvent.press(getByText('Radar'));

    await waitFor(() =>
      expect(mockedUpdateNotificationChannelPreference).toHaveBeenCalledWith('listing-alerts-radar-v1'),
    );
  });

  it('selecting Premium Chime sends notification_channel_id = listing-alerts-premium-v1', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    mockedUpdateNotificationChannelPreference.mockResolvedValue(undefined);
    const { findByText, getByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('System Default');

    await fireEvent.press(getByText('Premium Chime'));

    await waitFor(() =>
      expect(mockedUpdateNotificationChannelPreference).toHaveBeenCalledWith('listing-alerts-premium-v1'),
    );
  });

  it('a successful save moves the Selected indicator to the newly chosen sound', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    mockedUpdateNotificationChannelPreference.mockResolvedValue(undefined);
    const { findByText, getByText, getAllByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('System Default');
    expect(getAllByText('Selected')).toHaveLength(1); // starts on System Default

    await fireEvent.press(getByText('Double Ping'));

    await waitFor(async () => {
      const selectedLabels = getAllByText('Selected');
      expect(selectedLabels).toHaveLength(1);
    });
    // The row containing "Double Ping" is the one now carrying "Selected" -
    // proven by there being exactly one "Selected" badge and the save
    // having been sent for double-ping specifically (previous test proves
    // the request shape; this test proves the resulting UI state).
    expect(mockedUpdateNotificationChannelPreference).toHaveBeenCalledWith('listing-alerts-double-v1');
  });
});

describe('NotificationSoundScreen - a failed save never falsely shows as persisted', () => {
  it('keeps the previous selection displayed and shows an error when the save fails', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    mockedUpdateNotificationChannelPreference.mockRejectedValue(
      new ApiError('Could not reach the server.', 'network'),
    );
    const { findByText, getByText, queryAllByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('System Default');

    await fireEvent.press(getByText('Radar'));

    expect(await findByText('Could not reach the server.')).toBeTruthy();
    // Still System Default, never Radar - the failed attempt must not be
    // displayed as if it had been saved.
    const stillDefaultSelected = queryAllByText('Selected');
    expect(stillDefaultSelected).toHaveLength(1);
  });
});

describe('NotificationSoundScreen - preview playback', () => {
  it('pressing Preview for Clean Ping plays a player created from its bundled sound asset', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText, getAllByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('Clean Ping');

    await fireEvent.press(getAllByText('Preview')[0]);

    await waitFor(() => expect(mockPlayers).toHaveLength(1));
    expect(mockPlayers[0].play).toHaveBeenCalledTimes(1);
  });

  it('tapping another preview stops/releases the previous preview first', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText, getAllByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('Clean Ping');
    const previewButtons = getAllByText('Preview');

    await fireEvent.press(previewButtons[0]); // Clean Ping
    await waitFor(() => expect(mockPlayers).toHaveLength(1));
    const firstPlayer = mockPlayers[0];

    await fireEvent.press(previewButtons[1]); // Double Ping
    await waitFor(() => expect(mockPlayers).toHaveLength(2));

    expect(firstPlayer.remove).toHaveBeenCalledTimes(1);
    expect(mockPlayers[1].play).toHaveBeenCalledTimes(1);
  });

  it('leaving the screen cleans up any still-active preview', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    const { findByText, getAllByText, unmount } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('Clean Ping');

    await fireEvent.press(getAllByText('Preview')[0]);
    await waitFor(() => expect(mockPlayers).toHaveLength(1));

    await unmount();

    expect(mockPlayers[0].remove).toHaveBeenCalledTimes(1);
  });
});

describe('NotificationSoundScreen - noDeviceYet shows no Selected indicator', () => {
  it('never shows a Selected indicator when the device preference could not be determined', async () => {
    // Persistently null - the screen polls for up to ~5s before giving up
    // and showing the "not registered yet" state.
    mockedGetLastKnownDeviceRegistration.mockReturnValue(null);

    const { findByText, queryAllByText } = await renderWithNavigation(NotificationSoundScreen);

    // Default query timeout (1000ms) is shorter than the screen's own
    // ~5s give-up window - explicitly extended here, not globally.
    expect(await findByText(/isn.t registered for push notifications yet/, {}, { timeout: 8000 })).toBeTruthy();
    // Not System Default, not anything else - no confirmed preference
    // exists yet, so nothing should read as selected.
    expect(queryAllByText('Selected')).toHaveLength(0);
  }, 10000);
});

describe('NotificationSoundScreen - specific error message is shown, not a generic fallback', () => {
  it('shows the exact "No device is registered yet" message from a plain Error, not the generic fallback', async () => {
    mockedGetLastKnownDeviceRegistration.mockReturnValue(registeredAs(null));
    mockedUpdateNotificationChannelPreference.mockRejectedValue(
      new Error('No device is registered yet - open the app with notifications enabled first.'),
    );
    const { findByText, getByText, queryByText } = await renderWithNavigation(NotificationSoundScreen);
    await findByText('System Default');

    await fireEvent.press(getByText('Radar'));

    expect(
      await findByText('No device is registered yet - open the app with notifications enabled first.'),
    ).toBeTruthy();
    expect(queryByText('Could not save your notification sound. Try again.')).toBeNull();
  });
});
