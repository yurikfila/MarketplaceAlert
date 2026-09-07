import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { fireEvent, render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { ResetPasswordScreen } from './ResetPasswordScreen';

jest.mock('../api/endpoints');

const mockedResetPassword = endpoints.resetPassword as jest.MockedFunction<typeof endpoints.resetPassword>;
const mockedForgotPassword = endpoints.forgotPassword as jest.MockedFunction<typeof endpoints.forgotPassword>;

const EMAIL = 'shopper@example.com';

/**
 * Renders the real ResetPasswordScreen (with `email` as its route param,
 * matching how ForgotPasswordScreen navigates here) alongside a stub
 * "Login" route, so its "Back to sign in" navigation target (shown after
 * a successful reset) has somewhere real to land - `renderWithNavigation`
 * only ever registers a single route. Mirrors the harness in
 * SavedSearchesScreen.test.tsx.
 */
function renderWithLoginRoute() {
  const Stack = createNativeStackNavigator();

  function StubLoginScreen() {
    return <Text>stub-login-screen</Text>;
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="ResetPassword" component={ResetPasswordScreen} initialParams={{ email: EMAIL }} />
        <Stack.Screen name="Login" component={StubLoginScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

describe('ResetPasswordScreen', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('renders the email address passed as a route param', async () => {
    const { getByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    expect(getByText('Enter verification code')).toBeTruthy();
    expect(getByText(`A 6-digit code was sent to ${EMAIL}.`)).toBeTruthy();
  });

  it('strips non-digit characters and caps the code at 6 digits', async () => {
    const { getByLabelText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '12a3-45678');

    expect(getByLabelText('Verification code').props.value).toBe('123456');
  });

  it('blocks submission when the code is not exactly 6 digits', async () => {
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Enter the 6-digit code exactly as sent.')).toBeTruthy();
    expect(mockedResetPassword).not.toHaveBeenCalled();
  });

  it('blocks submission when the code is blank', async () => {
    const { getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Enter the verification code sent to your email.')).toBeTruthy();
    expect(mockedResetPassword).not.toHaveBeenCalled();
  });

  it('blocks submission when the new password is under 8 characters', async () => {
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'short');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'short');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Password must be at least 8 characters.')).toBeTruthy();
    expect(mockedResetPassword).not.toHaveBeenCalled();
  });

  it('blocks submission when the passwords do not match', async () => {
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-different-password');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Passwords do not match.')).toBeTruthy();
    expect(mockedResetPassword).not.toHaveBeenCalled();
  });

  it('calls resetPassword with the email, code, and new password on valid submission', async () => {
    mockedResetPassword.mockResolvedValue(undefined);
    const { getByLabelText, getByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockedResetPassword).toHaveBeenCalledWith({
      email: EMAIL,
      code: '123456',
      new_password: 'a-strong-password',
    });
  });

  it('shows a success message and does not auto-navigate away on its own after a successful reset', async () => {
    mockedResetPassword.mockResolvedValue(undefined);
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Password updated successfully.')).toBeTruthy();
  });

  it('returns to Login when "Back to sign in" is pressed after a successful reset', async () => {
    mockedResetPassword.mockResolvedValue(undefined);
    const { getByLabelText, getByText, findByText } = await renderWithLoginRoute();

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    await fireEvent.press(await findByText('Back to sign in'));

    expect(await findByText('stub-login-screen')).toBeTruthy();
  });

  it('shows the backend message for an invalid or expired code', async () => {
    mockedResetPassword.mockRejectedValue(new ApiError('Invalid or expired verification code', 'http', 400));
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Invalid or expired verification code')).toBeTruthy();
  });

  it('shows a generic message on a network failure', async () => {
    mockedResetPassword.mockRejectedValue(new ApiError('Could not reach the server. Check your connection and try again.', 'network'));
    const { getByLabelText, getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Verification code'), '123456');
    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    await fireEvent.press(getByText('Reset password'));

    expect(await findByText('Could not reach the server. Check your connection and try again.')).toBeTruthy();
  });

  it('calls the same forgot-password endpoint (not a separate resend API) when "Send a new code" is pressed', async () => {
    mockedForgotPassword.mockResolvedValue({ message: 'If that email is registered, a verification code has been sent.' });
    const { getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.press(getByText('Send a new code'));

    expect(mockedForgotPassword).toHaveBeenCalledWith({ email: EMAIL });
    expect(await findByText('If that email is registered, a new verification code has been sent.')).toBeTruthy();
  });

  it('shows the backend message when the resend request fails', async () => {
    mockedForgotPassword.mockRejectedValue(new ApiError('The server reported an error (HTTP 500).', 'http', 500));
    const { getByText, findByText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.press(getByText('Send a new code'));

    expect(await findByText('The server reported an error (HTTP 500).')).toBeTruthy();
  });

  it('toggles visibility on the new password field without altering its value', async () => {
    const { getAllByLabelText, getByLabelText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('New password'), 'a-strong-password');
    expect(getByLabelText('New password').props.secureTextEntry).toBe(true);

    await fireEvent.press(getAllByLabelText('Show password')[0]);

    expect(getByLabelText('New password').props.secureTextEntry).toBe(false);
    expect(getByLabelText('New password').props.value).toBe('a-strong-password');
  });

  it('toggles visibility on the confirm password field independently, without altering its value', async () => {
    const { getAllByLabelText, getByLabelText } = await renderWithNavigation(ResetPasswordScreen, { email: EMAIL });

    await fireEvent.changeText(getByLabelText('Confirm new password'), 'a-strong-password');
    expect(getByLabelText('Confirm new password').props.secureTextEntry).toBe(true);

    await fireEvent.press(getAllByLabelText('Show password')[1]);

    expect(getByLabelText('Confirm new password').props.secureTextEntry).toBe(false);
    expect(getByLabelText('Confirm new password').props.value).toBe('a-strong-password');
    // The other field's visibility is untouched by toggling this one.
    expect(getByLabelText('New password').props.secureTextEntry).toBe(true);
  });
});
