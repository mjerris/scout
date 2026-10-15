import { getCallKit } from './CallKeepBridge';

import type { CallObserver } from '@signalwire/react';

/**
 * A {@link CallObserver} backed by CallKit / ConnectionService.
 *
 * This is the whole of what the core needs to know about native call UI: the
 * core calls two optional methods, and everything else — the registry, the
 * bridge, callkeep itself — stays on this side of the boundary and never
 * reaches a browser bundle.
 */
export function createCallKitObserver(): CallObserver {
  return {
    bindClient: (client) => getCallKit().bindClient(client),
    onOutgoingCall: (call, destination) =>
      void getCallKit().trackCall(call, destination, destination),
    onIncomingAnswer: (call) => getCallKit().answerIncomingFromApp(call),
    onIncomingReject: (call) => getCallKit().rejectIncomingFromApp(call)
  };
}
