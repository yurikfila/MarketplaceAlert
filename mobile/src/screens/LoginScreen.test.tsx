import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { fireEvent, render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { LoginScreen } from './LoginScreen';

jest.mock('../auth/AuthContext');

/**
 * Renders the real LoginScreen alongside a stub "ForgotPassword" route in
 * a real stack, so pressing "Forgot password?" (wired to
 * `navigation.navigate('ForgotPassword')`) has somewhere real to land -
 * `renderWithNavigation` only ever registers a single route. Mirrors the
 * harness in SavedSearchesScreen.test.tsx.
 */
function renderWithForgotPasswordRoute() {
  const Stack = createNativeStackNavigator();

  function StubForgotPasswordScreen() {
    return <Text>stub-forgot-password-screen</Text>;
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="Login" component={LoginScreen} />
        <Stack.Screen name="ForgotPassword" component={StubForgotPasswordScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockLogin = jest.fn();

describe('LoginScreen', () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      status: 'unauthenticated',
      user: null,
      login: mockLogin,
      signup: jest.fn(),
      logout: jest.fn(),
      retryRestoration: jest.fn(),
      signInInstead: jest.fn(),
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('blocks submission and shows field errors when both fields are blank', async () => {
    const { getByText, findByText } = await renderWithNavigation(LoginScreen);

    await fireEvent.press(getByText('Sign in'));

    expect(await findByText('Enter your email address.')).toBeTruthy();
    expect(await findByText('Enter your password.')).toBeTruthy();
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it('calls login with the trimmed email and the password on valid submission', async () => {
    mockLogin.mockResolvedValue(undefined);
    const { getByText, getByLabelText } = await renderWithNavigation(LoginScreen);

    await fireEvent.changeText(getByLabelText('Email'), '  shopper@example.com  ');
    await fireEvent.changeText(getByLabelText('Password'), 'hunter2');
    await fireEvent.press(getByText('Sign in'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockLogin).toHaveBeenCalledWith('shopper@example.com', 'hunter2');
  });

  it('shows the server-provided message when login fails, and never navigates itself', async () => {
    mockLogin.mockRejectedValue(new ApiError('Incorrect email or password', 'http', 401));
    const { getByText, getByLabelText, findByText } = await renderWithNavigation(LoginScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    await fireEvent.changeText(getByLabelText('Password'), 'wrong-password');
    await fireEvent.press(getByText('Sign in'));

    expect(await findByText('Incorrect email or password')).toBeTruthy();
  });

  it('hides the password by default and reveals/hides it on tap without altering the value', async () => {
    const { getByLabelText } = await renderWithNavigation(LoginScreen);

    await fireEvent.changeText(getByLabelText('Password'), 'hunter2');
    expect(getByLabelText('Password').props.secureTextEntry).toBe(true);

    await fireEvent.press(getByLabelText('Show password'));
    expect(getByLabelText('Password').props.secureTextEntry).toBe(false);
    expect(getByLabelText('Password').props.value).toBe('hunter2');

    await fireEvent.press(getByLabelText('Hide password'));
    expect(getByLabelText('Password').props.secureTextEntry).toBe(true);
    expect(getByLabelText('Password').props.value).toBe('hunter2');
  });

  it('still submits correctly after the password visibility has been toggled', async () => {
    mockLogin.mockResolvedValue(undefined);
    const { getByText, getByLabelText } = await renderWithNavigation(LoginScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    await fireEvent.changeText(getByLabelText('Password'), 'hunter2');
    await fireEvent.press(getByLabelText('Show password'));
    await fireEvent.press(getByText('Sign in'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockLogin).toHaveBeenCalledWith('shopper@example.com', 'hunter2');
  });

  it('shows a "Forgot password?" link', async () => {
    const { getByText } = await renderWithNavigation(LoginScreen);

    expect(getByText('Forgot password?')).toBeTruthy();
  });

  it('navigates to ForgotPassword when "Forgot password?" is pressed', async () => {
    const { getByText, findByText } = await renderWithForgotPasswordRoute();

    await fireEvent.press(getByText('Forgot password?'));

    expect(await findByText('stub-forgot-password-screen')).toBeTruthy();
  });
});
