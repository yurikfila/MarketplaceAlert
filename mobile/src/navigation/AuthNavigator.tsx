import { createNativeStackNavigator } from '@react-navigation/native-stack';

import { LoginScreen } from '../screens/LoginScreen';
import { SignupScreen } from '../screens/SignupScreen';
import { colors } from '../theme/colors';
import type { AuthStackParamList } from './types';

const Stack = createNativeStackNavigator<AuthStackParamList>();

/** Rendered by RootNavigator whenever `useAuth().status === 'unauthenticated'` - a self-contained stack, entirely separate from the authenticated app's navigator (see RootNavigator). */
export function AuthNavigator() {
  return (
    <Stack.Navigator screenOptions={{ headerTintColor: colors.primary, headerShown: false }}>
      <Stack.Screen name="Login" component={LoginScreen} />
      <Stack.Screen name="Signup" component={SignupScreen} options={{ headerShown: true, title: 'Create Account' }} />
    </Stack.Navigator>
  );
}
