import { NavigationContainer } from '@react-navigation/native';
import { fireEvent, render } from '@testing-library/react-native';

import * as endpoints from '../api/endpoints';
import { useAuth } from '../auth/AuthContext';
import { AuthNavigator } from './AuthNavigator';

jest.mock('../auth/AuthContext');
jest.mock('../api/endpoints');

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockedForgotPassword = endpoints.forgotPassword as jest.MockedFunction<typeof endpoints.forgotPassword>;

describe('AuthNavigator', () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      status: 'unauthenticated',
      user: null,
      login: jest.fn(),
      signup: jest.fn(),
      logout: jest.fn(),
      retryRestoration: jest.fn(),
      signInInstead: jest.fn(),
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('registers ForgotPassword and ResetPassword alongside Login/Signup, and the forward navigation path works end to end', async () => {
    mockedForgotPassword.mockResolvedValue({ message: 'If that email is registered, a verification code has been sent.' });

    const { getByText, getByLabelText, findByText } = await render(
      <NavigationContainer>
        <AuthNavigator />
      </NavigationContainer>,
    );

    expect(getByText('MarketplaceAlert')).toBeTruthy(); // Login is the initial route

    await fireEvent.press(getByText('Forgot password?'));
    expect(await findByText('Reset password')).toBeTruthy(); // ForgotPasswordScreen

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    await fireEvent.press(getByText('Send verification code'));

    expect(await findByText('Enter verification code')).toBeTruthy(); // ResetPasswordScreen
    expect(await findByText('A 6-digit code was sent to shopper@example.com.')).toBeTruthy();
  });
});
