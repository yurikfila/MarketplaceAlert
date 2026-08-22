import { Pressable, StyleSheet, Text } from 'react-native';

import { colors, fontSize, radius, spacing } from '../theme/colors';

interface ChipProps {
  label: string;
  selected?: boolean;
  onPress?: () => void;
  muted?: boolean;
}

/** A small pill - used for marketplace tags and the marketplace multi-select/filter. */
export function Chip({ label, selected = false, onPress, muted = false }: ChipProps) {
  const content = (
    <Text
      style={[
        styles.label,
        selected ? styles.labelSelected : muted ? styles.labelMuted : styles.labelDefault,
      ]}
    >
      {label}
    </Text>
  );

  if (!onPress) {
    return <Text style={[styles.chip, muted ? styles.chipMuted : styles.chipDefault]}>{content}</Text>;
  }

  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected }}
      accessibilityLabel={label}
      style={({ pressed }) => [
        styles.chip,
        selected ? styles.chipSelected : styles.chipDefault,
        pressed && styles.chipPressed,
      ]}
    >
      {content}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  chip: {
    minHeight: 36,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: radius.pill,
    alignItems: 'center',
    justifyContent: 'center',
  },
  chipDefault: {
    backgroundColor: colors.chipBackground,
  },
  chipMuted: {
    backgroundColor: colors.chipBackgroundMuted,
  },
  chipSelected: {
    backgroundColor: colors.primary,
  },
  chipPressed: {
    opacity: 0.8,
  },
  label: {
    fontSize: fontSize.sm,
    fontWeight: '600',
  },
  labelDefault: {
    color: colors.chipText,
  },
  labelMuted: {
    color: colors.chipTextMuted,
  },
  labelSelected: {
    color: colors.onPrimary,
  },
});
