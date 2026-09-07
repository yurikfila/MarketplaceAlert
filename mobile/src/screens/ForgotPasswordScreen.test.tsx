import { NavigationContainer, useRoute } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { fireEvent, render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { ForgotPasswordScreen } from './ForgotPasswordScreen';

jest.mock('../api/endpoints');

const mockedForgotPassword = endpoints.forgotPassword as jest.MockedFunction<typeof endpoints.forgotPassword>;

/**
 * Renders the real ForgotPasswordScreen alongside stub "ResetPassword" and
 * "Login" routes, so its two navigation targets (success -> ResetPassword
 * with the email param; "Back to sign in" -> Login) have somewhere real to
 * land - `renderWithNavigation` only ever registers a single route.
 * Mirrors the harness in SavedSearchesScreen.test.tsx.
 */
function renderWithDestinationRoutes() {
  const Stack = createNativeStackNavigator();

  function StubResetPasswordScreen() {
    const { params } = useRoute<any>();
    return <Text>stub-reset-password-screen:{params?.email}</Text>;
  }

  function StubLoginScreen() {
    return <Text>stub-login-screen</Text>;
  }

  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="ForgotPassword" component={ForgotPasswordScreen} />
        <Stack.Screen name="ResetPassword" component={StubResetPasswordScreen} />
        <Stack.Screen name="Login" component={StubLoginScreen} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}

describe('ForgotPasswordScreen', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('renders the title, explanation, and email field', async () => {
    const { getByText, getByLabelText } = await renderWithNavigation(ForgotPasswordScreen);

    expect(getByText('Reset password')).toBeTruthy();
    expect(getByText('Enter the email address associated with your MarketplaceAlert account.')).toBeTruthy();
    expect(getByLabelText('Email')).toBeTruthy();
  });

  it('blocks submission and shows a field error when the email is blank', async () => {
    const { getByText, findByText } = await renderWithNavigation(ForgotPasswordScreen);

    await fireEvent.press(getByText('Send verification code'));

    expect(await findByText('Enter your email address.')).toBeTruthy();
    expect(mockedForgotPassword).not.toHaveBeenCalled();
  });

  it('calls forgotPassword with the trimmed email on valid submission', async () => {
    mockedForgotPassword.mockResolvedValue({ message: 'If that email is registered, a verification code has been sent.' });
    const { getByText, getByLabelText } = await renderWithDestinationRoutes();

    await fireEvent.changeText(getByLabelText('Email'), '  shopper@example.com  ');
    await fireEvent.press(getByText('Send verification code'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockedForgotPassword).toHaveBeenCalledWith({ email: 'shopper@example.com' });
  });

  it('navigates to ResetPassword with the trimmed email after a successful request, regardless of whether the account exists (enumeration-safe)', async () => {
    mockedForgotPassword.mockResolvedValue({ message: 'If that email is registered, a verification code has been sent.' });
    const { getByText, getByLabelText, findByText } = await renderWithDestinationRoutes();

    await fireEvent.changeText(getByLabelText('Email'), 'unknown@example.com');
    await fireEvent.press(getByText('Send verification code'));

    expect(await findByText('stub-reset-password-screen:unknown@example.com')).toBeTruthy();
  });

  it('shows a loading state and disables the button while the request is in flight', async () => {
    let resolveRequest: (value: { message: string }) => void = () => {};
    mockedForgotPassword.mockReturnValue(
      new Promise((resolve) => {
        resolveRequest = resolve;
      }),
    );
    const { getByText, getByLabelText } = await renderWithNavigation(ForgotPasswordScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    // Deliberately not awaited - the mocked request never resolves until
    // `resolveRequest` is called below, and `fireEvent.press` awaits the
    // pressed handler to settle, which would hang the test otherwise.
    fireEvent.press(getByText('Send verification code'));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(getByLabelText('Send verification code').props.accessibilityState.busy).toBe(true);

    resolveRequest({ message: 'If that email is registered, a verification code has been sent.' });
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  it('shows the server-provided message when the request fails', async () => {
    mockedForgotPassword.mockRejectedValue(new ApiError('The server reported an error (HTTP 500).', 'http', 500));
    const { getByText, getByLabelText, findByText } = await renderWithNavigation(ForgotPasswordScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    await fireEvent.press(getByText('Send verification code'));

    expect(await findByText('The server reported an error (HTTP 500).')).toBeTruthy();
  });

  it('shows a generic message on a network failure', async () => {
    mockedForgotPassword.mockRejectedValue(new ApiError('Could not reach the server. Check your connection and try again.', 'network'));
    const { getByText, getByLabelText, findByText } = await renderWithNavigation(ForgotPasswordScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'shopper@example.com');
    await fireEvent.press(getByText('Send verification code'));

    expect(await findByText('Could not reach the server. Check your connection and try again.')).toBeTruthy();
  });

  it('navigates to Login when "Back to sign in" is pressed', async () => {
    const { getByText, findByText } = await renderWithDestinationRoutes();

    await fireEvent.press(getByText('Back to sign in'));

    expect(await findByText('stub-login-screen')).toBeTruthy();
  });
});
