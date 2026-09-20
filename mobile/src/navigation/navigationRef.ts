import { createNavigationContainerRef } from '@react-navigation/native';

import type { RootStackParamList } from './types';

/**
 * A top-level ref onto the authenticated stack's `NavigationContainer`
 * (attached in `RootNavigator.tsx`), so code that lives outside any
 * screen component - specifically, `utils/pushNotifications.ts`'s
 * notification-tap handler - can navigate imperatively, without needing
 * its own `useNavigation()` hook (there is no enclosing component to call
 * it from).
 *
 * Only meaningful while the authenticated stack is actually mounted
 * (`useAuth().status === 'authenticated'`) - `navigationRef.isReady()`
 * is `false` before that (still restoring, or on the unauthenticated
 * Auth stack, which is a completely separate `NavigationContainer` - see
 * `AuthNavigator.tsx`). Every caller must check `isReady()` before
 * calling `navigate()` - a tap that arrives before the app has finished
 * restoring/authenticating is simply not actionable yet, never a crash.
 */
export const navigationRef = createNavigationContainerRef<RootStackParamList>();
