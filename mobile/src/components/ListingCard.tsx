import { useState } from 'react';
import { Image, Pressable, StyleSheet, Text, View } from 'react-native';

import type { Listing } from '../api/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { openListingUrl } from '../utils/linking';
import { displayNameForMarketplace } from '../utils/marketplaces';
import { formatPrice, formatRelativeTime, isRecentlyDiscovered } from '../utils/format';
import { Chip } from './Chip';
import { StatusPill } from './StatusPill';

interface ListingCardProps {
  listing: Listing;
}

/**
 * Renders only fields the backend actually returned for this listing -
 * never invents a price/location/seller/image (see api/types.ts). Every
 * optional field degrades gracefully: absent ones are simply omitted,
 * never shown as an empty row, a placeholder dash, or "undefined".
 */
export function ListingCard({ listing }: ListingCardProps) {
  const [imageFailed, setImageFailed] = useState(false);
  const price = formatPrice(listing.price, listing.currency);
  const showImage = Boolean(listing.image_url) && !imageFailed;
  const isNew = isRecentlyDiscovered(listing.first_discovered_at);
  const metaLine = [listing.condition, listing.location].filter(Boolean).join(' · ');

  return (
    <Pressable
      onPress={() => openListingUrl(listing.listing_url)}
      accessibilityRole="link"
      accessibilityLabel={`${listing.title}, ${displayNameForMarketplace(listing.marketplace)}${price ? `, ${price}` : ''}, open original listing`}
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
    >
      <View style={styles.row}>
        {showImage ? (
          <Image
            source={{ uri: listing.image_url as string }}
            style={styles.image}
            onError={() => setImageFailed(true)}
            accessibilityIgnoresInvertColors
          />
        ) : (
          <View style={[styles.image, styles.imagePlaceholder]} />
        )}

        <View style={styles.body}>
          <View style={styles.headerRow}>
            <Chip label={displayNameForMarketplace(listing.marketplace)} />
            {isNew ? <StatusPill label="New" tone="success" /> : null}
          </View>

          <Text style={styles.title} numberOfLines={2}>
            {listing.title}
          </Text>

          {price ? <Text style={styles.price}>{price}</Text> : null}

          {metaLine ? (
            <Text style={styles.meta} numberOfLines={1}>
              {metaLine}
            </Text>
          ) : null}

          {listing.seller ? (
            <Text style={styles.meta} numberOfLines={1}>
              Seller: {listing.seller}
            </Text>
          ) : null}

          <Text style={styles.timeMeta}>{formatRelativeTime(listing.first_discovered_at)}</Text>
        </View>
      </View>
    </Pressable>
  );
}

const IMAGE_SIZE = 72;

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
  },
  cardPressed: {
    backgroundColor: colors.background,
  },
  row: {
    flexDirection: 'row',
    gap: spacing.md,
  },
  image: {
    width: IMAGE_SIZE,
    height: IMAGE_SIZE,
    borderRadius: radius.md,
  },
  imagePlaceholder: {
    backgroundColor: colors.chipBackgroundMuted,
  },
  body: {
    flex: 1,
    gap: 4,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
  },
  title: {
    fontSize: fontSize.md,
    fontWeight: '600',
    color: colors.textPrimary,
  },
  price: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  meta: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  timeMeta: {
    fontSize: fontSize.xs,
    color: colors.textMuted,
  },
});
