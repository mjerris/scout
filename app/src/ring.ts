// Scout calls you (docs/phone-design.md, "Scout calls you"): Scout sends a VoIP push, the
// library's PushKit hook rings "Scout" in CallKit before any JavaScript runs, and the push
// carries `scoutRing`, a single-use token. Nothing is waiting at SignalWire: answering
// places an ordinary call to Scout's agent with the token in its userVariables, and the
// agent opens with what Scout called to say. Declining tells Scout (signed).
//
// Both are taken from the CallKit registry at import (index.ts), before React mounts: a
// push wakes a killed app, and the answer can come from the lock screen while the app is
// still starting. An answer waits here until the call panel (which needs the pairing from
// the Keychain) takes it.
import { getCallKit } from '@signalwire/react-native/callkit';
import { useSyncExternalStore } from 'react';

import { loadPairing } from './pairing';
import type { Pairing } from './pairing';
import { ScoutServer } from './server';

export interface RingAnswer {
  /** The CallKit entry the user answered. */
  uuid: string;
  /** The push's single-use token, sent with the call. */
  ring: string;
}

let waiting: RingAnswer[] = [];
const listeners = new Set<() => void>();

function changed(): void {
  listeners.forEach((l) => l());
}

/** The answered rings no call has been placed for yet. */
export function usePendingRings(): readonly RingAnswer[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => waiting,
  );
}

/** Take the oldest answered ring that is still live in CallKit (older ones are dropped). */
export function takeRing(isLive: (uuid: string) => boolean = (u) => getCallKit().registry.isLive(u)): RingAnswer | null {
  let got: RingAnswer | null = null;
  while (waiting.length && !got) {
    const [first, ...rest] = waiting;
    waiting = rest;
    if (first && isLive(first.uuid)) got = first;
  }
  changed();
  return got;
}

type Entry = { uuid: string; data?: Record<string, string> };
interface Registry {
  answerRequested$: { subscribe(fn: (e: Entry) => void): { unsubscribe(): void } };
  rejectRequested$: { subscribe(fn: (e: Entry) => void): { unsubscribe(): void } };
}
type Decliner = (pairing: Pairing, ring: string) => Promise<string>;

const decline: Decliner = (pairing, ring) => new ScoutServer(pairing.server, pairing).declineRing(ring);

/**
 * Watch the registry for Scout's rings: answers wait for the call panel, declines go to
 * Scout. Returns the unsubscribe (for tests); index.ts calls it once.
 */
export function watchRings(
  registry: Registry = getCallKit().registry,
  pairing: () => Promise<Pairing | null> = loadPairing,
  send: Decliner = decline,
): () => void {
  const answers = registry.answerRequested$.subscribe((e) => {
    const ring = e.data?.scoutRing;
    if (!ring) {
      // Not Scout's ring (nothing else sends this app VoIP pushes): it ends as missed.
      console.warn(`Answered push ${e.uuid} isn't Scout's call; ignoring`);
      return;
    }
    waiting = [...waiting, { uuid: e.uuid, ring }];
    changed();
  });
  const declines = registry.rejectRequested$.subscribe((e) => {
    const ring = e.data?.scoutRing;
    if (!ring) return;
    void (async () => {
      try {
        const p = await pairing();
        if (p) await send(p, ring);
      } catch (err) {
        console.warn("Couldn't tell Scout the call was declined:", String((err as Error)?.message ?? err));
      }
    })();
  });
  return () => {
    answers.unsubscribe();
    declines.unsubscribe();
  };
}

/** For tests. */
export function resetRingsForTesting(): void {
  waiting = [];
  changed();
}
