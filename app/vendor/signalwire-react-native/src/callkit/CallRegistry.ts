import { BehaviorSubject, Subject, concat, defer, from } from 'rxjs';

import { logger } from '@signalwire/react';

import type {
  CallEntry,
  CallIntent,
  CallRegistryHost,
  CallRegistryOptions,
  PushPayload
} from './types';
import type { Call } from '@signalwire/js';
import type { Observable } from 'rxjs';

/**
 * How long a pushed call rings before it is written off as missed.
 *
 * For a call that fuses with an inbound SDK call this is a safety net. For a
 * bridge topology it is the ring duration itself: no inbound call is ever
 * coming, so this timer is the only thing ending an unanswered call.
 *
 * 20s was too short for both. A cold start has to launch the app, load
 * JavaScript, connect and register before anything can be shown — and in a
 * development build it also downloads the bundle — so a call could expire
 * before the user ever saw it. Real phones ring for about 30 seconds, and the
 * server parks its caller for 60. Override with `fusionTimeoutMs`.
 */
const DEFAULT_FUSION_TIMEOUT_MS = 45_000;

/**
 * How long an *answered* entry may wait for the app to produce its call.
 *
 * The fusion deadline measures something else — how long a ringing push waits
 * for an inbound SDK call — and once the user answers, that question is
 * settled. In a bridge topology no inbound call is ever coming: the app dials
 * out instead, which takes a token round-trip, a dial and ICE. Letting the
 * original deadline run tore the entry down mid-dial, and the call the user
 * had already accepted was reported as missed.
 */
const ANSWERED_TIMEOUT_MS = 60_000;
const UNKNOWN_HANDLE = 'Unknown';
const UNKNOWN_NAME = 'Unknown caller';

/**
 * Reconciles native call UI entries with SDK calls.
 *
 * Entries arrive from two directions — a VoIP push, which lands before the SDK
 * is even connected, and the SDK itself — and this class fuses them into a
 * single identity. It holds no native imports and owns no timers; the host
 * supplies both, which keeps the state machine deterministic under test.
 */
export class CallRegistry {
  private readonly host: CallRegistryHost;
  private readonly fusionTimeoutMs: number;
  private readonly _entries$ = new BehaviorSubject<CallEntry[]>([]);
  private readonly _answered$ = new Subject<Call>();
  private readonly _answerRequested$ = new Subject<CallEntry>();
  private readonly byUuid = new Map<string, CallEntry>();

  constructor(options: CallRegistryOptions) {
    this.host = options.host;
    this.fusionTimeoutMs = options.fusionTimeoutMs ?? DEFAULT_FUSION_TIMEOUT_MS;
  }

  get entries$(): Observable<CallEntry[]> {
    return this._entries$.asObservable();
  }

  /**
   * Emits each call the moment it is answered through the native UI.
   *
   * This is how a native answer reaches the app's own screens. Without it,
   * tapping CallKit's Accept connects the call — audio and all — while the
   * React tree never learns: `activeCall` stays null, no call screen appears,
   * and the accept button looks like it did nothing.
   */
  get answered$(): Observable<Call> {
    return this._answered$.asObservable();
  }

  /**
   * Emits when the user accepts a push entry that has no SDK call yet.
   *
   * The fusion model assumes the call is on its way to us — the server dials
   * the subscriber and we wait. A bridge flow inverts that: the caller is
   * parked on ringback and *we* place the call that joins them, so nothing
   * will ever arrive to fuse with and the buffered intent would sit there
   * until the entry timed out as missed.
   *
   * Subscribe to place that call, then hand it back with {@link bindCall} so
   * it lands on the same native entry the user is already looking at.
   */
  get answerRequested$(): Observable<CallEntry> {
    // Replays answers that are still waiting for a call to be placed.
    //
    // A plain Subject drops anything emitted before its subscriber exists,
    // and on a cold start that is the normal order: the push launches the
    // app, the user answers from the lock screen, and the intent is applied
    // while React is still mounting. The answer reached JavaScript and was
    // buffered on the entry, but the hook that dials the bridge subscribed a
    // moment too late and never saw it — so nothing dialled and the caller
    // sat on a parked leg until it timed out.
    //
    // Only entries still awaiting a call are replayed: once one is bound the
    // request is satisfied, so a later subscriber must not redial it.
    return defer(() => {
      const outstanding = this.entries.filter(
        (entry) => entry.intent === 'answer' && entry.state !== 'ended' && !entry.call
      );
      return concat(from(outstanding), this._answerRequested$);
    });
  }

