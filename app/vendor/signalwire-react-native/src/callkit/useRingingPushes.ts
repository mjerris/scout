import { useCallback, useRef, useSyncExternalStore } from 'react';

import type { CallKeepBridge } from './CallKeepBridge';
import type { CallEntry } from './types';

/**
 * Where the bridge singleton lives; see `getCallKit`.
 *
 * Read off `globalThis` rather than imported, deliberately. Importing
 * `CallKeepBridge` pulls in `react-native-callkeep`, which constructs a
 * NativeEventEmitter at import time — so merely importing this hook would
 * make an optional peer mandatory, and would crash any app that uses the UI
 * kit without native call UI. If no bridge exists, nothing is ringing
 * natively, which is exactly the answer to give.
 */
const CALLKIT_GLOBAL = '__signalwireRNCallKitBridge';

function existingBridge(): CallKeepBridge | undefined {
  return (globalThis as Record<string, unknown>)[CALLKIT_GLOBAL] as CallKeepBridge | undefined;
}

/** A call that is ringing natively but has no SDK call yet. */
export interface RingingPush {
  uuid: string;
  from?: string;
  fromName?: string;
  data?: Record<string, string>;
}

export interface RingingPushes {
  ringing: RingingPush[];
  answer: (uuid: string) => void;
  reject: (uuid: string) => void;
}

const EMPTY: RingingPush[] = [];

export function ringingFrom(entries: CallEntry[]): RingingPush[] {
  const ringing = entries.filter((entry) => entry.state === 'pending-push' && !entry.intent);
  if (ringing.length === 0) {
    // A stable reference, or useSyncExternalStore re-renders forever: a fresh
    // [] every read is a new snapshot every time it is asked for.
    return EMPTY;
  }
  return ringing.map((entry) => ({
    uuid: entry.uuid,
    // `handle` and `displayName` are the registry's names for these; the
    // push calls them from / from_name, and so does every UI.
    from: entry.handle,
    fromName: entry.displayName,
    data: entry.data
  }));
}

/**
 * Calls ringing on the native side that the app must draw itself.
 *
 * iOS does not need this: CallKit draws the incoming call screen, and the user
 * answers there before the app is even in front. Android does, because
 * callkeep is registered self-managed — Telecom tracks the call but draws no
 * UI for it, by design, so a push arrives, a connection exists, and the user
 * sees nothing at all unless the app renders something.
 *
 * `answer` applies the same intent a CallKit answer would, so both platforms
 * converge on one path: the registry buffers it, and whatever places the call
 * (a bridge dial, or the SDK's own inbound call) picks it up from there.
 */
export function useRingingPushes(): RingingPushes {
  const registry = existingBridge()?.registry;

  const subscribe = useCallback(
    (onChange: () => void) => {
      if (!registry) {
        return () => undefined;
      }
      const subscription = registry.entries$.subscribe(onChange);
      return () => subscription.unsubscribe();
    },
    [registry]
  );

  // Derived from `entries`, not from a stored snapshot: the registry is the
  // single source of truth, and a second copy here would drift.
  //
  // Cached on the identity of the entries array, because useSyncExternalStore
  // compares snapshots with Object.is and calls getSnapshot on every render.
  // Deriving a fresh array each time is a new snapshot each time, which is an
  // infinite render loop — "Maximum update depth exceeded", with no clue that
  // a `.map()` caused it.
  const cache = useRef<{ entries: CallEntry[] | null; result: RingingPush[] }>({
    entries: null,
    result: EMPTY
  });

  const getSnapshot = useCallback(() => {
    const entries = registry?.entries ?? [];
    if (cache.current.entries !== entries) {
      cache.current = { entries, result: ringingFrom(entries) };
    }
    return cache.current.result;
  }, [registry]);

  const ringing = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const answer = useCallback(
    (uuid: string) => registry?.applyIntent(uuid, 'answer'),
    [registry]
  );
  const reject = useCallback(
    (uuid: string) => registry?.applyIntent(uuid, 'reject'),
    [registry]
  );

  return { ringing, answer, reject };
}
