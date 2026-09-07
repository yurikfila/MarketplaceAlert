/**
 * Direct tests for the forgotPassword/resetPassword wrapper functions -
 * verifies the request path, method, and payload shape without going
 * through fetch (see api/client.test.ts for apiRequest's own HTTP-level
 * behavior). Every other endpoints.ts function is exercised indirectly
 * through the screens that call it; these two are new enough, and
 * security-sensitive enough (enumeration-safe forgot-password, 204 on
 * reset), to warrant their own direct coverage.
 */

import { apiRequest } from './client';
import { forgotPassword, resetPassword } from './endpoints';

jest.mock('./client');

const mockedApiRequest = apiRequest as jest.MockedFunction<typeof apiRequest>;

describe('forgotPassword', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('POSTs to /auth/forgot-password with the email body', async () => {
    mockedApiRequest.mockResolvedValue({ message: 'If that email is registered, a verification code has been sent.' });

    const result = await forgotPassword({ email: 'shopper@example.com' });

    expect(mockedApiRequest).toHaveBeenCalledWith('/auth/forgot-password', {
      method: 'POST',
      body: { email: 'shopper@example.com' },
    });
    expect(result).toEqual({ message: 'If that email is registered, a verification code has been sent.' });
  });
});

describe('resetPassword', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('POSTs to /auth/reset-password with the email, code, and new password', async () => {
    mockedApiRequest.mockResolvedValue(undefined);

    const result = await resetPassword({ email: 'shopper@example.com', code: '123456', new_password: 'a-strong-password' });

    expect(mockedApiRequest).toHaveBeenCalledWith('/auth/reset-password', {
      method: 'POST',
      body: { email: 'shopper@example.com', code: '123456', new_password: 'a-strong-password' },
    });
    expect(result).toBeUndefined();
  });
});
