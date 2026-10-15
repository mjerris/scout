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
export interface CallKitObserverOptions {
  /**
   * The name the native call UI and Recents show for an outbound call. Default:
   * the destination itself, which for a Fabric address reads like
   * `/private/support-agent`.
   */
  displayName?: (destination: string) => string;
}

export function createCallKitObserver(options: CallKitObserverOptions = {}): CallObserver {
  return {
    bindClient: (client) => getCallKit().bindClient(client),
    onOutgoingCall: (call, destination, dialOptions) =>
      void getCallKit().trackCall(call, destination, options.displayName?.(destination) ?? destination, {
        video: Boolean(dialOptions?.video)
      }),
    onIncomingAnswer: (call) => getCallKit().answerIncomingFromApp(call),
    onIncomingReject: (call) => getCallKit().rejectIncomingFromApp(call)
  };
}
