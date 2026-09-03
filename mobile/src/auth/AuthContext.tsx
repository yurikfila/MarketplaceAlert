/**
 * Authentication state, shared across the whole app - the one piece of
 * cross-screen state this app has ever genuinely needed (see
 * mobile/README.md "Simple state - hooks only, no Redux": every other
 * screen's data stays local via useAsyncData; who's signed in has to be
 * known everywhere, so this is a plain React Context, not a new
 * dependency or state-management library).
 *
 * Owns:
 * - `status`: 'restoring' | 'authenticated' | 'unauthenticated' | 'restoration-error'
 * - the signed-in `user`
 * - the access token, in memory only (see tokenStorage.ts for why the
 *   refresh token is the only thing ever written to disk)
 *
 * Registers itself with src/api/client.ts (`setAccessToken`/
 * `setUnauthorizedHandler`) so every `apiRequest` call automatically
 * carries the current token and can silently recover from a 401 via the
 * single-flight refresh mechanism described in that file's docstring.
 *
 * **Definitive vs. transient refresh failure - the core distinction this
 * module encodes** (see `performRefresh`): a `401` from
 * `POST /auth/refresh` means the backend evaluated the token and
 * rejected it (invalid, expired, reused, or the account is inactive -
 * see the backend's `AuthService.refresh`, which collapses all four into
 * this one status code) - the session is genuinely gone, so the stored
 * refresh token is cleared. Anything else - a network failure, a
 * timeout, a 5xx - means the token was never evaluated at all. It is
 * left exactly as it was, on disk, untouched: the device might simply be
 * offline, or the backend momentarily down, and destroying a possibly-
 * still-valid session over that would be strictly worse than doing
 * nothing. Verified directly against the backend's actual code before
 * this was implemented - see `core/auth/service.py:AuthService.refresh`
 * and `api/v1/auth.py:refresh` on the backend.
 *
 * **Rotation storage failure**: if the backend successfully rotates the
 * refresh token but persisting the new one to `expo-secure-store` fails,
 * the *old* token is already dead (the backend's rotation is atomic -
 * revoke-old-then-issue-new, in one transaction) and the new one isn't
 * safely stored anywhere. Nothing about this is recoverable locally, so
 * it's treated the same as a definitive failure: clear everything, never
 * retry with the old token.
 *
 * **Session generations - why, and how.** `restoreSession` (startup),
 * the reactive 401 handler, `login`, `signup`, and `logout` can all be
 * in flight at once, each touching the same stored refresh token and the
 * same in-memory session. Without coordination, a *stale* operation -
 * one started under a session that logout/login has since replaced -
 * could resolve late and either resurrect a session the user just logged
 * out of, or clobber the session a newer login just established. Every
 * such operation captures `generationRef.current` (a plain monotonic
 * counter) the moment it starts; `logout`/`clearSession`/`login`/
 * `signup` bump it immediately when they begin replacing the session.
 * Before a captured-generation operation applies *any* effect - persists
 * a rotated refresh token, sets the access token, sets `user`, or sets
 * `status` - it re-checks that its generation is still current, and
 * silently discards its result otherwise. See `performRefresh` for the
 * one subtlety this requires around token rotation specifically.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { ApiError, setAccessToken as setClientAccessToken, setUnauthorizedHandler } from '../api/client';
import {
  getCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  refreshToken as refreshTokenRequest,
  signup as signupRequest,
} from '../api/endpoints';
import type { UserPublic } from '../api/types';
import * as tokenStorage from './tokenStorage';

export type AuthStatus = 'restoring' | 'authenticated' | 'unauthenticated' | 'restoration-error';

interface AuthContextValue {
  status: AuthStatus;
  user: UserPublic | null;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-runs session restoration - the "Retry" action on RestorationErrorScreen. */
  retryRestoration: () => void;
  /** The "Sign in instead" action on RestorationErrorScreen - discards whatever local session state exists (even though it might still be valid) and lands on Login, for a user who'd rather not wait. */
  signInInstead: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

