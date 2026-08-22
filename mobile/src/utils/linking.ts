import { Alert, Linking } from 'react-native';

/**
 * Only `http(s)` URLs are ever considered safe to open here. The backend
 * validates `listing_url` as a real URL before it's ever stored (see
 * `core/models/listing.py`'s `HttpUrl` field), but this app must never
 * trust that blindly - a missing/malformed value must disable the
 * "open" action rather than call `Linking.openURL` with something
 * unexpected.
 */
export function isOpenableListingUrl(url: string | null | undefined): url is string {
  if (!url) {
    return false;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Opens a listing's URL in the device's default browser - `Linking.openURL`
 * always hands off to an external browser/app, so the user is never
 * trapped inside this app. Returns whether it actually opened; never
 * throws - every failure mode (an invalid URL, no app able to handle it,
 * the OS call itself rejecting) is reported through a plain `Alert`
 * instead of an unhandled promise rejection or a silent no-op.
 */
export async function openListingUrl(url: string | null | undefined): Promise<boolean> {
  if (!isOpenableListingUrl(url)) {
    return false;
  }
  try {
    const canOpen = await Linking.canOpenURL(url);
    if (!canOpen) {
      Alert.alert('Could not open link', "Your device doesn't have an app available to open this link.");
      return false;
    }
    await Linking.openURL(url);
    return true;
  } catch {
    Alert.alert('Could not open link', 'Something went wrong opening this listing.');
    return false;
  }
}
