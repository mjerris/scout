import { Platform } from 'react-native';

import { logger } from '@signalwire/react';

// Metro supplies `require` at runtime; this package's tsconfig has no Node types.
declare const require: (moduleName: string) => unknown;

/**
 * The slice of `@react-native-firebase/messaging` this package uses.
 *
 * Declared structurally so the package still type-checks when Firebase is not
 * installed — it is an optional peer. Version 26 dropped the default export
 * for a modular API, so these are read off the namespace.
 */
interface MessagingModule {
  getMessaging(): unknown;
  getToken(messaging: unknown): Promise<string>;
  onTokenRefresh(messaging: unknown, listener: (token: string) => void): () => void;
  requestPermission?(messaging: unknown): Promise<number>;
}

/**
 * Loads Firebase messaging, or returns undefined when it is unusable.
 *
 * Required lazily, not imported. Guarding at use is enough for a peer that is
 * merely *absent*, but React Native Firebase throws `Native module
 * RNFBAppModule not found` while the module is being evaluated on a build
 * without Firebase — an iOS prebuild, since the plugin became conditional. A
 * static import therefore crashed the app's entry file before any guard ran.
 * See `androidCallPush.ts` for the full account; Metro still resolves the
 * literal specifier at bundle time, so only the evaluation is deferred.
 */
function messagingModule(): MessagingModule | undefined {
  let loaded: unknown;
  try {
    loaded = require('@react-native-firebase/messaging');
  } catch (error) {
    logger.debug(`Firebase messaging failed to load; Android push is off: ${String(error)}`);
    return undefined;
  }

  const candidate = loaded as { default?: unknown } | undefined;
  const mod = (candidate?.default ?? candidate) as Partial<MessagingModule> | undefined;
  if (typeof mod?.getMessaging !== 'function' || typeof mod.getToken !== 'function') {
    logger.debug(
      'Firebase messaging not installed. Android push needs ' +
        '@react-native-firebase/app and @react-native-firebase/messaging.'
    );
    return undefined;
  }
  return mod as MessagingModule;
}

/**
 * Reads this device's FCM registration token.
 *
 * Returns `null` on iOS, where VoIP push is PushKit's job, and whenever
 * Firebase is absent — callers register a device the same way on both
 * platforms and simply get nothing to send.
 */
export async function getFcmToken(): Promise<string | null> {
  if (Platform.OS !== 'android') {
    return null;
  }

  const mod = messagingModule();
  if (!mod) {
    return null;
  }

  try {
    const instance = mod.getMessaging();
    // Android 13+ gates notifications behind a runtime permission. A call
    // arrives as a data message and wakes the app regardless, but without the
    // grant nothing can be shown, so ask before the first token is used.
    await mod.requestPermission?.(instance);
    return (await mod.getToken(instance)) || null;
  } catch (error) {
    logger.warn('Failed to read the FCM token:', error);
    return null;
  }
}

/**
 * Subscribes to FCM token changes, calling back with the current one first.
 *
 * FCM rotates tokens — on reinstall, on restore to a new device, and at its
 * own discretion. A stale token fails silently at Google, so the push simply
 * never arrives and nothing points at the cause; re-registering on refresh is
 * what keeps that from happening.
 */
export function watchFcmToken(onToken: (token: string) => void): () => void {
  if (Platform.OS !== 'android') {
    return () => undefined;
  }

  const mod = messagingModule();
  if (!mod) {
    return () => undefined;
  }

  let cancelled = false;
  void getFcmToken().then((token) => {
    if (token && !cancelled) {
      onToken(token);
    }
  });

  const unsubscribe = mod.onTokenRefresh(mod.getMessaging(), (token) => {
    if (!cancelled) {
      onToken(token);
    }
  });

  return () => {
    cancelled = true;
    unsubscribe();
  };
}
