import { Ionicons } from '@expo/vector-icons';
import { useState } from 'react';
import { Pressable, StyleSheet, TextInput, View, type TextInputProps } from 'react-native';

import { colors, fontSize, radius, spacing } from '../theme/colors';

interface PasswordInputProps extends Omit<TextInputProps, 'secureTextEntry' | 'style'> {
  accessibilityLabel: string;
}

/**
 * A password field with a visibility toggle overlaid inside it - shared
 * by LoginScreen and SignupScreen so the show/hide behavior exists in
 * exactly one place instead of being duplicated per screen. The toggle
 * only flips `secureTextEntry`; it never reads, logs, or modifies the
 * password value itself, and each instance owns its own independent
 * visibility state.
 */
export function PasswordInput({ accessibilityLabel, ...textInputProps }: PasswordInputProps) {
  const [visible, setVisible] = useState(false);

  return (
    <View style={styles.wrapper}>
      <TextInput
        {...textInputProps}
        secureTextEntry={!visible}
        style={styles.input}
        accessibilityLabel={accessibilityLabel}
      />
      <Pressable
        onPress={() => setVisible((current) => !current)}
        style={styles.toggle}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={visible ? 'Hide password' : 'Show password'}
      >
        <Ionicons name={visible ? 'eye-outline' : 'eye-off-outline'} size={20} color={colors.textMuted} />
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrapper: {
    justifyContent: 'center',
  },
  input: {
    minHeight: 48,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    paddingRight: spacing.xxl + spacing.sm,
    fontSize: fontSize.md,
    color: colors.textPrimary,
    backgroundColor: colors.surface,
  },
  toggle: {
    position: 'absolute',
    right: spacing.xs,
    height: 40,
    width: 40,
    alignItems: 'center',
    justifyContent: 'center',
  },
});