  get entries(): CallEntry[] {
    return this._entries$.value;
  }

  entryForUuid(uuid: string): CallEntry | undefined {
    return this.byUuid.get(uuid);
  }

  uuidForCall(call: Call): string | undefined {
    for (const entry of this.byUuid.values()) {
      if (entry.call === call) {
        return entry.uuid;
      }
    }
    return undefined;
  }

  /**
   * Reports a VoIP push to the native call UI immediately.
   *
   * Returns the UUID identifying the call for every later native callback.
   * A duplicate push for a call id already pending returns the original UUID.
   */
  reportIncomingPush(payload: PushPayload): string {
    if (payload.callId) {
      const existing = this.findPending((entry) => entry.expectedCallId === payload.callId);
      if (existing) {
        logger.debug(`Duplicate push for call ${payload.callId}, reusing ${existing.uuid}`);
        return existing.uuid;
      }
    } else {
      logger.warn(
        'Push payload has no callId. Fusion will fall back to matching a single ' +
          'unmatched inbound call, which is unreliable with concurrent calls. ' +
          'Expected if you bridge to a parked caller instead: no call is coming ' +
          'to fuse with — subscribe to answerRequested$ and bind the call you place.'
      );
    }

    const uuid = this.host.generateUuid();
    const handle = payload.from ?? UNKNOWN_HANDLE;
    const displayName = payload.fromName ?? UNKNOWN_NAME;

    this.write({
      uuid,
      state: 'pending-push',
      expectedCallId: payload.callId ?? null,
      call: null,
      handle,
      displayName,
      intent: null,
      data: payload.data,
      fuseDeadline: this.host.now() + this.fusionTimeoutMs
    });

    this.host.displayIncomingCall(uuid, handle, displayName);
    return uuid;
  }

  /**
   * Records a call the native layer already displayed.
   *
   * On a cold start the VoIP push is reported to CallKit before JavaScript
   * exists, so the entry has a UUID the native side chose. Reporting it here
   * rather than minting a new one keeps a single identity for the call — a
   * second UUID would leave the ringing native entry orphaned, which is the
   * stuck-call-log failure iOS penalises hardest.
   *
   * Idempotent: callkeep may replay the event, and the app may also call
   * `reportIncomingPush` for the same call.
   */
  adoptNativeEntry(payload: PushPayload & { uuid: string }): void {
    const existing = this.byUuid.get(payload.uuid);
    if (existing) {
      // Later knowledge wins, but nothing already learned is discarded.
      this.write({
        ...existing,
        expectedCallId: existing.expectedCallId ?? payload.callId ?? null,
        data: existing.data ?? payload.data
      });
      return;
    }

    this.write({
      uuid: payload.uuid,
      state: 'pending-push',
      expectedCallId: payload.callId ?? null,
      call: null,
      handle: payload.from ?? UNKNOWN_HANDLE,
      displayName: payload.fromName ?? UNKNOWN_NAME,
      intent: null,
      data: payload.data,
      fuseDeadline: this.host.now() + this.fusionTimeoutMs
    });
    logger.debug(`Adopted native push entry ${payload.uuid}`);
  }

