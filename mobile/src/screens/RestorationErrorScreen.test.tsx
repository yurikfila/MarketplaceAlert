import { fireEvent } from '@testing-library/react-native';

import { useAuth } from '../auth/AuthContext';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { RestorationErrorScreen } from './RestorationErrorScreen';

jest.mock('../auth/AuthContext');

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockRetryRestoration = jest.fn();
const mockSignInInstead = jest.fn();

describe('RestorationErrorScreen', () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      status: 'restoration-error',
      user: null,
      login: jest.fn(),
      signup: jest.fn(),
      logout: jest.fn(),
      retryRestoration: mockRetryRestoration,
      signInInstead: mockSignInInstead,
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('calls retryRestoration when "Try again" is pressed', async () => {
    const { getByText } = await renderWithNavigation(RestorationErrorScreen);

    await fireEvent.press(getByText('Try again'));

    expect(mockRetryRestoration).toHaveBeenCalledTimes(1);
  });

  it('calls signInInstead when "Sign in instead" is pressed - never touches the network itself', async () => {
    const { getByText } = await renderWithNavigation(RestorationErrorScreen);

    await fireEvent.press(getByText('Sign in instead'));

    expect(mockSignInInstead).toHaveBeenCalledTimes(1);
  });
});
