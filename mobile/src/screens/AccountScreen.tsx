import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { useAuth } from '../auth/AuthContext';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import type { RootStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';

export function AccountScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const { user, logout } = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  async function handleLogout() {
    setSigningOut(true);
    // logout() is designed to never throw (best-effort network call,
    // always clears local state - see AuthContext) - no try/catch
    // needed. RootNavigator swaps to the Auth stack automatically once
    // `status` becomes 'unauthenticated'.
    await logout();
  }

  return (
    <Screen>
      <View style={styles.content}>
        <View style={styles.card}>
          <Text style={styles.label}>Signed in as</Text>
          <Text style={styles.email}>{user?.email ?? '—'}</Text>
        </View>

        {/* Server-authoritative only - never an email comparison. The
            backend enforces this independently on every admin request
            regardless of what this button does (see AdminUsersScreen). */}
        {user?.is_admin ? (
          <PrimaryButton
            label="Admin: User Management"
            onPress={() => navigation.navigate('AdminUsers')}
            variant="secondary"
          />
        ) : null}

        <PrimaryButton label="Log out" onPress={handleLogout} loading={signingOut} variant="danger" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: {
    padding: spacing.lg,
    gap: spacing.xl,
  },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: spacing.sm,
  },
  label: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  email: {
    fontSize: fontSize.lg,
    fontWeight: '700',
    color: colors.textPrimary,
  },
});
