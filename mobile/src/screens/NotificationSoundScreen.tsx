import { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { createAudioPlayer, type AudioPlayer } from 'expo-audio';

import type { NotificationChannelId } from '../api/types';
import { LoadingView } from '../components/LoadingView';
import { PrimaryButton } from '../components/PrimaryButton';
import { Screen } from '../components/Screen';
import { StatusPill } from '../components/StatusPill';
import { colors, fontSize, radius, spacing } from '../theme/colors';
import { getLastKnownDeviceRegistration, updateNotificationChannelPreference } from '../utils/pushNotifications';

/**
 * How long to keep polling `getLastKnownDeviceRegistration()` for a
 * result before giving up and falling back to "System Default" as the
 * displayed selection - the automatic startup registration
 * (`usePushNotificationSetup`) is asynchronous and may not have resolved
 * yet by the time a user navigates all the way to this screen, but in
 * practice it almost always has (see that hook's own docstring). Not a
 * network call of its own - purely local, in-memory polling of a cache
 * this screen never owns (see `pushNotifications.ts`'s own docstring for
 * why: the backend, not this screen, is this preference's source of
 * truth).
 */
const CURRENT_PREFERENCE_POLL_INTERVAL_MS = 250;
const CURRENT_PREFERENCE_MAX_POLL_ATTEMPTS = 20; // ~5 seconds total

const DEFAULT_CHANNEL_ID: NotificationChannelId = 'listing-alerts-default-v1';

interface SoundChoice {
  id: NotificationChannelId;
  label: string;
  /** `require()`'d asset module id, or `null` for System Default, which has no bundled file to preview - see this screen's own docstring for why it gets no Preview button. */
  soundAsset: number | null;
}

const CHOICES: readonly SoundChoice[] = [
  { id: 'listing-alerts-default-v1', label: 'System Default', soundAsset: null },
  { id: 'listing-alerts-ping-v1', label: 'Clean Ping', soundAsset: require('../../assets/sounds/clean_ping.wav') },
  { id: 'listing-alerts-double-v1', label: 'Double Ping', soundAsset: require('../../assets/sounds/double_ping.wav') },
  { id: 'listing-alerts-radar-v1', label: 'Radar', soundAsset: require('../../assets/sounds/radar.wav') },
  {
    id: 'listing-alerts-premium-v1',
    label: 'Premium Chime',
    soundAsset: require('../../assets/sounds/premium_chime.wav'),
  },
];

/**
 * Lets the user pick which Android notification channel/sound this
 * *device* uses for new listing alerts - one of five fixed choices,
 * saved immediately on selection (see `updateNotificationChannelPreference`).
 *
 * System Default has no Preview button, by design (per the task this
 * screen was built from: "System Default does NOT need a preview button
 * unless there is a clean, supported way to preview the actual Android
 * system notification sound" - there isn't one here without either
 * bundling a duplicate of whatever the OS's current default happens to
 * be, which could drift out of sync with the real thing, or reaching
 * into native platform APIs this app has no other reason to depend on -
 * so this deliberately doesn't fake it).
 *
 * Never invents client-only persistence: the currently-selected choice
 * comes from `getLastKnownDeviceRegistration()` (the backend's own
 * response to this device's last registration call, cached in
 * `pushNotifications.ts` - see that module's docstring), never
 * AsyncStorage duplicating the server's source of truth.
 */
export function NotificationSoundScreen() {
  const [loadingCurrent, setLoadingCurrent] = useState(true);
  const [selectedChannelId, setSelectedChannelId] = useState<NotificationChannelId>(DEFAULT_CHANNEL_ID);
  const [noDeviceYet, setNoDeviceYet] = useState(false);
  const [savingChannelId, setSavingChannelId] = useState<NotificationChannelId | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [previewingChannelId, setPreviewingChannelId] = useState<NotificationChannelId | null>(null);
  const activePlayerRef = useRef<AudioPlayer | null>(null);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;

    function poll() {
      const current = getLastKnownDeviceRegistration();
      if (current) {
        if (!cancelled) {
          // NULL ("never explicitly chosen") and the explicit
          // "listing-alerts-default-v1" choice both display as System
          // Default - see this screen's own docstring and
          // DeviceRegisterResponse's backend docstring for why they
          // stay distinct values underneath even though they render
          // identically here.
          setSelectedChannelId(current.notificationChannelId ?? DEFAULT_CHANNEL_ID);
          setLoadingCurrent(false);
        }
        return;
      }
      attempts += 1;
      if (attempts >= CURRENT_PREFERENCE_MAX_POLL_ATTEMPTS) {
        if (!cancelled) {
          setNoDeviceYet(true);
          setLoadingCurrent(false);
        }
        return;
      }
      setTimeout(poll, CURRENT_PREFERENCE_POLL_INTERVAL_MS);
    }
    poll();

    return () => {
      cancelled = true;
    };
  }, []);

  // Leaving the screen must clean up playback - stop/release whatever
  // preview is still active, exactly like a new preview tap would.
  useEffect(() => {
    return () => {
      activePlayerRef.current?.remove();
      activePlayerRef.current = null;
    };
  }, []);

  async function handleSelect(choice: SoundChoice) {
    if (savingChannelId !== null) {
      return;
    }
    setSavingChannelId(choice.id);
    setSaveError(null);
    try {
      await updateNotificationChannelPreference(choice.id);
      // Only reflect the new selection as saved after the API call has
      // actually succeeded - never optimistically, so a failed save can
      // never be shown as if it were persisted.
      setSelectedChannelId(choice.id);
    } catch (error) {
      // `error instanceof Error` (not the narrower `ApiError`) - a save
      // attempted with no device registered yet throws a plain, specific
      // `Error` (see `updateNotificationChannelPreference`'s own
      // docstring), and that specific message must reach the user rather
      // than being discarded in favor of a generic fallback just because
      // it isn't an `ApiError`.
      setSaveError(error instanceof Error ? error.message : 'Could not save your notification sound. Try again.');
    } finally {
      setSavingChannelId(null);
    }
  }

  function handlePreview(choice: SoundChoice) {
    if (choice.soundAsset === null) {
      return;
    }
    // Tapping any preview button - including the one already playing -
    // stops/releases whatever was previously active first, so at most
    // one preview is ever audible at a time.
    activePlayerRef.current?.remove();
    activePlayerRef.current = null;

    const player = createAudioPlayer(choice.soundAsset);
    activePlayerRef.current = player;
    setPreviewingChannelId(choice.id);
    player.addListener('playbackStatusUpdate', (status) => {
      if (!status.didJustFinish) {
        return;
      }
      player.remove();
      if (activePlayerRef.current === player) {
        activePlayerRef.current = null;
        setPreviewingChannelId(null);
      }
    });
    player.play();
  }

  if (loadingCurrent) {
    return (
      <Screen>
        <LoadingView label="Loading your notification sound…" />
      </Screen>
    );
  }

  return (
    <Screen padded={false}>
      <ScrollView contentContainerStyle={styles.content}>
        {noDeviceYet ? (
          <Text style={styles.hint}>
            This device isn&apos;t registered for push notifications yet - open the app with notifications enabled,
            then come back here.
          </Text>
        ) : null}
        {saveError ? <Text style={styles.errorText}>{saveError}</Text> : null}

        {CHOICES.map((choice) => {
          // While `noDeviceYet` is true, there is no confirmed preference
          // to show at all - never mark any row as selected, so the
          // "not registered yet" state stays honest rather than implying
          // a selection (System Default) that hasn't actually been
          // confirmed by the backend.
          const isSelected = !noDeviceYet && selectedChannelId === choice.id;
          const isSaving = savingChannelId === choice.id;
          const isPreviewing = previewingChannelId === choice.id;
          return (
            <View key={choice.id} style={styles.row}>
              <Pressable
                style={styles.rowMain}
                onPress={() => handleSelect(choice)}
                disabled={savingChannelId !== null}
                accessibilityRole="button"
                accessibilityLabel={choice.label}
                accessibilityState={{ selected: isSelected, disabled: savingChannelId !== null }}
              >
                <Text style={[styles.rowLabel, isSelected && styles.rowLabelSelected]}>{choice.label}</Text>
                {isSaving ? (
                  <ActivityIndicator size="small" color={colors.primary} />
                ) : isSelected ? (
                  <StatusPill label="Selected" tone="success" />
                ) : null}
              </Pressable>
              {choice.soundAsset !== null ? (
                <PrimaryButton
                  label={isPreviewing ? 'Playing…' : 'Preview'}
                  onPress={() => handlePreview(choice)}
                  variant="secondary"
                />
              ) : null}
            </View>
          );
        })}
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: {
    padding: spacing.lg,
    gap: spacing.md,
  },
  hint: {
    fontSize: fontSize.sm,
    color: colors.textSecondary,
  },
  errorText: {
    fontSize: fontSize.sm,
    color: colors.danger,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.md,
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
  },
  rowMain: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
    minHeight: 44,
  },
  rowLabel: {
    fontSize: fontSize.md,
    color: colors.textPrimary,
  },
  rowLabelSelected: {
    color: colors.primary,
    fontWeight: '700',
  },
});
