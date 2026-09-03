/**
 * Tests for AuthContext - specifically the definitive-vs-transient refresh
 * classification and its effect on both session restoration (startup) and
 * the reactive 401 handler registered with api/client.ts. `../api/endpoints`
 * and `./tokenStorage` are mocked so every test controls exactly what the
 * "backend" and "device storage" do, without a real network call or a real
 * expo-secure-store.
 */

import { act, render, waitFor } from '@testing-library/react-native';
import { Text } from 'react-native';

import { ApiError } from '../api/client';
import * as clientModule from '../api/client';
import * as endpoints from '../api/endpoints';
import type { TokenPair, UserPublic } from '../api/types';
import { AuthProvider, useAuth, type AuthStatus } from './AuthContext';
import * as tokenStorage from './tokenStorage';

jest.mock('../api/endpoints');
jest.mock('./tokenStorage');

const mockedEndpoints = endpoints as jest.Mocked<typeof endpoints>;
const mockedTokenStorage = tokenStorage as jest.Mocked<typeof tokenStorage>;

const USER: UserPublic = { id: 1, email: 'shopper@example.com', created_at: '2026-01-01T00:00:00Z' };

type Auth = ReturnType<typeof useAuth>;

function Probe({ onReady }: { onReady: (auth: Auth) => void }) {
  const auth = useAuth();
  onReady(auth);
  return <Text testID="status">{auth.status}</Text>;
}

async function renderAuth() {
  let auth: Auth | null = null;
  const utils = await render(
    <AuthProvider>
      <Probe
        onReady={(value) => {
          auth = value;
        }}
      />
    </AuthProvider>,
  );
  await waitFor(() => expect(auth).not.toBeNull());
  return { ...utils, getAuth: (): Auth => auth as Auth };
}

async function waitForStatus(getAuth: () => Auth, status: AuthStatus) {
  await waitFor(() => expect(getAuth().status).toBe(status));
}

/** A promise this test controls the settlement of - used to hold a mocked network/storage call open so a "stale" operation can be raced against a session-replacing one (logout/login). */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Drives AuthProvider to `authenticated` via a mocked successful restoration, and captures the 401 handler it registers with api/client.ts - shared by every describe block below that needs to simulate a reactive refresh. */
async function renderAuthenticated() {
  mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
  mockedEndpoints.refreshToken.mockResolvedValueOnce({ access_token: 'initial-access', refresh_token: 'initial-refresh', token_type: 'bearer' });
  mockedTokenStorage.setRefreshToken.mockResolvedValue(undefined);
  mockedEndpoints.getCurrentUser.mockResolvedValue(USER);

  const registerSpy = jest.spyOn(clientModule, 'setUnauthorizedHandler');
  const utils = await renderAuth();
  await waitForStatus(utils.getAuth, 'authenticated');

  const registeredHandler = registerSpy.mock.calls.at(-1)?.[0];
  if (!registeredHandler) {
    throw new Error('AuthProvider never registered a 401 handler with api/client.ts');
  }
  return { ...utils, registeredHandler };
}

