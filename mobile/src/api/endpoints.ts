/**
 * Every backend call this app makes, typed, in one place. Screens/hooks
 * import from here - never from api/client.ts directly - so there is
 * exactly one function per backend operation, never a `fetch()` scattered
 * in a screen file.
 */

import { apiRequest } from './client';
import type {
  AuthResponse,
  ListingListResponse,
  ListListingsParams,
  LoginInput,
  MarketplaceInfo,
  MobileStatus,
  SavedSearch,
  SavedSearchCreateInput,
  SavedSearchRunResult,
  SavedSearchUpdateInput,
  SignupInput,
  TokenPair,
  UserPublic,
} from './types';

export function getStatus(): Promise<MobileStatus> {
  return apiRequest<MobileStatus>('/status');
}

export function getMarketplaces(): Promise<MarketplaceInfo[]> {
  return apiRequest<MarketplaceInfo[]>('/marketplaces');
}

export function listSavedSearches(): Promise<SavedSearch[]> {
  return apiRequest<SavedSearch[]>('/saved-searches');
}

export function getSavedSearch(id: number): Promise<SavedSearch> {
  return apiRequest<SavedSearch>(`/saved-searches/${id}`);
}

export function createSavedSearch(input: SavedSearchCreateInput): Promise<SavedSearch> {
  return apiRequest<SavedSearch>('/saved-searches', { method: 'POST', body: input });
}

export function updateSavedSearch(id: number, input: SavedSearchUpdateInput): Promise<SavedSearch> {
  return apiRequest<SavedSearch>(`/saved-searches/${id}`, { method: 'PATCH', body: input });
}

export function deleteSavedSearch(id: number): Promise<void> {
  return apiRequest<void>(`/saved-searches/${id}`, { method: 'DELETE' });
}

export function runSavedSearch(id: number): Promise<SavedSearchRunResult> {
  return apiRequest<SavedSearchRunResult>(`/saved-searches/${id}/run`, { method: 'POST' });
}

export function listListings(params: ListListingsParams = {}): Promise<ListingListResponse> {
  // Spread into a fresh object literal so it structurally satisfies
  // apiRequest's indexed `Record<string, QueryValue>` query type - a named
  // interface variable (without its own index signature) isn't directly
  // assignable to a Record type, even when every field's type matches.
  return apiRequest<ListingListResponse>('/listings', { query: { ...params } });
}

// --- Authentication -----------------------------------------------------
//
// See src/auth/AuthContext.tsx for how these are actually used - screens
// never call these directly, they call AuthContext's login/signup/logout.

export function signup(input: SignupInput): Promise<AuthResponse> {
  return apiRequest<AuthResponse>('/auth/signup', { method: 'POST', body: input });
}

export function login(input: LoginInput): Promise<AuthResponse> {
  return apiRequest<AuthResponse>('/auth/login', { method: 'POST', body: input });
}

/**
 * `skipAuthRefresh: true` - a failed refresh must never try to
 * refresh-and-retry *itself* (see api/client.ts's module docstring).
 */
export function refreshToken(refreshTokenValue: string): Promise<TokenPair> {
  return apiRequest<TokenPair>('/auth/refresh', {
    method: 'POST',
    body: { refresh_token: refreshTokenValue },
    skipAuthRefresh: true,
  });
}

/**
 * `skipAuthRefresh: true` - logging out must never attempt a refresh at
 * all just to log out (see api/client.ts's module docstring).
 */
export function logout(refreshTokenValue: string): Promise<void> {
  return apiRequest<void>('/auth/logout', {
    method: 'POST',
    body: { refresh_token: refreshTokenValue },
    skipAuthRefresh: true,
  });
}

export function getCurrentUser(): Promise<UserPublic> {
  return apiRequest<UserPublic>('/auth/me');
}
