import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { SavedSearch } from '../api/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatIntervalSeconds, formatTimestamp } from '../utils/format';
import { displayNameForMarketplace } from '../utils/marketplaces';
import { Chip } from './Chip';
import { StatusPill } from './StatusPill';

interface SavedSearchCardProps {
  savedSearch: SavedSearch;
  onPress: () => void;
}

export function SavedSearchCard({ savedSearch, onPress }: SavedSearchCardProps) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`${savedSearch.query}, ${savedSearch.is_active ? 'active' : 'inactive'}`}
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
    >
      <View style={styles.headerRow}>
        <Text style={styles.query} numberOfLines={1}>
          {savedSearch.query}
        </Text>
        <StatusPill
          label={savedSearch.is_active ? 'Active' : 'Inactive'}
          tone={savedSearch.is_active ? 'success' : 'neutral'}
        />
      </View>

      <View style={styles.chipRow}>
        {savedSearch.marketplaces.map((marketplace) => (
          <Chip key={marketplace} label={displayNameForMarketplace(marketplace)} muted />
        ))}
      </View>

      <View style={styles.metaRow}>
        <Text style={styles.metaText}>Every {formatIntervalSeconds(savedSearch.scan_interval_seconds)}</Text>
        <Text style={styles.metaText}>Last scanned: {formatTimestamp(savedSearch.last_scanned_at)}</Text>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: spacing.sm,
  },
  cardPressed: {
    backgroundColor: colors.background,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  query: {
    flex: 1,
    fontSize: fontSize.lg,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  chipRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.xs,
  },
  metaRow: {
    gap: 2,
  },
  metaText: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
});
