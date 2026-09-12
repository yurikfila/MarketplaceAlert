import { FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';

import { listAdminUsers } from '../api/endpoints';
import type { AdminUserOut } from '../api/types';
import { EmptyState } from '../components/EmptyState';
import { ErrorState } from '../components/ErrorState';
import { LoadingView } from '../components/LoadingView';
import { Screen } from '../components/Screen';
import { StatusPill } from '../components/StatusPill';
import { useAsyncData } from '../hooks/useAsyncData';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatTimestamp } from '../utils/format';

/**
 * Read-only user-management view for admins only - reached exclusively
 * from AccountScreen's Admin entry point, itself only shown when
 * `useAuth().user?.is_admin` is true. That client-side gating is a
 * convenience, never the real protection: `listAdminUsers()` calls
 * `GET /api/v1/admin/users`, which the backend rejects with 401/403 for
 * anyone who isn't a genuine, authenticated admin, regardless of what
 * this screen shows or hides (see marketplace_alert/api/v1/admin.py).
 *
 * Deliberately read-only in this phase - no promote/demote/delete/ban
 * action exists here, matching the backend, which has no such endpoint
 * at all yet.
 */
export function AdminUsersScreen() {
  const { data, loading, refreshing, error, refresh, retry } = useAsyncData(listAdminUsers, []);

  if (loading) {
    return (
      <Screen>
        <LoadingView label="Loading users…" />
      </Screen>
    );
  }

  if (error && !data) {
    return (
      <Screen>
        <ErrorState message={error} onRetry={retry} />
      </Screen>
    );
  }

  return (
    <Screen padded={false}>
      <FlatList<AdminUserOut>
        data={data?.users ?? []}
        keyExtractor={(item) => String(item.id)}
        contentContainerStyle={styles.listContent}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
        renderItem={({ item }) => <AdminUserRow user={item} />}
        ListHeaderComponent={
          <View style={styles.header}>
            <Text style={styles.headerCount}>Users: {data?.total_users ?? 0}</Text>
          </View>
        }
        ListEmptyComponent={<EmptyState title="No registered users" message="No accounts have signed up yet." />}
      />
    </Screen>
  );
}

function AdminUserRow({ user }: { user: AdminUserOut }) {
  return (
    <View style={styles.card}>
      <View style={styles.headerRow}>
        <Text style={styles.email} numberOfLines={1}>
          {user.email}
        </Text>
        <StatusPill label={user.is_admin ? 'Admin' : 'User'} tone={user.is_admin ? 'success' : 'neutral'} />
      </View>

      <View style={styles.metaRow}>
        <Text style={styles.metaText}>Registered: {formatTimestamp(user.created_at)}</Text>
        <Text style={styles.metaText}>Saved searches: {user.saved_search_count}</Text>
      </View>

      <StatusPill label={user.is_active ? 'Active' : 'Inactive'} tone={user.is_active ? 'success' : 'warning'} />
    </View>
  );
}

const styles = StyleSheet.create({
  listContent: {
    padding: spacing.lg,
    gap: spacing.md,
    flexGrow: 1,
  },
  header: {
    marginBottom: spacing.sm,
  },
  headerCount: {
    fontSize: fontSize.xl,
    fontWeight: '800',
    color: colors.textPrimary,
  },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: spacing.sm,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  email: {
    flex: 1,
    fontSize: fontSize.lg,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  metaRow: {
    gap: 2,
  },
  metaText: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
});
