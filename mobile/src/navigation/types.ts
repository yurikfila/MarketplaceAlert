import type { NavigatorScreenParams } from '@react-navigation/native';

export type TabParamList = {
  Home: undefined;
  Searches: undefined;
  Listings: undefined;
};

export type RootStackParamList = {
  Tabs: NavigatorScreenParams<TabParamList>;
  CreateSearch: undefined;
  SavedSearchDetail: { id: number };
};

// Lets useNavigation()/navigation.navigate() infer route names/params
// throughout the app without passing a generic everywhere.
declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace ReactNavigation {
    interface RootParamList extends RootStackParamList {}
  }
}