type RefreshOutcome =
  | { kind: 'no-session' }
  | { kind: 'success'; accessToken: string }
  | { kind: 'definitive-failure' }
  | { kind: 'transient-failure' }
  /** This operation's generation was superseded (logout/login/signInInstead) while it was in flight - its result must be discarded, not applied. */
  | { kind: 'stale' };

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('restoring');
  const [user, setUser] = useState<UserPublic | null>(null);

  // Mirrors the access token into client.ts's module-level state -
  // client.ts has no React dependency, so this is the one place the two
  // stay in sync. Not itself rendered anywhere, so a plain ref is enough;
  // `status`/`user` above are what drive the UI.
  const accessTokenRef = useRef<string | null>(null);
  const setAccessToken = useCallback((token: string | null) => {
    accessTokenRef.current = token;
    setClientAccessToken(token);
  }, []);

  // Bumped by logout/login/signup/clearSession every time they begin
  // replacing the current session - see the module docstring.
  const generationRef = useRef(0);
  const beginNewGeneration = useCallback((): number => {
    generationRef.current += 1;
    return generationRef.current;
  }, []);

  const isMountedRef = useRef(true);
  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Shares one in-flight restoration across overlapping restoreSession()/
  // retryRestoration() calls - see `restoreSession` below. Without this,
  // two near-simultaneous calls (e.g. a fast double-tap of "Retry") would
  // each independently present the same stored, single-use refresh token
  // to the backend; the backend would treat the second presentation as a
  // reuse of an already-rotated token, revoking every session this
  // account has - a purely self-inflicted logout of a perfectly valid
  // session.
  const restorationInFlightRef = useRef<Promise<void> | null>(null);

  const clearSession = useCallback(async () => {
    beginNewGeneration();
    try {
      await tokenStorage.clearRefreshToken();
    } catch {
      // A deletion failure isn't a reason to keep anything - in-memory
      // state is cleared regardless. A stale SecureStore entry left
      // behind (deletion failures are rare) is harmless: it will simply
      // fail its own next refresh attempt too, the same as any other
      // invalid/unrecognized token.
    }
    if (isMountedRef.current) {
      setAccessToken(null);
      setUser(null);
      setStatus('unauthenticated');
    }
  }, [beginNewGeneration, setAccessToken]);

  /**
   * The shared core of every refresh attempt (reactive 401 and startup/
   * retry restoration alike) - verifies the stored refresh token,
   * rotates it, and classifies the outcome. Callers are responsible for
   * re-checking `generationRef.current === generation` before applying
   * *any* effect from the returned outcome (setting the access token,
   * `user`, or `status`) - this function only guards the one effect it
   * performs itself: persisting the rotated refresh token.
   *
   * **Rotation ordering.** By the time `refreshTokenRequest` resolves
   * successfully, the backend has *already* rotated the presented token -
   * there is no way to "return" it. If this operation's generation has
   * been superseded by then (logout/login/signInInstead ran while the
   * network call was in flight), the new token belongs to a session that
   * no longer exists client-side, and must never be written into
   * whatever session *is* now current - so it is deliberately never
   * persisted in that case. If staleness is instead discovered *after*
   * the write already landed (the generation changed during the write
   * itself, a much narrower window), the write is undone - but only via
   * `clearRefreshTokenIfMatches`, a compare-and-clear: it deletes the
   * stored token only if it *still* equals the one this stale operation
   * just wrote, so it can never delete a different, newer token that a
   * subsequent login has since stored in its place.
   */
  const performRefresh = useCallback(async (generation: number): Promise<RefreshOutcome> => {
    const stored = await tokenStorage.getRefreshToken();
    if (!stored) {
      return { kind: 'no-session' };
    }

    let tokens;
    try {
      tokens = await refreshTokenRequest(stored);
    } catch (error) {
      if (error instanceof ApiError && error.kind === 'http' && error.status === 401) {
        return { kind: 'definitive-failure' };
      }
      // network / timeout / 5xx / anything else - the token was never
      // evaluated, so its validity is unknown, not disproven.
      return { kind: 'transient-failure' };
    }

    if (generationRef.current !== generation) {
      // Superseded while the network round-trip was in flight - the
      // backend already rotated `stored`, but this rotated pair belongs
      // to a dead generation. Never persist it.
      return { kind: 'stale' };
    }

    try {
      await tokenStorage.setRefreshToken(tokens.refresh_token);
    } catch {
      return { kind: 'definitive-failure' }; // rotation storage failure - see module docstring
    }

    if (generationRef.current !== generation) {
      // Superseded *during* the write itself - undo it, but only if
      // nothing newer has since overwritten it (see this function's
      // docstring).
      try {
        await tokenStorage.clearRefreshTokenIfMatches(tokens.refresh_token);
      } catch {
        // best-effort cleanup only
      }
      return { kind: 'stale' };
    }

    return { kind: 'success', accessToken: tokens.access_token };
  }, []);

  // Registered with client.ts for as long as this provider is mounted -
  // called for every 401 to a token-bearing request, via client.ts's own
  // single-flight guard (so this never runs concurrently with itself).
  const handleUnauthorized = useCallback(async (): Promise<string | null> => {
    const generation = generationRef.current;
    const outcome = await performRefresh(generation);

    if (generationRef.current !== generation || !isMountedRef.current) {
      // Superseded (or unmounted) while this was running - whatever the
      // outcome was, it no longer describes the current session.
      return null;
    }

    if (outcome.kind === 'success') {
      setAccessToken(outcome.accessToken);
      return outcome.accessToken;
    }
    if (outcome.kind === 'definitive-failure' || outcome.kind === 'no-session') {
      await clearSession();
      return null;
    }
    // transient-failure: session preserved, `status` stays 'authenticated' -
    // only the request(s) that triggered this fail with their own
    // ApiError, surfaced by whichever screen's useAsyncData made them.
    // ('stale' can't reach here - the guard above already returns for it.)
    return null;
  }, [performRefresh, clearSession, setAccessToken]);

  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized);
    return () => setUnauthorizedHandler(null);
  }, [handleUnauthorized]);

  const performRestore = useCallback(async (): Promise<void> => {
    if (isMountedRef.current) {
      setStatus('restoring');
    }
    const generation = generationRef.current;
    const outcome = await performRefresh(generation);

    if (generationRef.current !== generation || !isMountedRef.current) {
      return;
    }

    if (outcome.kind === 'no-session') {
      setStatus('unauthenticated');
      return;
    }
    if (outcome.kind === 'definitive-failure') {
      await clearSession();
      return;
    }
    if (outcome.kind === 'transient-failure') {
      // Deliberately does NOT touch SecureStore or `user` - the stored
      // refresh token is still there, untouched, ready for the next
      // Retry (or the next natural 401-triggered refresh once the app
      // does get past this screen).
      setStatus('restoration-error');
      return;
    }
    if (outcome.kind === 'stale') {
      return; // unreachable given the guard above - kept for exhaustiveness
    }

    setAccessToken(outcome.accessToken);
    try {
      const me = await getCurrentUser();
      if (generationRef.current !== generation || !isMountedRef.current) {
        return;
      }
      setUser(me);
      setStatus('authenticated');
    } catch {
      // Got a fresh access token but /me itself failed right after
      // (extremely unlikely, but could be its own transient blip) - the
      // same "don't discard the session over an unconfirmed problem"
      // principle applies here too.
      if (generationRef.current === generation && isMountedRef.current) {
        setStatus('restoration-error');
      }
    }
  }, [performRefresh, clearSession, setAccessToken]);

  /**
   * Single-flight wrapper around `performRestore` - see
   * `restorationInFlightRef` above. Both the mount-time effect below and
   * `retryRestoration` call this, never `performRestore` directly.
   */
  const restoreSession = useCallback((): Promise<void> => {
    if (restorationInFlightRef.current) {
      return restorationInFlightRef.current;
    }
    const promise = performRestore().finally(() => {
      restorationInFlightRef.current = null;
    });
    restorationInFlightRef.current = promise;
    return promise;
  }, [performRestore]);

  useEffect(() => {
    restoreSession();
  }, [restoreSession]);

  const login = useCallback(
    async (email: string, password: string) => {
      const response = await loginRequest({ email, password });
      // A successful login always starts a new session generation,
      // superseding anything still in flight from before (a stale
      // restoration attempt, a stale reactive refresh) - see module
      // docstring.
      const generation = beginNewGeneration();
      await tokenStorage.setRefreshToken(response.tokens.refresh_token);
      if (generationRef.current !== generation || !isMountedRef.current) {
        // Something even newer superseded this login while its own
        // token was being persisted - do not apply state for a login
        // that is itself now stale.
        return;
      }
      setAccessToken(response.tokens.access_token);
      setUser(response.user);
      setStatus('authenticated');
    },
    [setAccessToken, beginNewGeneration],
  );

  const signup = useCallback(
    async (email: string, password: string) => {
      const response = await signupRequest({ email, password });
      const generation = beginNewGeneration();
      await tokenStorage.setRefreshToken(response.tokens.refresh_token);
      if (generationRef.current !== generation || !isMountedRef.current) {
        return;
      }
      setAccessToken(response.tokens.access_token);
      setUser(response.user);
      setStatus('authenticated');
    },
    [setAccessToken, beginNewGeneration],
  );

  const logout = useCallback(async () => {
    // Invalidate any in-flight refresh/restoration BEFORE the network
    // call - not after. A refresh already in flight at this instant must
    // not be able to reinstall credentials once logout has started, no
    // matter how long its own network call takes relative to this one.
    beginNewGeneration();
    const stored = await tokenStorage.getRefreshToken();
    if (stored) {
      try {
        await logoutRequest(stored);
      } catch {
        // Best-effort, by design - local logout must succeed even if
        // this call fails (offline, timeout, backend hiccup, whatever).
      }
    }
    await clearSession();
  }, [clearSession, beginNewGeneration]);

  const retryRestoration = useCallback(() => {
    restoreSession();
  }, [restoreSession]);

  const signInInstead = useCallback(() => {
    clearSession();
  }, [clearSession]);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, login, signup, logout, retryRestoration, signInInstead }),
    [status, user, login, signup, logout, retryRestoration, signInInstead],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
