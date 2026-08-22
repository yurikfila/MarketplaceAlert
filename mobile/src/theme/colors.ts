/** A small, consistent design palette - deliberately simple for a first version. */
export const colors = {
  background: '#F4F5F9',
  surface: '#FFFFFF',
  border: '#E4E6EE',
  textPrimary: '#171A22',
  textSecondary: '#5B6072',
  textMuted: '#9A9EAD',
  primary: '#2F5FED',
  primaryPressed: '#2449BC',
  onPrimary: '#FFFFFF',
  success: '#1C9A5B',
  successBackground: '#E4F6EC',
  danger: '#D8393F',
  dangerPressed: '#B12E33',
  dangerBackground: '#FBE8E8',
  warning: '#B5730F',
  warningBackground: '#FBF0DC',
  chipBackground: '#EAEFFE',
  chipText: '#33449A',
  chipBackgroundMuted: '#EEF0F5',
  chipTextMuted: '#5B6072',
} as const;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const radius = {
  sm: 6,
  md: 10,
  lg: 14,
  pill: 999,
} as const;

export const fontSize = {
  xs: 12,
  sm: 13,
  md: 15,
  lg: 17,
  xl: 20,
  xxl: 26,
} as const;
