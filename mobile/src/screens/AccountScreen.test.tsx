import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { fireEvent, render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { useAuth } from '../auth/AuthContext';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { AccountScreen } from './AccountScreen';

jest.mock('../auth/AuthContext');

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockLogout = jest.fn();

function mockAuthedUser(overrides: { is_admin: boolean }) {
  mockedUseAuth.mockReturnValue({
    status: 'authenticated',
    user: { id: 1, email: 'shopper@example.com', created_at: '2026-01-01T00:00:00Z', is_admin: overrides.is_admin },
    login: jest.fn(),
    signup: jest.fn(),
    logout: mockLogout,
    retryRestoration: jest.fn(),
    signInInstead: jest.fn(),
  });
}

/**
 * Renders the real AccountScreen alongside a stub "AdminUsers" route, so
 * pressing the Admin entry point (wired to `navigation.navigate
 * ('AdminUsers')`) has somewhere real to land - `renderWithNavigation`
 * only ever registers a single route. Mirrors the harness in
 * SavedSearchesScreen.test.tsx/LoginScreen.test.tsx.
 */
function renderWithAdminUsersRoute() {
  const Stack = createNativeStackNavigator();

  function StubAdminUsersScreen() {
    return <Text>stub-admin-users-screen</Text>;
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="Account" component={AccountScreen} />
        <Stack.Screen name="AdminUsers" component={StubAdminUsersScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

describe('AccountScreen', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it("shows the signed-in user's email", async () => {
    mockAuthedUser({ is_admin: false });
    const { findByText } = await renderWithNavigation(AccountScreen);

    expect(await findByText('shopper@example.com')).toBeTruthy();
  });

  it('calls logout when "Log out" is pressed - the screen itself never catches, by design', async () => {
    mockAuthedUser({ is_admin: false });
    mockLogout.mockResolvedValue(undefined);
    const { getByText } = await renderWithNavigation(AccountScreen);

    await fireEvent.press(getByText('Log out'));

    expect(mockLogout).toHaveBeenCalledTimes(1);
  });

  it('does not show an Admin entry point for a normal (non-admin) user', async () => {
    mockAuthedUser({ is_admin: false });
    const { queryByText } = await renderWithNavigation(AccountScreen);

    expect(queryByText('Admin: User Management')).toBeNull();
  });

  it('shows an Admin entry point for an admin user', async () => {
    mockAuthedUser({ is_admin: true });
    const { findByText } = await renderWithNavigation(AccountScreen);

    expect(await findByText('Admin: User Management')).toBeTruthy();
  });

  it('navigates to AdminUsers when the Admin entry point is pressed', async () => {
    mockAuthedUser({ is_admin: true });
    const { getByText, findByText } = await renderWithAdminUsersRoute();

    await fireEvent.press(getByText('Admin: User Management'));

    expect(await findByText('stub-admin-users-screen')).toBeTruthy();
  });
});
