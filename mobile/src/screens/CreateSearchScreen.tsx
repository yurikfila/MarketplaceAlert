import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { useState } from 'react';
import { ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';

import { ApiError } from '../api/client';
import { createSavedSearch, getMarketplaces } from '../api/endpoints';
import { Chip } from '../components/Chip';
import { ErrorState } from '../components/ErrorState';
import { LoadingView } from '../components/LoadingView';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { useAsyncData } from '../hooks/useAsyncData';
import type { RootStackParamList } from '../navigation/types';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { formatIntervalSeconds, scanIntervalPresets } from '../utils/format';
import { validateCreateSearchForm } from '../utils/validation';

export function CreateSearchScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const marketplaces = useAsyncData(getMarketplaces, []);

  const [query, setQuery] = useState('');
  const [selectedMarketplaces, setSelectedMarketplaces] = useState<string[]>([]);
  const [scanIntervalSeconds, setScanIntervalSeconds] = useState(scanIntervalPresets()[0].seconds);
  const [isActive, setIsActive] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<ReturnType<typeof validateCreateSearchForm>['errors']>({});

  function toggleMarketplace(id: string) {
    setSelectedMarketplaces((current) => (current.includes(id) ? current.filter((m) => m !== id) : [...current, id]));
  }

  async function handleSubmit() {
    const validation = validateCreateSearchForm({ query, marketplaces: selectedMarketplaces, scanIntervalSeconds });
    setFieldErrors(validation.errors);
    if (!validation.valid) {
      return;
    }

    setSubmitError(null);
    setSubmitting(true);
    try {
      await createSavedSearch({
        query: query.trim(),
        marketplaces: selectedMarketplaces,
        scan_interval_seconds: scanIntervalSeconds,
        is_active: isActive,
      });
      navigation.goBack();
    } catch (error) {
      setSubmitError(error instanceof ApiError ? error.message : 'Could not create the saved search.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Screen padded={false}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <View style={styles.field}>
          <Text style={styles.label}>Search keyword or phrase</Text>
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder='e.g. "Rolex Submariner"'
            placeholderTextColor={colors.textMuted}
            style={styles.input}
            accessibilityLabel="Search keyword or phrase"
            returnKeyType="done"
          />
          {fieldErrors.query ? <Text style={styles.fieldError}>{fieldErrors.query}</Text> : null}
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>Marketplaces</Text>
          {marketplaces.loading ? (
            <LoadingView label="Loading marketplaces…" />
          ) : marketplaces.error ? (
            <ErrorState message={marketplaces.error} onRetry={marketplaces.retry} />
          ) : (
            <View style={styles.chipRow}>
              {marketplaces.data?.map((marketplace) => (
                <Chip
                  key={marketplace.id}
                  label={marketplace.configured ? marketplace.name : `${marketplace.name} (not configured)`}
                  selected={selectedMarketplaces.includes(marketplace.id)}
                  onPress={() => toggleMarketplace(marketplace.id)}
                />
              ))}
            </View>
          )}
          {fieldErrors.marketplaces ? <Text style={styles.fieldError}>{fieldErrors.marketplaces}</Text> : null}
        </View>

        <View style={styles.field}>
          <Text style={styles.label}>Scan interval</Text>
          <View style={styles.chipRow}>
            {scanIntervalPresets().map((preset) => (
              <Chip
                key={preset.seconds}
                label={preset.label}
                selected={scanIntervalSeconds === preset.seconds}
                onPress={() => setScanIntervalSeconds(preset.seconds)}
              />
            ))}
          </View>
          <Text style={styles.helperText}>Scans every {formatIntervalSeconds(scanIntervalSeconds)}.</Text>
          {fieldErrors.scanIntervalSeconds ? <Text style={styles.fieldError}>{fieldErrors.scanIntervalSeconds}</Text> : null}
        </View>

        <View style={[styles.field, styles.switchRow]}>
          <Text style={styles.label}>Active</Text>
          <Switch
            value={isActive}
            onValueChange={setIsActive}
            accessibilityLabel="Active"
            accessibilityHint="When off, this saved search will not run automatically"
          />
        </View>

        {submitError ? <Text style={styles.submitError}>{submitError}</Text> : null}

        <PrimaryButton label="Create search" onPress={handleSubmit} loading={submitting} />
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
  chipRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.sm,
  },
  helperText: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  fieldError: {
    fontSize: fontSize.sm,
    color: colors.danger,
  },
  switchRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  submitError: {
    fontSize: fontSize.md,
    color: colors.danger,
    textAlign: 'center',
  },
});
