import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { render } from '@testing-library/react-native';
import type { ComponentType } from 'react';

const Stack = createNativeStackNavigator();

/**
 * Renders a screen component as the sole route of a real (if minimal)
 * navigator, wrapped in a real NavigationContainer - the pattern React
 * Navigation's own docs recommend for testing, since it makes
 * useNavigation()/useRoute()/useFocusEffect() behave exactly as they do
 * in the real app, rather than needing hand-mocked hook stubs that could
 * drift from real behavior.
 *
 * `render()` itself is async as of React Native Testing Library v14 -
 * callers must `await` this.
 */
export function renderWithNavigation(Component: ComponentType, params?: object) {
  return render(
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        <Stack.Screen name="UnderTest" component={Component} initialParams={params} />
      </Stack.Navigator>
    </NavigationContainer>,
  );
}