describe('AuthContext - session restoration', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('with no stored refresh token, restoration lands on unauthenticated without touching storage', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue(null);

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'unauthenticated');
    expect(mockedEndpoints.refreshToken).not.toHaveBeenCalled();
    expect(mockedTokenStorage.clearRefreshToken).not.toHaveBeenCalled();
  });

  it('a transient network failure during restoration preserves the stored token and shows restoration-error', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValue(new ApiError('Could not reach the server.', 'network'));

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'restoration-error');
    expect(mockedTokenStorage.clearRefreshToken).not.toHaveBeenCalled();
    expect(mockedTokenStorage.setRefreshToken).not.toHaveBeenCalled();
  });

  it('a 5xx from refresh during restoration also preserves the stored token and shows restoration-error', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValue(new ApiError('The server reported an error (HTTP 503).', 'http', 503));

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'restoration-error');
    expect(mockedTokenStorage.clearRefreshToken).not.toHaveBeenCalled();
  });

  it('a definitive 401 from refresh during restoration clears the stored token exactly once and lands on unauthenticated', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValue(new ApiError('Invalid or expired refresh token', 'http', 401));

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'unauthenticated');
    expect(mockedTokenStorage.clearRefreshToken).toHaveBeenCalledTimes(1);
  });

  it('a rotation storage failure (refresh succeeds, SecureStore write fails) clears the session and never retries with the old token', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockResolvedValue({ access_token: 'new-access', refresh_token: 'new-refresh', token_type: 'bearer' });
    mockedTokenStorage.setRefreshToken.mockRejectedValue(new Error('Keychain write failed'));

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'unauthenticated');
    expect(mockedTokenStorage.clearRefreshToken).toHaveBeenCalledTimes(1);
    expect(mockedEndpoints.getCurrentUser).not.toHaveBeenCalled();
  });

  it('a successful refresh followed by a successful /auth/me lands on authenticated with the fetched user', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockResolvedValue({ access_token: 'new-access', refresh_token: 'new-refresh', token_type: 'bearer' });
    mockedTokenStorage.setRefreshToken.mockResolvedValue(undefined);
    mockedEndpoints.getCurrentUser.mockResolvedValue(USER);

    const { getAuth } = await renderAuth();

    await waitForStatus(getAuth, 'authenticated');
    expect(getAuth().user).toEqual(USER);
  });

  it('retryRestoration re-runs restoration from a restoration-error state', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));

    const { getAuth } = await renderAuth();
    await waitForStatus(getAuth, 'restoration-error');

    mockedEndpoints.refreshToken.mockResolvedValueOnce({ access_token: 'a', refresh_token: 'b', token_type: 'bearer' });
    mockedEndpoints.getCurrentUser.mockResolvedValue(USER);

    await act(async () => {
      getAuth().retryRestoration();
    });

    await waitForStatus(getAuth, 'authenticated');
  });

  it('signInInstead discards the local session without calling the refresh endpoint again', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValue(new ApiError('Could not reach the server.', 'network'));

    const { getAuth } = await renderAuth();
    await waitForStatus(getAuth, 'restoration-error');
    mockedEndpoints.refreshToken.mockClear();

    await act(async () => {
      getAuth().signInInstead();
    });

    await waitForStatus(getAuth, 'unauthenticated');
    expect(mockedTokenStorage.clearRefreshToken).toHaveBeenCalledTimes(1);
    expect(mockedEndpoints.refreshToken).not.toHaveBeenCalled();
  });
});

describe('AuthContext - reactive 401 handler (registered with api/client.ts)', () => {
  afterEach(() => {
    jest.clearAllMocks();
    clientModule.setUnauthorizedHandler(null);
  });

  it('a transient failure leaves status unchanged and preserves the stored token - distinct from restoration', async () => {
    const { getAuth, registeredHandler } = await renderAuthenticated();

    mockedEndpoints.refreshToken.mockRejectedValueOnce(new ApiError('The server reported an error (HTTP 500).', 'http', 500));

    let outcome: string | null = 'unset';
    await act(async () => {
      outcome = await registeredHandler();
    });

    expect(outcome).toBeNull();
    expect(getAuth().status).toBe('authenticated');
    expect(mockedTokenStorage.clearRefreshToken).not.toHaveBeenCalled();
  });

  it('a definitive 401 clears the session exactly once', async () => {
    const { getAuth, registeredHandler } = await renderAuthenticated();

    mockedEndpoints.refreshToken.mockRejectedValueOnce(new ApiError('Invalid or expired refresh token', 'http', 401));

    let outcome: string | null = 'unset';
    await act(async () => {
      outcome = await registeredHandler();
    });

    expect(outcome).toBeNull();
    await waitForStatus(getAuth, 'unauthenticated');
    expect(mockedTokenStorage.clearRefreshToken).toHaveBeenCalledTimes(1);
  });

  it('a successful refresh returns the new access token and keeps status authenticated', async () => {
    const { getAuth, registeredHandler } = await renderAuthenticated();

    mockedEndpoints.refreshToken.mockResolvedValueOnce({ access_token: 'rotated-access', refresh_token: 'rotated-refresh', token_type: 'bearer' });

    let outcome: string | null = null;
    await act(async () => {
      outcome = await registeredHandler();
    });

    expect(outcome).toBe('rotated-access');
    expect(getAuth().status).toBe('authenticated');
  });
});

