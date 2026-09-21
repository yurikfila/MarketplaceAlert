import { useContext, type ReactNode } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import {
  SafeAreaFrameContext,
  SafeAreaInsetsContext,
  SafeAreaProvider,
  SafeAreaView,
} from 'react-native-safe-area-context';

import type { ListingSort } from '../api/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { displayNameForMarketplace } from '../utils/marketplaces';
import { Chip } from './Chip';
import { PrimaryButton } from './PrimaryButton';

export interface SavedSearchOption {
  id: number;
  query: string;
}

/** The Listings screen's full filter/sort state - `minPrice`/`maxPrice`/`condition` stay as raw text here (validated/parsed only when actually applied) so the modal can hold an in-progress, possibly-invalid draft without the screen underneath reacting to every keystroke. */
export interface ListingFilterValues {
  marketplaces: string[];
  savedSearchId: number | null;
  minPrice: string;
  maxPrice: string;
  condition: string;
  sort: ListingSort;
}

export function emptyListingFilters(): ListingFilterValues {
  return { marketplaces: [], savedSearchId: null, minPrice: '', maxPrice: '', condition: '', sort: 'newest' };
}

/** How many of `value`'s fields represent an active *filter* - sort is deliberately excluded (it reorders results, it doesn't narrow them), matching the badge shown on the Listings screen's "Filters" button. */
export function activeFilterCount(value: ListingFilterValues): number {
  let count = 0;
  if (value.marketplaces.length > 0) count += 1;
  if (value.savedSearchId !== null) count += 1;
  if (value.minPrice.trim() !== '') count += 1;
  if (value.maxPrice.trim() !== '') count += 1;
  if (value.condition.trim() !== '') count += 1;
  return count;
}

const SORT_OPTIONS: Array<{ value: ListingSort; label: string }> = [
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'price_asc', label: 'Price: low to high' },
  { value: 'price_desc', label: 'Price: high to low' },
];

interface ListingFilterModalProps {
  visible: boolean;
  onClose: () => void;
  availableMarketplaces: Array<{ id: string; name: string }>;
  availableSavedSearches: SavedSearchOption[];
  value: ListingFilterValues;
  onChange: (value: ListingFilterValues) => void;
  onApply: () => void;
  onClear: () => void;
}

/**
 * A full-screen modal (the app's first use of `Modal` - no existing
 * bottom-sheet/dedicated-screen precedent to follow, and a modal keeps
 * this self-contained without touching navigation param types) holding a
 * *draft* of the Listings screen's filter/sort state. Nothing here calls
 * the API directly - `onChange` just updates the draft the parent holds,
 * `onApply`/`onClear` are the only two ways a draft actually takes
 * effect, both closing the modal.
 */