  /** Fuses an inbound SDK call with a pending push, or creates a new entry. */
  attachIncomingCall(call: Call): void {
    if (this.uuidForCall(call)) {
      return;
    }

    const pending = this.findFusionTarget(call.id);
    if (pending) {
      this.fuse(pending, call);
      return;
    }

    const uuid = this.host.generateUuid();
    const handle = call.from ?? UNKNOWN_HANDLE;
    const displayName = call.fromName ?? UNKNOWN_NAME;

    this.write({
      uuid,
      state: 'fused',
      expectedCallId: call.id,
      call,
      handle,
      displayName,
      intent: null,
      fuseDeadline: null
    });

    this.host.displayIncomingCall(uuid, handle, displayName);
  }

  /** Registers an outbound call and starts the native outgoing call UI. */
  attachOutgoingCall(call: Call, handle: string, displayName: string): string {
    const existing = this.uuidForCall(call);
    if (existing) {
      return existing;
    }

    const uuid = this.host.generateUuid();
    this.write({
      uuid,
      state: 'fused',
      expectedCallId: call.id,
      call,
      handle,
      displayName,
      intent: null,
      fuseDeadline: null
    });

    this.host.startOutgoingCall(uuid, handle, displayName);
    return uuid;
  }

  /**
   * Attaches a call to an entry that already exists, keeping its UUID.
   *
   * For the bridge flow: the user answered a push, we dialled to join them to
   * a parked caller, and the resulting call belongs to the native entry that
   * is already on screen. {@link attachOutgoingCall} would mint a second UUID
   * and leave the original ringing forever — a stuck CallKit entry, which is
   * the failure iOS penalises hardest.
   *
   * Returns `false` when the entry is gone (the caller hung up, or the user
   * declined, while the call was being placed), which is the signal to hang
   * that call straight back up.
   */
  bindCall(uuid: string, call: Call): boolean {
    const entry = this.byUuid.get(uuid);
    if (!entry || entry.state === 'ended') {
      logger.debug(`No live entry for ${uuid}; the bridge call has nothing to attach to`);
      return false;
    }

    // The intent is cleared: the user's answer is what caused this call to
    // exist, so replaying it against the new call would answer an outbound
    // leg that was never ringing.
    this.write({
      ...entry,
      state: 'fused',
      call,
      expectedCallId: call.id,
      intent: null,
      fuseDeadline: null
    });
    logger.debug(`Bound bridge call ${call.id} to ${uuid}`);
    return true;
  }

  /** Applies an answer/reject, buffering it when the entry is still pending. */
  applyIntent(uuid: string, intent: CallIntent): void {
    const entry = this.byUuid.get(uuid);
    if (!entry || entry.state === 'ended') {
      return;
    }

    if (entry.state === 'pending-push' || !entry.call) {
      logger.debug(`Buffering "${intent}" for ${uuid} until the SDK call arrives`);
      // An answered entry is no longer waiting to fuse, it is waiting for the
      // app. Restart the clock, generously: the alternative is expiring a call
      // the user has already accepted.
      const buffered = {
        ...entry,
        intent,
        fuseDeadline:
          intent === 'answer' ? this.host.now() + ANSWERED_TIMEOUT_MS : entry.fuseDeadline
      };
      this.write(buffered);
      if (intent === 'answer') {
        this._answerRequested$.next(buffered);
      }
      return;
    }

    this.runIntent(entry.call, intent);
    this.write({ ...entry, intent: null });
  }

  /** Marks an outgoing call as connected in the native UI. */
  reportConnected(uuid: string): void {
    const entry = this.byUuid.get(uuid);
    if (!entry || entry.state === 'ended') {
      return;
    }
    this.host.reportOutgoingConnected(uuid);
  }

  /** The local side ended the call, from the native UI or in-app. */
  endCall(uuid: string): void {
    const entry = this.byUuid.get(uuid);
    if (!entry || entry.state === 'ended') {
      return;
    }

    void entry.call?.hangup().catch((error: unknown) => {
      logger.debug('Hangup on an already-ended call:', error);
    });

    this.write({ ...entry, state: 'ended', intent: null, fuseDeadline: null });
    this.host.reportCallEnded(uuid, 'local');
  }

