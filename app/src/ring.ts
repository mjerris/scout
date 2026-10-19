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
//
// With more than one phone, the ring ends on the others once one answers or declines, or
// nobody does (Scout's cancel push, phone_push.cancel_payload). iOS wants a CallKit call for
// every VoIP push, so the cancel carries the uuid this phone was rung with: the native hook
// reports it, CallKit already has that call (CallUUIDAlreadyExists), and it is ended here,
// as answered or declined elsewhere, or as missed. A cancel for a call this phone doesn't
// have (it came first, or the call already ended) is reported as a new call and ended at
// once: Apple's pattern. A cancel for the call this phone answered is ignored.
import RNCallKeep, { CONSTANTS } from '@signalwire/react-native-callkeep';
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
/** CallKit uuids (lowercase) this phone answered Scout's ring on, and ones a cancel ended. */
const answeredHere = new Set<string>();
const cancelled = new Set<string>();
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
    answeredHere.add(e.uuid.toLowerCase());
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

export type CancelReason = 'answered' | 'declined' | 'missed';

/** CallKit's end reason for each (callkeep's iOS values: missed is "unanswered"). */
export function endReason(reason: string): number {
  const r = CONSTANTS.END_CALL_REASONS;
  if (reason === 'answered') return r.ANSWERED_ELSEWHERE;
  if (reason === 'declined') return r.DECLINED_ELSEWHERE;
  return r.UNANSWERED;
}

type Display = { callUUID: string; fromPushKit?: string; payload?: unknown };
type Entries = { subscribe(fn: (entries: readonly CancelEntry[]) => void): { unsubscribe(): void } };
type CancelEntry = { uuid: string; state: string; data?: Record<string, string> };

export interface CancelDeps {
  /** callkeep's didDisplayIncomingCall, with the push's payload (a known uuid too). */
  onDisplay: (fn: (e: Display) => void) => () => void;
  /** The registry's entries: a cancel the wrapper adopted at a cold start shows up here. */
  entries$: Entries;
  /** Ends the CallKit call with a reason (callkeep's reportEndCallWithUUID). */
  reportEnded: (uuid: string, reason: number) => void;
  /** Tells the registry the call is over (its own native end is then a no-op). */
  forget: (uuid: string) => void;
}

const CANCEL_DEPS = (): CancelDeps => ({
  onDisplay: (fn) => {
    const sub = RNCallKeep.addEventListener('didDisplayIncomingCall', fn);
    return () => sub?.remove?.();
  },
  entries$: getCallKit().registry.entries$,
  reportEnded: (uuid, reason) => RNCallKeep.reportEndCallWithUUID(uuid, reason),
  forget: (uuid) => getCallKit().registry.reportRemoteEnded(uuid),
});

function cancelOf(data: unknown): string | null {
  const c = (data as Record<string, unknown> | null | undefined)?.scoutCancel;
  return typeof c === 'string' && c ? c : null;
}

/**
 * End Scout's ring here when another phone settled it, or nobody answered (the cancel
 * push). Returns the unsubscribe (for tests); index.ts calls it once.
 */
export function watchCancels(deps: CancelDeps = CANCEL_DEPS()): () => void {
  const end = (rawUuid: string, reason: string) => {
    const uuid = rawUuid.toLowerCase();
    if (answeredHere.has(uuid)) {
      console.warn(`Scout ended its ring (${reason}), but this phone answered ${uuid}; keeping the call`);
      return;
    }
    if (!cancelled.has(uuid)) {
      cancelled.add(uuid);
      deps.reportEnded(uuid, endReason(reason));
      const left = waiting.filter((w) => w.uuid.toLowerCase() !== uuid);
      if (left.length !== waiting.length) {
        waiting = left;
        changed();
      }
    }
    deps.forget(uuid);
  };
  const stopDisplay = deps.onDisplay((e) => {
    const reason = cancelOf(e.payload);
    if (reason && e.fromPushKit === '1' && e.callUUID) end(e.callUUID, reason);
  });
  const entries = deps.entries$.subscribe((list) => {
    for (const e of list) {
      const reason = cancelOf(e.data);
      if (reason && e.state !== 'ended') end(e.uuid, reason);
    }
  });
  return () => {
    stopDisplay();
    entries.unsubscribe();
  };
}

/** For tests. */
export function resetRingsForTesting(): void {
  waiting = [];
  answeredHere.clear();
  cancelled.clear();
  changed();
}
