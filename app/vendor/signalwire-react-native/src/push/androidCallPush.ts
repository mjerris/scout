import { Platform } from 'react-native';

import { logger } from '@signalwire/react';
import { getCallKit } from '../callkit/CallKeepBridge';

// Metro supplies `require` at runtime; this package's tsconfig has no Node types.
declare const require: (moduleName: string) => unknown;

/** The slice of Firebase messaging used here; see `fcmToken` for why. */
interface MessagingModule {
  getMessaging(): unknown;
  onMessage(messaging: unknown, handler: (message: RemoteMessage) => void): () => void;
  setBackgroundMessageHandler(
    messaging: unknown,
    handler: (message: RemoteMessage) => Promise<void>
  ): void;
}

interface RemoteMessage {
  data?: Record<string, string>;
}

/**
 * Loads Firebase messaging, or returns undefined when it is unusable.
 *
 * Required lazily rather than imported at module scope. React Native Firebase
 * evaluates its native binding when the module is *evaluated* and throws
 * `Native module RNFBAppModule not found` on a build without Firebase — which
 * an iOS prebuild now is, since the plugin became conditional. A static import
 * ran that at entry-file load, so the throw escaped before the `Platform` guard
 * in `registerAndroidCallPush` could return, and `registerRootComponent` on the
 * following line never ran: the app died with `"main" has not been registered`.
 *
 * `peers.ts` explains why the other optional peers are imported statically —
 * Metro resolves `require` at bundle time, so a try/catch cannot rescue a
 * package that is genuinely absent. That still holds: the specifier here is a
 * literal, so Metro bundles it exactly as before. Only the *evaluation* moves,
 * which is what a module that throws on load requires.
 */
function messagingModule(): MessagingModule | undefined {
  let loaded: unknown;
  try {
    loaded = require('@react-native-firebase/messaging');
  } catch (error) {
    logger.debug(`Firebase messaging failed to load; Android call push is off: ${String(error)}`);
    return undefined;
  }

  // A CJS build exposes the members directly; an ESM interop layer nests them
  // under `default`. Accept both rather than depending on which one Metro hands
  // back for a given Firebase version.
  const candidate = loaded as { default?: unknown } | undefined;
  const mod = (candidate?.default ?? candidate) as Partial<MessagingModule> | undefined;
  if (typeof mod?.getMessaging !== 'function' || typeof mod.onMessage !== 'function') {
    return undefined;
  }
  return mod as MessagingModule;
}

/**
 * Undoes the renaming the sender had to do to satisfy FCM.
 *
 * FCM rejects a data payload containing `from` — and `to`, `notification`,
 * `message_type`, `collapse_key`, or anything prefixed `google`/`gcm` — with
 * `400 Invalid data payload key`, failing the whole message. The server
 * prefixes those with `sw_`; this puts them back, so both platforms hand the
 * registry the same shape and nothing downstream has to care which push
 * service delivered the call.
 */
export function decodePushData(data: Record<string, string>): Record<string, string> {
  const decoded: Record<string, string> = {};
  for (const [key, value] of Object.entries(data)) {
    decoded[key.startsWith('sw_') ? key.slice(3) : key] = value;
  }
  return decoded;
}

/** Turns an FCM data message into a native incoming call. */
function reportCall(message: RemoteMessage): void {
  const data = decodePushData(message.data ?? {});
  const callId = data.call_id;
  if (!callId) {
    logger.debug('FCM message carries no call_id; not a call, ignoring');
    return;
  }

  logger.debug(`FCM call push received for ${callId}`);
  const bridge = getCallKit();
  bridge.reportIncomingPush({
    callId,
    from: data.from,
    fromName: data.from_name,
    // Everything else rides along — a bridge topology puts its single-use
    // token here, and there is no second chance to ask for it.
    data
  });

  // The push may have woken a killed app into a headless task, where nothing
  // is on screen. Without this the call sits in Telecom, invisible.
  bridge.bringToForeground();
}

/**
 * Wires FCM call pushes into the native call UI.
 *
 * Call this from the app's entry file, outside React. Two handlers, because
 * Android delivers to different places depending on whether the app is alive:
 *
 * - `setBackgroundMessageHandler` runs in a headless JavaScript task when the
 *   app is backgrounded or killed. This is the cold-start path, and the one
 *   that matters: an incoming call normally arrives at a dead app.
 * - `onMessage` runs when the app is in the foreground, where the background
 *   handler is never called.
 *
 * Registering only one of them produces a bug that looks like flakiness —
 * calls arrive when the app happens to be open and vanish when it is not.
 *
 * Safe to call on iOS and without Firebase installed: both are no-ops.
 */
export function registerAndroidCallPush(): void {
  if (Platform.OS !== 'android') {
    return;
  }

  const mod = messagingModule();
  if (!mod) {
    logger.debug(
      'Firebase messaging not installed; Android call push is off. ' +
        'Install @react-native-firebase/app and @react-native-firebase/messaging.'
    );
    return;
  }

  const instance = mod.getMessaging();

  mod.setBackgroundMessageHandler(instance, async (message) => {
    reportCall(message);
  });

  mod.onMessage(instance, (message) => {
    reportCall(message);
  });

  logger.debug('Android call push registered');
}
