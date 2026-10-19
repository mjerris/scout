// Push from Scout (app/README.md, "Push"): this phone registers two tokens with Scout,
// signed with its device secret:
// - the PushKit token, which Scout rings for its calls (VoIP pushes; src/ring.ts);
// - the APNs token for notifications: pairing requests, with Approve and Deny.
// And the APNs environment the build is signed for, read from its provisioning profile
// (a sandbox token only works against Apple's sandbox host). Registered every launch and
// again whenever a token changes; Scout keeps the last of each.
import { watchVoipToken } from '@signalwire/react-native/callkit';
import * as Application from 'expo-application';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';

import type { ScoutServer } from './server';

export const PAIR_CATEGORY = 'SCOUT_PAIR';
export type PushEnv = 'sandbox' | 'production';

/** The notification actions Scout's pairing requests carry (phone_push.PAIR_CATEGORY). */
export async function setUpPairActions(): Promise<void> {
  await Notifications.setNotificationCategoryAsync(PAIR_CATEGORY, [
    // Both open the app (unlocking the phone first): approving a device is not a lock-screen action.
    { identifier: 'approve', buttonTitle: 'Approve', options: { opensAppToForeground: true } },
    { identifier: 'deny', buttonTitle: 'Deny', options: { opensAppToForeground: true, isDestructive: true } },
  ]);
}

/** Show Scout's notifications while the app is open too. */
export function showWhileOpen(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: true,
      shouldSetBadge: false,
    }),
  });
}

/** The APNs environment of this build's signing: no profile (the App Store) is production. */
export async function apnsEnv(): Promise<PushEnv> {
  try {
    return (await Application.getIosPushNotificationServiceEnvironmentAsync()) === 'development' ? 'sandbox' : 'production';
  } catch {
    return 'production';
  }
}

/** The APNs token for notifications, asking for permission once; '' if not granted. */
export async function alertToken(): Promise<string> {
  try {
    const perm = await Notifications.requestPermissionsAsync({
      ios: { allowAlert: true, allowSound: true, allowBadge: false },
    });
    if (!perm.granted) return '';
    const t = await Notifications.getDevicePushTokenAsync();
    return typeof t.data === 'string' ? t.data : '';
  } catch {
    return ''; // a build without push (SCOUT_IOS_PUSH=0), or the Simulator
  }
}

export interface PushDeps {
  watchVoip: (fn: (token: string) => void) => () => void;
  alertToken: () => Promise<string>;
  env: () => Promise<PushEnv>;
}

const DEPS: PushDeps = { watchVoip: watchVoipToken, alertToken, env: apnsEnv };

/**
 * Keep Scout told of this phone's push tokens while `server` (the pairing) holds. Returns
 * the stop. The PushKit token needs no permission; notifications are asked for only once
 * Scout says it can send them (it has an APNs key), so a Scout without push never prompts.
 * A registration that fails is retried with the next change or launch; the outcome goes
 * to `onResult` (the screen shows it only when it failed).
 */
export function registerPush(
  server: Pick<ScoutServer, 'registerPush'>,
  onResult: (error: string) => void = () => {},
  deps: PushDeps = DEPS,
): () => void {
  if (Platform.OS !== 'ios') return () => {};
  let stopped = false;
  let voip = '';
  let alert = '';
  let asked = false;
  let sent = '';
  const send = async () => {
    const env = await deps.env();
    const key = `${voip}|${alert}|${env}`;
    if (stopped || key === sent || !voip) return;
    try {
      const { push } = await server.registerPush(voip, alert, env);
      sent = key;
      onResult('');
      if (push && !asked) {
        asked = true;
        alert = await deps.alertToken();
        if (alert) await send();
      }
    } catch (e) {
      onResult(`Scout couldn't be told where to reach this phone: ${String((e as Error)?.message ?? e)}`);
    }
  };
  const stopVoip = deps.watchVoip((t) => {
    voip = t;
    void send();
  });
  return () => {
    stopped = true;
    stopVoip();
  };
}
