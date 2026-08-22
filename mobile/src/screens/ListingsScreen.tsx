import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';

import { ApiError } from '../api/client';
import { getMarketplaces, listListings, listSavedSearches } from '../api/endpoints';
import type { Listing, ListListingsParams } from '../api/types';
import {
  activeFilterCount,
  emptyListingFilters,
  ListingFilterModal,
  type ListingFilterValues,
} from '../components/ListingFilterModal';
import { EmptyState } from '../components/EmptyState';
import { ErrorState } from '../components/ErrorState';
import { LoadingView } from '../components/LoadingView';
import { ListingCard } from '../components/ListingCard';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import { colors, fontSize, radius, spacing } from '../theme/colors';

const PAGE_SIZE = 20;

const SORT_LABELS: Record<ListingFilterValues['sort'], string> = {
  newest: 'Newest',
  oldest: 'Oldest',
  price_asc: 'Price: low to high',
  price_desc: 'Price: high to low',
};

/** A blank/whitespace-only price field means "no bound" - never treated as 0. Returns `undefined` for anything that doesn't parse to a finite number, so a stray non-numeric paste can't reach the API as `NaN`. */
function parsePriceField(value: string): number | undefined {
  const trimmed = value.trim();
  if (trimmed === '') return undefined;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function toListingsParams(filters: ListingFilterValues, limit: number, offset: number): ListListingsParams {
  return {
    limit,
    offset,
    marketplaces: filters.marketplaces.length > 0 ? filters.marketplaces : undefined,
    saved_search_id: filters.savedSearchId ?? undefined,
    min_price: parsePriceField(filters.minPrice),
    max_price: parsePriceField(filters.maxPrice),
    condition: filters.condition.trim() === '' ? undefined : filters.condition.trim(),
    sort: filters.sort,
  };
}

export function ListingsScreen() {
  const marketplaces = useAsyncData(getMarketplaces, []);
  const savedSearches = useAsyncData(listSavedSearches, []);

  const [appliedFilters, setAppliedFilters] = useState<ListingFilterValues>(emptyListingFilters());
  const [draftFilters, setDraftFilters] = useState<ListingFilterValues>(emptyListingFilters());
  const [filterModalVisible, setFilterModalVisible] = useState(false);

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
        const page = await listListings(toListingsParams(appliedFilters, PAGE_SIZE, offset));
        setItems((current) => (mode === 'more' ? [...current, ...page.items] : page.items));
        setTotalCount(page.total_count);
      } catch (err) {
        // Filters/sort are never reset on a failure - the user's chosen
        // view stays exactly as they left it, only the list content and
        // an error message change.
        setError(err instanceof ApiError ? err.message : 'Could not load listings.');
      } finally {
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
        inFlightRef.current = false;
      }
    },
    [appliedFilters],
  );

  // Automatic background reload: re-fetches exactly as many items as are
  // currently loaded (so newly discovered listings above already-seen
  // ones are picked up, and pagination position isn't lost), using the
  // same applied filters/sort - and replaces the dataset in place - never
  // touches loading/refreshing/error, so a failure (a cold start, a
  // dropped connection) just leaves the current list on screen untouched
  // for the next tick to retry, and a success never resets FlatList's
  // scroll position since item keys/order for everything already on
  // screen stay the same.
  const refreshQuietly = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const page = await listListings(
        toListingsParams(appliedFilters, Math.max(itemsLengthRef.current, PAGE_SIZE), 0),
      );
      setItems(page.items);
      setTotalCount(page.total_count);
    } catch {
      // Preserve whatever is currently displayed - see doc comment above.
    } finally {
      inFlightRef.current = false;
    }
  }, [appliedFilters]);
  useAutoRefresh(refreshQuietly);

  useEffect(() => {
    loadPage(0, 'initial');
  }, [loadPage]);

  const openFilterModal = useCallback(() => {
    setDraftFilters(appliedFilters);
    setFilterModalVisible(true);
  }, [appliedFilters]);

  const handleApplyFilters = useCallback(() => {
    const min = parsePriceField(draftFilters.minPrice);
    const max = parsePriceField(draftFilters.maxPrice);
    if (min !== undefined && max !== undefined && min > max) {
      Alert.alert('Invalid price range', 'Minimum price must not be greater than maximum price.');
      return;
    }
    setAppliedFilters(draftFilters);
    setFilterModalVisible(false);
  }, [draftFilters]);

  const handleClearFilters = useCallback(() => {
    const cleared = emptyListingFilters();
    setDraftFilters(cleared);
    setAppliedFilters(cleared);
    setFilterModalVisible(false);
  }, []);

  const filterCount = activeFilterCount(appliedFilters);
  const canLoadMore = totalCount !== null && items.length < totalCount;

  return (
    <Screen padded={false}>
      <View style={styles.toolbar}>
        <Pressable
          onPress={openFilterModal}
          accessibilityRole="button"
          accessibilityLabel={filterCount > 0 ? `Filters, ${filterCount} active` : 'Filters'}
          style={({ pressed }) => [styles.filterButton, pressed && styles.filterButtonPressed]}
        >
          <Text style={styles.filterButtonLabel}>Filters</Text>
          {filterCount > 0 ? (
            <View style={styles.filterBadge}>
              <Text style={styles.filterBadgeLabel}>{filterCount}</Text>
            </View>
          ) : null}
        </Pressable>

        <Pressable
          onPress={openFilterModal}
          accessibilityRole="button"
          accessibilityLabel={`Sort: ${SORT_LABELS[appliedFilters.sort]}`}
          style={styles.sortButton}
          hitSlop={8}
        >
          <Text style={styles.sortLabel}>Sort: {SORT_LABELS[appliedFilters.sort]}</Text>
        </Pressable>
      </View>

      {loading ? (
        <LoadingView label="Loading listings…" />
      ) : error && items.length === 0 ? (
        <ErrorState message={error} onRetry={() => loadPage(0, 'initial')} />
      ) : (
        <FlatList<Listing>
          testID="listings-list"
          data={items}
          keyExtractor={(item) => `${item.marketplace}-${item.id}`}
          contentContainerStyle={styles.listContent}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => loadPage(0, 'refresh')} />}
          renderItem={({ item }) => <ListingCard listing={item} />}
          ListHeaderComponent={
            error ? (
              <Text style={styles.inlineError}>{error} · pull to refresh to retry</Text>
            ) : totalCount !== null ? (
              <Text style={styles.resultCount}>
                {totalCount} listing{totalCount === 1 ? '' : 's'}
              </Text>
            ) : null
          }
          ListEmptyComponent={
            <EmptyState
              title={filterCount > 0 ? 'No listings match these filters' : 'No listings yet'}
              message={
                filterCount > 0
                  ? 'Try widening your filters.'
                  : 'Discovered listings will show up here once a saved search finds something.'
              }
            />
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

      <ListingFilterModal
        visible={filterModalVisible}
        onClose={() => setFilterModalVisible(false)}
        availableMarketplaces={marketplaces.data ?? []}
        availableSavedSearches={(savedSearches.data ?? []).map((s) => ({ id: s.id, query: s.query }))}
        value={draftFilters}
        onChange={setDraftFilters}
        onApply={handleApplyFilters}
        onClear={handleClearFilters}
      />
    </Screen>
  );
}

