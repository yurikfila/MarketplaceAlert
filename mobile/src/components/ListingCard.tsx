import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';

import type { Listing } from '../api/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatPrice, formatTimestamp } from '../utils/format';
import { Chip } from './Chip';

interface ListingCardProps {
  listing: Listing;
}

/** Renders only fields the backend actually returned - never invents a price/location/image (see api/types.ts). */
export function ListingCard({ listing }: ListingCardProps) {
  const price = formatPrice(listing.price, listing.currency);

  return (
    <Pressable
      onPress={() => Linking.openURL(listing.listing_url)}
      accessibilityRole="link"
      accessibilityLabel={`${listing.title}, open original listing`}
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
    >
      <View style={styles.headerRow}>
        <Chip label={listing.marketplace} />
        {price ? <Text style={styles.price}>{price}</Text> : null}
      </View>

      <Text style={styles.title} numberOfLines={2}>
        {listing.title}
      </Text>

      {listing.location ? <Text style={styles.meta}>{listing.location}</Text> : null}

      <Text style={styles.meta}>Discovered {formatTimestamp(listing.first_discovered_at)}</Text>
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
    gap: spacing.xs,
  },
  cardPressed: {
    backgroundColor: colors.background,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  price: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  title: {
    fontSize: fontSize.md,
    fontWeight: '600',
    color: colors.textPrimary,
  },
  meta: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
});