describe('AuthContext - logout', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('clears local state even when the network logout call fails, and never touches the refresh endpoint', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockResolvedValueOnce({ access_token: 'a', refresh_token: 'b', token_type: 'bearer' });
    mockedEndpoints.getCurrentUser.mockResolvedValue(USER);

    const { getAuth } = await renderAuth();
    await waitForStatus(getAuth, 'authenticated');

    mockedEndpoints.logout.mockRejectedValue(new ApiError('Could not reach the server.', 'network'));

    await act(async () => {
      await getAuth().logout();
    });

    expect(getAuth().status).toBe('unauthenticated');
    expect(mockedTokenStorage.clearRefreshToken).toHaveBeenCalledTimes(1);
    expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1); // only the earlier restoration call - never re-invoked by logout
  });

  it('is a no-op call to /auth/logout when there is no stored refresh token', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue(null);

    const { getAuth } = await renderAuth();
    await waitForStatus(getAuth, 'unauthenticated');

    await act(async () => {
      await getAuth().logout();
    });

    expect(mockedEndpoints.logout).not.toHaveBeenCalled();
    expect(getAuth().status).toBe('unauthenticated');
  });
});

/**
 * Regression tests for the two pre-commit review findings fixed here:
 * (1, HIGH) logout did not coordinate with a concurrently in-flight
 * reactive refresh, so a late-resolving refresh could silently reinstate
 * a session the user had just logged out of, or leave a live rotated
 * refresh token behind in SecureStore. (2, MEDIUM) restoreSession had no
 * reentrancy guard, so two overlapping restoration attempts could each
 * present the same rotating refresh token to the backend, triggering its
 * reuse-detection and revoking every session for the account. Both are
 * closed by AuthContext's session-generation guard (see that module's
 * docstring) plus the restoration single-flight wrapper.
 */
