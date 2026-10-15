import AsyncStorage from '@react-native-async-storage/async-storage';

import { logger } from '@signalwire/react';
import { assertPolyfillsInstalled } from './assertPolyfills';
import { installNetworkShim } from './networkShim';
import { ReactNativeStorage } from './storage';
import { installVisibilityShim } from './visibilityShim';
import { createWebRTCApiProvider } from './webrtc';

import type { SignalWireOptions, Storage, WebRTCApiProvider } from '@signalwire/js';

export interface ReactNativePlatformOptions {
  /** Drive the SDK's network monitor from NetInfo. Default `true`. */
  netInfo?: boolean;
  /** Drive the SDK's visibility controller from AppState. Default `true`. */
  appState?: boolean;
  /** Override the storage implementation. */
  storage?: Storage;
  /** Override the WebRTC provider. */
  webRTCApiProvider?: WebRTCApiProvider;
}

export interface ReactNativePlatform {
  /** Spread into the `SignalWire` constructor's options argument. */
  readonly options: SignalWireOptions;
  /** Releases the shims' subscriptions. Safe to call more than once. */
  dispose(): void;
}

function installOptional(name: string, install: () => () => void): (() => void) | null {
  try {
    return install();
  } catch (error) {
    logger.warn(`${name} unavailable, continuing with reduced resilience:`, error);
    return null;
  }
}

/**
 * Builds the React Native platform layer for `@signalwire/js`.
 *
 * ```ts
 * const platform = createReactNativePlatform();
 * const client = new SignalWire(credentials, platform.options);
 * // later: platform.dispose();
 * ```
 */
export function createReactNativePlatform(
  options: ReactNativePlatformOptions = {}
): ReactNativePlatform {
  assertPolyfillsInstalled();

  const { netInfo = true, appState = true } = options;
  const teardowns: Array<() => void> = [];

  if (netInfo) {
    const stop = installOptional('Network monitoring', installNetworkShim);
    if (stop) {
      teardowns.push(stop);
    }
  }

  if (appState) {
    const stop = installOptional('Visibility tracking', installVisibilityShim);
    if (stop) {
      teardowns.push(stop);
    }
  }

  let disposed = false;

  return {
    options: {
      webRTCApiProvider: options.webRTCApiProvider ?? createWebRTCApiProvider(),
      storageImplementation: options.storage ?? new ReactNativeStorage(AsyncStorage),
      skipDeviceMonitoring: true
    },
    dispose: (): void => {
      if (disposed) {
        return;
      }
      disposed = true;
      for (const teardown of teardowns) {
        try {
          teardown();
        } catch (error) {
          logger.warn('Platform teardown threw:', error);
        }
      }
    }
  };
}
