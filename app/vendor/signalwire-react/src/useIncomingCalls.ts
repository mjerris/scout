import { useCallback, useContext } from 'react';

import { logger } from './logger';
import { SignalWireContext } from './SignalWireProvider';
import { useObservable } from './useObservable';

import type { Call, MediaOptions } from '@signalwire/js';

const NO_CALLS: Call[] = [];

export interface UseIncomingCallsResult {
  readonly calls: Call[];
  answer(call: Call, options?: MediaOptions): Promise<void>;
  reject(call: Call): Promise<void>;
}

/**
 * Pending inbound calls from the SDK session.
 *
 * With CallKit enabled the native UI usually handles these first; this hook
 * remains useful for in-app inbound UI and for apps that do not use CallKit.
 */
export function useIncomingCalls(): UseIncomingCallsResult {
  const context = useContext(SignalWireContext);
  const session = context?.client?.session;
  const observer = context?.observer;
  const calls = useObservable(session?.incomingCalls$, session?.incomingCalls ?? NO_CALLS);

  const answer = useCallback(
    async (call: Call, options?: MediaOptions): Promise<void> => {
      // The observer answers through the native call UI when it can, so the OS
      // activates the audio session and dismisses its ringing screen. Answering
      // the SDK directly while CallKit still rings yields a connected call
      // with no audio. When the observer takes over, the native answer flow
      // calls `call.answer` itself and `options` are not applied.
      if (observer?.onIncomingAnswer?.(call)) {
        return;
      }
      await call.answer(options);
    },
    [observer]
  );

  const reject = useCallback(
    async (call: Call): Promise<void> => {
      if (observer?.onIncomingReject?.(call)) {
        return;
      }
      try {
        await call.reject();
      } catch (error) {
        logger.debug('Reject on an already-ended call:', error);
      }
    },
    [observer]
  );

  return { calls, answer, reject };
}