describe('AuthContext - session generation guard (logout/login vs. stale refresh races)', () => {
  afterEach(() => {
    jest.clearAllMocks();
    clientModule.setUnauthorizedHandler(null);
  });

  it('A: a reactive refresh in flight when logout begins cannot resurrect the session once it resolves', async () => {
    const { getAuth, registeredHandler } = await renderAuthenticated();
    mockedEndpoints.refreshToken.mockClear();

    const pending = deferred<TokenPair>();
    mockedEndpoints.refreshToken.mockReturnValueOnce(pending.promise);
    mockedEndpoints.logout.mockResolvedValue(undefined);

    const handlerPromise = registeredHandler();
    await waitFor(() => expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1));

    await act(async () => {
      await getAuth().logout();
    });

    expect(getAuth().status).toBe('unauthenticated');
    expect(getAuth().user).toBeNull();

    // The stale reactive refresh finally resolves successfully, well
    // after logout has already cleared everything.
    await act(async () => {
      pending.resolve({ access_token: 'late-access', refresh_token: 'late-refresh', token_type: 'bearer' });
      await handlerPromise;
    });

    expect(getAuth().status).toBe('unauthenticated');
    expect(getAuth().user).toBeNull();
    expect(mockedTokenStorage.setRefreshToken).not.toHaveBeenCalledWith('late-refresh');
  });

  it('B: a new login remains authoritative over a late-resolving stale restoration refresh', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    const pending = deferred<TokenPair>();
    mockedEndpoints.refreshToken.mockReturnValueOnce(pending.promise);

    const { getAuth } = await renderAuth();
    await waitFor(() => expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1));
    expect(getAuth().status).toBe('restoring');

    const NEW_USER: UserPublic = { id: 2, email: 'newaccount@example.com', created_at: '2026-02-01T00:00:00Z' };
    mockedEndpoints.login.mockResolvedValue({
      user: NEW_USER,
      tokens: { access_token: 'login-access', refresh_token: 'login-refresh', token_type: 'bearer' },
    });

    await act(async () => {
      await getAuth().login('newaccount@example.com', 'hunter2');
    });

    expect(getAuth().status).toBe('authenticated');
    expect(getAuth().user).toEqual(NEW_USER);
    expect(mockedTokenStorage.setRefreshToken).toHaveBeenLastCalledWith('login-refresh');

    // The stale restoration attempt from before login finally resolves.
    await act(async () => {
      pending.resolve({ access_token: 'stale-access', refresh_token: 'stale-refresh', token_type: 'bearer' });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(getAuth().status).toBe('authenticated');
    expect(getAuth().user).toEqual(NEW_USER);
    expect(mockedTokenStorage.setRefreshToken).not.toHaveBeenCalledWith('stale-refresh');
  });

  it('C: two concurrent retryRestoration calls share a single in-flight refresh request', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    mockedEndpoints.refreshToken.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));

    const { getAuth } = await renderAuth();
    await waitForStatus(getAuth, 'restoration-error');

    mockedEndpoints.refreshToken.mockClear();
    const pending = deferred<TokenPair>();
    mockedEndpoints.refreshToken.mockReturnValueOnce(pending.promise);

    await act(async () => {
      getAuth().retryRestoration();
      getAuth().retryRestoration(); // second, overlapping call
    });

    expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1);

    mockedEndpoints.getCurrentUser.mockResolvedValue(USER);
    await act(async () => {
      pending.resolve({ access_token: 'a', refresh_token: 'b', token_type: 'bearer' });
    });

    await waitForStatus(getAuth, 'authenticated');
    expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1); // still just one, even once settled
  });

  it('D: a refresh that becomes stale mid-write triggers a compare-and-clear, never leaving its token in SecureStore', async () => {
    const { getAuth, registeredHandler } = await renderAuthenticated();
    mockedEndpoints.refreshToken.mockClear();
    mockedTokenStorage.setRefreshToken.mockClear();

    mockedEndpoints.refreshToken.mockResolvedValueOnce({
      access_token: 'late-access',
      refresh_token: 'late-refresh',
      token_type: 'bearer',
    });
    const pendingWrite = deferred<void>();
    mockedTokenStorage.setRefreshToken.mockReturnValueOnce(pendingWrite.promise);
    mockedEndpoints.logout.mockResolvedValue(undefined);
    mockedTokenStorage.clearRefreshTokenIfMatches.mockResolvedValue(undefined);

    const handlerPromise = registeredHandler();
    // The refresh call has resolved and this refresh is now writing the
    // rotated token to SecureStore - generation is still current at this
    // point, so the write was attempted (unlike test A, where logout wins
    // before the write is even started).
    await waitFor(() => expect(mockedTokenStorage.setRefreshToken).toHaveBeenCalledWith('late-refresh'));

    await act(async () => {
      await getAuth().logout();
    });

    // The write this stale refresh started finally lands, after logout
    // has already cleared the session.
    await act(async () => {
      pendingWrite.resolve();
      await handlerPromise;
    });

    expect(mockedTokenStorage.clearRefreshTokenIfMatches).toHaveBeenCalledWith('late-refresh');
    expect(getAuth().status).toBe('unauthenticated');
  });

  it('E: a stale definitive-failure from an old generation cannot clear a newer, already-authenticated session', async () => {
    mockedTokenStorage.getRefreshToken.mockResolvedValue('stored-refresh-token');
    const pending = deferred<TokenPair>();
    mockedEndpoints.refreshToken.mockReturnValueOnce(pending.promise);

    const { getAuth } = await renderAuth();
    await waitFor(() => expect(mockedEndpoints.refreshToken).toHaveBeenCalledTimes(1));
    expect(getAuth().status).toBe('restoring');

    mockedEndpoints.login.mockResolvedValue({
      user: USER,
      tokens: { access_token: 'login-access', refresh_token: 'login-refresh', token_type: 'bearer' },
    });

    await act(async () => {
      await getAuth().login('shopper@example.com', 'hunter2');
    });

    expect(getAuth().status).toBe('authenticated');

    // The stale restoration attempt from before login now rejects with a
    // definitive 401 - it must not clear the session login just established.
    await act(async () => {
      pending.reject(new ApiError('Invalid or expired refresh token', 'http', 401));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(getAuth().status).toBe('authenticated');
    expect(getAuth().user).toEqual(USER);
    expect(mockedTokenStorage.clearRefreshToken).not.toHaveBeenCalled();
  });
});
