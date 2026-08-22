import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { FlatList, RefreshControl, StyleSheet, View } from 'react-native';

import { listSavedSearches } from '../api/endpoints';
import type { SavedSearch } from '../api/types';
import { EmptyState } from '../components/EmptyState';
import { ErrorState } from '../components/ErrorState';
import { LoadingView } from '../components/LoadingView';
import { PrimaryButton } from '../components/PrimaryButton';
import { SavedSearchCard } from '../components/SavedSearchCard';
import { Screen } from '../components/Screen';
import { useAsyncData } from '../hooks/useAsyncData';
import { useAutoRefresh } from '../hooks/useAutoRefresh';
import type { RootStackParamList } from '../navigation/types';
import { spacing } from '../theme/colors';

export function SavedSearchesScreen() {
  const navigation = useNavigation<NativeStackNavigationProp<RootStackParamList>>();
  const { data, loading, refreshing, error, refresh, retry, refreshQuietly } = useAsyncData(listSavedSearches, []);

  // Keeps ACTIVE/INACTIVE state, last_scanned_at, and any other
  // server-side change current while this tab is open - interval polling,
  // app resume, and refocusing this tab (e.g. after creating, running, or
  // deleting a search on another screen) - see mobile/README.md
  // "Automatic refresh".
  useAutoRefresh(refreshQuietly);

  if (loading) {
    return (
      <Screen>
        <LoadingView label="Loading saved searches…" />
      </Screen>
    );
  }

  if (error && !data) {
    return (
      <Screen>
        <ErrorState message={error} onRetry={retry} />
      </Screen>
    );
  }

  return (
    <Screen padded={false}>
      <FlatList<SavedSearch>
        data={data ?? []}
        keyExtractor={(item) => String(item.id)}
        contentContainerStyle={styles.listContent}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} />}
        renderItem={({ item }) => (
          <SavedSearchCard
            savedSearch={item}
            onPress={() => navigation.navigate('SavedSearchDetail', { id: item.id })}
          />
        )}
        ListHeaderComponent={
          <View style={styles.header}>
            <PrimaryButton label="Create a saved search" onPress={() => navigation.navigate('CreateSearch')} />
          </View>
        }
        ListEmptyComponent={
          <EmptyState
            title="No saved searches yet"
            message="Create one to start tracking new listings automatically."
          />
        }
      />
    </Screen>
  );
}

const styles = StyleSheet.create({
  listContent: {
    padding: spacing.lg,
    gap: spacing.md,
    flexGrow: 1,
  },
  header: {
    marginBottom: spacing.sm,
  },
});
