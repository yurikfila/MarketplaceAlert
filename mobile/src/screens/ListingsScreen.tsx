import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, View } from 'react-native';

import { ApiError } from '../api/client';
import { getMarketplaces, listListings } from '../api/endpoints';
import type { Listing } from '../api/types';
import { Chip } from '../components/Chip';
import { EmptyState } from '../components/EmptyState';
import { ErrorState } from '../components/ErrorState';
import { LoadingView } from '../components/LoadingView';
import { ListingCard } from '../components/ListingCard';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import { spacing } from '../theme/colors';

const PAGE_SIZE = 20;

export function ListingsScreen() {
  const marketplaces = useAsyncData(getMarketplaces, []);
  const [selectedMarketplace, setSelectedMarketplace] = useState<string | null>(null);

  const [items, setItems] = useState<Listing[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Overlap protection (shared by every load below, manual or automatic) -
  // see useAsyncData's `inFlightRef` for the same pattern/reasoning.
  const inFlightRef = useRef(false);
  // Read inside refreshQuietly without making it depend on (and therefore
  // change identity every time the list grows) `items.length`.
  const itemsLengthRef = useRef(0);
  useEffect(() => {
    itemsLengthRef.current = items.length;
  }, [items.length]);

  const loadPage = useCallback(
    async (offset: number, mode: 'initial' | 'refresh' | 'more') => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      if (mode === 'initial') setLoading(true);
      if (mode === 'refresh') setRefreshing(true);
      if (mode === 'more') setLoadingMore(true);
      setError(null);
      try {
        const page = await listListings({
          limit: PAGE_SIZE,
          offset,
          marketplace: selectedMarketplace ?? undefined,
        });
        setItems((current) => (mode === 'more' ? [...current, ...page.items] : page.items));
        setTotalCount(page.total_count);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not load listings.');
      } finally {
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
        inFlightRef.current = false;
      }
    },
    [selectedMarketplace],
  );

  // Automatic background reload: re-fetches exactly as many items as are
  // currently loaded (so newly discovered listings above already-seen
  // ones are picked up, and pagination position isn't lost) and replaces
  // the dataset in place - never touches loading/refreshing/error, so a
  // failure (a cold start, a dropped connection) just leaves the current
  // list on screen untouched for the next tick to retry, and a success
  // never resets FlatList's scroll position since item keys/order for
  // everything already on screen stay the same.
  const refreshQuietly = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const page = await listListings({
        limit: Math.max(itemsLengthRef.current, PAGE_SIZE),
        offset: 0,
        marketplace: selectedMarketplace ?? undefined,
      });
      setItems(page.items);
      setTotalCount(page.total_count);
    } catch {
      // Preserve whatever is currently displayed - see doc comment above.
    } finally {
      inFlightRef.current = false;
    }
  }, [selectedMarketplace]);
  useAutoRefresh(refreshQuietly);

  useEffect(() => {
    loadPage(0, 'initial');
  }, [loadPage]);

  const canLoadMore = totalCount !== null && items.length < totalCount;

  return (
    <Screen padded={false}>
      <View style={styles.filterRow}>
        <Chip label="All" selected={selectedMarketplace === null} onPress={() => setSelectedMarketplace(null)} />
        {marketplaces.data?.map((marketplace) => (
          <Chip
            key={marketplace.id}
            label={marketplace.name}
            selected={selectedMarketplace === marketplace.id}
            onPress={() => setSelectedMarketplace(marketplace.id)}
          />
        ))}
      </View>

      {loading ? (
        <LoadingView label="Loading listings…" />
      ) : error && items.length === 0 ? (
        <ErrorState message={error} onRetry={() => loadPage(0, 'initial')} />
      ) : (
        <FlatList<Listing>
          data={items}
          keyExtractor={(item) => `${item.marketplace}-${item.id}`}
          contentContainerStyle={styles.listContent}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => loadPage(0, 'refresh')} />}
          renderItem={({ item }) => <ListingCard listing={item} />}
          ListEmptyComponent={
            <EmptyState title="No listings yet" message="Discovered listings will show up here once a saved search finds something." />
          }
          ListFooterComponent={
            canLoadMore ? (
              <View style={styles.footer}>
                {loadingMore ? (
                  <ActivityIndicator />
                ) : (
                  <PrimaryButton label="Load more" onPress={() => loadPage(items.length, 'more')} variant="secondary" />
                )}
              </View>
            ) : null
          }
        />
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  filterRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.sm,
  },
  listContent: {
    padding: spacing.lg,
    gap: spacing.md,
    flexGrow: 1,
  },
  footer: {
    paddingVertical: spacing.md,
  },
});
