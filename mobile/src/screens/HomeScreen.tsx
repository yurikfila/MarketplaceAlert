import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { useCallback } from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';

import { getStatus, listListings, listSavedSearches } from '../api/endpoints';
import { ErrorState } from '../components/ErrorState';
import { ListingCard } from '../components/ListingCard';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { StatusPill } from '../components/StatusPill';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import type { RootStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatRelativeTime } from '../utils/format';
import { displayNameForMarketplace } from '../utils/marketplaces';

const RECENT_LISTINGS_LIMIT = 5;

/** The most recent `last_scanned_at` across every saved search, or null if none has ever run - a quick "is the backend actually finding things" signal, distinct from the per-search timestamps shown on SavedSearchDetailScreen. */
function mostRecentScanTime(savedSearches: { last_scanned_at: string | null }[] | null): string | null {
  if (!savedSearches) return null;
  const timestamps = savedSearches.map((s) => s.last_scanned_at).filter((t): t is string => t !== null);
  if (timestamps.length === 0) return null;
  return timestamps.reduce((latest, current) => (current > latest ? current : latest));
}

export function HomeScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();

  const status = useAsyncData(getStatus, []);
  const savedSearches = useAsyncData(listSavedSearches, []);
  const recentListings = useAsyncData(() => listListings({ limit: RECENT_LISTINGS_LIMIT }), []);

  const refreshAll = useCallback(() => {
    status.refresh();
    savedSearches.refresh();
    recentListings.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keeps backend status, the active-search count, and the recent-listings
  // preview current while Home is open, without a manual pull-to-refresh -
  // see mobile/README.md "Automatic refresh".
  const refreshAllQuietly = useCallback(() => {
    status.refreshQuietly();
    savedSearches.refreshQuietly();
    recentListings.refreshQuietly();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useAutoRefresh(refreshAllQuietly);

  const activeCount = savedSearches.data?.filter((s) => s.is_active).length ?? null;
  const lastScanTime = mostRecentScanTime(savedSearches.data);
  const anyRefreshing = status.refreshing || savedSearches.refreshing || recentListings.refreshing;

  return (
    <Screen padded={false}>
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={anyRefreshing} onRefresh={refreshAll} />}
      >
        <Text style={styles.appTitle}>MarketplaceAlert</Text>

        <View style={styles.card}>
          <Text style={styles.cardTitle}>Backend status</Text>
          {status.loading ? (
            <Text style={styles.metaText}>Checking…</Text>
          ) : status.error ? (
            <ErrorState message={status.error} onRetry={status.retry} />
          ) : (
            <>
              <StatusPill label={status.data?.backend ? 'Connected' : 'Unreachable'} tone={status.data?.backend ? 'success' : 'danger'} />
              <Text style={styles.metaText}>
                Database: {status.data?.database ? 'ok' : 'unavailable'} · Telegram:{' '}
                {status.data?.telegram_configured ? 'configured' : 'not configured'}
              </Text>
              <Text style={styles.metaText}>
                Marketplaces:{' '}
                {status.data?.supported_marketplaces.map(displayNameForMarketplace).join(', ') ?? '—'}
              </Text>
            </>
          )}
        </View>

        <View style={styles.card}>
          <Text style={styles.cardTitle}>Saved searches</Text>
          {savedSearches.loading ? (
            <Text style={styles.metaText}>Loading…</Text>
          ) : savedSearches.error ? (
            <ErrorState message={savedSearches.error} onRetry={savedSearches.retry} />
          ) : (
            <>
              <Text style={styles.bigNumber}>{activeCount} active</Text>
              <Text style={styles.metaText}>
                Last scan activity: {lastScanTime ? formatRelativeTime(lastScanTime) : 'None yet'}
              </Text>
            </>
          )}
          <PrimaryButton label="Create a saved search" onPress={() => navigation.navigate('CreateSearch')} />
        </View>

        <View style={styles.card}>
          <Text style={styles.cardTitle}>Recently discovered listings</Text>
          {recentListings.loading ? (
            <Text style={styles.metaText}>Loading…</Text>
          ) : recentListings.error ? (
            <ErrorState message={recentListings.error} onRetry={recentListings.retry} />
          ) : recentListings.data && recentListings.data.items.length > 0 ? (
            <View style={styles.listingList}>
              {recentListings.data.items.map((listing) => (
                <ListingCard key={`${listing.marketplace}-${listing.id}`} listing={listing} />
              ))}
            </View>
          ) : (
            <Text style={styles.metaText}>No listings discovered yet.</Text>
          )}
        </View>
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: {
    padding: spacing.lg,
    gap: spacing.lg,
    paddingBottom: spacing.xxl,
  },
  appTitle: {
    fontSize: fontSize.xxl,
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
  cardTitle: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  metaText: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  bigNumber: {
    fontSize: fontSize.xxl,
    fontWeight: '800',
    color: colors.primary,
  },
  listingList: {
    gap: spacing.sm,
  },
});
