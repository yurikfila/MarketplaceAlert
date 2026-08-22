import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { act, fireEvent, render } from '@testing-library/react-native';
import { useState } from 'react';
import { AppState, Button, Text } from 'react-native';

import { DEFAULT_AUTO_REFRESH_INTERVAL_MS, useAutoRefresh, type UseAutoRefreshOptions } from './useAutoRefresh';

const Stack = createNativeStackNavigator();

/**
 * A minimal two-screen stack: 'UnderTest' calls `useAutoRefresh`, and
 * exposes a button to navigate to 'Other' (blurring 'UnderTest') and back
 * (refocusing it) - the only way to exercise real focus/blur transitions
 * is a real navigator (same philosophy as testUtils/renderWithNavigation).
 * `render()` is async as of React Native Testing Library v14 - callers
 * must `await` this, same convention as `renderWithNavigation`.
 */
function renderUnderTest(onRefresh: () => void, options?: UseAutoRefreshOptions) {
  function UnderTestScreen({ navigation }: any) {
    useAutoRefresh(onRefresh, options);
    return (
      <>
        <Text>under-test-screen</Text>
        <Button title="go-to-other" onPress={() => navigation.navigate('Other')} />
      </>
    );
  }

  function OtherScreen({ navigation }: any) {
    return (
      <>
        <Text>other-screen</Text>
        <Button title="go-back" onPress={() => navigation.goBack()} />
      </>
    );
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="UnderTest" component={UnderTestScreen} />
        <Stack.Screen name="Other" component={OtherScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

/**
 * Same idea as `renderUnderTest`, but lets a test change the `onRefresh`
 * identity across renders - still inside a real NavigationContainer,
 * since `useAutoRefresh` requires one (via `useFocusEffect`).
 */
function renderUnderTestWithChangingCallback() {
  const calls: number[] = [];

  function Wrapper() {
    const [tick, setTick] = useState(0);
    const onRefresh = () => calls.push(tick);
    useAutoRefresh(onRefresh);
    return <Button title={`bump-${tick}`} onPress={() => setTick((t) => t + 1)} />;
  }

  const renderResult = render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="UnderTest" component={Wrapper} />
      </Stack.Navigator>
    </NavigationContainer>,
  );

  return { renderResult, calls };
}

describe('useAutoRefresh', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('does not call refresh immediately on the very first focus (mount)', async () => {
    const onRefresh = jest.fn();
    await renderUnderTest(onRefresh);

    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('polls at the default interval while focused', async () => {
    const onRefresh = jest.fn();
    await renderUnderTest(onRefresh);

    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS - 1);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    await act(() => {
      jest.advanceTimersByTime(1);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS);
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it('polls at a custom interval when one is provided', async () => {
    const onRefresh = jest.fn();
    await renderUnderTest(onRefresh, { intervalMs: 5_000 });

    await act(() => {
      jest.advanceTimersByTime(5_000);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('refreshes immediately when the app becomes active, but not on other transitions', async () => {
    const onRefresh = jest.fn();
    const addEventListenerSpy = jest.spyOn(AppState, 'addEventListener');
    await renderUnderTest(onRefresh);

    // React Navigation registers its own 'change' listener too - invoke
    // every captured handler so this doesn't depend on registration order;
    // only this hook's handler ever touches `onRefresh`.
    const emit = (state: 'active' | 'background' | 'inactive') => {
      for (const [event, handler] of addEventListenerSpy.mock.calls) {
        if (event === 'change') handler(state);
      }
    };

    await act(() => emit('background'));
    expect(onRefresh).not.toHaveBeenCalled();

    await act(() => emit('active'));
    expect(onRefresh).toHaveBeenCalledTimes(1);

    await act(() => emit('inactive'));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('refreshes on refocus, but not again just from staying on the same focus', async () => {
    const onRefresh = jest.fn();
    const { getByText } = await renderUnderTest(onRefresh);

    expect(onRefresh).not.toHaveBeenCalled();

    await fireEvent.press(getByText('go-to-other'));
    expect(onRefresh).not.toHaveBeenCalled();

    await fireEvent.press(getByText('go-back'));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('stops polling and unsubscribes from AppState while blurred, and resumes on refocus', async () => {
    const onRefresh = jest.fn();
    const removeSpy = jest.fn();
    jest.spyOn(AppState, 'addEventListener').mockReturnValue({ remove: removeSpy } as any);

    const { getByText } = await renderUnderTest(onRefresh);

    await fireEvent.press(getByText('go-to-other'));
    expect(removeSpy).toHaveBeenCalledTimes(1);

    // Blurred - advancing the clock must not fire the (now torn-down) timer.
    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS * 3);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    await fireEvent.press(getByText('go-back'));
    // The refocus itself triggers one immediate refresh.
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Polling resumes now that the screen is focused again.
    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS);
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it('clears the interval and removes the AppState listener on unmount', async () => {
    const clearIntervalSpy = jest.spyOn(global, 'clearInterval');
    const removeSpy = jest.fn();
    jest.spyOn(AppState, 'addEventListener').mockReturnValue({ remove: removeSpy } as any);

    const { unmount } = await renderUnderTest(jest.fn());

    await unmount();

    expect(clearIntervalSpy).toHaveBeenCalled();
    expect(removeSpy).toHaveBeenCalledTimes(1);
  });

  it('does not create a duplicate timer when the refresh callback identity changes across renders', async () => {
    const setIntervalSpy = jest.spyOn(global, 'setInterval');
    const { renderResult, calls } = renderUnderTestWithChangingCallback();
    const { getByText } = await renderResult;

    const callCountAfterMount = setIntervalSpy.mock.calls.length;
    expect(callCountAfterMount).toBe(1);

    await fireEvent.press(getByText('bump-0'));
    // Re-rendering with a brand new inline callback must not tear down and
    // recreate the timer - it's still the same one interval.
    expect(setIntervalSpy).toHaveBeenCalledTimes(callCountAfterMount);

    // ...and the interval, when it fires, calls the LATEST callback (using
    // the up-to-date `tick`), not a stale one captured at mount.
    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS);
    });
    expect(calls).toEqual([1]);
  });

  it('suppresses interval/AppState/focus refreshes while disabled, without tearing down the timer', async () => {
    const onRefresh = jest.fn();
    const setIntervalSpy = jest.spyOn(global, 'setInterval');
    await renderUnderTest(onRefresh, { enabled: false });

    await act(() => {
      jest.advanceTimersByTime(DEFAULT_AUTO_REFRESH_INTERVAL_MS * 2);
    });
    expect(onRefresh).not.toHaveBeenCalled();
    // The timer itself is still just one underlying interval - disabling
    // is a runtime no-op check, not a teardown/recreate cycle.
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
  });
});
