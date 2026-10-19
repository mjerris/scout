// "Scout" in the Phone app's Recents (or Siri) asks CallKit to start a call: the CallKit
// bridge passes it on as startCallRequested$ (not the app's own calls, and a request that
// launched the app is held for the first subscriber). On a cold start that arrives before
// the pairing is read from the Keychain, so this subscribes at import and keeps the
// request until the call panel takes it.
import { getCallKit } from '@signalwire/react-native/callkit';
import { useSyncExternalStore } from 'react';

import { testFlags } from './testFlags';

let pending = 0;
const listeners = new Set<() => void>();

function request(): void {
  pending++;
  listeners.forEach((l) => l());
}

getCallKit().startCallRequested$.subscribe(() => request());

/** How many call requests are waiting (Recents, Siri, the -ScoutTestCall launch argument). */
export function usePendingCallRequests(): number {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pending,
  );
}

/** Take the waiting requests: true if there were any. */
export function takeCallRequests(): boolean {
  const had = pending > 0;
  pending = 0;
  return had;
}

if (testFlags.call) request();
