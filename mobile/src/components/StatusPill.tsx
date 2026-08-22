import { StyleSheet, Text, View } from 'react-native';

import { colors, fontSize, radius, spacing } from '../theme/colors';

type Tone = 'success' | 'warning' | 'danger' | 'neutral';

interface StatusPillProps {
  label: string;
  tone: Tone;
}

const TONE_STYLES: Record<Tone, { background: string; text: string }> = {
  success: { background: colors.successBackground, text: colors.success },
  warning: { background: colors.warningBackground, text: colors.warning },
  danger: { background: colors.dangerBackground, text: colors.danger },
  neutral: { background: colors.chipBackgroundMuted, text: colors.chipTextMuted },
};

/** A small colored status badge - e.g. "Active"/"Inactive", "Connected"/"Unavailable". */
export function StatusPill({ label, tone }: StatusPillProps) {
  const toneStyle = TONE_STYLES[tone];
  return (
    <View style={[styles.pill, { backgroundColor: toneStyle.background }]}>
      <Text style={[styles.label, { color: toneStyle.text }]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  pill: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    borderRadius: radius.pill,
    alignSelf: 'flex-start',
  },
  label: {
    fontSize: fontSize.xs,
    fontWeight: '700',
    textTransform: 'uppercase',
    letterSpacing: 0.4,
  },
});
