import { fireEvent } from '@testing-library/react-native';

import { useAuth } from '../auth/AuthContext';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { AccountScreen } from './AccountScreen';

jest.mock('../auth/AuthContext');

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockLogout = jest.fn();

describe('AccountScreen', () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      status: 'authenticated',
      user: { id: 1, email: 'shopper@example.com', created_at: '2026-01-01T00:00:00Z' },
      login: jest.fn(),
      signup: jest.fn(),
      logout: mockLogout,
      retryRestoration: jest.fn(),
      signInInstead: jest.fn(),
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it("shows the signed-in user's email", async () => {
    const { findByText } = await renderWithNavigation(AccountScreen);

    expect(await findByText('shopper@example.com')).toBeTruthy();
  });

  it('calls logout when "Log out" is pressed - the screen itself never catches, by design', async () => {
    mockLogout.mockResolvedValue(undefined);
    const { getByText } = await renderWithNavigation(AccountScreen);

    await fireEvent.press(getByText('Log out'));

    expect(mockLogout).toHaveBeenCalledTimes(1);
  });
});
