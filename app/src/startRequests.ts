// "Scout" in the Phone app's Recents (or Siri) asks CallKit to start a call, and callkeep
// passes it on as didReceiveStartCallAction. On a cold start that arrives before the
// pairing is read from the Keychain, so the listener is registered at import and keeps
// the request until the call panel takes it. CallKit also reports the calls this app
// starts itself that way, with their uuid: those are already ours.
import { getCallKit } from '@signalwire/react-native/callkit';
import { useSyncExternalStore } from 'react';
import RNCallKeep from 'react-native-callkeep';

import { testFlags } from './testFlags';

let pending = 0;
const listeners = new Set<() => void>();

function request(): void {
  pending++;
  listeners.forEach((l) => l());
}

RNCallKeep.addEventListener('didReceiveStartCallAction', (e: { callUUID?: string }) => {
  if (e?.callUUID && getCallKit().registry.entryForUuid(e.callUUID)) return;
  request();
});

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
