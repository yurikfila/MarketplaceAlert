import { useNavigation, useRoute } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import type { RouteProp } from '@react-navigation/native';
import { useCallback, useState } from 'react';
import { Alert, ScrollView, StyleSheet, Text, View } from 'react-native';

import { ApiError } from '../api/client';
import { deleteSavedSearch, getSavedSearch, listListings, runSavedSearch, updateSavedSearch } from '../api/endpoints';
import type { SavedSearchRunResult } from '../api/types';
import { Chip } from '../components/Chip';
import { ErrorState } from '../components/ErrorState';
import { ListingCard } from '../components/ListingCard';
import { LoadingView } from '../components/LoadingView';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { StatusPill } from '../components/StatusPill';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import type { RootStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatIntervalSeconds, formatTimestamp } from '../utils/format';
import { displayNameForMarketplace } from '../utils/marketplaces';

type DetailRouteProp = RouteProp<RootStackParamList, 'SavedSearchDetail'>;

const LATEST_LISTINGS_LIMIT = 5;

export function SavedSearchDetailScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const { params } = useRoute<DetailRouteProp>();
  const { id } = params;

  const detail = useAsyncData(() => getSavedSearch(id), [id]);
  const latestListings = useAsyncData(
    () => listListings({ saved_search_id: id, limit: LATEST_LISTINGS_LIMIT, sort: 'newest' }),
    [id],
  );

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<SavedSearchRunResult | null>(null);
  const [togglingActive, setTogglingActive] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Keeps this saved search's status/last-scanned time, and its latest-
  // listings preview, current while its detail screen is open. Suspended
  // for the duration of any in-flight mutation on this screen (Run Now,
  // Pause/Resume, Delete) - a concurrent background refetch racing one of
  // those could otherwise clobber the optimistic "loading" state those
  // actions show, or refetch right before Run Now's own explicit
  // `detail.refresh()`/`latestListings.refresh()` calls and make it look
  // like nothing happened. See mobile/README.md "Automatic refresh".
  const refreshBothQuietly = useCallback(() => {
    detail.refreshQuietly();
    latestListings.refreshQuietly();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useAutoRefresh(refreshBothQuietly, { enabled: !running && !togglingActive && !deleting });

  const handleRunNow = useCallback(async () => {
    setRunning(true);
    setRunError(null);
    try {
      const result = await runSavedSearch(id);
      setRunResult(result);
      detail.refresh();
      latestListings.refresh();
    } catch (error) {
      setRunError(error instanceof ApiError ? error.message : 'Could not run this saved search.');
    } finally {
      setRunning(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const handleToggleActive = useCallback(async () => {
    if (!detail.data) return;
    setTogglingActive(true);
    setActionError(null);
    try {
      await updateSavedSearch(id, { is_active: !detail.data.is_active });
      detail.refresh();
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : 'Could not update this saved search.');
    } finally {
      setTogglingActive(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, detail.data?.is_active]);

  const handleDelete = useCallback(() => {
    Alert.alert('Delete saved search?', 'This cannot be undone.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete',
        style: 'destructive',
        onPress: async () => {
          setDeleting(true);
          setActionError(null);
          try {
            await deleteSavedSearch(id);
            navigation.goBack();
          } catch (error) {
            setActionError(error instanceof ApiError ? error.message : 'Could not delete this saved search.');
            setDeleting(false);
          }
        },
      },
    ]);
  }, [id, navigation]);

  if (detail.loading) {
    return (
      <Screen>
        <LoadingView label="Loading saved search…" />
      </Screen>
    );
  }

  if (detail.error && !detail.data) {
    return (
      <Screen>
        <ErrorState message={detail.error} onRetry={detail.retry} />
      </Screen>
    );
  }

  const savedSearch = detail.data;
  if (!savedSearch) {
    return null;
  }

  return (
    <Screen>
      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.headerRow}>
          <Text style={styles.query}>{savedSearch.query}</Text>
          <StatusPill label={savedSearch.is_active ? 'Active' : 'Inactive'} tone={savedSearch.is_active ? 'success' : 'neutral'} />
        </View>

        <View style={styles.chipRow}>
          {savedSearch.marketplaces.map((marketplace) => (
            <Chip key={marketplace} label={displayNameForMarketplace(marketplace)} muted />
          ))}
        </View>

        <View style={styles.infoCard}>
          <InfoRow label="Scan interval" value={`Every ${formatIntervalSeconds(savedSearch.scan_interval_seconds)}`} />
          <InfoRow label="Last scanned" value={formatTimestamp(savedSearch.last_scanned_at)} />
          <InfoRow label="Created" value={formatTimestamp(savedSearch.created_at)} />
          <InfoRow label="Updated" value={formatTimestamp(savedSearch.updated_at)} />
          <InfoRow
            label="Listings found"
            value={latestListings.data ? String(latestListings.data.total_count) : '—'}
          />
        </View>

        {actionError ? <Text style={styles.errorText}>{actionError}</Text> : null}

        <View style={styles.actions}>
          <PrimaryButton label="Run now" onPress={handleRunNow} loading={running} />
          <PrimaryButton
            label={savedSearch.is_active ? 'Pause' : 'Resume'}
            onPress={handleToggleActive}
            loading={togglingActive}
            variant="secondary"
          />
          <PrimaryButton label="Delete" onPress={handleDelete} loading={deleting} variant="danger" />
        </View>

        {runError ? <Text style={styles.errorText}>{runError}</Text> : null}

        {runResult ? (
          <View style={styles.infoCard}>
            <Text style={styles.resultTitle}>
              Last run: {runResult.total_new_count} new, {runResult.total_already_seen_count} already seen
            </Text>
            {Object.entries(runResult.marketplaces).map(([marketplace, outcome]) => (
              <View key={marketplace} style={styles.resultRow}>
                <Text style={styles.resultMarketplace}>{displayNameForMarketplace(marketplace)}</Text>
                <Text style={styles.resultCounts}>
                  {outcome.error ? `Failed: ${outcome.error}` : `${outcome.new_count} new, ${outcome.already_seen_count} already seen`}
                </Text>
              </View>
            ))}
          </View>
        ) : null}

        <View style={styles.latestSection}>
          <Text style={styles.sectionTitle}>Latest listings</Text>
          {latestListings.loading ? (
            <Text style={styles.metaText}>Loading…</Text>
          ) : latestListings.error ? (
            <ErrorState message={latestListings.error} onRetry={latestListings.retry} />
          ) : latestListings.data && latestListings.data.items.length > 0 ? (
            <View style={styles.listingList}>
              {latestListings.data.items.map((listing) => (
                <ListingCard key={`${listing.marketplace}-${listing.id}`} listing={listing} />
              ))}
            </View>
          ) : (
            <Text style={styles.metaText}>No listings discovered by this saved search yet.</Text>
          )}
        </View>
      </ScrollView>
    </Screen>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.infoRow}>
      <Text style={styles.infoLabel}>{label}</Text>
      <Text style={styles.infoValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  content: {
    gap: spacing.lg,
    paddingBottom: spacing.xxl,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  query: {
    flex: 1,
    fontSize: fontSize.xl,
    fontWeight: '800',
    color: colors.textPrimary,
  },
  chipRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.xs,
  },
  infoCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: spacing.sm,
  },
  infoRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  infoLabel: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  infoValue: {
    fontSize: fontSize.sm,
    color: colors.textPrimary,
    fontWeight: '600',
  },
  actions: {
    gap: spacing.sm,
  },
  errorText: {
    fontSize: fontSize.sm,
    color: colors.danger,
    textAlign: 'center',
  },
  resultTitle: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  resultRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  resultMarketplace: {
    fontSize: fontSize.sm,
    fontWeight: '600',
    color: colors.textPrimary,
  },
  resultCounts: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  latestSection: {
    gap: spacing.sm,
  },
  sectionTitle: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  metaText: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  listingList: {
    gap: spacing.sm,
  },
});
