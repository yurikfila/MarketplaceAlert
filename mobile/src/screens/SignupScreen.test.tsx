import { fireEvent } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { SignupScreen } from './SignupScreen';

jest.mock('../auth/AuthContext');

const mockedUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockSignup = jest.fn();

describe('SignupScreen', () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      status: 'unauthenticated',
      user: null,
      login: jest.fn(),
      signup: mockSignup,
      logout: jest.fn(),
      retryRestoration: jest.fn(),
      signInInstead: jest.fn(),
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('blocks submission when the password is under 8 characters', async () => {
    const { getByText, getByLabelText, findByText } = await renderWithNavigation(SignupScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'new@example.com');
    await fireEvent.changeText(getByLabelText('Password'), 'short');
    await fireEvent.press(getByText('Create account'));

    expect(await findByText('Password must be at least 8 characters.')).toBeTruthy();
    expect(mockSignup).not.toHaveBeenCalled();
  });

  it('calls signup with the trimmed email and the password on valid submission', async () => {
    mockSignup.mockResolvedValue(undefined);
    const { getByText, getByLabelText } = await renderWithNavigation(SignupScreen);

    await fireEvent.changeText(getByLabelText('Email'), '  new@example.com  ');
    await fireEvent.changeText(getByLabelText('Password'), 'a-strong-password');
    await fireEvent.press(getByText('Create account'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockSignup).toHaveBeenCalledWith('new@example.com', 'a-strong-password');
  });

  it('shows the server-provided message when signup fails', async () => {
    mockSignup.mockRejectedValue(new ApiError('An account with this email already exists', 'http', 409));
    const { getByText, getByLabelText, findByText } = await renderWithNavigation(SignupScreen);

    await fireEvent.changeText(getByLabelText('Email'), 'new@example.com');
    await fireEvent.changeText(getByLabelText('Password'), 'a-strong-password');
    await fireEvent.press(getByText('Create account'));

    expect(await findByText('An account with this email already exists')).toBeTruthy();
  });
});
