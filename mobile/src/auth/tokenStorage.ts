/**
 * The ONLY place this app reads or writes the refresh token to disk -
 * `expo-secure-store` (iOS Keychain / Android Keystore, encrypted at
 * rest), never `AsyncStorage` (unencrypted, plain key-value - never
 * acceptable for a credential). The access token is never persisted at
 * all - see src/auth/AuthContext.tsx for why (short-lived, kept in
 * memory only, trivially re-derived from the refresh token on launch).
 */

import * as SecureStore from 'expo-secure-store';

const REFRESH_TOKEN_KEY = 'marketplacealert.refreshToken';

/** `null` if nothing is stored (never logged in, or already logged out) - never throws for "not found", only for a genuine storage failure. */
export async function getRefreshToken(): Promise<string | null> {
  return SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
}

/** Overwrites any previously stored value - callers must always pass the newest (rotated) token, never the one that was just exchanged. */
export async function setRefreshToken(token: string): Promise<void> {
  await SecureStore.setItemAsync(REFRESH_TOKEN_KEY, token);
}

export async function clearRefreshToken(): Promise<void> {
  await SecureStore.deleteItemAsync(REFRESH_TOKEN_KEY);
}

/**
 * Deletes the stored refresh token only if it still equals `expectedToken` -
 * a compare-and-clear used by AuthContext to undo a stale refresh's write
 * (see AuthContext.tsx's generation guard) without ever risking deleting a
 * *different*, newer token that a subsequent login/refresh has since
 * written in its place.
 */
export async function clearRefreshTokenIfMatches(expectedToken: string): Promise<void> {
  const current = await SecureStore.getItemAsync(REFRESH_TOKEN_KEY);
  if (current === expectedToken) {
    await SecureStore.deleteItemAsync(REFRESH_TOKEN_KEY);
  }
}