export function ListingFilterModal({
  visible,
  onClose,
  availableMarketplaces,
  availableSavedSearches,
  value,
  onChange,
  onApply,
  onClear,
}: ListingFilterModalProps) {
  const toggleMarketplace = (id: string) => {
    const isSelected = value.marketplaces.includes(id);
    onChange({
      ...value,
      marketplaces: isSelected ? value.marketplaces.filter((m) => m !== id) : [...value.marketplaces, id],
    });
  };

  // React Native's `Modal` presents in its own separate native window on
  // Android - the app's root `SafeAreaProvider` (App.tsx) measures insets
  // for the *main* window only, and that stale value is what any
  // `SafeAreaView` in here would otherwise read via inherited React
  // context, never this window's own insets. `react-native-safe-area-
  // context`'s own docs address exactly this: "You may need to add it in
  // other places like the root of modals and routes." Re-mounting the
  // provider below, inside the Modal, makes it measure and provide
  // *this* window's real insets on Android.
  //
  // `initialMetrics` (seeded from whatever the outer provider already
  // knows, read directly off context rather than the throwing
  // `useSafeAreaInsets()`/`useSafeAreaFrame()` hooks, so this still works
  // the one render before any ancestor provider exists) is required, not
  // optional: `SafeAreaProvider` renders none of its children at all
  // until its own `insets` state is non-null (see its source), and
  // without a seed that only ever happens after a real native
  // `onInsetsChange` event - which fires quickly in a real app, but never
  // in a Jest test renderer, and could still cause a real, if brief,
  // blank-modal flash on an actual device. Falling back to all-zero
  // insets/a zero frame when no parent context exists guarantees this
  // never blocks rendering, in tests or in the app.
  const parentInsets = useContext(SafeAreaInsetsContext);
  const parentFrame = useContext(SafeAreaFrameContext);
  const initialMetrics = {
    insets: parentInsets ?? { top: 0, right: 0, bottom: 0, left: 0 },
    frame: parentFrame ?? { x: 0, y: 0, width: 0, height: 0 },
  };

  return (
    <Modal visible={visible} animationType="slide" presentationStyle="pageSheet" onRequestClose={onClose}>
      <SafeAreaProvider initialMetrics={initialMetrics}>
        <View style={styles.container} testID="listing-filter-modal">
          <SafeAreaView edges={['top']} style={styles.header}>
            <Text style={styles.headerTitle}>Filters &amp; sort</Text>
            <Pressable onPress={onClose} accessibilityRole="button" accessibilityLabel="Close filters" hitSlop={12}>
              <Text style={styles.closeLabel}>Done</Text>
            </Pressable>
          </SafeAreaView>

          <ScrollView contentContainerStyle={styles.content}>
          <Section title="Sort">
            <View style={styles.chipRow}>
              {SORT_OPTIONS.map((option) => (
                <Chip
                  key={option.value}
                  label={option.label}
                  selected={value.sort === option.value}
                  onPress={() => onChange({ ...value, sort: option.value })}
                />
              ))}
            </View>
          </Section>

          <Section title="Marketplace">
            <View style={styles.chipRow}>
              {availableMarketplaces.map((marketplace) => (
                <Chip
                  key={marketplace.id}
                  label={displayNameForMarketplace(marketplace.id)}
                  selected={value.marketplaces.includes(marketplace.id)}
                  onPress={() => toggleMarketplace(marketplace.id)}
                />
              ))}
            </View>
            <Text style={styles.hint}>None selected means every marketplace.</Text>
          </Section>

          {availableSavedSearches.length > 0 ? (
            <Section title="Saved search">
              <Pressable
                onPress={() => onChange({ ...value, savedSearchId: null })}
                accessibilityRole="button"
                accessibilityState={{ selected: value.savedSearchId === null }}
                style={styles.optionRow}
              >
                <Text style={[styles.optionLabel, value.savedSearchId === null && styles.optionLabelSelected]}>
                  Any saved search
                </Text>
              </Pressable>
              {availableSavedSearches.map((option) => (
                <Pressable
                  key={option.id}
                  onPress={() => onChange({ ...value, savedSearchId: option.id })}
                  accessibilityRole="button"
                  accessibilityState={{ selected: value.savedSearchId === option.id }}
                  style={styles.optionRow}
                >
                  <Text
                    style={[styles.optionLabel, value.savedSearchId === option.id && styles.optionLabelSelected]}
                    numberOfLines={1}
                  >
                    {option.query}
                  </Text>
                </Pressable>
              ))}
            </Section>
          ) : null}

          <Section title="Price range">
            <View style={styles.priceRow}>
              <TextInput
                style={styles.priceInput}
                placeholder="Min"
                keyboardType="decimal-pad"
                value={value.minPrice}
                onChangeText={(text) => onChange({ ...value, minPrice: text })}
                accessibilityLabel="Minimum price"
              />
              <Text style={styles.priceSeparator}>–</Text>
              <TextInput
                style={styles.priceInput}
                placeholder="Max"
                keyboardType="decimal-pad"
                value={value.maxPrice}
                onChangeText={(text) => onChange({ ...value, maxPrice: text })}
                accessibilityLabel="Maximum price"
              />
            </View>
          </Section>

          <Section title="Condition">
            <TextInput
              style={styles.conditionInput}
              placeholder="e.g. New, Used"
              value={value.condition}
              onChangeText={(text) => onChange({ ...value, condition: text })}
              accessibilityLabel="Condition"
            />
          </Section>
        </ScrollView>

          {/* `edges={['bottom']}` only - adds the device's real bottom
              inset as extra padding on top of `styles.footer`'s own
              `padding` (never replaces it) - see the `SafeAreaProvider`
              comment above for why this now measures correctly. This row
              was already outside the `ScrollView` above (a sibling, not
              a child), so it was already fixed/"sticky" - nothing about
              that structure needed to change. */}
          <SafeAreaView edges={['bottom']} style={styles.footer}>
            <PrimaryButton label="Clear all" onPress={onClear} variant="secondary" />
            <PrimaryButton label="Apply filters" onPress={onApply} />
          </SafeAreaView>
        </View>
      </SafeAreaProvider>
    </Modal>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
  },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    backgroundColor: colors.surface,
  },
  headerTitle: {
    fontSize: fontSize.lg,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  closeLabel: {
    fontSize: fontSize.md,
    fontWeight: '600',
    color: colors.primary,
  },
  content: {
    padding: spacing.lg,
    gap: spacing.lg,
  },
  section: {
    gap: spacing.sm,
  },
  sectionTitle: {
    fontSize: fontSize.sm,
    fontWeight: '700',
    color: colors.textSecondary,
    textTransform: 'uppercase',
    letterSpacing: 0.4,
  },
  chipRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.sm,
  },
  hint: {
    fontSize: fontSize.xs,
    color: colors.textMuted,
  },
  optionRow: {
    minHeight: 44,
    justifyContent: 'center',
    paddingVertical: spacing.xs,
  },
  optionLabel: {
    fontSize: fontSize.md,
    color: colors.textSecondary,
  },
  optionLabelSelected: {
    color: colors.primary,
    fontWeight: '700',
  },
  priceRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  priceInput: {
    flex: 1,
    minHeight: 44,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    fontSize: fontSize.md,
    color: colors.textPrimary,
    backgroundColor: colors.surface,
  },
  priceSeparator: {
    color: colors.textMuted,
  },
  conditionInput: {
    minHeight: 44,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    fontSize: fontSize.md,
    color: colors.textPrimary,
    backgroundColor: colors.surface,
  },
  footer: {
    flexDirection: 'row',
    gap: spacing.sm,
    padding: spacing.lg,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.surface,
  },
});
