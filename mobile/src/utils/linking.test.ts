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

  /**
   * A: a valid https URL is opened directly, with no `canOpenURL` pre-check.
   * `canOpenURL` is deliberately never mocked/stubbed here - if the
   * implementation regressed to calling it, this would fail against
   * jest-expo's real (unstubbed) React Native mock, not silently pass.
   */
  it('A: opens a valid https URL directly - openURL called exactly once, canOpenURL never called, returns true', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');
    const openURLSpy = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined as never);

    const result = await openListingUrl('https://example.com/item/1');

    expect(result).toBe(true);
    expect(openURLSpy).toHaveBeenCalledTimes(1);
    expect(openURLSpy).toHaveBeenCalledWith('https://example.com/item/1');
    expect(canOpenSpy).not.toHaveBeenCalled();
  });

  it('B: opens a valid http URL directly - openURL called exactly once, canOpenURL never called, returns true', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');
    const openURLSpy = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined as never);

    const result = await openListingUrl('http://example.com/item/1');

    expect(result).toBe(true);
    expect(openURLSpy).toHaveBeenCalledTimes(1);
    expect(openURLSpy).toHaveBeenCalledWith('http://example.com/item/1');
    expect(canOpenSpy).not.toHaveBeenCalled();
  });

  it('does nothing and returns false for a missing URL - never calls Linking at all', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');
    const openURLSpy = jest.spyOn(Linking, 'openURL');

    const result = await openListingUrl(null);

    expect(result).toBe(false);
    expect(canOpenSpy).not.toHaveBeenCalled();
    expect(openURLSpy).not.toHaveBeenCalled();
  });

  it('C: does nothing and returns false for a malformed URL - openURL never called', async () => {
    const openURLSpy = jest.spyOn(Linking, 'openURL');

    const result = await openListingUrl('not a url');

    expect(result).toBe(false);
    expect(openURLSpy).not.toHaveBeenCalled();
  });

  it.each(['javascript:alert(1)', 'mailto:someone@example.com', 'tel:+15551234567', 'myapp://some/path'])(
    'D: rejects unsupported scheme %s - openURL never called, returns false',
    async (url) => {
      const openURLSpy = jest.spyOn(Linking, 'openURL');

      const result = await openListingUrl(url);

      expect(result).toBe(false);
      expect(openURLSpy).not.toHaveBeenCalled();
    },
  );

  it('E: alerts and returns false, never throws, when Linking.openURL itself rejects', async () => {
    const canOpenSpy = jest.spyOn(Linking, 'canOpenURL');
    jest.spyOn(Linking, 'openURL').mockRejectedValue(new Error('simulated platform failure'));
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});

    await expect(openListingUrl('https://example.com/item/1')).resolves.toBe(false);
    expect(alertSpy).toHaveBeenCalled();
    expect(canOpenSpy).not.toHaveBeenCalled();
  });

  it.each([
    ['eBay', 'https://www.ebay.com/itm/276123456789?hash=item404abc123%3Ag%3AxyzAAOSw'],
    ['Etsy', 'https://www.etsy.com/listing/123456789/maccabi-vintage-pennant?ref=shop_home_active_1'],
    ['Reverb', 'https://reverb.com/item/84838674-fender-stratocaster'],
    ['Bonanza', 'https://www.bonanza.com/listings/998877'],
  ])('F: a real-shaped %s listing URL is still accepted and opened', async (_marketplace, url) => {
    const openURLSpy = jest.spyOn(Linking, 'openURL').mockResolvedValue(undefined as never);

    const result = await openListingUrl(url);

    expect(result).toBe(true);
    expect(openURLSpy).toHaveBeenCalledWith(url);
  });
});
