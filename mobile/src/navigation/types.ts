import type { NavigatorScreenParams } from '@react-navigation/native';

/** Shown instead of RootStackParamList's tree while `useAuth().status !== 'authenticated'` - see RootNavigator/AuthNavigator. */
export type AuthStackParamList = {
  Login: undefined;
  Signup: undefined;
};

export type TabParamList = {
  Home: undefined;
  Searches: undefined;
  Listings: undefined;
  Account: undefined;
};

export type RootStackParamList = {
  Tabs: NavigatorScreenParams<TabParamList>;
  CreateSearch: undefined;
  SavedSearchDetail: { id: number };
};

// Lets useNavigation()/navigation.navigate() infer route names/params
// throughout the authenticated app without passing a generic everywhere.
// AuthStackParamList is deliberately NOT included here - LoginScreen/
// SignupScreen type their navigation prop explicitly with
// NativeStackNavigationProp<AuthStackParamList> instead (see
// AuthNavigator.tsx), the same way every other screen already does for
// RootStackParamList - the two stacks are never mounted at once, so
// there's nothing to merge.
declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace ReactNavigation {
    interface RootParamList extends RootStackParamList {}
  }
}
