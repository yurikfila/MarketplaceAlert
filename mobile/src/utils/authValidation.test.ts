import { MIN_SIGNUP_PASSWORD_LENGTH, validateLoginForm, validateSignupForm } from './authValidation';

describe('validateLoginForm', () => {
  it('requires a non-blank email', () => {
    const result = validateLoginForm({ email: '  ', password: 'anything' });

    expect(result.valid).toBe(false);
    expect(result.errors.email).toBe('Enter your email address.');
  });

  it('requires a non-blank password', () => {
    const result = validateLoginForm({ email: 'a@b.com', password: '' });

    expect(result.valid).toBe(false);
    expect(result.errors.password).toBe('Enter your password.');
  });

  it('does not enforce a minimum password length on login', () => {
    const result = validateLoginForm({ email: 'a@b.com', password: 'x' });

    expect(result.valid).toBe(true);
    expect(result.errors.password).toBeUndefined();
  });

  it('passes with a valid email and password', () => {
    expect(validateLoginForm({ email: 'a@b.com', password: 'hunter2' })).toEqual({ valid: true, errors: {} });
  });
});

describe('validateSignupForm', () => {
  it('requires a non-blank email', () => {
    const result = validateSignupForm({ email: '', password: 'a-strong-password' });

    expect(result.valid).toBe(false);
    expect(result.errors.email).toBe('Enter your email address.');
  });

  it(`rejects a password shorter than ${MIN_SIGNUP_PASSWORD_LENGTH} characters`, () => {
    const result = validateSignupForm({ email: 'a@b.com', password: 'short' });

    expect(result.valid).toBe(false);
    expect(result.errors.password).toBe(`Password must be at least ${MIN_SIGNUP_PASSWORD_LENGTH} characters.`);
  });

  it(`accepts a password exactly ${MIN_SIGNUP_PASSWORD_LENGTH} characters long`, () => {
    const result = validateSignupForm({ email: 'a@b.com', password: '12345678' });

    expect(result.valid).toBe(true);
    expect(result.errors.password).toBeUndefined();
  });

  it('passes with a valid email and password', () => {
    expect(validateSignupForm({ email: 'a@b.com', password: 'a-strong-password' })).toEqual({ valid: true, errors: {} });
  });
});
