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
import { forgotPassword, getAdminStats, listAdminUsers, registerDeviceToken, resetPassword, unregisterDeviceToken } from './endpoints';

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

describe('listAdminUsers', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('GETs /admin/users with no query and no body - authorization comes only from the bearer token apiRequest already attaches', async () => {
    const response = {
      total_users: 1,
      users: [
        {
          id: 1,
          email: 'admin@example.com',
          created_at: '2026-01-01T00:00:00Z',
          is_admin: true,
          is_active: true,
          saved_search_count: 0,
        },
      ],
    };
    mockedApiRequest.mockResolvedValue(response);

    const result = await listAdminUsers();

    expect(mockedApiRequest).toHaveBeenCalledWith('/admin/users');
    expect(result).toEqual(response);
  });
});

describe('getAdminStats', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('GETs /admin/stats', async () => {
    mockedApiRequest.mockResolvedValue({ total_users: 5, total_saved_searches: 12 });

    const result = await getAdminStats();

    expect(mockedApiRequest).toHaveBeenCalledWith('/admin/stats');
    expect(result).toEqual({ total_users: 5, total_saved_searches: 12 });
  });
});

describe('registerDeviceToken', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('POSTs to /devices with the token and platform', async () => {
    mockedApiRequest.mockResolvedValue(undefined);

    const result = await registerDeviceToken({ expo_push_token: 'ExponentPushToken[abc]', platform: 'android' });

    expect(mockedApiRequest).toHaveBeenCalledWith('/devices', {
      method: 'POST',
      body: { expo_push_token: 'ExponentPushToken[abc]', platform: 'android' },
    });
    expect(result).toBeUndefined();
  });
});

describe('unregisterDeviceToken', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('DELETEs /devices with the token', async () => {
    mockedApiRequest.mockResolvedValue(undefined);

    const result = await unregisterDeviceToken({ expo_push_token: 'ExponentPushToken[abc]' });

    expect(mockedApiRequest).toHaveBeenCalledWith('/devices', {
      method: 'DELETE',
      body: { expo_push_token: 'ExponentPushToken[abc]' },
    });
    expect(result).toBeUndefined();
  });
});
