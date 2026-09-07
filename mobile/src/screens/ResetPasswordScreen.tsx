import { useNavigation, useRoute } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import type { RouteProp } from '@react-navigation/native';
import { useState } from 'react';
import { ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { ApiError } from '../api/client';
import { forgotPassword, resetPassword } from '../api/endpoints';
import { PasswordInput } from '../components/PasswordInput';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import type { AuthStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { RESET_CODE_LENGTH, validateResetPasswordForm, type ResetPasswordFieldErrors } from '../utils/authValidation';

type ResetPasswordRouteProp = RouteProp<AuthStackParamList, 'ResetPassword'>;

export function ResetPasswordScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<AuthStackParamList>>();
  const { params } = useRoute<ResetPasswordRouteProp>();
  const { email } = params;

  const [code, setCode] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<ResetPasswordFieldErrors>({});
  const [resending, setResending] = useState(false);
  const [resendMessage, setResendMessage] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function handleSubmit() {
    const validation = validateResetPasswordForm({ code, newPassword, confirmPassword });
    setFieldErrors(validation.errors);
    if (!validation.valid) {
      return;
    }

    setSubmitError(null);
    setResendMessage(null);
    setSubmitting(true);
    try {
      await resetPassword({ email, code, new_password: newPassword });
      setSuccess(true);
      // Deliberately no auto-login here - the backend revokes every
      // existing session as part of a successful reset, so the user
      // signs in fresh with the new password, same as any other change
      // of credentials.
    } catch (error) {
      setSubmitError(error instanceof ApiError ? error.message : 'Could not reset your password.');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResend() {
    setSubmitError(null);
    setResendMessage(null);
    setResending(true);
    try {
      // The same forgot-password endpoint ForgotPasswordScreen already
      // called for this email - the backend's own resend cooldown/rate
      // limit governs whether a new code is actually issued. There is no
      // separate resend API to call.
      await forgotPassword({ email });
      setResendMessage('If that email is registered, a new verification code has been sent.');
    } catch (error) {
      setSubmitError(error instanceof ApiError ? error.message : 'Could not send a new code.');
    } finally {
      setResending(false);
    }
  }

  if (success) {
    return (
      <Screen>
        <View style={styles.successContent}>
          <Text style={styles.title}>Password updated successfully.</Text>
          <PrimaryButton label="Back to sign in" onPress={() => navigation.navigate('Login')} />
        </View>
      </Screen>
    );
  }

  return (
    <Screen padded={false}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>Enter verification code</Text>
        <Text style={styles.explanation}>A 6-digit code was sent to {email}.</Text>

        <View style={styles.field}>
          <Text style={styles.label}>Verification code</Text>
          <TextInput
            value={code}
            onChangeText={(text) => setCode(text.replace(/\D/g, '').slice(0, RESET_CODE_LENGTH))}
            placeholder="123456"
            placeholderTextColor={colors.textMuted}
            style={styles.input}
            accessibilityLabel="Verification code"
            keyboardType="number-pad"
            maxLength={RESET_CODE_LENGTH}
            returnKeyType="next"
          />
          {fieldErrors.code ? <Text style={styles.fieldError}>{fieldErrors.code}</Text> : null}
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>New password</Text>
          <PasswordInput
            value={newPassword}
            onChangeText={setNewPassword}
            placeholder="At least 8 characters"
            placeholderTextColor={colors.textMuted}
            accessibilityLabel="New password"
            textContentType="newPassword"
            returnKeyType="next"
          />
          {fieldErrors.newPassword ? <Text style={styles.fieldError}>{fieldErrors.newPassword}</Text> : null}
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>Confirm new password</Text>
          <PasswordInput
            value={confirmPassword}
            onChangeText={setConfirmPassword}
            placeholder="Re-enter your new password"
            placeholderTextColor={colors.textMuted}
            accessibilityLabel="Confirm new password"
            textContentType="newPassword"
            returnKeyType="done"
            onSubmitEditing={handleSubmit}
          />
          {fieldErrors.confirmPassword ? <Text style={styles.fieldError}>{fieldErrors.confirmPassword}</Text> : null}
        </View>

        {submitError ? <Text style={styles.submitError}>{submitError}</Text> : null}
        {resendMessage ? <Text style={styles.resendMessage}>{resendMessage}</Text> : null}

        <PrimaryButton label="Reset password" onPress={handleSubmit} loading={submitting} />
        <PrimaryButton label="Send a new code" onPress={handleResend} variant="secondary" loading={resending} />
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
  resendMessage: {
    fontSize: fontSize.sm,
    color: colors.success,
    textAlign: 'center',
  },
  successContent: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.xl,
    paddingHorizontal: spacing.lg,
  },
});
