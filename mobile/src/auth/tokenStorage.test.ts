/**
 * Tests for the refresh-token storage wrapper - confirms it's backed by
 * expo-secure-store (never AsyncStorage) and uses one consistent key.
 */

import * as SecureStore from 'expo-secure-store';

import { clearRefreshToken, getRefreshToken, setRefreshToken } from './tokenStorage';

jest.mock('expo-secure-store', () => ({
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
  deleteItemAsync: jest.fn(),
}));

describe('tokenStorage', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('getRefreshToken reads from expo-secure-store', async () => {
    (SecureStore.getItemAsync as jest.Mock).mockResolvedValue('stored-token');

    const result = await getRefreshToken();

    expect(result).toBe('stored-token');
    expect(SecureStore.getItemAsync).toHaveBeenCalledTimes(1);
  });

  it('getRefreshToken returns null when nothing is stored', async () => {
    (SecureStore.getItemAsync as jest.Mock).mockResolvedValue(null);

    expect(await getRefreshToken()).toBeNull();
  });

  it('setRefreshToken writes to expo-secure-store', async () => {
    (SecureStore.setItemAsync as jest.Mock).mockResolvedValue(undefined);

    await setRefreshToken('a-new-token');

    expect(SecureStore.setItemAsync).toHaveBeenCalledWith(expect.any(String), 'a-new-token');
  });

  it('setRefreshToken propagates a storage failure', async () => {
    (SecureStore.setItemAsync as jest.Mock).mockRejectedValue(new Error('Keychain error'));

    await expect(setRefreshToken('a-new-token')).rejects.toThrow('Keychain error');
  });

  it('clearRefreshToken deletes from expo-secure-store', async () => {
    (SecureStore.deleteItemAsync as jest.Mock).mockResolvedValue(undefined);

    await clearRefreshToken();

    expect(SecureStore.deleteItemAsync).toHaveBeenCalledTimes(1);
  });

  it('get/set/clear all use the same storage key', async () => {
    (SecureStore.getItemAsync as jest.Mock).mockResolvedValue(null);
    (SecureStore.setItemAsync as jest.Mock).mockResolvedValue(undefined);
    (SecureStore.deleteItemAsync as jest.Mock).mockResolvedValue(undefined);

    await getRefreshToken();
    await setRefreshToken('x');
    await clearRefreshToken();

    const getKey = (SecureStore.getItemAsync as jest.Mock).mock.calls[0][0];
    const setKey = (SecureStore.setItemAsync as jest.Mock).mock.calls[0][0];
    const deleteKey = (SecureStore.deleteItemAsync as jest.Mock).mock.calls[0][0];
    expect(getKey).toBe(setKey);
    expect(setKey).toBe(deleteKey);
  });
});