const styles = StyleSheet.create({
  toolbar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.sm,
  },
  filterButton: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    minHeight: 44,
    paddingHorizontal: spacing.md,
    borderRadius: radius.pill,
    backgroundColor: colors.chipBackground,
  },
  filterButtonPressed: {
    opacity: 0.8,
  },
  filterButtonLabel: {
    fontSize: fontSize.sm,
    fontWeight: '700',
    color: colors.chipText,
  },
  filterBadge: {
    minWidth: 18,
    height: 18,
    borderRadius: 9,
    backgroundColor: colors.primary,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 4,
  },
  filterBadgeLabel: {
    fontSize: fontSize.xs,
    fontWeight: '700',
    color: colors.onPrimary,
  },
  sortButton: {
    minHeight: 44,
    justifyContent: 'center',
    paddingHorizontal: spacing.sm,
  },
  sortLabel: {
    fontSize: fontSize.sm,
    fontWeight: '600',
    color: colors.textSecondary,
  },
  listContent: {
    padding: spacing.lg,
    gap: spacing.md,
    flexGrow: 1,
  },
  resultCount: {
    fontSize: fontSize.sm,
    color: colors.textMuted,
    marginBottom: spacing.xs,
  },
  inlineError: {
    fontSize: fontSize.sm,
    color: colors.danger,
    marginBottom: spacing.sm,
  },
  footer: {
    paddingVertical: spacing.md,
  },
});
