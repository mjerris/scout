import NetInfo from '@react-native-community/netinfo';

import { logger } from '@signalwire/react';
import { installBaseGlobals } from './baseGlobals';
import { assertPeerModule } from './peers';

type Listener = () => void;

interface ShimTarget {
  addEventListener?: (type: string, listener: Listener) => void;
  removeEventListener?: (type: string, listener: Listener) => void;
}

let installed: (() => void) | null = null;

/**
 * Feeds NetInfo connectivity into the globals the SDK's `NetworkMonitor` probes:
 * `window.addEventListener('online' | 'offline')` and `navigator.onLine`.
 *
 * The SDK builds `NetworkMonitor` internally with no injection point
 * (`SignalWire.ts:993`), but guards every access with `typeof`, so feeding
 * those globals is the only way to supply real data without forking the SDK.
 *
 * Additive and idempotent — never replaces an existing `window` or `navigator`.
 */
export function installNetworkShim(): () => void {
  if (installed) {
    return installed;
  }

  assertPeerModule(
    NetInfo as object | undefined,
    '@react-native-community/netinfo',
    'network monitoring',
    'addEventListener'
  );

  installBaseGlobals();

  const root = globalThis as unknown as {
    window: ShimTarget;
    navigator?: { onLine?: boolean };
  };

  root.navigator ??= {};

  const listeners = new Map<string, Set<Listener>>();
  const previousAdd = root.window.addEventListener;
  const previousRemove = root.window.removeEventListener;

  root.window.addEventListener = (type: string, listener: Listener): void => {
    const set = listeners.get(type) ?? new Set<Listener>();
    set.add(listener);
    listeners.set(type, set);
    previousAdd?.call(root.window, type, listener);
  };

  root.window.removeEventListener = (type: string, listener: Listener): void => {
    listeners.get(type)?.delete(listener);
    previousRemove?.call(root.window, type, listener);
  };

  const dispatch = (type: string): void => {
    for (const listener of listeners.get(type) ?? []) {
      try {
        listener();
      } catch (error) {
        logger.warn(`Network shim listener for "${type}" threw:`, error);
      }
    }
  };

  let online = root.navigator.onLine ?? true;
  root.navigator.onLine = online;

  const stopNetInfo = NetInfo.addEventListener((state) => {
    const next = state.isConnected === true;
    if (next === online) {
      return;
    }
    online = next;
    root.navigator!.onLine = next;
    logger.debug(`Network shim: ${next ? 'online' : 'offline'}`);
    dispatch(next ? 'online' : 'offline');
  });

  installed = (): void => {
    stopNetInfo();
    listeners.clear();
    root.window.addEventListener = previousAdd;
    root.window.removeEventListener = previousRemove;
    installed = null;
  };

  return installed;
}
