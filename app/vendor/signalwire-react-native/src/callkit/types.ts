import type { Call } from '@signalwire/js';

export type CallEntryState = 'pending-push' | 'fused' | 'ended';

/** A lock-screen action recorded before the SDK call arrived. */
export type CallIntent = 'answer' | 'reject';

/** The data your push handler forwards from the VoIP/FCM payload. */
export interface PushPayload {
  /** SignalWire call id. Required for reliable fusion. */
  callId?: string;
  /** Caller address or number, shown in the native UI. */
  from?: string;
  /** Caller display name, shown in the native UI. */
  fromName?: string;
  /**
   * Opaque data the push carried, passed through untouched.
   *
   * This package does not interpret it. A bridge topology, for instance, sends
   * a single-use token here that the app hands to its own backend, which
   * resolves it to the parked caller — keeping the real call identifier off the
   * device, where a push payload would otherwise act as a capability anyone
   * replaying it could use.
   */
  data?: Record<string, string>;
}

export interface CallEntry {
  readonly uuid: string;
  readonly state: CallEntryState;
  readonly expectedCallId: string | null;
  readonly call: Call | null;
  readonly handle: string;
  readonly displayName: string;
  readonly intent: CallIntent | null;
  /** Opaque data from the push that created this entry, if any. */
  readonly data?: Record<string, string>;
  /** Deadline for fusion, in `host.now()` milliseconds. Null once fused. */
  readonly fuseDeadline: number | null;
}

/** Everything the registry needs from the native layer. */
export interface CallRegistryHost {
  displayIncomingCall(uuid: string, handle: string, displayName: string): void;
  startOutgoingCall(uuid: string, handle: string, displayName: string): void;
  reportOutgoingConnected(uuid: string): void;
  reportCallEnded(uuid: string, reason: 'missed' | 'remote' | 'local'): void;
  generateUuid(): string;
  now(): number;
}

export interface CallRegistryOptions {
  host: CallRegistryHost;
  /** How long a push entry may wait for its SDK call. Default 20000. */
  fusionTimeoutMs?: number;
}