  /** The remote side ended the call; tell the native UI. */
  reportRemoteEnded(uuid: string): void {
    const entry = this.byUuid.get(uuid);
    if (!entry || entry.state === 'ended') {
      return;
    }
    this.write({ ...entry, state: 'ended', intent: null, fuseDeadline: null });
    this.host.reportCallEnded(uuid, 'remote');
  }

  /**
   * Expires pending push entries past their fusion deadline.
   *
   * A stuck native call entry is the failure mode iOS punishes hardest, so the
   * host must call this on an interval while any entry is pending.
   */
  tick(): void {
    const now = this.host.now();
    for (const entry of [...this.byUuid.values()]) {
      if (entry.state !== 'pending-push' || entry.fuseDeadline === null) {
        continue;
      }
      if (now > entry.fuseDeadline) {
        logger.warn(
          entry.intent === 'answer'
            ? `Answered entry ${entry.uuid} never got its call; giving up.`
            : `Push entry ${entry.uuid} never fused; reporting a missed call.`
        );
        this.write({ ...entry, state: 'ended', intent: null, fuseDeadline: null });
        this.host.reportCallEnded(entry.uuid, 'missed');
      }
    }
  }

  destroy(): void {
    this.byUuid.clear();
    this._entries$.complete();
    this._answered$.complete();
    this._answerRequested$.complete();
  }

  // ---------------------------------------------------------------- internals

  private findPending(predicate: (entry: CallEntry) => boolean): CallEntry | undefined {
    for (const entry of this.byUuid.values()) {
      if (entry.state === 'pending-push' && predicate(entry)) {
        return entry;
      }
    }
    return undefined;
  }

  /** Exact id match first; the single-payload-less fallback second. */
  private findFusionTarget(callId: string): CallEntry | undefined {
    const exact = this.findPending((entry) => entry.expectedCallId === callId);
    if (exact) {
      return exact;
    }

    const payloadLess = [...this.byUuid.values()].filter(
      (entry) => entry.state === 'pending-push' && entry.expectedCallId === null
    );

    const only = payloadLess[0];
    if (payloadLess.length === 1 && only) {
      logger.warn(
        `Fusing call ${callId} with push entry ${only.uuid} by fallback — the push ` +
          'payload carried no callId. Include it to make this deterministic.'
      );
      return only;
    }

    if (payloadLess.length > 1) {
      logger.warn(
        `${payloadLess.length} push entries have no callId; refusing to guess. ` +
          `Call ${callId} gets a new entry.`
      );
    }

    return undefined;
  }

  private fuse(entry: CallEntry, call: Call): void {
    const fused: CallEntry = {
      ...entry,
      state: 'fused',
      call,
      expectedCallId: call.id,
      fuseDeadline: null
    };
    this.write(fused);
    logger.debug(`Fused call ${call.id} into ${entry.uuid}`);

    if (entry.intent) {
      this.runIntent(call, entry.intent);
      this.write({ ...fused, intent: null });
    }
  }

  private runIntent(call: Call, intent: CallIntent): void {
    try {
      if (intent === 'answer') {
        this._answered$.next(call);
        // Audio-only, explicitly. A bare answer() lets the SDK's defaults
        // request video, and an SDP answer cannot introduce an m-line the
        // offer lacks — so answering an audio-only call that way fails with
        // "Error creating inbound answer" after the user has already accepted
        // on the native UI. The call gives us no way to inspect the offered
        // m-lines before answering; matching the offer automatically belongs
        // in the SDK. Video answer stays available to in-app UI via
        // useIncomingCalls().answer(call, { video: true }).
        call.answer({ audio: true, video: false });
      } else {
        call.reject();
      }
    } catch (error) {
      logger.warn(`Failed to ${intent} the call:`, error);
    }
  }

  private write(entry: CallEntry): void {
    this.byUuid.set(entry.uuid, entry);
    this._entries$.next([...this.byUuid.values()]);
  }
}
