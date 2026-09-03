import { StyleSheet, View } from 'react-native';

import { useAuth } from '../auth/AuthContext';
import { ErrorState } from '../components/ErrorState';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { spacing } from '../theme/colors';

/**
 * Shown only when session restoration at cold start fails *transiently*
 * (no network, timeout, backend 5xx) - never for a definitive session
 * rejection, which lands directly on the Login screen instead (see
 * AuthContext's `restoreSession`). The stored refresh token is still
 * there, untouched - "Retry" simply re-attempts restoration; "Sign in
 * instead" is an explicit, user-chosen escape hatch for someone who'd
 * rather not wait (or suspects their session really is gone), which
 * discards the local session and lands on Login.
 */
export function RestorationErrorScreen() {
  const { retryRestoration, signInInstead } = useAuth();

  return (
    <Screen>
      <View style={styles.content}>
        <ErrorState
          message="Couldn't reconnect to MarketplaceAlert. Check your connection and try again."
          onRetry={retryRestoration}
        />
        <PrimaryButton label="Sign in instead" onPress={signInInstead} variant="secondary" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: {
    flex: 1,
    justifyContent: 'center',
    gap: spacing.lg,
    padding: spacing.lg,
  },
});
