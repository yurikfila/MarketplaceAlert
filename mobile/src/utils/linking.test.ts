import { Alert, Linking } from 'react-native';

import { isOpenableListingUrl, openListingUrl } from './linking';

describe('isOpenableListingUrl', () => {
  it('accepts an https URL', () => {
    expect(isOpenableListingUrl('https://example.com/item/1')).toBe(true);
  });

  it('accepts an http URL', () => {
    expect(isOpenableListingUrl('http://example.com/item/1')).toBe(true);
  });

  it('rejects null/undefined/empty', () => {
    expect(isOpenableListingUrl(null)).toBe(false);
    expect(isOpenableListingUrl(undefined)).toBe(false);
    expect(isOpenableListingUrl('')).toBe(false);
  });

  it('rejects a malformed URL', () => {
    expect(isOpenableListingUrl('not a url')).toBe(false);
  });

  it('rejects a non-http(s) scheme', () => {
    expect(isOpenableListingUrl('javascript:alert(1)')).toBe(false);
    expect(isOpenableListingUrl('mailto:someone@example.com')).toBe(false);
  });
});

describe('openListingUrl', () => {
  beforeEach(() => {
    // jest-expo's own React Native mock already provides Linking.canOpenURL/
    // openURL as jest.fn() stubs, so `jest.spyOn` re-wraps the *same*
    // underlying mock rather than a fresh one - explicit clearing (not just
    // `restoreAllMocks` in afterEach) is what actually resets call counts
    // between tests here.
    jest.clearAllMocks();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('opens a valid, openable URL and returns true', async () => {
    jest.spyOn(Linking, 'canOpenURL').mockResolvedValue(true);
    const openURLSpy = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined as never);

    const result = await openListingUrl('https://example.com/item/1');

    expect(result).toBe(true);
    expect(openURLSpy).toHaveBeenCalledWith('https://example.com/item/1');
  });

  it('does nothing and returns false for a missing URL - never calls Linking at all', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');
    const openURLSpy = jest.spyOn(Linking, 'openURL');

    const result = await openListingUrl(null);

    expect(result).toBe(false);
    expect(canOpenSpy).not.toHaveBeenCalled();
    expect(openURLSpy).not.toHaveBeenCalled();
  });

  it('does nothing and returns false for a malformed URL', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');

    const result = await openListingUrl('not a url');

    expect(result).toBe(false);
    expect(canOpenSpy).not.toHaveBeenCalled();
  });

  it('alerts and returns false when the device cannot open the URL', async () => {
    jest.spyOn(Linking, 'canOpenURL').mockResolvedValue(false);
    const openURLSpy = jest.spyOn(Linking, 'openURL');
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});

    const result = await openListingUrl('https://example.com/item/1');

    expect(result).toBe(false);
    expect(openURLSpy).not.toHaveBeenCalled();
    expect(alertSpy).toHaveBeenCalled();
  });

  it('alerts and returns false, never throws, when Linking itself rejects', async () => {
    jest.spyOn(Linking, 'canOpenURL').mockResolvedValue(true);
    jest.spyOn(Linking, 'openURL').mockRejectedValue(new Error('simulated platform failure'));
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});

    await expect(openListingUrl('https://example.com/item/1')).resolves.toBe(false);
    expect(alertSpy).toHaveBeenCalled();
  });
});
