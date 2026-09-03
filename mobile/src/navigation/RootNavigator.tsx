import { Ionicons } from '@expo/vector-icons';
import { NavigationContainer } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import type { ComponentProps } from 'react';

import { useAuth } from '../auth/AuthContext';
import { LoadingView } from '../components/LoadingView';
import { AccountScreen } from '../screens/AccountScreen';
import { CreateSearchScreen } from '../screens/CreateSearchScreen';
import { HomeScreen } from '../screens/HomeScreen';
import { ListingsScreen } from '../screens/ListingsScreen';
import { RestorationErrorScreen } from '../screens/RestorationErrorScreen';
import { SavedSearchDetailScreen } from '../screens/SavedSearchDetailScreen';
import { SavedSearchesScreen } from '../screens/SavedSearchesScreen';
import { colors } from '../theme/colors';
import { AuthNavigator } from './AuthNavigator';
import type { RootStackParamList, TabParamList } from './types';

const Tab = createBottomTabNavigator<TabParamList>();
const Stack = createNativeStackNavigator<RootStackParamList>();

type IoniconName = ComponentProps<typeof Ionicons>['name'];

const TAB_ICONS: Record<keyof TabParamList, IoniconName> = {
  Home: 'home-outline',
  Searches: 'search-outline',
  Listings: 'list-outline',
  Account: 'person-outline',
};

function Tabs() {
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textMuted,
        tabBarIcon: ({ color, size }) => <Ionicons name={TAB_ICONS[route.name as keyof TabParamList]} size={size} color={color} />,
      })}
    >
      <Tab.Screen name="Home" component={HomeScreen} />
      <Tab.Screen name="Searches" component={SavedSearchesScreen} options={{ title: 'Saved Searches' }} />
      <Tab.Screen name="Listings" component={ListingsScreen} />
      <Tab.Screen name="Account" component={AccountScreen} />
    </Tab.Navigator>
  );
}

function AuthenticatedNavigator() {
  return (
    <Stack.Navigator screenOptions={{ headerTintColor: colors.primary }}>
      <Stack.Screen name="Tabs" component={Tabs} options={{ headerShown: false }} />
      <Stack.Screen name="CreateSearch" component={CreateSearchScreen} options={{ title: 'Create Search' }} />
      <Stack.Screen name="SavedSearchDetail" component={SavedSearchDetailScreen} options={{ title: 'Saved Search' }} />
    </Stack.Navigator>
  );
}

/**
 * Swaps the entire navigator tree based on `useAuth().status` - React
 * Navigation's own documented pattern for an authentication flow. All
 * four states render inside the same `NavigationContainer` (see
 * RootNavigator below), so nothing here needs its own container.
 */
function RootNavigatorContent() {
  const { status } = useAuth();

  if (status === 'restoring') {
    return <LoadingView label="Loading…" />;
  }
  if (status === 'restoration-error') {
    return <RestorationErrorScreen />;
  }
  if (status === 'unauthenticated') {
    return <AuthNavigator />;
  }
  return <AuthenticatedNavigator />;
}

export function RootNavigator() {
  return (
    <NavigationContainer>
      <RootNavigatorContent />
    </NavigationContainer>
  );
}
