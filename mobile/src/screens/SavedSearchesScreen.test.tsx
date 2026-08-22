import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { Button, Text } from 'react-native';
import { act, fireEvent, render } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import type { SavedSearch } from '../api/types';
import { DEFAULT_AUTO_REFRESH_INTERVAL_MS } from '../hooks/useAutoRefresh';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { SavedSearchesScreen } from './SavedSearchesScreen';

jest.mock('../api/endpoints');

const mockedListSavedSearches = endpoints.listSavedSearches as jest.MockedFunction<typeof endpoints.listSavedSearches>;

const SAMPLE: SavedSearch = {
  id: 1,
  query: 'Makita drill',
  marketplaces: ['etsy', 'ebay'],
  is_active: true,
  scan_interval_seconds: 300,
  created_at: '2026-08-16T18:10:36.570797Z',
  updated_at: '2026-08-16T18:10:36.570804Z',
  last_scanned_at: '2026-08-21T18:59:20.671535Z',
};

/**
 * Renders the real SavedSearchesScreen alongside a stub "CreateSearch"
 * route in a real stack - reusing SavedSearchesScreen's own existing
 * "Create a saved search" button (already wired to
 * `navigation.navigate('CreateSearch')`) to blur it, with a "go-back"
 * button on the stub to refocus it. The only way to exercise a genuine
 * blur/refocus for the "refetch on refocus" test below -
 * renderWithNavigation only ever mounts a single, permanently-focused
 * route, which can't blur. Mirrors the harness in
 * hooks/useAutoRefresh.test.tsx.
 */
function renderWithBackAndForthNavigation() {
  const Stack = createNativeStackNavigator();

  function StubCreateSearchScreen({ navigation }: any) {
    return (
      <>
        <Text>stub-create-search-screen</Text>
        <Button title="go-back" onPress={() => navigation.goBack()} />
      </>
    );
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="SavedSearches" component={SavedSearchesScreen} />
        <Stack.Screen name="CreateSearch" component={StubCreateSearchScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

describe('SavedSearchesScreen', () => {
  afterEach(() => {
    jest.clearAllMocks();
    jest.useRealTimers();
  });

  it('shows saved searches once loaded', async () => {
    mockedListSavedSearches.mockResolvedValue([SAMPLE]);

    const { findByText } = await renderWithNavigation(SavedSearchesScreen);

    expect(await findByText('Makita drill')).toBeTruthy();
    expect(mockedListSavedSearches).toHaveBeenCalledTimes(1);
  });

  it('shows an empty state when there are no saved searches', async () => {
    mockedListSavedSearches.mockResolvedValue([]);

    const { findByText } = await renderWithNavigation(SavedSearchesScreen);

    expect(await findByText('No saved searches yet')).toBeTruthy();
  });

  it('shows an error state with a retry option when loading fails', async () => {
    mockedListSavedSearches.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));

    const { findByText } = await renderWithNavigation(SavedSearchesScreen);

    expect(await findByText('Could not reach the server.')).toBeTruthy();
    expect(await findByText('Try again')).toBeTruthy();
  });

  it('retries after a failure and shows data once the retry succeeds', async () => {
    mockedListSavedSearches.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));
    mockedListSavedSearches.mockResolvedValueOnce([SAMPLE]);

    const { findByText } = await renderWithNavigation(SavedSearchesScreen);

    const retryButton = await findByText('Try again');
    await fireEvent.press(retryButton);

    expect(await findByText('Makita drill')).toBeTruthy();
    expect(mockedListSavedSearches).toHaveBeenCalledTimes(2);
  });

  it('automatically picks up a server-side change (e.g. last_scanned_at) on the next poll, without manual action', async () => {
    jest.useFakeTimers();
    mockedListSavedSearches.mockResolvedValueOnce([SAMPLE]);

    const { findByText, queryByText, unmount } = await renderWithNavigation(SavedSearchesScreen);
    try {
      expect(await findByText('Makita drill')).toBeTruthy();
      expect(mockedListSavedSearches).toHaveBeenCalledTimes(1);

      const rescanned: SavedSearch = { ...SAMPLE, is_active: false, last_scanned_at: '2026-08-22T10:17:00Z' };
      mockedListSavedSearches.mockResolvedValueOnce([rescanned]);

      await act(async () => {
        jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS);
        await Promise.resolve();
      });

      expect(mockedListSavedSearches).toHaveBeenCalledTimes(2);
      expect(await findByText('Inactive')).toBeTruthy();
      expect(queryByText('Active')).toBeNull();
    } finally {
      // Otherwise this screen's setInterval (created under fake timers)
      // stays alive into later tests, which run under real timers again -
      // explicit cleanup here, rather than relying on the next test's
      // teardown, is what keeps this test file's tests independent.
      await unmount();
    }
  });

  it('refetches when this tab regains focus after navigating away and back, but not on the very first focus', async () => {
    mockedListSavedSearches.mockResolvedValueOnce([SAMPLE]);

    const { findByText, getByText } = await renderWithBackAndForthNavigation();
    expect(await findByText('Makita drill')).toBeTruthy();
    // The very first focus (right after mount) must not cause a second,
    // redundant fetch on top of the initial load.
    expect(mockedListSavedSearches).toHaveBeenCalledTimes(1);

    await fireEvent.press(getByText('Create a saved search'));
    expect(await findByText('stub-create-search-screen')).toBeTruthy();

    const updated: SavedSearch = { ...SAMPLE, query: 'Bosch drill' };
    mockedListSavedSearches.mockResolvedValueOnce([updated]);

    await fireEvent.press(getByText('go-back'));

    expect(await findByText('Bosch drill')).toBeTruthy();
    expect(mockedListSavedSearches).toHaveBeenCalledTimes(2);
  });
});
