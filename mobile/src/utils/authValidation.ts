/**
 * Signup/login form validation - pure functions, mirroring the backend's
 * own rules (marketplace_alert/api/v1/schemas.py's `SignupRequest`/
 * `LoginRequest`) so a user sees the same problem client-side,
 * immediately, instead of only after a round trip. The backend remains
 * the source of truth - these are a fast pre-check, not a replacement
 * for server-side errors (see LoginScreen/SignupScreen's ApiError handling).
 */

export const MIN_SIGNUP_PASSWORD_LENGTH = 8;

export type AuthFieldErrors = Partial<Record<'email' | 'password', string>>;

export interface AuthValidationResult {
  valid: boolean;
  errors: AuthFieldErrors;
}

/**
 * Shared by both forms - only an empty/blank email is ever rejected
 * client-side. Real format validation (and whether the address is
 * actually usable) is the backend's job; this is not a full email
 * validator, deliberately, to avoid rejecting something the backend
 * would have accepted.
 */
function validateEmailField(email: string): string | undefined {
  if (email.trim().length === 0) {
    return 'Enter your email address.';
  }
  return undefined;
}

export function validateLoginForm(values: { email: string; password: string }): AuthValidationResult {
  const errors: AuthFieldErrors = {};

  const emailError = validateEmailField(values.email);
  if (emailError) {
    errors.email = emailError;
  }

  // Deliberately no minimum length here (unlike signup below) - an
  // existing credential is either right or wrong, and that's the
  // backend's job to decide; there's nothing about its shape to
  // pre-validate.
  if (values.password.length === 0) {
    errors.password = 'Enter your password.';
  }

  return { valid: Object.keys(errors).length === 0, errors };
}

export function validateSignupForm(values: { email: string; password: string }): AuthValidationResult {
  const errors: AuthFieldErrors = {};

  const emailError = validateEmailField(values.email);
  if (emailError) {
    errors.email = emailError;
  }

  if (values.password.length < MIN_SIGNUP_PASSWORD_LENGTH) {
    errors.password = `Password must be at least ${MIN_SIGNUP_PASSWORD_LENGTH} characters.`;
  }

  return { valid: Object.keys(errors).length === 0, errors };
}

/** Shared by ForgotPasswordScreen - only email-blank is checked client-side, same rationale as validateEmailField above. */
export function validateForgotPasswordForm(values: { email: string }): AuthValidationResult {
  const errors: AuthFieldErrors = {};

  const emailError = validateEmailField(values.email);
  if (emailError) {
    errors.email = emailError;
  }

  return { valid: Object.keys(errors).length === 0, errors };
}

export const RESET_CODE_LENGTH = 6;

export type ResetPasswordFieldErrors = Partial<Record<'code' | 'newPassword' | 'confirmPassword', string>>;

export interface ResetPasswordValidationResult {
  valid: boolean;
  errors: ResetPasswordFieldErrors;
}

/**
 * ResetPasswordScreen's form - the new-password rule intentionally
 * matches `validateSignupForm`'s (`MIN_SIGNUP_PASSWORD_LENGTH`), mirroring
 * the backend's own `_MIN_NEW_PASSWORD_LENGTH` for reset, which is the
 * same value as signup's `Field(min_length=8)` - never a different rule
 * for reset than for signup.
 */
export function validateResetPasswordForm(values: {
  code: string;
  newPassword: string;
  confirmPassword: string;
}): ResetPasswordValidationResult {
  const errors: ResetPasswordFieldErrors = {};

  if (values.code.length === 0) {
    errors.code = 'Enter the verification code sent to your email.';
  } else if (!/^\d{6}$/.test(values.code)) {
    errors.code = `Enter the ${RESET_CODE_LENGTH}-digit code exactly as sent.`;
  }

  if (values.newPassword.length < MIN_SIGNUP_PASSWORD_LENGTH) {
    errors.newPassword = `Password must be at least ${MIN_SIGNUP_PASSWORD_LENGTH} characters.`;
  }

  if (values.confirmPassword !== values.newPassword) {
    errors.confirmPassword = 'Passwords do not match.';
  }

  return { valid: Object.keys(errors).length === 0, errors };
}
