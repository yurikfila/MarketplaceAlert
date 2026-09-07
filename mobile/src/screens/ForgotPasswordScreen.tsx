import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { useState } from 'react';
import { ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { ApiError } from '../api/client';
import { forgotPassword } from '../api/endpoints';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import type { AuthStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { validateForgotPasswordForm, type AuthFieldErrors } from '../utils/authValidation';

export function ForgotPasswordScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<AuthStackParamList>>();

  const [email, setEmail] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<AuthFieldErrors>({});

  async function handleSubmit() {
    const validation = validateForgotPasswordForm({ email });
    setFieldErrors(validation.errors);
    if (!validation.valid) {
      return;
    }

    const trimmedEmail = email.trim();
    setSubmitError(null);
    setSubmitting(true);
    try {
      await forgotPassword({ email: trimmedEmail });
      // The backend's response is deliberately identical whether or not
      // this email is registered (enumeration-safe) - always proceed to
      // the code-entry screen, never branch on "does this account exist".
      navigation.navigate('ResetPassword', { email: trimmedEmail });
    } catch (error) {
      setSubmitError(error instanceof ApiError ? error.message : 'Could not send the verification code.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Screen padded={false}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>Reset password</Text>
        <Text style={styles.explanation}>Enter the email address associated with your MarketplaceAlert account.</Text>

        <View style={styles.field}>
          <Text style={styles.label}>Email</Text>
          <TextInput
            value={email}
            onChangeText={setEmail}
            placeholder="you@example.com"
            placeholderTextColor={colors.textMuted}
            style={styles.input}
            accessibilityLabel="Email"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="email-address"
            textContentType="username"
            returnKeyType="done"
            onSubmitEditing={handleSubmit}
          />
          {fieldErrors.email ? <Text style={styles.fieldError}>{fieldErrors.email}</Text> : null}
        </View>

        {submitError ? <Text style={styles.submitError}>{submitError}</Text> : null}

        <PrimaryButton label="Send verification code" onPress={handleSubmit} loading={submitting} />
        <PrimaryButton label="Back to sign in" onPress={() => navigation.navigate('Login')} variant="secondary" />
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: {
    padding: spacing.lg,
    gap: spacing.xl,
    paddingBottom: spacing.xxl,
  },
  title: {
    fontSize: fontSize.xxl,
    fontWeight: '800',
    color: colors.textPrimary,
    textAlign: 'center',
    marginTop: spacing.xl,
  },
  explanation: {
    fontSize: fontSize.md,
    color: colors.textSecondary,
    textAlign: 'center',
  },
  field: {
    gap: spacing.sm,
  },
  label: {
    fontSize: fontSize.md,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  input: {
    minHeight: 48,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    fontSize: fontSize.md,
    color: colors.textPrimary,
    backgroundColor: colors.surface,
  },
  fieldError: {
    fontSize: fontSize.sm,
    color: colors.danger,
  },
  submitError: {
    fontSize: fontSize.md,
    color: colors.danger,
    textAlign: 'center',
  },
});
