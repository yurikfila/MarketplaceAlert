# MarketplaceAlert Mobile

The first real mobile client for [MarketplaceAlert](../README.md) - Android
and iPhone, built with **React Native + Expo + TypeScript**. It talks to
the existing backend's versioned [`/api/v1`](../ARCHITECTURE.md) over
plain HTTPS - it does not implement any new backend logic, and it does not
yet have login, payments, or push notifications (see "Not yet implemented"
below).

## Requirements

- Node.js 18+ and npm
- The [Expo Go](https://expo.dev/go) app on your Android or iPhone (for
  the fastest way to try this out - see "Running on your phone" below)

## Install

```bash
cd mobile
npm install
```

## Environment variable

**`EXPO_PUBLIC_API_BASE_URL`** - the backend base URL (no trailing slash,
no `/api/v1` suffix - that's added automatically). Expo inlines any env
var prefixed `EXPO_PUBLIC_` into the JS bundle at build/start time - see
[Expo's environment variables guide](https://docs.expo.dev/guides/environment-variables/).
Never put a secret in an `EXPO_PUBLIC_` variable - it ships inside the app
and is readable by anyone who has it installed.

**You do not need to set this to try the app** - with no `.env` file at
all, it already points at the live production backend
(`https://marketplacealert.onrender.com`, hard-coded as the *fallback
default only*, in the one place this decision is made:
[`src/api/config.ts`](src/api/config.ts) - never scattered across screens).

To point at a different backend (e.g. one running locally on your own
machine while developing the backend itself - see the main repo's
[`README.md`](../README.md) "Running the app"):

```bash
cp .env.example .env
# then edit .env, e.g.:
# EXPO_PUBLIC_API_BASE_URL=http://192.168.1.23:8000
```

Use your computer's LAN IP, not `localhost` - a physical phone running
Expo Go can't reach your computer's `localhost`. (An Android *emulator*
is the one exception - it can reach a local backend via `10.0.2.2`
instead of `localhost`.)

## Running the app

```bash
npx expo start
```

This starts the Metro bundler and prints a QR code in the terminal, plus a
small menu (press `a` for an Android emulator, `i` for an iOS simulator on
a Mac, `w` for a web preview).

### On your Android phone, with Expo Go

1. Install **Expo Go** from the Play Store.
2. Make sure your phone and computer are on the same Wi-Fi network.
3. Run `npx expo start` (above).
4. Open Expo Go and scan the QR code shown in the terminal (or in the
   browser tab Expo opens).

### On your iPhone, with Expo Go

1. Install **Expo Go** from the App Store.
2. Make sure your phone and computer are on the same Wi-Fi network.
3. Run `npx expo start` (above).
4. Open your iPhone's **Camera** app (not Expo Go itself) and point it at
   the QR code in the terminal/browser - it'll offer to open the link in
   Expo Go.

If your network blocks phone-to-computer connections (common on some
corporate/public Wi-Fi), run `npx expo start --tunnel` instead - slower,
but works across networks.

Either way, since the app already defaults to the live production
backend, it works immediately with no backend setup on your part.

## Architecture

```
App.tsx                       Root component: providers + RootNavigator
src/
  api/
    config.ts                  The ONLY place the backend base URL is decided
    types.ts                   TypeScript types mirroring the backend's /api/v1 schemas
    client.ts                  The ONLY place this app calls fetch() - timeout, JSON, ApiError
    endpoints.ts                One typed function per backend operation
  navigation/
    types.ts                    Tab/stack param list types
    RootNavigator.tsx            Bottom tabs (Home, Searches, Listings) + stack (Create, Detail)
  screens/                      One file per screen (see "Screens" below)
  components/                   Small reusable UI pieces (buttons, cards, empty/error/loading states)
  hooks/
    useAsyncData.ts              The one data-fetching hook every screen uses
    useAutoRefresh.ts             Interval polling + app-resume + screen-focus refresh, in one hook
  utils/
    format.ts                    Display formatting (dates, intervals, prices) - pure, tested
    validation.ts                 Create-search form validation - pure, tested
  theme/
    colors.ts                     Colors/spacing/radii/font sizes used throughout
  testUtils/
    renderWithNavigation.tsx       Shared test helper (renders a screen inside a real navigator)
```

- **Centralized, typed API layer.** Screens never call `fetch` directly -
  they call a function from `src/api/endpoints.ts`, which calls
  `apiRequest` in `src/api/client.ts`. Every failure mode (timeout, no
  network, non-2xx status, malformed JSON body) becomes one `ApiError`
  type with a `message` that's always safe to show directly in the UI, and
  an `isRetryable` flag. See "Network behavior" below.
- **Simple state - hooks only, no Redux.** `useAsyncData` (a small custom
  hook, `src/hooks/useAsyncData.ts`) is the one pattern every screen uses
  for "load this from the API, support pull-to-refresh and retry, never
  crash on a network error." `useAutoRefresh` (`src/hooks/useAutoRefresh.ts`)
  is the one pattern every screen uses for "keep this current without the
  user manually doing anything" - see "Automatic refresh" below. Nothing
  in this app needs data shared across unrelated screens, so nothing
  beyond plain hooks was introduced for either.
- **Navigation**: bottom tabs (Home, Searches, Listings) nested inside a
  stack navigator (adds Create Search and Saved Search Detail on top) -
  `@react-navigation/native` + `bottom-tabs` + `native-stack`.
- **No secrets in the app.** The only configuration is the backend's
  public base URL - there is nothing else to configure, and nothing here
  ever reads or embeds a credential (the backend itself never exposes one
  through `/api/v1` either - see the backend's `ARCHITECTURE.md` "Mobile
  API").

## Screens

1. **Home** - app title, live backend status (`GET /api/v1/status`),
   count of active saved searches, a button to create one, and a preview
   of recently discovered listings. Auto-refreshes (see "Automatic
   refresh" below).
2. **Saved Searches** - `GET /api/v1/saved-searches`, pull-to-refresh,
   query/marketplaces/active state/scan interval/last-scanned time per
   card. Auto-refreshes, so ACTIVE/INACTIVE state and `last_scanned_at`
   stay current on their own.
3. **Create Search** - query text input, marketplace multi-select
   (`GET /api/v1/marketplaces`), scan-interval presets, active toggle,
   `POST /api/v1/saved-searches`. Client-side validation mirrors the
   backend's own rules (non-blank query, at least one marketplace, a
   60-second minimum interval) so mistakes are caught immediately, with
   the backend's own error message shown if a submission still fails
   server-side.
4. **Saved Search Detail** - full detail, **Run Now**
   (`POST /api/v1/saved-searches/{id}/run`, with a loading state and the
   per-marketplace result counts shown afterward), **Pause/Resume**
   (`PATCH`), **Delete** (`DELETE`, behind an `Alert.alert` confirmation).
   Auto-refreshes the saved search's own detail; automatically pauses
   while Run Now/Pause-Resume/Delete is in flight, so a background
   refetch can never race one of those.
5. **Listings** - `GET /api/v1/listings`, a marketplace filter chip row
   (from `GET /api/v1/marketplaces`), "Load more" pagination, pull-to-
   refresh, opens the original listing in the browser on tap.
   Auto-refreshes to pick up newly discovered listings without disturbing
   scroll position.

## Automatic refresh

The backend's scheduler keeps scanning saved searches on its own schedule
whether or not this app is open - without this, the app could show a
stale "last scanned" time or miss newly discovered listings until the
user thought to manually pull-to-refresh or navigate away and back.
`useAutoRefresh` (`src/hooks/useAutoRefresh.ts`) is the one hook every
screen that needs this uses, combining three triggers:

- **Foreground polling, every 20 seconds** (`DEFAULT_AUTO_REFRESH_INTERVAL_MS`)
  while a screen is focused - in the middle of a deliberately modest
  15-30 second range, frequent enough that data feels current without
  meaningfully adding to backend load. A screen sitting unfocused in a
  background tab never polls - the timer starts on focus and stops on
  blur, so backend load stays proportional to what's actually on screen,
  not every screen that's ever been visited.
- **App resume**: via React Native's `AppState`, refreshes immediately
  whenever the app comes back to `active` from the background - the
  scenario that motivated this feature (the backend scheduler runs while
  the phone app is backgrounded, so data can go stale purely from time
  passing while a screen's own timer was suspended by the OS).
- **Screen focus**: refreshes immediately when a screen *regains* focus
  (e.g. backing out of a detail screen), via React Navigation's
  `useFocusEffect` - but deliberately not on the very first focus right
  after mount, since the screen's own initial load already just fetched
  that data.

All of this is layered on top of - never a replacement for - manual
pull-to-refresh, which still works exactly as before. The two are
deliberately kept behaviorally different: **automatic refreshes are
silent** (`useAsyncData`'s `refreshQuietly`, or the equivalent hand-rolled
version in `ListingsScreen`) - on success they update the data in place;
on failure (a Render cold start, a dropped connection) they leave
whatever's currently displayed completely untouched and let the next
poll simply try again, never flashing an error screen over good data.
**Manual pull-to-refresh can still show a useful error** (with a **Try
again** button), since that's a deliberate user action expecting visible
feedback either way.

Every automatic-refresh call is also protected against overlapping itself
or a concurrent manual refresh - only one request is ever in flight per
screen at a time; an overlapping call is simply dropped rather than
queued or fired alongside it, which is what keeps this from turning into
a request storm.

`ListingsScreen` deserves a specific note: an automatic refresh re-fetches
exactly as many items as are already loaded (not just the first page) and
replaces the dataset in place, so newly discovered listings are picked up
without losing "Load more" position or jumping the list's scroll
position for items that were already on screen.

## Network behavior

- **Render cold starts**: the backend's free-tier hosting can take several
  real seconds to wake up from idle. The API client's default timeout is a
  generous 20 seconds specifically so this isn't mistaken for the backend
  being down (`src/api/config.ts`).
- **Timeouts / no connectivity / a 5xx from the backend**: surfaced as an
  `ApiError` with `isRetryable: true`; every screen's error state includes
  a **Try again** button that re-runs the same request.
- **A malformed/non-JSON response body**: caught explicitly and turned
  into a clear `ApiError` rather than letting a JSON-parse exception
  propagate and crash the screen.
- Nothing in this app crashes the UI on a network failure - every screen
  that loads data has a loading, error (with retry), and empty state.

## Current limitations

- **No login/authentication yet.** Every `/api/v1` endpoint is exactly as
  open as the backend itself is right now - there is no per-user data.
- **No payments, no push notifications.** Alerts still only go out via the
  backend's existing Telegram integration; this app doesn't send or
  receive its own notifications yet.
- **Several listing fields are always empty.** `price`, `currency`,
  `location`, `condition`, and `image_url` on a listing are always `null`
  today - the backend doesn't persist them yet (see the backend's
  `ARCHITECTURE.md` "Mobile API" for exactly why). This app renders them
  conditionally and never fakes a value - if the backend starts returning
  them, they'll simply start appearing.
- **Listings aren't filterable by saved search.** The backend has no
  stored relationship between a discovered listing and the saved search
  that found it (by design - see the backend's own docs) - `saved_search_id`
  is not offered as a filter here for the same reason.
- **No App Store / Play Store release yet** - this is a first version run
  through Expo Go / a dev build, not a store submission.
- **No offline support** - every screen requires a live connection to the
  backend to load anything (though it degrades to a clear error + retry,
  never a crash, when there isn't one).

## Testing

```bash
npm test          # Jest - API client, format/validation utils, core screen behavior
npm run typecheck # tsc --noEmit
```

Both are safe to run repeatedly and never touch the network - every test
mocks `fetch`/the API layer directly, so nothing here ever calls the real
backend (and, transitively, never the real Etsy/eBay/Reverb/Bonanza/
Telegram behind it).

What's covered:
- `src/api/client.test.ts` - URL/query-string construction, JSON request
  bodies, success and every failure mode (timeout, network error, non-2xx
  with a string or Pydantic-array-shaped `detail`, malformed JSON, a 204
  with no body), and the retryable/non-retryable classification.
- `src/utils/format.test.ts` / `src/utils/validation.test.ts` - display
  formatting (intervals, timestamps, marketplace display names, prices)
  and the create-search form validation rules.
- `src/screens/SavedSearchesScreen.test.tsx` /
  `src/screens/CreateSearchScreen.test.tsx` - loading → data, empty state,
  error state with a working retry button, client-side validation
  blocking submission, and a successful submission calling the API with
  the expected payload. `SavedSearchesScreen.test.tsx` also covers two
  automatic-refresh integration scenarios end to end: a server-side
  change (e.g. `last_scanned_at`) appearing after an interval tick with no
  manual action, and a refetch on genuine refocus (navigating away and
  back) that doesn't double-fetch on the initial mount.
  `CreateSearchScreen.test.tsx` also proves the marketplace selector is
  purely driven by whatever `GET /api/v1/marketplaces` returns: two tests
  each add a new marketplace entry ("reverb", then "bonanza") only to the
  *mocked API response* (never to `CreateSearchScreen.tsx` itself) and
  confirm it renders as a selectable chip, respects its `configured`
  flag, and reaches a submitted saved search - concrete proof this screen
  needs no code change to support a new backend marketplace, proven
  twice, not just once.
- `src/hooks/useAutoRefresh.test.tsx` - interval polling (default and
  custom interval), an `AppState` transition to `active` triggering an
  immediate refresh (and other transitions not doing so), refetch on
  refocus without refetching on the very first focus, the timer/listener
  being torn down on blur and unmount (with polling correctly resuming on
  refocus), no duplicate timer when the caller re-renders with a new
  callback identity, and the `enabled` option suppressing refreshes
  without tearing down the underlying timer.
- `src/hooks/useAsyncData.test.tsx` - `refreshQuietly` updating data
  silently on success; a failed `refreshQuietly` preserving whatever's
  currently displayed (data and error both untouched); a failed manual
  `refresh()` still surfacing a useful, retryable error; an overlapping
  `refresh`/`refreshQuietly`/`retry` call being dropped while one request
  is already in flight, with a new request allowed once it resolves; and
  manual pull-to-refresh's `refreshing` state still working exactly as
  before.

## Production backend

`https://marketplacealert.onrender.com` - the same live backend the web
dashboard uses. This app calls only its versioned `/api/v1` surface, never
the HTML dashboard routes.
